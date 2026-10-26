"""Real pipeline/checkpoint boundaries for narrowly scoped source revisions.

The generator is deterministic and does not judge scientific correctness. All
production recipe, coverage, privacy, decision and continuation checks still run.
"""
import copy
import hashlib
import json
import re
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_creation.evidence import AnalysisContext
from app.domain.services.skills.skill_creation.pipeline import SkillCreationPipeline
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillRepairReviewRejected
from test_skill_creation_checkpoint_integration import Gateway, context, response, verifier
from test_skill_creation_checkpoints import MemoryCache, builds
from test_session_skill_creator import helper_script, recipe_payload


class Authoring:
    def __init__(self, verdicts, *, edit=None, scripts=1):
        self.verdicts = list(verdicts)
        self.edit = edit
        self.designs = 0
        self.review_documents = []
        self.scripts = []
        for index in range(scripts):
            script = helper_script()
            if index:
                script.update(path=f"scripts/secondary_{index}.py",
                    invocation=f"python scripts/secondary_{index}.py --input INPUT --output OUTPUT")
            script["content"] = script["content"].replace("import csv\n", "import csv\nimport json\n").replace(
                "str(len(rows))", "json.dumps({'field_0': len(rows)})")
            self.scripts.append(script)
        self.base = recipe_payload()
        self.base["implementation"] = {
            "kind": "parameterized_scripts",
            "reason": "Preserve the original method in executable parameterized helpers.",
            "scripts": [{key: value for key, value in script.items() if key != "content"} |
                {"requirement_ids": ["R001"], "implementation_source_ids": []}
                for script in self.scripts],
        }
        self.gateway = Gateway(self.reply)

    def reply(self, stage, payload):
        if stage == "workflow":
            self.designs += 1
            candidate = copy.deepcopy(self.base)
            if revision := payload.get("method_revision"):
                candidate["steps"] = copy.deepcopy(revision["previous_skill_package"]["steps"])
                candidate["steps"][0]["verification"] += f" 增加来源要求的质量核验步骤 {self.designs}。"
            return response(stage, payload, candidate)
        if stage == "implementation":
            path = payload["script_request"]["path"]
            scripts = (payload["method_revision"]["previous_skill_package"]["scripts"]
                       if payload.get("method_revision") else self.scripts)
            return copy.deepcopy(next(script for script in scripts if script["path"] == path))
        if stage == "implementation_revision":
            if self.edit is not None:
                return self.edit(payload)
            script = payload["previous_skill_package"]["scripts"][0]
            before = re.search(r"'field_(\d+)'", script["content"])
            assert before is not None
            return {"edits": [{"script_path": script["path"], "before": before[0],
                "after": f"'field_{int(before[1]) + 1}'"}]}
        value = response(stage, payload, self.base)
        if stage == "method_review":
            assert self.verdicts, "No extra reviewer vote is authorized by this fixture"
            verdict = self.verdicts.pop(0)
            if verdict in {"implementation", "mixed"}:
                value.update(implementation_appropriate=False,
                    implementation_reason="当前实现读取了与方法规定不一致的结果字段，需要修订可执行代码。")
            if verdict in {"method", "mixed"}:
                value["requirements"][0].update(status="changed", explanation="当前工作流改变了来源中明确要求的质量核验方法。")
            if verdict == "source":
                value["sources"][0].update(status="missing", explanation="当前设计遗漏了该来源中明确要求的质量核验方法。")
            self.review_documents.append(copy.deepcopy(value))
        return value

    def calls(self, stage):
        return [payload for actual, payload in self.gateway.calls if actual == stage]


def make_pipeline(author, cache, verify=None, *, privacy=None):
    ctx = context()
    if privacy is not None:
        ctx = AnalysisContext(privacy, ctx.evidence, ctx.sources, ctx.programs)
    return SkillCreationPipeline(ctx, author.gateway, script_verifier=verify or verifier(), checkpoints=builds(cache))


def without_source(document):
    value = copy.deepcopy(document)
    for script in value["scripts"]:
        script.pop("content")
    return value


def adverse_entries(cache):
    return {key: copy.deepcopy(value) for key, value in cache.entries.items() if ":adverse:" in key}


