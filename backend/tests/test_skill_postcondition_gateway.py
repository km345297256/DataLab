"""One generation stage can carry distinct host-frozen typed protocols."""
import copy
import json
from unittest.mock import Mock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills import skill_validation
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan, validation_generation_schema
from app.domain.services.skills.skill_validation_review import (
    RejectionPostconditionAmendment, apply_postcondition_amendment,
)
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_creation_gateway import make_gateway, requests
from test_skill_model_completion_boundary import setup_boundary
from test_skill_postcondition_revision import amendment
from test_skill_validation_plan import method_contract, plan_payload


KEYS = {("scripts/group_summary.py", "group-invalid")}
STAGE = "synthetic_validation_plan"


@pytest.mark.asyncio
async def test_governed_gateway_sends_actual_amendment_schema_without_plan_derivation(monkeypatch):
    expected = amendment(KEYS)
    derive = Mock(side_effect=AssertionError("Full-plan source derivation cannot process an amendment"))
    monkeypatch.setattr(skill_validation, "derive_validation_sources", derive)
    _, client, gateway = setup_boundary(monkeypatch, [AIMessage(
        content=expected.model_dump_json(), response_metadata={"finish_reason": "stop"})], 65536)
    check = Mock(side_effect=lambda value: apply_postcondition_amendment(
        SyntheticValidationPlan.model_validate(plan_payload()), value, KEYS, required_cases=KEYS))
    result = await gateway.generate(RejectionPostconditionAmendment, STAGE,
        "Return only the requested rejection postcondition additions.",
        {"required_cases": [{"script_path": path, "case_id": case_id} for path, case_id in sorted(KEYS)]},
        check, contract=method_contract())
    assert result == expected and check.call_count >= 1
    derive.assert_not_called()
    wire, = client._requests
    request = json.loads(wire["messages"][1].content)
    assert request["schema"] == RejectionPostconditionAmendment.model_json_schema()
    assert set(request["schema"]["properties"]) == {"additions"}
    assert "SyntheticValidationCase" not in request["schema"].get("$defs", {})
    assert wire["max_tokens"] == 65536


@pytest.mark.asyncio
async def test_same_stage_cache_keys_keep_full_plan_and_amendment_protocols_distinct():
    document = plan_payload()
    for script in document["scripts"]:
        for case in script["cases"]:
            case.pop("source_ids")
    expected_amendment = amendment(KEYS)
    gateway, calls, _ = make_gateway([document, expected_amendment.model_dump()])
    cache = MemoryCache()
    payload, instruction = {"method_contract": method_contract().model_dump()}, "Follow the supplied typed schema."
    async def run(schema):
        return await builds(cache).run(schema, STAGE, instruction, payload, None,
            lambda: gateway.generate(schema, STAGE, instruction, payload, contract=method_contract()))
    full = await run(SyntheticValidationPlan)
    tiny = await run(RejectionPostconditionAmendment)
    # Same stage, instruction, payload and owner; only the schema separates the
    # positive artifacts. Rehydrating either type never consumes the other.
    assert await run(SyntheticValidationPlan) == full
    assert await run(RejectionPostconditionAmendment) == tiny == expected_amendment
    assert len(calls) == len(cache.entries) == 2
    sent = requests(calls)
    assert sent[0]["schema"] == validation_generation_schema()
    assert sent[1]["schema"] == RejectionPostconditionAmendment.model_json_schema()
    assert all(case.source_ids == ["S001"] for script in full.scripts for case in script.cases)


@pytest.mark.asyncio
async def test_gateway_amendment_still_enforces_required_case_domain_boundary():
    invalid = copy.deepcopy(amendment(KEYS).model_dump())
    invalid["additions"][0]["case_id"] = "group-normal"
    gateway, calls, _ = make_gateway([invalid, invalid])
    def check(value):
        apply_postcondition_amendment(SyntheticValidationPlan.model_validate(plan_payload()), value,
                                     KEYS, required_cases=KEYS)
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(RejectionPostconditionAmendment, STAGE,
            "Return only the requested postcondition additions.", {}, check, contract=method_contract())
    assert failure.value.code == "validation_oracle_revision_invalid"
    assert len(calls) == 2
    assert all(request["schema"] == RejectionPostconditionAmendment.model_json_schema() for request in requests(calls))
