"""Independent oracles stay fixed through real-result-driven script repair."""
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.services.skills.skill_creation import service as creator_service
from langchain.messages import AIMessage

from app.application.errors.exceptions import BadRequestError
from app.core.config import Settings
from app.domain.services.skills import session_skill_creator as creator
from app.domain.services.skills.registry import SkillRegistry
from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_session_skill_creator import Repository, helper_script, recipe_payload, review_wire_response, stage_response
from test_skill_drafts import make_service
from test_skill_validation_plan import method_contract, plan_payload, sources
from test_skill_validation_review import accepted_fixture_review


@pytest.fixture(autouse=True)
def isolated_reference_boundary(monkeypatch):
    """These tests exercise candidate repair, not the separately tested reference solver.

    Real-model acceptance keeps the production reference layer enabled. Its
    blind-input and computational-correction guarantees have dedicated tests.
    """
    from app.domain.services.skills import skill_script_verification as verification

    async def reference(plan, contract, sources, interfaces, **kwargs):
        plan = SyntheticValidationPlan.model_validate(plan.model_dump(mode="json"))
        return plan.model_copy(deep=True), {"status": "unit_test_isolated", "scope": "synthetic_reference",
            "corrections": [], "execution": {"cases": []},
            "corrected_plan_sha256": hashlib.sha256(worker._json(plan.model_dump())).hexdigest()}

    monkeypatch.setattr(verification, "verify_reference_plan", reference)


def directory_script():
    script = helper_script()
    script["content"] = script["content"].replace("Path(a.output).write_text", "(Path(a.output) / 'summary.csv').write_text")
    return script


def recipe():
    payload = recipe_payload()
    script = directory_script()
    script["path"] = "scripts/group_summary.py"
    script["invocation"] = "python scripts/group_summary.py --input INPUT --output OUTPUT"
    script["content"] += "\n# IMPLEMENTATION_MARKER_NOT_FOR_ORACLE\nobserved_value = 987654.321\n"
    script["content"] += "summary_file = 'summary.csv'\nstatus_file = 'status.json'\n"
    payload["scripts"] = [script]
    return creator.SkillRecipe.model_validate(payload)


def receipt(status="passed", *, script_path="scripts/group_summary.py"):
    return {"status": status, "scope": "synthetic", "cases": [
        {"script_path": script_path, "id": "group-" + kind, "kind": kind,
         "status": "failed" if status == "failed" and kind == "boundary" else "passed",
         "reason": "assertion_mismatch" if status == "failed" and kind == "boundary" else "expectations_matched",
         "checks": [{"passed": status != "failed" or kind != "boundary", "expected": None, "actual": 0}]}
        for kind in ("normal", "boundary", "invalid")],
        "source_sha256": {script_path: "a" * 64}, "plan_sha256": "b" * 64}


def generation(plan=None, *, repair=None):
    calls = []

    async def generate(schema, stage, instruction, payload, check=None):
        calls.append({"stage": stage, "payload": copy.deepcopy(payload), "instruction": instruction})
        if stage == "synthetic_validation_plan":
            value = schema.model_validate(plan or plan_payload())
        elif stage == "synthetic_plan_review":
            value = schema.model_validate(accepted_fixture_review(payload["validation_plan"]))
        else:
            assert stage == "synthetic_script_repair"
            candidate = copy.deepcopy(payload["script"])
            candidate["content"] += "\nimplementation_repaired = True\n" if repair is None else repair
            value = schema.model_validate(candidate)
        if check:
            check(value)
        return value

    return generate, calls


def revalidate(value):
    creator.validate_recipe(value, set())