@pytest.mark.asyncio
async def test_implementation_negative_changes_only_selected_source_and_keeps_exact_review():
    author, cache, verify = Authoring(["implementation", "accepted"], scripts=2), MemoryCache(), verifier()
    result = await make_pipeline(author, cache, verify).run()
    old, new = [item["skill_package"] for item in author.calls("method_review")]
    assert without_source(old) == without_source(new) == without_source(result.recipe.model_dump())
    assert new["scripts"][0]["content"] == old["scripts"][0]["content"].replace("'field_0'", "'field_1'")
    assert new["scripts"][1] == old["scripts"][1]
    [revision] = author.calls("implementation_revision")
    assert revision["review"] == author.review_documents[0]
    assert revision["previous_skill_package"] == old
    assert set(revision) == {"review", "previous_skill_package", "method_contract", "sources", "implementation_sources"}
    assert len(author.calls("workflow")) == 1
    assert len(author.calls("implementation")) == 2
    verify.assert_awaited_once()
    saved = await builds(cache).get_revision()
    assert saved.review.model_dump() == author.review_documents[0]
    assert saved.recipe.model_dump() == old
    assert len(adverse_entries(cache)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("negative", ["method", "source", "mixed"])
async def test_real_method_or_source_loss_keeps_workflow_revision_route(negative):
    author, cache = Authoring([negative, "accepted"]), MemoryCache()
    result = await make_pipeline(author, cache).run()
    assert author.calls("implementation_revision") == []
    assert len(author.calls("workflow")) == len(author.calls("implementation")) == 2
    revision = author.calls("workflow")[1]["method_revision"]
    assert revision["review"] == author.review_documents[0]
    assert result.recipe.steps[0].verification.endswith("质量核验步骤 2。")


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded_status", ["valid_exclusion", "preserved"])
async def test_excluded_source_requires_valid_exclusion_for_implementation_only_route(excluded_status):
    author, cache = Authoring(["implementation", "accepted"]), MemoryCache()
    excluded = context().sources[-1]["id"]
    original_reply = author.reply
    def reply(stage, payload):
        value = original_reply(stage, payload)
        if stage == "method_requirements":
            value["requirements"][0]["source_ids"].remove(excluded)
            value["excluded_sources"] = [{"source_id": excluded, "reason": "observed_result",
                "explanation": "该段为一次性观察总结，其余来源已经完整记录可迁移方法。"}]
        elif stage == "method_review":
            verdict = next(item for item in value["sources"] if item["source_id"] == excluded)
            verdict["status"] = excluded_status if len(author.review_documents) == 1 else "valid_exclusion"
            author.review_documents[-1] = copy.deepcopy(value)
        return value
    author.gateway.reply = reply
    await make_pipeline(author, cache).run()
    assert len(author.calls("implementation_revision")) == (1 if excluded_status == "valid_exclusion" else 0)
    assert len(author.calls("workflow")) == (1 if excluded_status == "valid_exclusion" else 2)
    assert len(author.calls("method_review")) == 2
    saved = await builds(cache).get_revision()
    assert saved.review.model_dump() == author.review_documents[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("method", "implementation"), ("implementation", "method")])
async def test_different_revision_kinds_can_each_run_once_without_losing_previous_code(order):
    author, cache = Authoring([*order, "accepted"]), MemoryCache()
    result = await make_pipeline(author, cache).run()
    assert len(author.calls("method_review")) == 3
    assert len(author.calls("implementation_revision")) == 1
    assert len(author.calls("workflow")) == 2
    assert "'field_1'" in result.recipe.scripts[0].content
    assert result.recipe.steps[0].verification.endswith("质量核验步骤 2。")
    assert len(adverse_entries(cache)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["implementation", "method"])
