"""Pipeline continuation preserves completed work and resumes rejected methods."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.method_contract import evidence_sources
from app.domain.services.skills.skill_creation.evidence import AnalysisContext, build_synthesis_evidence
from app.domain.services.skills.skill_creation.pipeline import SkillCreationPipeline
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_session_skill_creator import analysis_events, helper_script, recipe_payload, stage_response
from test_skill_creation_checkpoints import MemoryCache, builds


def context():
    evidence = build_synthesis_evidence(analysis_events())
    return AnalysisContext(PrivacyContext(), evidence, evidence_sources(evidence), [])


class Gateway:
    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self._progress = AsyncMock()

    async def generate(self, schema, stage, instruction, payload, check, *, contract):
        self.stage = stage
        self.calls.append((stage, copy.deepcopy(payload)))
        value = schema.model_validate(self.reply(stage, payload))
        check(value)
        return value


def response(stage, payload, recipe=None):
    return stage_response([None, SimpleNamespace(content=json.dumps({"stage": stage, **payload}))], recipe)


def verifier():
    async def verify(recipe, *args, **kwargs):
        return recipe, None, {"status": "not_applicable", "scope": "synthetic", "cases": []}
    return AsyncMock(side_effect=verify)


def pipeline(cache, gateway, verify):
    return SkillCreationPipeline(context(), gateway, script_verifier=verify, checkpoints=builds(cache))


@pytest.mark.asyncio
async def test_real_pipeline_retry_recovers_requirements_workflow_and_first_script():
    cache, verify, failed_once = MemoryCache(), verifier(), False
    first = helper_script()
    second = {**first, "path": "scripts/second.py", "invocation": "python scripts/second.py --input INPUT --output OUTPUT"}
    source = recipe_payload()
    source["implementation"] = {"kind": "parameterized_scripts", "reason": "Preserve all reusable computations in parameterized helpers.",
        "scripts": [{key: value for key, value in script.items() if key != "content"} |
            {"requirement_ids": ["R001"], "implementation_source_ids": []} for script in (first, second)]}
    def reply(stage, payload):
        nonlocal failed_once
        if stage == "implementation":
            path = payload["script_request"]["path"]
            if path == second["path"] and not failed_once:
                failed_once = True
                raise SkillCreationError(stage, "invalid_schema")
            return first if path == first["path"] else second
        return response(stage, payload, source)
    gateway = Gateway(reply)
    with pytest.raises(SkillCreationError) as error:
        await pipeline(cache, gateway, verify).run()
    assert error.value.code == "invalid_schema"
    verify.assert_not_awaited()
    result = await pipeline(cache, gateway, verify).run()
    assert [item.path for item in result.recipe.scripts] == [first["path"], second["path"]]
    assert result.recipe.scripts[0].content == first["content"]
    stages = [stage for stage, _ in gateway.calls]
    assert stages.count("method_requirements") == stages.count("workflow") == stages.count("method_review") == 1
    assert [payload["script_request"]["path"] for stage, payload in gateway.calls if stage == "implementation"] == [first["path"], second["path"], second["path"]]
    verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_continues_latest_rejected_candidate_and_exact_review():
    cache, verify = MemoryCache(), verifier()
    designs = []
    def reply(stage, payload):
        if stage == "workflow":
            revision = payload.get("method_revision")
            designs.append(copy.deepcopy(revision))
            recipe = recipe_payload()
            recipe["steps"][0]["verification"] += f" 质量核验步骤完成版本 {len(designs)}。"
            return response(stage, payload, recipe)
        value = response(stage, payload)
        if stage == "method_review" and len(designs) < 3:
            value["requirements"][0].update(status="missing", explanation=f"仍需补充当前候选的质量检查版本 {len(designs)}。")
        return value
    gateway = Gateway(reply)
    with pytest.raises(SkillCreationError) as error:
        await pipeline(cache, gateway, verify).run()
    assert error.value.code == "method_review_rejected"
    verify.assert_not_awaited()
    result = await pipeline(cache, gateway, verify).run()
    assert len(designs) == 3 and designs[0] is None
    assert designs[2]["review"]["requirements"][0]["explanation"].endswith("版本 2。")
    assert designs[2]["previous_skill_package"]["steps"][0]["verification"].endswith("版本 2。")
    assert result.recipe.steps[0].verification.endswith("版本 3。")
    assert [stage for stage, _ in gateway.calls].count("method_requirements") == 1
    assert [stage for stage, _ in gateway.calls].count("method_review") == 3
    verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_unchanged_revision_is_evicted_so_retry_can_make_substantive_progress():
    cache, verify = MemoryCache(), verifier()
    workflow_count = 0
    def reply(stage, payload):
        nonlocal workflow_count
        recipe = recipe_payload()
        if stage == "workflow":
            workflow_count += 1
            if workflow_count >= 3:
                recipe["steps"][0]["verification"] += " 增加缺失数据的逐项检查并报告异常数量。"
        value = response(stage, payload, recipe)
        if stage == "method_review" and workflow_count < 3:
            value["requirements"][0].update(status="missing", explanation="缺失数据逐项检查尚未明确落实。")
        return value
    gateway = Gateway(reply)
    with pytest.raises(SkillCreationError) as error:
        await pipeline(cache, gateway, verify).run()
    assert error.value.code == "method_revision_unchanged"
    verify.assert_not_awaited()
    result = await pipeline(cache, gateway, verify).run()
    assert workflow_count == 3
    assert result.recipe.steps[0].verification.endswith("报告异常数量。")
    stages = [stage for stage, _ in gateway.calls]
    assert stages.count("method_requirements") == 1 and stages.count("method_review") == 2


def test_production_scope_initializes_without_redis_io_and_hashes_private_metadata(monkeypatch):
    from app.core.config import Settings
    from app.domain.services.skills.skill_creation.model_policy import resolve_skill_model_policy
    from app.domain.services.skills.skill_creation.service import get_build_checkpoints
    cache, ctx = MemoryCache(), context()
    ctx = AnalysisContext(PrivacyContext((PrivateValue("private-owner-marker", "identity"),)), ctx.evidence, ctx.sources, ctx.programs)
    monkeypatch.setattr("app.infrastructure.external.cache.get_cache", lambda: cache)
    monkeypatch.setattr("app.domain.services.skills.skill_creation.service._creator_code_version", lambda: "test-release")
    settings = Settings(_env_file=None, model_provider="openai", model_name="test-model", api_key="test-credential", api_base="https://provider.invalid")
    policy = resolve_skill_model_policy(settings, {})
    def create(owner="owner", context=ctx, overrides=None):
        return get_build_checkpoints(context=context, policy=policy, overrides=overrides or {}, owner_id=owner, session_id="session")
    first = create()
    assert first.cache is cache and not cache.entries
    assert create().scope == first.scope
    assert create(owner="other").scope != first.scope
    assert create(overrides={"temperature": 0.5}).scope.artifacts != first.scope.artifacts
    changed = AnalysisContext(ctx.privacy, {**ctx.evidence, "goal": "A different analysis"}, ctx.sources, ctx.programs)
    assert create(context=changed).scope.identity != first.scope.identity
    assert all(value not in repr(first.scope) for value in ("test-credential", "private-owner-marker", "provider.invalid"))


@pytest.mark.asyncio
async def test_method_rejected_execution_repair_returns_real_review_without_rewriting_workflow():
    cache, script, source = MemoryCache(), helper_script(), recipe_payload()
    source["implementation"] = {"kind": "parameterized_scripts", "reason": "Preserve each computation in a reusable parameterized helper.",
        "scripts": [{key: value for key, value in script.items() if key != "content"} |
            {"requirement_ids": ["R001"], "implementation_source_ids": []}]}
    repaired_content = None
    def reply(stage, payload):
        if stage == "implementation":
            return script
        candidate = copy.deepcopy(source)
        if stage == "workflow" and payload.get("method_revision"):
            candidate["steps"][0]["verification"] += " 对所有缺失与异常记录逐项检查并保留质量核验。"
        value = response(stage, payload, candidate)
        if stage == "method_review" and any("quality_check = 1" in item["content"] for item in payload["skill_package"]["scripts"]):
            value["requirements"][0].update(status="changed", explanation="执行修复改变原方法，必须恢复全部科学检查。")
        return value
    gateway, attempts = Gateway(reply), 0
    async def verify(recipe, *args, review, **kwargs):
        nonlocal attempts, repaired_content
        attempts += 1
        if attempts == 1:
            repaired = recipe.model_copy(deep=True)
            repaired.scripts[0].content += "\nquality_check = 1"
            repaired_content = repaired.scripts[0].content
            # A passing synthetic execution still needs independent method
            # review of the actual repaired code before it can be delivered.
            await review(repaired)
        return recipe, None, {"status": "passed", "scope": "synthetic", "cases": []}
    with pytest.raises(SkillCreationError) as error:
        await pipeline(cache, gateway, verify).run()
    assert error.value.code == "method_review_rejected"
    from app.domain.services.skills.skill_creation_error import SkillRepairReviewRejected
    assert isinstance(error.value, SkillRepairReviewRejected)
    assert error.value.review.requirements[0].status == "changed"
    assert repaired_content is not None
    assert await builds(cache).get_revision() is None
    revisions = [payload["method_revision"] for stage, payload in gateway.calls
                 if stage == "workflow" and "method_revision" in payload]
    assert revisions == []
    assert attempts == 1