@pytest.mark.asyncio
async def test_oracle_generation_gets_declarative_interfaces_but_no_implementation_or_observed_answers(monkeypatch):
    generate, calls = generation()
    run = AsyncMock(return_value=receipt())
    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    independent_sources = [{**sources()[0], "source_snapshot": "PRIVATE_SUCCESSFUL_SOURCE",
                            "actual": "WRONG_OBSERVED_ANSWER"}]
    review = AsyncMock()

    returned, frozen, execution = await verify_skill_script_cases(recipe(), method_contract(), independent_sources,
        generate=generate, revalidate=revalidate, review=review)

    assert [call["stage"] for call in calls] == ["synthetic_validation_plan", "synthetic_plan_review"]
    payload = calls[0]["payload"]
    assert set(payload) == {"method_contract", "sources", "script_interfaces"}
    assert payload["sources"] == sources()
    encoded = json.dumps(payload)
    assert all(marker not in encoded for marker in (
        "IMPLEMENTATION_MARKER_NOT_FOR_ORACLE", "987654.321", "PRIVATE_SUCCESSFUL_SOURCE",
        "WRONG_OBSERVED_ANSWER", "source_snapshot", "observed_validation"))
    assert payload["script_interfaces"][0]["arguments"] == ["--input", "--output"]
    assert "summary.csv" in encoded and "status.json" in encoded
    assert returned.scripts[0].content == recipe().scripts[0].content
    assert frozen == SyntheticValidationPlan.model_validate(plan_payload()).model_dump(mode="json")
    assert execution["status"] == "passed"
    run.assert_awaited_once()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_result_repairs_only_script_and_reuses_original_frozen_oracle(monkeypatch):
    generate, calls = generation()
    received = []

    async def run(scripts, plan):
        digest = hashlib.sha256(worker._json(plan.model_dump())).hexdigest()
        received.append({"scripts": dict(scripts), "plan": plan.model_dump(mode="json"), "digest": digest})
        if len(received) == 1:
            # Even an accidental mutation by an execution dependency must not
            # turn the wrong observed answer into the repaired program's oracle.
            plan.scripts[0].cases[0].assertions[0].fields["mean"].value = 999
            return {**receipt("failed"), "plan_sha256": digest}
        return {**receipt(), "plan_sha256": digest}

    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    review = AsyncMock()
    checked = Mock(side_effect=revalidate)
    returned, frozen, execution = await verify_skill_script_cases(recipe(), method_contract(), sources(),
        generate=generate, revalidate=checked, review=review)

    assert [call["stage"] for call in calls] == ["synthetic_validation_plan", "synthetic_plan_review", "synthetic_script_repair"]
    assert calls[2]["payload"]["observed_validation"]["status"] == "failed"
    assert calls[2]["payload"]["frozen_validation_plan"] == frozen
    assert frozen == SyntheticValidationPlan.model_validate(plan_payload()).model_dump(mode="json")
    assert received[0]["plan"] == received[1]["plan"] == frozen
    assert received[0]["digest"] == received[1]["digest"] == execution["plan_sha256"] == execution["oracle_review"]["plan_sha256"]
    assert received[0]["scripts"] != received[1]["scripts"]
    assert "implementation_repaired = True" in returned.scripts[0].content
    assert checked.call_count == 2
    review.assert_awaited_once_with(returned)
    assert execution["status"] == "passed"