async def test_second_same_kind_negative_stops_and_explicit_retry_uses_latest_candidate(kind):
    author, cache, verify = Authoring([kind, kind, "accepted"]), MemoryCache(), verifier()
    current = make_pipeline(author, cache, verify)
    with pytest.raises(SkillCreationError) as error:
        await current.run()
    assert error.value.code == "method_review_rejected"
    verify.assert_not_awaited()
    saved = await builds(cache).get_revision(check=current.check_revision)
    assert saved.recipe.model_dump() == author.calls("method_review")[-1]["skill_package"]
    assert saved.review.model_dump() == author.review_documents[-1]
    original_decisions = adverse_entries(cache)
    assert len(original_decisions) == 2
    result = await make_pipeline(author, cache, verify).run()
    assert all(cache.entries[key] == value for key, value in original_decisions.items())
    assert len(author.calls("method_requirements")) == 1
    assert len(author.calls("method_review")) == 3
    if kind == "implementation":
        assert len(author.calls("workflow")) == len(author.calls("implementation")) == 1
        latest = author.calls("implementation_revision")[-1]
        assert latest["previous_skill_package"] == saved.recipe.model_dump()
        assert latest["review"] == saved.review.model_dump()
        assert "'field_2'" in result.recipe.scripts[0].content
    else:
        assert author.calls("implementation_revision") == []
        latest = author.calls("workflow")[-1]["method_revision"]
        assert latest["previous_skill_package"] == saved.recipe.model_dump()
        assert latest["review"] == saved.review.model_dump()
        assert result.recipe.steps[0].verification.endswith("质量核验步骤 3。")
    verify.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["implementation", "method"])
async def test_restored_negative_consumes_current_run_revision_allowance(kind):
    author, cache, verify = Authoring([kind] * 3), MemoryCache(), verifier()
    with pytest.raises(SkillCreationError):
        await make_pipeline(author, cache, verify).run()
    before = len(author.calls("method_review"))
    with pytest.raises(SkillCreationError) as error:
        await make_pipeline(author, cache, verify).run()
    assert error.value.code == "method_review_rejected"
    assert len(author.calls("method_review")) == before + 1
    assert len(author.calls("implementation_revision")) == (2 if kind == "implementation" else 0)
    assert len(author.calls("workflow")) == (1 if kind == "implementation" else 3)
    saved = await builds(cache).get_revision()
    assert saved.review.model_dump() == author.review_documents[-1]
    assert saved.recipe.model_dump() == author.calls("method_review")[-1]["skill_package"]
    assert len(adverse_entries(cache)) == 3
    verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_only_transaction_gets_no_new_vote_and_is_not_cached_as_progress():
    def comment(payload):
        script = payload["previous_skill_package"]["scripts"][0]
        return {"edits": [{"script_path": script["path"], "before": "import csv\n", "after": "import csv  # cosmetic change\n"}]}
    author, cache, verify = Authoring(["implementation", "accepted"], edit=comment), MemoryCache(), verifier()
    with pytest.raises(SkillCreationError) as error:
        await make_pipeline(author, cache, verify).run()
    assert error.value.code == "method_revision_unchanged"
    assert len(author.calls("method_review")) == 1
    verify.assert_not_awaited()
    saved = await builds(cache).get_revision()
    assert "cosmetic change" not in saved.recipe.scripts[0].content
    decisions = adverse_entries(cache)
    author.edit = None
    result = await make_pipeline(author, cache, verify).run()
    assert "'field_1'" in result.recipe.scripts[0].content
    assert len(author.calls("implementation_revision")) == 2
    assert len(author.calls("method_review")) == 2
    assert all(cache.entries[key] == value for key, value in decisions.items())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown_script", "anchor_missing", "duplicate", "overlap", "new_metadata"])
async def test_invalid_source_transaction_cannot_reach_review_or_replace_latest_negative(failure):
    def invalid(payload):
        script = payload["previous_skill_package"]["scripts"][0]
        item = {"script_path": script["path"], "before": "'field_0'", "after": "'field_1'"}
        edits = [item]
        if failure == "unknown_script":
            item["script_path"] = "scripts/undeclared.py"
        elif failure == "anchor_missing":
            item["before"] = "'not_in_original_source'"
        elif failure == "duplicate":
            edits.append(copy.deepcopy(item))
        elif failure == "overlap":
            edits.append({"script_path": script["path"], "before": "field_0", "after": "new_field"})
        elif failure == "new_metadata":
            item["invocation"] = "python scripts/undeclared.py --input INPUT --output OUTPUT"
        return {"edits": edits}
    author, cache, verify = Authoring(["implementation"], edit=invalid), MemoryCache(), verifier()
    with pytest.raises((SkillCreationError, ValidationError)) as error:
        await make_pipeline(author, cache, verify).run()
    if failure != "new_metadata":
        assert error.value.code == "implementation_revision_invalid"
    assert len(author.calls("method_review")) == len(author.calls("workflow")) == 1
    saved = await builds(cache).get_revision()
    assert saved.recipe.model_dump() == author.calls("method_review")[0]["skill_package"]
    assert saved.review.model_dump() == author.review_documents[0]
    assert len(adverse_entries(cache)) == 1
    verify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["syntax", "privacy"])
