"""A scientific rejection belongs to its exact reviewed targets, not a sibling part."""
import copy

import pytest

from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_validation import JSONObjectKeysAssertion
from app.domain.services.skills.skill_validation_review import (
    ValidationPlanReview, _validate_review_targets, review_targets,
)
from skill_review_fixtures import review_for_payload
from test_skill_creation_checkpoint_integration import Gateway, pipeline, verifier
from test_skill_creation_checkpoints import MemoryCache
from test_skill_validation_plan import method_contract
from test_skill_validation_review import accepted_fixture_review, typed


@pytest.mark.asyncio
async def test_negative_part_is_sticky_without_poisoning_other_targets():
    plan = typed()
    complete = accepted_fixture_review(plan)
    script = complete["scripts"][0]
    case = script["cases"][0]
    first, second = [item["target_id"] for item in case["assertions"][:2]]

    def reply(stage, payload):
        result = copy.deepcopy(complete)
        result["scripts"] = [result["scripts"][0]]
        result["scripts"][0]["cases"] = [result["scripts"][0]["cases"][0]]
        selected = result["scripts"][0]["cases"][0]
        identifier = payload["review_scope"]["target_ids"][0]
        selected["assertions"] = [item for item in selected["assertions"] if item["target_id"] == identifier]
        if identifier == first:
            selected["assertions"][0]["correctness"].update(
                status="rejected", explanation="Independent calculation disagrees with this target.")
        return result

    gateway, cache = Gateway(reply), MemoryCache()

    async def review(identifier):
        build = pipeline(cache, gateway, verifier())
        build.contract = method_contract()
        payload = {"validation_plan": plan.model_dump(mode="json"),
                   "review_scope": {"script_path": script["script_path"],
                                    "case_ids": [case["case_id"]], "target_ids": [identifier]}}
        def check(value):
            actual = [item.target_id for item in value.scripts[0].cases[0].assertions]
            if actual != [identifier]:
                raise SkillPackageValidationError("validation_oracle_review_incomplete")
        return await build.generate(ValidationPlanReview, "synthetic_plan_review", "Review selected target.", payload, check)

    assert (await review(first)).verdict() == "rejected"
    assert (await review(second)).verdict() == "accepted"
    assert (await review(first)).verdict() == "rejected"
    assert (await review(second)).verdict() == "accepted"
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_json_object_key_order_cannot_repoll_an_existing_negative_decision(scoped):
    original = typed()
    original.scripts[0].cases[0].assertions.append(JSONObjectKeysAssertion(
        type="json_object_keys", path="status.json", pointer="", keys=["status", "summary"]))
    reordered = original.model_copy(deep=True)
    reordered.scripts[0].cases[0].assertions[-1].keys.reverse()
    path, case_id = original.scripts[0].script_path, original.scripts[0].cases[0].id
    target_id = review_targets(original)[0]["cases"][0]["targets"][-1]["target_id"]

    def reply(stage, payload):
        document = review_for_payload(accepted_fixture_review(payload["validation_plan"]), payload)
        case = next(item for item in document["scripts"][0]["cases"] if item["case_id"] == case_id)
        assessment = next(item for item in case["assertions"] if item["target_id"] == target_id)
        assessment["correctness"].update(
            status="rejected", explanation="The declared interface does not require this extra object key.")
        return document

    gateway, cache = Gateway(reply), MemoryCache()

    async def review(plan):
        build = pipeline(cache, gateway, verifier())
        build.contract = method_contract()
        targets = review_targets(plan)
        payload = {"validation_plan": plan.model_dump(mode="json")}
        if scoped:
            targets = [{"script_path": path, "cases": [{"case_id": case_id,
                "targets": [targets[0]["cases"][0]["targets"][-1]]}]}]
            payload["review_scope"] = {"script_path": path, "case_ids": [case_id],
                                       "target_ids": [target_id]}
        payload["review_targets"] = targets
        return await build.generate(ValidationPlanReview, "synthetic_plan_review", "Review the exact object keys.",
            payload, lambda value: _validate_review_targets(value, targets))

    first = await review(original)
    second = await review(reordered)
    assert first.verdict() == second.verdict() == "rejected"
    assert second.model_dump(mode="json") == first.model_dump(mode="json")
    assert len(gateway.calls) == 1
    assert len([key for key in cache.entries if ":adverse:" in key]) == 1
    assert original.scripts[0].cases[0].assertions[-1].keys == ["status", "summary"]
    assert reordered.scripts[0].cases[0].assertions[-1].keys == ["summary", "status"]