@pytest.mark.asyncio
async def test_second_execution_failure_stops_after_prior_method_review_without_third_attempt(monkeypatch):
    generate, calls = generation()
    run = AsyncMock(side_effect=[receipt("failed"), receipt("failed")])
    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    review = AsyncMock()
    with pytest.raises(ValueError, match="未创建技能"):
        await verify_skill_script_cases(recipe(), method_contract(), sources(),
            generate=generate, revalidate=revalidate, review=review)
    assert run.await_count == 2
    assert [call["stage"] for call in calls].count("synthetic_validation_plan") == 1
    assert [call["stage"] for call in calls].count("synthetic_script_repair") == 1
    review.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_supported_failure_and_unsupported_script_repairs_only_failed_code_then_reviews_method(monkeypatch):
    candidate = recipe()
    unsupported = candidate.scripts[0].model_copy(update={"path": "scripts/unsupported.py",
        "invocation": "python scripts/unsupported.py --input INPUT --output OUTPUT"})
    candidate.scripts.append(unsupported)
    plan = plan_payload()
    plan["scripts"].append({"script_path": unsupported.path, "support": "unsupported",
        "reason": "This scientific modality cannot be tested by the tabular runner.", "cases": []})
    generate, calls = generation(plan)
    first, second = receipt("failed"), receipt()
    unavailable_case = {"script_path": unsupported.path, "id": "not_run", "kind": "unsupported",
                        "status": "unsupported", "reason": "unsupported_validation_contract"}
    first["cases"].append(unavailable_case)
    second["cases"].append(unavailable_case)
    second["status"] = "unsupported"
    run = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    review = AsyncMock()

    result, _, execution = await verify_skill_script_cases(candidate, method_contract(), sources(),
        generate=generate, revalidate=revalidate, review=review)

    repaired_paths = [item["payload"]["script"]["path"] for item in calls if item["stage"] == "synthetic_script_repair"]
    assert repaired_paths == ["scripts/group_summary.py"]
    assert result.scripts[1].content == unsupported.content
    assert execution["status"] == "unsupported"
    review.assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_comment_only_change_cannot_relabel_a_failed_program_as_repaired(monkeypatch):
    generate, _ = generation(repair="\n# The same unchanged program is now claimed repaired.\n")
    run = AsyncMock(return_value=receipt("failed"))
    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    with pytest.raises(ValueError, match="自动修复后仍未通过"):
        await verify_skill_script_cases(recipe(), method_contract(), sources(),
            generate=generate, revalidate=revalidate, review=AsyncMock())
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_execution_unavailable_is_not_a_method_failure_to_repair(monkeypatch):
    generate, calls = generation()
    monkeypatch.setattr(worker, "validate_skill_scripts", AsyncMock(return_value={
        "status": "unavailable", "scope": "synthetic", "cases": [], "reason": "sandbox_image_not_configured"}))
    with pytest.raises(ValueError, match="未创建技能"):
        await verify_skill_script_cases(recipe(), method_contract(), sources(),
            generate=generate, revalidate=revalidate, review=AsyncMock())
    assert [call["stage"] for call in calls] == ["synthetic_validation_plan", "synthetic_plan_review"]


@pytest.mark.asyncio
async def test_unsupported_and_method_only_do_not_invent_execution_or_touch_docker(monkeypatch):
    docker = Mock(side_effect=AssertionError("Docker must not be contacted"))
    monkeypatch.setattr(worker.docker, "from_env", docker)
    plan = plan_payload()
    plan["scripts"][0].update(support="unsupported", reason="The required scientific modality cannot use this tabular runner.", cases=[])
    generate, calls = generation(plan)
    _, frozen, execution = await verify_skill_script_cases(recipe(), method_contract(), sources(),
        generate=generate, revalidate=revalidate, review=AsyncMock())
    assert execution["status"] == "unsupported" and execution["cases"] == []
    assert frozen == plan
    assert len(execution["source_sha256"]["scripts/group_summary.py"]) == 64
    assert len(execution["plan_sha256"]) == 64
    no_script = recipe().model_copy(update={"scripts": []})
    untouched = AsyncMock(side_effect=AssertionError("Method-only needs no executable tests"))
    returned, frozen, execution = await verify_skill_script_cases(no_script, method_contract(), sources(),
        generate=untouched, revalidate=revalidate, review=AsyncMock())
    assert returned is no_script and frozen is None
    assert execution == {"status": "not_applicable", "scope": "synthetic", "cases": []}
    assert len(calls) == 2
    docker.assert_not_called()


def wire_creator(monkeypatch, *, script=True):
    monkeypatch.setattr(creator_service, "get_build_checkpoints", lambda **kwargs: None)
    payload = recipe_payload()
    if script:
        payload["scripts"] = [directory_script()]
        from skill_interface_fixtures import group_summary_interface
        source = directory_script()
        payload["implementation"] = {"interface_version": 2, "kind": "parameterized_scripts",
            "reason": "The authored directory interface preserves both independent result files.",
            "scripts": [{**{key: source[key] for key in ("path", "purpose", "invocation")},
                         "requirement_ids": ["R001"], "interface_contract": group_summary_interface()}]}
    calls = []

    async def invoke(messages, **kwargs):
        request = json.loads(messages[1].content)
        calls.append(request)
        if request["stage"] == "synthetic_validation_plan":
            result = plan_payload()
            result["scripts"][0]["script_path"] = helper_script()["path"]
        elif request["stage"] == "synthetic_plan_review":
            result = accepted_fixture_review(request["validation_plan"])
        elif request["stage"] == "synthetic_script_repair":
            result = copy.deepcopy(request["script"])
            result["content"] += "\nimplementation_repaired = True\n"
        elif request["stage"] == "implementation":
            result = directory_script()
        else:
            result = stage_response(messages, payload)
        return AIMessage(content=json.dumps(review_wire_response(messages, result), ensure_ascii=False))

    monkeypatch.setattr(creator_service, "get_settings", lambda: Settings(_env_file=None, api_key="unit-test-placeholder"))
    monkeypatch.setattr(creator_service, "create_chat_model", lambda *args, **kwargs: SimpleNamespace(ainvoke=invoke))
    monkeypatch.setattr(creator_service.TokenUsageService, "record_from_message", AsyncMock())
    service = SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(name="draft")))
    monkeypatch.setattr(creator_service, "get_skill_draft_service", lambda: service)
    return calls, service