async def test_source_transaction_still_passes_compilation_and_private_content_gates(failure):
    def edit(payload):
        script = payload["previous_skill_package"]["scripts"][0]
        return {"edits": [{"script_path": script["path"], "before": "'field_0'",
            "after": "(" if failure == "syntax" else "'private-sample-record'"}]}
    author, cache, verify = Authoring(["implementation"], edit=edit), MemoryCache(), verifier()
    privacy = PrivacyContext((PrivateValue("private-sample-record", "identity"),))
    with pytest.raises(SkillCreationError) as error:
        await make_pipeline(author, cache, verify, privacy=privacy).run()
    assert error.value.code == ("script_syntax_invalid" if failure == "syntax" else "private_package_content")
    assert len(author.calls("method_review")) == 1
    saved = await builds(cache).get_revision()
    assert "'field_0'" in saved.recipe.scripts[0].content
    verify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["", "pass\n"])
async def test_source_transaction_cannot_bypass_original_content_schema(replacement):
    def edit(payload):
        script = payload["previous_skill_package"]["scripts"][0]
        return {"edits": [{"script_path": script["path"], "before": script["content"], "after": replacement}]}
    author, cache, verify = Authoring(["implementation"], edit=edit), MemoryCache(), verifier()
    with pytest.raises(SkillCreationError) as error:
        await make_pipeline(author, cache, verify).run()
    assert error.value.code == "implementation_revision_invalid"
    assert len(author.calls("method_review")) == 1
    assert len(author.calls("workflow")) == 1
    saved = await builds(cache).get_revision()
    assert saved.recipe.model_dump() == author.calls("method_review")[0]["skill_package"]
    assert saved.review.model_dump() == author.review_documents[0]
    verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_frozen_synthetic_repair_negative_cannot_enter_initial_implementation_revision(monkeypatch):
    from app.domain.services.skills import skill_script_verification as verification
    from app.domain.services.skills.skill_validation import SyntheticValidationPlan
    from app.infrastructure.external.sandbox import skill_validation_worker as worker
    from test_skill_script_verification import receipt
    from test_skill_validation_plan import plan_payload
    from test_skill_validation_review import accepted_fixture_review

    author, cache = Authoring(["accepted", "implementation"]), MemoryCache()
    path = author.scripts[0]["path"]
    author.scripts[0]["content"] = helper_script()["content"].replace(
        "Path(a.output).write_text", "(Path(a.output) / 'summary.csv').write_text")
    author.scripts[0]["content"] += "\nfield_name = 'field_0'\nstatus_file = 'status.json'\n"
    frozen = plan_payload()
    frozen["scripts"][0]["script_path"] = path
    frozen = SyntheticValidationPlan.model_validate(frozen).model_dump(mode="json")
    original_reply = author.reply
    def reply(stage, payload):
        if stage == "synthetic_validation_plan":
            return copy.deepcopy(frozen)
        if stage == "synthetic_plan_review":
            return accepted_fixture_review(payload["validation_plan"])
        if stage == "synthetic_script_repair":
            script = copy.deepcopy(payload["script"])
            script["content"] = script["content"].replace("'field_0'", "'field_1'")
            return script
        return original_reply(stage, payload)
    author.gateway.reply = reply
    async def reference(plan, *args, **kwargs):
        return plan.model_copy(deep=True), {"status": "unit_test_isolated", "scope": "synthetic_reference",
            "corrections": [], "execution": {"cases": []},
            "corrected_plan_sha256": hashlib.sha256(worker._json(plan.model_dump())).hexdigest()}
    monkeypatch.setattr(verification, "verify_reference_plan", reference)
    run = AsyncMock(return_value=receipt("failed", script_path=path))
    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    async def execute(recipe, *args, **kwargs):
        return await verification.verify_skill_script_cases(recipe, *args, **kwargs, checkpoints=builds(cache))
    with pytest.raises(SkillRepairReviewRejected) as error:
        await make_pipeline(author, cache, execute).run()
    assert not error.value.review.implementation_appropriate
    assert error.value.review.model_dump() == author.review_documents[-1]
    assert run.await_count == 1  # The rejected repair is never executed.
    assert run.await_args.args[1].model_dump(mode="json") == frozen
    saved = await builds(cache).get_execution_revision()
    assert saved.plan.model_dump(mode="json") == frozen
    assert saved.pending_review.model_dump() == author.review_documents[-1]
    assert "'field_0'" in saved.recipe.scripts[0].content
    assert "'field_1'" in saved.pending_recipe.scripts[0].content
    assert saved.repair_used is True
    assert author.calls("implementation_revision") == []
    assert len(author.calls("workflow")) == 1
    assert await builds(cache).get_revision() is None
    assert len(adverse_entries(cache)) == 1


