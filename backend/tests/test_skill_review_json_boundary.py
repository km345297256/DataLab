"""Malformed JSON cannot erase an already explicit scientific rejection."""
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_creation.gateway import _raw_review_negative_code
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_validation_review import ValidationPlanReview
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_creation_gateway import make_gateway
from test_skill_creation_pipeline import review_payload
from test_skill_model_completion_boundary import setup_boundary
from test_skill_review_schema_repair import review_case


FIXTURE = Path(__file__).parent / "fixtures" / "skill_creator_review_json_boundary"


def candidate(kind):
    document, schema, stage, check, code = review_case(
        "method" if kind != "oracle" else "oracle", text="Independent evidence identifies a concrete method error.")
    if kind == "reference":
        stage, code = "synthetic_reference_review", "reference_calculation_unconfirmed"
    return document, schema, stage, check, code


@pytest.mark.asyncio
async def test_actual_call30_malformed_negative_stops_once_and_persists_across_retry(monkeypatch):
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    raw = (FIXTURE / provenance["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == provenance["sha256"]
    answer = json.loads(raw)["content"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(answer)
    assert answer.index('"status":"rejected"') < 1000
    _, client, gateway = setup_boundary(monkeypatch, [AIMessage(content=answer)], 65536)
    async def produce():
        return await gateway.generate(ValidationPlanReview,
            "synthetic_plan_review", "Review the unchanged boundary case.", {})
    producer = AsyncMock(side_effect=produce)
    cache = MemoryCache()
    for _ in range(2):
        with pytest.raises(SkillCreationError) as failure:
            await builds(cache).run(ValidationPlanReview, "synthetic_plan_review", "Review the unchanged boundary case.",
                {}, None, producer, decision_identity="same-original-call30-plan-and-scope")
        assert failure.value.code == "validation_oracle_rejected"
    assert producer.await_count == len(client._requests) == 1
    assert not any(event.status == "passed" for event in gateway.attempts)


@pytest.mark.parametrize("kind", ["method", "reference", "oracle"])
@pytest.mark.parametrize("malformation", ["unclosed_json", "unclosed_fence", "fenced_bad_json",
    "two_documents", "two_fences", "duplicate_key", "truncated_finish"])
@pytest.mark.asyncio
async def test_explicit_known_judgments_survive_json_damage_without_whole_review_retry(kind, malformation):
    document, schema, stage, check, code = candidate(kind)
    answer = json.dumps(document)
    metadata = {}
    if malformation == "unclosed_json": answer = answer[:-1]
    elif malformation == "unclosed_fence": answer = "```json\n" + answer
    elif malformation == "fenced_bad_json": answer = "```json\n" + answer[:-1] + "\n```"
    elif malformation in {"two_documents", "two_fences"}:
        first = "{}"
        answer = first + "\n" + answer if malformation == "two_documents" else "```json\n" + first + "\n```\n```json\n" + answer + "\n```"
    elif malformation == "duplicate_key":
        bad = "missing" if kind != "oracle" else "rejected"
        good = "preserved" if kind != "oracle" else "accepted"
        answer = answer.replace('"status": "' + bad + '"', '"status": "' + bad + '", "status": "' + good + '"', 1)
    else:
        metadata = {"finish_reason": "length"}
    gateway, calls, _ = make_gateway([AIMessage(content=answer, response_metadata=metadata), document])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(schema, stage, "Review independently.", {}, check)
    assert failure.value.code == code and len(calls) == 1


@pytest.mark.parametrize("fence", [False, True])
@pytest.mark.parametrize("kind", ["method", "reference", "oracle"])
@pytest.mark.asyncio
async def test_complete_valid_negative_is_returned_unchanged_for_substantive_revision(kind, fence):
    document, schema, stage, check, _ = candidate(kind)
    answer = json.dumps(document)
    if fence: answer = "```json\n" + answer + "\n```"
    gateway, calls, _ = make_gateway([AIMessage(content=answer)])
    result = await gateway.generate(schema, stage, "Review independently.", {}, check)
    assert result.model_dump() == document and len(calls) == 1


@pytest.mark.parametrize("path", ["support", "input_validity", "exit_behavior", "method_coverage", "correctness", "tolerance"])
@pytest.mark.parametrize("status", ["rejected", "uncertain"])
def test_only_complete_scalar_judgments_at_every_known_oracle_location_are_observed(path, status):
    assessment = {"status": status, "explanation": "Independent assessment."}
    if path == "support": script = {"support": assessment}
    elif path in {"correctness", "tolerance"}: script = {"cases": [{"assertions": [{path: assessment}]}]}
    else: script = {"cases": [{path: assessment}]}
    answer = json.dumps({"scripts": [script]})[:-1]
    assert _raw_review_negative_code(answer, ValidationPlanReview, "synthetic_plan_review") == "validation_oracle_" + status


@pytest.mark.parametrize("answer", [
    'The explanation quotes "status": "rejected" but this is not a review object.',
    json.dumps({"explanation": 'Example: {"scripts":[{"support":{"status":"rejected"}}]}'}),
    json.dumps({"quoted_example": {"scripts": [{"support": {"status": "rejected"}}]}}),
    json.dumps({"scripts": [{"support": {"status": "accepted", "explanation": "Earlier review was rejected; do not repeat its verdict."}}]}),
    json.dumps({"scripts": [{"cases": [{"method_coverage": {"explanation": {"status": "rejected"}}}]}]}),
    '{"scripts":[{"support":{"status":"reject',
    '{"scripts":[{"support":{"status":"rejected"unquoted_suffix',
    '```python\n{"scripts":[{"support":{"status":"rejected"}}]}\n```',
])
def test_prose_quoted_examples_unknown_paths_and_incomplete_tokens_are_not_scientific_decisions(answer):
    assert _raw_review_negative_code(answer, ValidationPlanReview, "synthetic_plan_review") is None


def test_raw_method_false_and_uncertain_precedence_use_only_known_judgment_paths():
    assert _raw_review_negative_code('{"implementation_appropriate": false,', MethodReview,
        "synthetic_reference_review") == "reference_calculation_unconfirmed"
    answer = '{"scripts":[{"support":{"status":"rejected"},"cases":[{"input_validity":{"status":"uncertain"}}]}'
    assert _raw_review_negative_code(answer, ValidationPlanReview, "synthetic_plan_review") == "validation_oracle_uncertain"
    assert _raw_review_negative_code('{"implementation_reason":"implementation_appropriate: false"}', MethodReview,
        "method_review") is None


@pytest.mark.parametrize("script", [
    {"cases": [{"input_validity": {"method_coverage": {"status": "rejected"}}}]},
    {"support": {"cases": [{"exit_behavior": {"status": "rejected"}}]}},
    {"cases": [{"input_validity": {"exit_behavior": {"method_coverage": {"status": "rejected"}}}}]},
    {"cases": [{"method_coverage": {"assertions": [{"correctness": {"tolerance": {"status": "rejected"}}}]}}]},
])
def test_missing_brace_misnesting_between_known_judgments_retains_only_an_adverse_observation(script):
    answer = json.dumps({"scripts": [script]})[:-1]
    assert _raw_review_negative_code(answer, ValidationPlanReview, "synthetic_plan_review") == "validation_oracle_rejected"


@pytest.mark.asyncio
async def test_balanced_json_with_a_misnested_explicit_negative_cannot_be_whole_schema_rewritten():
    document, schema, stage, check, code = candidate("oracle")
    case = document["scripts"][0]["cases"][0]
    # The same missing-brace ambiguity can still leave syntactically balanced
    # JSON. Schema failure must retain the observation too, not just parsing.
    case["input_validity"]["method_coverage"] = {"status": "rejected", "explanation": "A required branch is not exercised."}
    case.pop("method_coverage")
    case["assertions"][0]["correctness"]["status"] = "accepted"
    gateway, calls, _ = make_gateway([document, document])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(schema, stage, "Review independently.", {}, check)
    assert failure.value.code == code and len(calls) == 1


@pytest.mark.parametrize("malformation", ["unclosed", "multiple", "duplicate", "fence"])
@pytest.mark.asyncio
async def test_positive_prefix_is_not_promoted_from_incomplete_or_ambiguous_review(malformation):
    good = review_payload()
    answer = json.dumps(good)
    if malformation == "unclosed": answer = answer[:-1]
    elif malformation == "multiple": answer += "\n{}"
    elif malformation == "duplicate": answer = answer.replace('"implementation_appropriate": true', '"implementation_appropriate": true, "implementation_appropriate": true')
    else: answer = "```json\n" + answer
    gateway, calls, _ = make_gateway([AIMessage(content=answer), good])
    result = await gateway.generate(MethodReview, "method_review", "Review independently.", {})
    assert result.model_dump() == good and len(calls) == 2
    assert gateway.attempts[1].reason == "invalid_json"