@pytest.mark.asyncio
async def test_creator_persists_exact_host_receipt_and_frozen_plan_without_claiming_full_workflow_execution(tmp_path, monkeypatch):
    calls, service = wire_creator(monkeypatch)
    execution = receipt(script_path=helper_script()["path"])
    monkeypatch.setattr(worker, "validate_skill_scripts", AsyncMock(return_value=execution))
    await creator.create_skill_from_session("session-owned", "owner", Repository(), SkillRegistry(str(tmp_path), user_id="owner"))
    service.create.assert_awaited_once()
    saved = service.create.call_args.kwargs
    saved_execution = saved["synthetic_execution"]
    assert {key: saved_execution[key] for key in execution} == execution
    assert saved_execution["oracle_review"]["status"] == "reviewed"
    assert saved_execution["oracle_review"]["verdict"] == "accepted"
    reference = json.loads(saved["references"]["references/validation.json"])
    assert reference["execution"] == saved_execution
    assert reference["status"] == "not_run"
    frozen = json.loads(saved["references"]["references/synthetic-validation.json"])
    assert frozen["status"] == "not_run"
    assert frozen["scripts"][0]["script_path"] == helper_script()["path"]
    assert [request["stage"] for request in calls].count("synthetic_validation_plan") == 1


@pytest.mark.asyncio
async def test_creator_never_persists_draft_after_second_synthetic_failure(tmp_path, monkeypatch):
    calls, service = wire_creator(monkeypatch)
    run = AsyncMock(return_value=receipt("failed", script_path=helper_script()["path"]))
    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    with pytest.raises(ValueError, match="未创建技能"):
        await creator.create_skill_from_session("session-owned", "owner", Repository(), SkillRegistry(str(tmp_path), user_id="owner"))
    assert run.await_count == 2
    assert [request["stage"] for request in calls].count("synthetic_script_repair") == 1
    service.create.assert_not_awaited()


async def save_with_receipt(service, execution):
    return await service.create(owner_id="owner", session_id="session", name="synthetic-summary",
        description="A parameterized method with separately recorded synthetic checks.", triggers=["summary"],
        applicability=["Selected tabular observations"], capabilities=["Aggregate observations"],
        content="# Method\nKeep inputs read-only.", references={"references/validation.json": json.dumps({"execution": execution})},
        scripts={"scripts/group_summary.py": recipe().scripts[0].content}, synthetic_execution=execution)


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", [
    {"status": "passed", "cases": []},
    {"status": "passed", "cases": [{"status": "passed"}, {"status": "failed"}]},
    {"status": "passed", "cases": [{"status": "passed"}, {"status": "unsupported"}]},
    {"status": "failed", "cases": [{"status": "passed"}]},
    {"status": "unavailable", "cases": []},
    {"status": "unsupported", "cases": [{"status": "passed"}, {"status": "failed"}]},
    {"status": "not_run", "cases": [{"status": "failed"}]},
])
async def test_draft_service_rejects_incomplete_or_failed_execution_before_any_durable_write(execution):
    service = make_service()
    with pytest.raises(BadRequestError):
        await save_with_receipt(service, execution)
    assert service.storage.upload_count == 0
    assert service.repository.rows == {}
    service.retention.retain_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["passed", "unsupported"])
async def test_draft_service_public_projection_reports_only_recorded_synthetic_scope(state):
    service = make_service()
    execution = receipt() if state == "passed" else {"status": "unsupported", "scope": "synthetic", "cases": []}
    draft = await save_with_receipt(service, execution)
    validation = draft.public_view().validation
    assert validation.synthetic_execution == state
    assert validation.synthetic_case_count == (3 if state == "passed" else 0)
    assert validation.synthetic_passed_count == (3 if state == "passed" else 0)
    assert validation.workflow_execution == "not_run"
    assert service.storage.upload_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["uncertain", "rejected", "incomplete"])