@pytest.mark.asyncio
async def test_gateway_repairs_comment_only_edits_against_same_original_before_new_review():
    from test_skill_creation_gateway import make_gateway, requests

    author, cache, verify = Authoring(["implementation", "accepted"]), MemoryCache(), verifier()
    path = author.scripts[0]["path"]
    real, calls, _ = make_gateway([
        {"edits": [{"script_path": path, "before": "import csv\n", "after": "import csv  # cosmetic only\n"}]},
        {"edits": [{"script_path": path, "before": "'field_0'", "after": "'field_1'"}]},
    ], privacy=PrivacyContext())
    fake_generate = author.gateway.generate
    async def generate(schema, stage, instruction, payload, check, *, contract):
        if stage != "implementation_revision":
            return await fake_generate(schema, stage, instruction, payload, check, contract=contract)
        author.gateway.stage = stage
        author.gateway.calls.append((stage, copy.deepcopy(payload)))
        return await real.generate(schema, stage, instruction, payload, check, contract=contract)
    author.gateway.generate = generate
    result = await make_pipeline(author, cache, verify).run()
    first, repair = requests(calls)
    assert repair["repair_feedback"]["reason"] == "method_revision_unchanged"
    assert repair["repair_feedback"]["fields"] == ["edits"]
    assert first["previous_skill_package"] == repair["previous_skill_package"]
    assert first["method_contract"] == repair["method_contract"]
    assert first["review"] == repair["review"] == author.review_documents[0]
    assert len(author.calls("method_review")) == 2
    old, new = [item["skill_package"] for item in author.calls("method_review")]
    assert without_source(old) == without_source(new)
    assert "cosmetic only" not in result.recipe.scripts[0].content
    assert "'field_1'" in result.recipe.scripts[0].content
    verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_edits_resume_after_reviewer_outage_without_discarding_negative():
    author, cache, verify = Authoring(["implementation", "accepted"]), MemoryCache(), verifier()
    original_reply, failed_once = author.reply, False
    def reply(stage, payload):
        nonlocal failed_once
        if stage == "method_review" and "'field_1'" in payload["skill_package"]["scripts"][0]["content"] and not failed_once:
            failed_once = True
            raise SkillCreationError(stage, "model_unavailable")
        return original_reply(stage, payload)
    author.gateway.reply = reply
    with pytest.raises(SkillCreationError) as error:
        await make_pipeline(author, cache, verify).run()
    assert error.value.code == "model_unavailable"
    decisions = adverse_entries(cache)
    assert len(decisions) == 1
    edits = [entry["value"] for key, entry in cache.entries.items()
             if ":artifact:" in key and "edits" in entry.get("value", {})]
    assert len(edits) == 1
    verify.assert_not_awaited()
    result = await make_pipeline(author, cache, verify).run()
    assert "'field_1'" in result.recipe.scripts[0].content
    assert len(author.calls("implementation_revision")) == 1
    assert len(author.calls("workflow")) == len(author.calls("implementation")) == 1
    assert len(author.calls("method_review")) == 3
    assert author.calls("method_review")[-1] == author.calls("method_review")[-2]
    assert all(cache.entries[key] == value for key, value in decisions.items())
    verify.assert_awaited_once()
