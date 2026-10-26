"""A rejected safety gap adds checks, never rewrites a reference answer."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from skill_review_fixtures import review_for_payload
from pydantic import ValidationError

from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan, validate_validation_plan
from app.domain.services.skills.skill_validation_extension import VerificationCoverageAmendment
from app.domain.services.skills.skill_validation_review import (
    RejectionPostconditionAmendment, ValidationPlanReview, _combine_review_parts,
    apply_postcondition_amendment, eligible_postcondition_cases, oracle_decision_identity, postcondition_revision_cases,
    review_targets, review_validation_plan, validate_postcondition_revision,
)
from app.domain.services.skills.skill_reference_verification import (
    correct_reference_expectations, reference_generation_payload, verify_reference_plan,
)
from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_reference_verification import generation, host_receipt, output_files, method_review
from test_skill_script_verification import recipe, revalidate
from test_skill_validation_plan import method_contract, sources, interfaces
from test_skill_validation_review import accepted_fixture_review, typed


FIXTURE = Path(__file__).parent / "fixtures" / "skill_creator_postcondition_revision"


def missing_postcondition_review(plan):
    result = accepted_fixture_review(plan)
    for script in result["scripts"]:
        for case in script["cases"]:
            if case["case_id"] == "group-invalid":
                case["method_coverage"] = {"status": "rejected", "explanation": "A nonzero exit does not prove that no partial output was written."}
    return ValidationPlanReview.model_validate(result)


def amendment(keys):
    return RejectionPostconditionAmendment.model_validate({"additions": [
        {"script_path": path, "case_id": case_id, "assertion": {"type": "output_empty"}}
        for path, case_id in sorted(keys)]})


def coverage_amendment(keys):
    return VerificationCoverageAmendment.model_validate({
        "assertion_additions": amendment(keys).model_dump()["additions"], "new_cases": []})


def extended(plan=None):
    plan = plan or typed()
    return apply_postcondition_amendment(plan, amendment({("scripts/group_summary.py", "group-invalid")}),
                                        {("scripts/group_summary.py", "group-invalid")})


def evidence(programs, plan, *, empty=True):
    result = host_receipt(programs, plan)
    for script in plan.scripts:
        for case in script.cases:
            if not any(item.type == "output_empty" for item in case.assertions):
                continue
            observed = next(item for item in result["cases"] if item["script_path"] == script.script_path and item["id"] == case.id)
            observed["checks"] = worker._assertions(case, {}, output_empty=empty)
            observed["status"] = "passed" if empty else "failed"
            observed["reason"] = "expectations_matched" if empty else "assertion_mismatch"
    result["status"] = "failed" if any(item["status"] == "failed" for item in result["cases"]) else "passed"
    return result


@pytest.mark.parametrize("strengthen_all_invalid", [False, True])
def test_actual_eleven_review_fragments_keep_all_four_original_rejections_and_only_add_safety_checks(strengthen_all_invalid):
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    plan = SyntheticValidationPlan.model_validate_json((FIXTURE / "plan.json").read_text())
    parts = []
    for entry in provenance["parts"]:
        raw = (FIXTURE / entry["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        parts.append(ValidationPlanReview.model_validate_json(json.loads(raw)["content"]))
    review = _combine_review_parts(parts, plan)
    original_review = review.model_dump(mode="json")
    required = postcondition_revision_cases(plan, review)
    eligible = eligible_postcondition_cases(plan)
    assert len(required) == 4 and len(eligible) == 6
    assert {case for _, case in required} == {
        "invalid_missing_table", "invalid_unknown_unit_null", "invalid_string_number", "invalid_duplicate_long"}
    assert {case for _, case in eligible - required} == {"invalid_group_same", "invalid_same_path"}
    additions = eligible if strengthen_all_invalid else required
    updated = apply_postcondition_amendment(plan, amendment(additions), eligible, required_cases=required)
    assert validate_postcondition_revision(plan, updated, eligible_cases=eligible, required_cases=required) == additions
    assert review.model_dump(mode="json") == original_review and review.verdict() == "rejected"
    assert sum(len(case.assertions) for script in updated.scripts for case in script.cases) == (
        sum(len(case.assertions) for script in plan.scripts for case in script.cases) + len(additions))
    # Strengthening a previously accepted invalid case cannot omit any actual
    # rejection, even when the resulting plan otherwise adds valid safety checks.
    omitted = additions - {next(iter(required))}
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
        apply_postcondition_amendment(plan, amendment(omitted), eligible, required_cases=required)
    partial = apply_postcondition_amendment(plan, amendment(omitted), eligible, required_cases=omitted)
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
        validate_postcondition_revision(plan, partial, eligible_cases=eligible, required_cases=required)


@pytest.mark.parametrize("mutation", ["input", "argv", "exit", "expected", "tolerance", "kind", "requirement", "source",
    "description", "script_order", "boolean_for_integer", "float_for_integer"])
def test_extension_cannot_modify_any_old_semantics_or_scalar_type(mutation):
    plan, updated = typed(), extended()
    case = updated.scripts[0].cases[0]
    if mutation == "input": case.input_files[0].rows[0]["value"] = 7
    elif mutation == "argv": case.argv[-1] = "{output}/other"
    elif mutation == "exit": updated.scripts[0].cases[-1].expected_exit_code = "nonzero"
    elif mutation == "expected": case.assertions[0].fields["mean"].value = 5
    elif mutation == "tolerance": case.assertions[0].fields["mean"].rtol = 0.1
    elif mutation == "kind": case.kind = "boundary"
    elif mutation == "requirement": case.requirement_ids.append("R999")
    elif mutation == "source": case.source_ids.append("S999")
    elif mutation == "description": case.description += " Reworded."
    elif mutation == "script_order": updated.scripts[0].cases.reverse()
    elif mutation == "boolean_for_integer": case.assertions[0].fields["missing_count"].value = False
    else: case.assertions[0].fields["missing_count"].value = 0.0
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
        validate_postcondition_revision(plan, updated)


@pytest.mark.parametrize("mutation", ["duplicate", "foreign", "missing", "expected", "path", "full_plan"])
def test_small_amendment_cannot_escape_host_bound_additions(mutation):
    plan = typed()
    keys = postcondition_revision_cases(plan, missing_postcondition_review(plan))
    document = amendment(keys).model_dump()
    if mutation == "duplicate": document["additions"] *= 2
    elif mutation == "foreign": document["additions"][0]["case_id"] = "group-normal"
    elif mutation == "missing": document["additions"] = []
    elif mutation == "expected": document["additions"][0]["assertion"]["expected"] = False
    elif mutation == "path": document["additions"][0]["assertion"]["path"] = "report.json"
    else: document["validation_plan"] = plan.model_dump()
    with pytest.raises((ValidationError, SkillPackageValidationError)):
        apply_postcondition_amendment(plan, RejectionPostconditionAmendment.model_validate(document), keys)


@pytest.mark.parametrize("field", ["input_validity", "exit_behavior", "support", "correctness", "tolerance", "normal_coverage", "uncertain"])
def test_scientific_or_input_rejection_cannot_be_repolled_by_adding_irrelevant_safety_check(field):
    plan = typed()
    review = missing_postcondition_review(plan)
    script, case = review.scripts[0], review.scripts[0].cases[0]
    if field == "support": script.support.status = "rejected"
    elif field in {"correctness", "tolerance"}: getattr(case.assertions[0], field).status = "rejected"
    elif field == "normal_coverage": case.method_coverage.status = "rejected"
    elif field == "uncertain": case.input_validity.status = "uncertain"
    else: getattr(case, field).status = "rejected"
    with pytest.raises(SkillCreationError):
        postcondition_revision_cases(plan, review)


@pytest.mark.parametrize("exit_code", [124, 137, 255])
def test_postcondition_amendment_cannot_turn_timeout_or_signal_exit_into_normal_rejection(exit_code):
    document = typed().model_dump(mode="json")
    document["scripts"][0]["cases"][-1]["expected_exit_code"] = exit_code
    plan = SyntheticValidationPlan.model_validate(document)
    key = ("scripts/group_summary.py", "group-invalid")
    assert key not in eligible_postcondition_cases(plan)
    with pytest.raises(SkillCreationError):
        postcondition_revision_cases(plan, missing_postcondition_review(plan))
    # Even an injected eligible list or restored unvalidated model cannot
    # bypass the postcondition's typed exit semantics.
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
        apply_postcondition_amendment(plan, amendment({key}), {key})
    invalid = plan.model_copy(deep=True)
    invalid.scripts[0].cases[-1].assertions = extended().scripts[0].cases[-1].assertions
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
        validate_postcondition_revision(plan, invalid)


def test_new_target_is_host_fixed_and_blind_author_never_receives_an_answer():
    plan = extended()
    target = review_targets(plan)[0]["cases"][-1]["targets"]
    assert target == [{"target_id": "T001", "kind": "output_empty", "assertion_index": 0,
                       "assertion_type": "output_empty", "expected": True,
                       "observation_scope": "isolated_output_directory", "reads_scientific_output": False}]
    blind = reference_generation_payload(plan, method_contract(), sources(), interfaces())
    assert blind["blind_cases"][0]["cases"][-1]["output_targets"] == [{"type": "output_empty"}]
    assert oracle_decision_identity(plan) != oracle_decision_identity(typed())


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", [None, "nonempty", "wrong_plan", "wrong_source", "missing_case", "changed_value", "runtime"])
async def test_frozen_reference_extension_reruns_same_bytes_and_never_regenerates_or_corrects(defect):
    generated, calls = generation()
    retained, runs = [], []
    async def execute(programs, plan):
        runs.append((dict(programs), plan.model_copy(deep=True)))
        result = evidence(programs, plan, empty=not (len(runs) > 1 and defect == "nonempty"))
        if len(runs) > 1 and defect == "wrong_plan": result["plan_sha256"] = "0" * 64
        if len(runs) > 1 and defect == "wrong_source": result["source_sha256"] = {key: "0" * 64 for key in programs}
        if len(runs) > 1 and defect == "missing_case": result["cases"].pop()
        if len(runs) > 1 and defect == "changed_value":
            result["cases"][0]["checks"][1].update(actual="4", passed=False)
            result["cases"][0].update(status="failed", reason="assertion_mismatch")
            result["status"] = "failed"
        if len(runs) > 1 and defect == "runtime":
            result["cases"][0].update(status="failed", reason="program_runtime_failure",
                                      runtime_error_type="TypeError", actual_exit_code=1, checks=[])
            result["status"] = "failed"
        return result
    plan, before = await verify_reference_plan(typed(), method_contract(), sources(), interfaces(),
        generate=generated, execute=execute, on_verified=retained.append)
    if defect:
        with pytest.raises(SkillCreationError, match="未创建技能"):
            await retained[0](extended(plan))
    else:
        confirmation = await retained[0](extended(plan))
        after = confirmation.evidence
        assert after["corrected_plan_sha256"] == after["execution"]["plan_sha256"]
        assert after["initial_execution"] == after["execution"]
        extension = after["postcondition_extension"]
        assert extension["base_plan_sha256"] == before["corrected_plan_sha256"]
        assert extension["base_reference_verification"] == before
        assert extension["confirmed_plan_sha256"] == after["corrected_plan_sha256"]
        assert extension["previous_confirmations"] == []
        assert after["reference_source_sha256"] == before["reference_source_sha256"]
        assert after["corrections"] == []
    assert len(runs) == 2 and runs[0][0] == runs[1][0]
    assert [stage for stage, _ in calls] == ["synthetic_reference_implementation", "synthetic_reference_review"]


@pytest.mark.asyncio
async def test_full_pipeline_orders_new_check_same_reference_execution_then_review_before_candidate(monkeypatch):
    calls, runs, saved, sequence = [], [], [], []
    original = recipe()
    from test_skill_reference_verification import generation as reference_generation
    reference_generate, _ = reference_generation()
    async def generate(schema, stage, instruction, payload, check):
        calls.append((schema, stage, copy.deepcopy(payload)))
        if schema is VerificationCoverageAmendment:
            sequence.append("amend")
            value = coverage_amendment({(item["script_path"], item["case_id"]) for item in payload["eligible_cases"]})
            assert payload["validation_review"] == saved[0]
            assert payload["required_cases"] == payload["eligible_cases"] == [
                {"script_path": "scripts/group_summary.py", "case_id": "group-invalid"}]
        elif stage == "synthetic_validation_plan": value = typed()
        elif stage == "synthetic_plan_review":
            extended_case = payload["validation_plan"]["scripts"][0]["cases"][-1]["assertions"]
            sequence.append("review_extended" if extended_case else "review_original")
            if extended_case:
                assert sequence[-2] == "execute_reference_extended"
                assert payload["reference_evidence"]["cases"][-1]["checks"][0]["passed"] is True
                # Every case still receives fresh metadata review, but the
                # previously accepted numerical targets are not voted on again.
                assert [case["case_id"] for case in payload["review_targets"][0]["cases"]] == [
                    "group-normal", "group-boundary", "group-invalid"]
                assert [[target["target_id"] for target in case["targets"]]
                        for case in payload["review_targets"][0]["cases"]] == [[], [], ["T001"]]
                value = schema.model_validate(review_for_payload(accepted_fixture_review(payload["validation_plan"]), payload))
            else:
                value = schema.model_validate(review_for_payload(
                    missing_postcondition_review(SyntheticValidationPlan.model_validate(payload["validation_plan"])), payload))
                saved.append(value.model_dump())
        else: return await reference_generate(schema, stage, instruction, payload, check)
        check(value)
        return value
    async def execute(programs, plan):
        is_candidate = "IMPLEMENTATION_MARKER_NOT_FOR_ORACLE" in "\n".join(programs.values())
        is_extended = bool(plan.scripts[0].cases[-1].assertions)
        sequence.append("execute_candidate" if is_candidate else "execute_reference_extended" if is_extended else "execute_reference_original")
        runs.append((dict(programs), plan.model_copy(deep=True)))
        return evidence(programs, plan)
    monkeypatch.setattr(worker, "validate_skill_scripts", execute)
    result, frozen, record = await verify_skill_script_cases(original, method_contract(), sources(),
        generate=generate, revalidate=revalidate, review=AsyncMock())
    assert result == original
    assert sequence == ["execute_reference_original", "review_original", "amend", "execute_reference_extended",
                        "review_extended", "execute_candidate"]
    assert runs[0][0] == runs[1][0]
    assert record["plan_sha256"] == record["oracle_review"]["plan_sha256"] == record["reference_verification"]["corrected_plan_sha256"]
    assert frozen["scripts"][0]["cases"][-1]["assertions"] == [{"type": "output_empty"}]
    assert [stage for _, stage, _ in calls].count("synthetic_reference_implementation") == 1
    reuse = record["oracle_review"]["reused_target_reviews"][0]
    assert len(reuse["reused_targets"]) == 12 and reuse["new_review_target_count"] == 1
    assert record["oracle_review"]["review"]["scripts"][0]["cases"][0]["assertions"] == (
        saved[0]["scripts"][0]["cases"][0]["assertions"])


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["transport", "negative"])
async def test_checkpoint_resume_reuses_amendment_but_reruns_reference_without_repolling_original_rejection(monkeypatch, interruption):
    from test_skill_creation_checkpoint_integration import Gateway, pipeline, verifier
    from test_skill_creation_checkpoints import MemoryCache, builds
    from test_session_skill_creator import helper_script
    from app.domain.services.skills.skill_creation.checkpoints import content_digest
    from app.domain.services.skills.skill_script_verification import script_interfaces
    cache, runs = MemoryCache(), []
    seen_extended = 0
    original = recipe()
    def reply(stage, payload):
        nonlocal seen_extended
        if stage == "synthetic_validation_plan":
            if "eligible_cases" in payload:
                keys = {(item["script_path"], item["case_id"]) for item in payload["eligible_cases"]}
                if not keys:
                    # A further coverage revision is now permitted (for
                    # example a required diagnostic or new branch). This
                    # deliberately invalid response repeats the old check;
                    # it must fail without polling the rejected plan again.
                    keys = {(item["script_path"], item["case_id"]) for item in payload["required_cases"]}
                return coverage_amendment(keys).model_dump()
            return typed().model_dump()
        if stage == "synthetic_reference_implementation":
            return {**payload["script_request"], "content": helper_script()["content"]}
        if stage == "synthetic_reference_review": return method_review(payload)
        assert stage == "synthetic_plan_review"
        plan = SyntheticValidationPlan.model_validate(payload["validation_plan"])
        if not plan.scripts[0].cases[-1].assertions:
            return review_for_payload(missing_postcondition_review(plan), payload)
        seen_extended += 1
        if interruption == "transport" and seen_extended == 1:
            raise SkillCreationError(stage, "model_unavailable")
        return review_for_payload(missing_postcondition_review(plan) if interruption == "negative"
                                  else accepted_fixture_review(plan), payload)
    gateway = Gateway(reply)
    async def execute(programs, plan):
        runs.append((dict(programs), plan.model_copy(deep=True)))
        return evidence(programs, plan)
    monkeypatch.setattr(worker, "validate_skill_scripts", execute)
    async def run():
        build = pipeline(cache, gateway, verifier())
        build.contract = method_contract()
        return await verify_skill_script_cases(original, method_contract(), sources(), generate=build.generate,
            revalidate=revalidate, review=AsyncMock(), checkpoints=build.checkpoints)
    with pytest.raises(SkillCreationError):
        await run()
    saved = await builds(cache).get_oracle_revision()
    assert saved.review.verdict() == "rejected"
    assert saved.contract_digest == content_digest(method_contract())
    assert saved.interfaces_digest == content_digest(script_interfaces(original))
    before_second = len(runs)
    if interruption == "transport":
        _, _, record = await run()
        assert record["status"] == "passed"
        assert record["reference_verification"]["execution"]["plan_sha256"] == record["plan_sha256"]
        assert seen_extended == 2
    else:
        with pytest.raises(SkillPackageValidationError) as failure:
            await run()
        assert failure.value.code == "validation_oracle_revision_invalid"
        assert seen_extended == 1
        assert (await builds(cache).get_oracle_revision()).review == saved.review
    assert len(runs) >= before_second + 2
    # No second model vote on the original rejected coverage; positive source
    # and amendment artifacts are reused, while reference evidence is fresh.
    reviews = [payload for stage, payload in gateway.calls if stage == "synthetic_plan_review"]
    assert sum(not payload["validation_plan"]["scripts"][0]["cases"][-1]["assertions"] for payload in reviews) == 1
    assert sum("eligible_cases" in payload for _, payload in gateway.calls) == (2 if interruption == "negative" else 1)
    assert sum(stage == "synthetic_reference_implementation" for stage, _ in gateway.calls) == 1
    assert runs[0][0] == runs[1][0] == runs[before_second][0] == runs[before_second + 1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_public", [False, True])
async def test_reconfirmed_extension_keeps_same_public_contract_and_rejects_wrong_digest(corrupt_public):
    from test_skill_reference_public_columns import declared, source
    from test_skill_reference_public_runtime import observed_receipt, files_with_columns
    calls, retained, contracts = [], [], []
    async def generate(schema, stage, instruction, payload, check):
        calls.append(stage)
        value = schema.model_validate({**payload["script_request"], "content": source()}) if stage == "synthetic_reference_implementation" else schema.model_validate(method_review(payload))
        check(value)
        return value
    async def execute(programs, plan, *, public_output_contract):
        contracts.append(public_output_contract.model_dump(mode="json"))
        result = observed_receipt(programs, plan, public_output_contract, files=files_with_columns)
        if len(contracts) > 1:
            for script in plan.scripts:
                for case in script.cases:
                    if any(item.type == "output_empty" for item in case.assertions):
                        row = next(item for item in result["cases"] if item["script_path"] == script.script_path and item["id"] == case.id)
                        row.update(checks=worker._assertions(case, {}, output_empty=True), status="passed", reason="expectations_matched")
            result["status"] = "passed"
            if corrupt_public: result["public_output_verification"]["contract_sha256"] = "0" * 64
        return result
    plan, _ = await verify_reference_plan(typed(), method_contract(), sources(), [declared()],
        generate=generate, execute=execute, on_verified=retained.append)
    if corrupt_public:
        with pytest.raises(SkillCreationError, match="未创建技能"):
            await retained[0](extended(plan))
    else:
        confirmation = await retained[0](extended(plan))
        assert confirmation.evidence["execution"]["public_output_verification"]["status"] == "passed"
    assert contracts[0] == contracts[1] and len(calls) == 2