async def test_untrusted_oracle_never_reaches_worker_or_script_repair(monkeypatch, verdict):
    from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
    run = AsyncMock(side_effect=AssertionError("An unreviewed oracle must never run"))
    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    calls = []

    async def generate(schema, stage, instruction, payload, check=None):
        calls.append(stage)
        if stage == "synthetic_validation_plan":
            candidate = copy.deepcopy(payload.get("previous_validation_plan") or plan_payload())
            if "previous_validation_plan" in payload:
                candidate["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] = 4
            value = schema.model_validate(candidate)
        else:
            assert stage == "synthetic_plan_review"
            candidate = accepted_fixture_review(payload["validation_plan"])
            if verdict == "incomplete":
                candidate["scripts"][0]["cases"][0]["assertions"].pop()
            else:
                candidate["scripts"][0]["cases"][0]["input_validity"]["status"] = verdict
            value = schema.model_validate(candidate)
        if check:
            check(value)
        return value

    with pytest.raises((SkillCreationError, SkillPackageValidationError)):
        await verify_skill_script_cases(recipe(), method_contract(), sources(),
            generate=generate, revalidate=revalidate, review=AsyncMock())
    run.assert_not_awaited()
    assert "synthetic_script_repair" not in calls
    # A contradiction with the computed reference stops; textual revisions
    # cannot replace its frozen scientific values or solicit a fresh vote.
    assert calls.count("synthetic_plan_review") == 1


@pytest.mark.asyncio
async def test_reference_corrects_wrong_oracle_before_review_or_candidate_execution(monkeypatch):
    import hashlib
    from app.domain.services.skills import skill_script_verification as verification
    calls = []
    original = plan_payload()
    original["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] = 999

    async def generate(schema, stage, instruction, payload, check=None):
        calls.append(stage)
        if stage == "synthetic_validation_plan":
            candidate = original
        else:
            assert stage == "synthetic_plan_review"
            candidate = accepted_fixture_review(payload["validation_plan"])
            if payload["validation_plan"]["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] != 3:
                candidate["scripts"][0]["cases"][0]["assertions"][1]["correctness"] = {
                    "status": "rejected", "explanation": "Independent arithmetic: (2+4)/2=3, not 999."}
        value = schema.model_validate(candidate)
        if check:
            check(value)
        return value

    async def computed_reference(plan, contract, source_evidence, interfaces, **kwargs):
        calls.append("independent_reference_boundary")
        assert plan.scripts[0].cases[0].assertions[0].fields["mean"].value == 999
        corrected = SyntheticValidationPlan.model_validate(SyntheticValidationPlan.model_validate(plan_payload()).model_dump(mode="json"))
        return corrected, {"status": "unit_test_isolated", "scope": "synthetic_reference",
            "corrections": [{"script_path": corrected.scripts[0].script_path,
                "case_id": corrected.scripts[0].cases[0].id, "assertion_index": 0,
                "field": "mean", "before": 999, "after": 3}], "execution": {"cases": []},
            "corrected_plan_sha256": hashlib.sha256(worker._json(corrected.model_dump())).hexdigest()}

    async def run(scripts, plan):
        assert calls == ["synthetic_validation_plan", "independent_reference_boundary", "synthetic_plan_review"]
        assert plan.scripts[0].cases[0].assertions[0].fields["mean"].value == 3
        return {**receipt(), "plan_sha256": hashlib.sha256(worker._json(plan.model_dump())).hexdigest()}

    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    monkeypatch.setattr(verification, "verify_reference_plan", computed_reference)
    source = recipe()
    result, _, execution = await verify_skill_script_cases(source, method_contract(), sources(),
        generate=generate, revalidate=revalidate, review=AsyncMock())
    assert result.scripts[0].content == source.scripts[0].content
    assert execution["plan_sha256"] == execution["oracle_review"]["plan_sha256"]
    assert execution["oracle_review"]["revision_count"] == 0
    assert execution["reference_verification"]["corrections"][0]["after"] == 3
