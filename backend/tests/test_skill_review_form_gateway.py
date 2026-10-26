"""Host-owned review targets separate protocol mistakes from scientific votes."""
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import (
    CoverageError, MethodContract, MethodReview, validate_review,
)
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_creation_gateway import make_gateway, requests
from test_skill_review_schema_repair import repair_spec, repairs


def scope():
    sources = [
        {"id": "S004", "kind": "user_request", "text": "Count all accepted records once and reject invalid input."},
        {"id": "S019", "kind": "completed_step", "text": "Export the complete count summary as a JSON document."},
    ]
    contract = MethodContract(requirements=[
        {"id": "R003", "kind": "method", "statement": sources[0]["text"], "source_ids": ["S004"]},
        {"id": "R017", "kind": "output", "statement": sources[1]["text"], "source_ids": ["S019"]},
    ])
    payload = {"method_contract": contract.model_dump(), "sources": sources}

    def check(value):
        try:
            validate_review(value, contract, sources)
        except CoverageError as error:
            if error.code != "method_review_rejected":
                raise

    return contract, payload, check


def as_form(document):
    """Test-only wire encoder; production responses never gain a legacy bypass."""
    return {
        "sources": {item["source_id"]: {key: value for key, value in item.items() if key != "source_id"}
                    for item in document["sources"]},
        "requirements": {item["requirement_id"]: {key: value for key, value in item.items() if key != "requirement_id"}
                         for item in document["requirements"]},
        "implementation": {"status": "accepted" if document["implementation_appropriate"] else "rejected",
                           **({"explanation": document["implementation_reason"]}
                              if "implementation_reason" in document else {})},
    }


def canonical():
    return {
        "sources": [{"source_id": identifier, "status": "preserved", "explanation": "The complete source obligation is implemented."}
                    for identifier in ("S004", "S019")],
        "requirements": [{"requirement_id": identifier, "status": "preserved", "explanation": "The declared method is faithfully implemented."}
                         for identifier in ("R003", "R017")],
        "implementation_appropriate": True,
        "implementation_reason": "The implementation choice supports the declared reusable workflow.",
    }


def schema_node(root, node):
    while "$ref" in node:
        node = root["$defs"][node["$ref"].split("/")[-1]]
    return node


async def generate(gateway, *, stage="method_review"):
    contract, payload, check = scope()
    return await gateway.generate(MethodReview, stage, "Review exactly the host targets.", payload, check, contract=contract)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["method_review", "synthetic_reference_review"])
async def test_actual_scope_is_advertised_as_fixed_keys_and_positive_is_projected(stage):
    expected = canonical()
    form = as_form(expected)
    # Response member order does not create host target IDs or array positions.
    form["requirements"] = dict(reversed(list(form["requirements"].items())))
    gateway, calls, _ = make_gateway([form])
    result = await generate(gateway, stage=stage)
    assert result.model_dump() == expected
    schema = requests(calls)[0]["schema"]
    assert set(schema["required"]) == {"sources", "requirements", "implementation"}
    assert schema["additionalProperties"] is False
    for name, identifiers in (("sources", {"S004", "S019"}), ("requirements", {"R003", "R017"})):
        node = schema_node(schema, schema["properties"][name])
        assert set(node["properties"]) == set(node["required"]) == identifiers
        assert node["additionalProperties"] is False
        for target in node["properties"].values():
            target = schema_node(schema, target)
            assert set(target["properties"]) == set(target["required"]) == {"status", "explanation"}
            assert target["additionalProperties"] is False
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["requirement", "source", "implementation"])
async def test_valid_scoped_negative_returns_original_typed_review_for_revision(target):
    expected = canonical()
    if target == "requirement":
        expected["requirements"][0]["status"] = "missing"
    elif target == "source":
        expected["sources"][1]["status"] = "changed"
    else:
        expected["implementation_appropriate"] = False
    gateway, calls, _ = make_gateway([as_form(expected), as_form(canonical())])
    result = await generate(gateway)
    assert result.model_dump() == expected and len(calls) == 1
    contract, payload, _ = scope()
    with pytest.raises(CoverageError, match="method_review_rejected"):
        validate_review(result, contract, payload["sources"])


@pytest.mark.asyncio
async def test_complete_form_negative_roundtrips_as_canonical_checkpoint_without_a_new_vote():
    expected = canonical()
    expected["requirements"][0]["status"] = "changed"
    expected["implementation_appropriate"] = False
    gateway, calls, _ = make_gateway([as_form(expected), as_form(canonical())])
    contract, payload, check = scope()

    async def produce_review():
        return await generate(gateway)

    produce = AsyncMock(side_effect=produce_review)
    cache = MemoryCache()
    for _ in range(2):
        result = await builds(cache).run(MethodReview, "method_review", "Same scoped review.", payload,
            check, produce, decision_identity={"contract": contract.model_dump(), "candidate": "same"})
        assert result.model_dump() == expected
    assert len(calls) == produce.await_count == 1


def add_unknown_negative(form, kind):
    if kind == "requirement":
        form["requirements"]["R022"] = {"status": "rejected", "explanation": "This unknown target is not an obligation."}
    elif kind == "source":
        form["sources"]["S999"] = {"status": "missing", "explanation": "This unknown source is not part of the request."}
    else:
        form["implementation_appropriate"] = False
    return form


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["requirement", "source", "legacy_global_false"])
async def test_unknown_target_negative_is_protocol_error_then_bounded_scope_repair(kind):
    wrong = add_unknown_negative(as_form(canonical()), kind)
    gateway, calls, _ = make_gateway([wrong, as_form(canonical())])
    result = await generate(gateway)
    assert result.model_dump() == canonical() and len(calls) == 2
    assert requests(calls)[1]["repair_feedback"]["reason"] == "invalid_review_scope"
    assert gateway.attempts[1].status == "repairing"


@pytest.mark.asyncio
async def test_repeated_unknown_target_cannot_loop_or_be_promoted_to_scientific_rejection():
    wrong = add_unknown_negative(as_form(canonical()), "requirement")
    gateway, calls, _ = make_gateway([wrong, wrong, as_form(canonical())])
    with pytest.raises(SkillCreationError) as failure:
        await generate(gateway)
    assert failure.value.code == "invalid_review_scope" and len(calls) == 2
    assert not any(event.status == "passed" for event in gateway.attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize("section,identifier", [("sources", "S004"), ("requirements", "R017")])
@pytest.mark.parametrize("problem", ["missing", "unknown", "wrong_type"])
async def test_non_adverse_invalid_status_repair_keeps_the_wire_form_and_its_schema(section, identifier, problem):
    wrong = as_form(canonical())
    if problem == "missing":
        wrong[section][identifier].pop("status")
    else:
        wrong[section][identifier]["status"] = "not-a-judgment" if problem == "unknown" else 7
    gateway, calls, _ = make_gateway([wrong, as_form(canonical())])
    result = await generate(gateway)
    assert result.model_dump() == canonical() and len(calls) == 2
    first, second = requests(calls)
    assert second["repair_feedback"]["reason"] == "invalid_review_scope"
    assert second["schema"] == first["schema"]
    assert second["schema"]["title"] == "HostBoundMethodReview"
    assert second["previous_candidate"] == wrong


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["missing", "changed", "unsupported", "rejected", "uncertain"])
async def test_known_target_negative_with_unknown_neighbor_cannot_be_rewritten_or_retried(status):
    form = add_unknown_negative(as_form(canonical()), "requirement")
    form["requirements"]["R003"]["status"] = status
    gateway, calls, _ = make_gateway([form, as_form(canonical())])
    contract, payload, check = scope()
    async def produce_review():
        return await generate(gateway)

    produce = AsyncMock(side_effect=produce_review)
    cache = MemoryCache()
    for _ in range(2):
        with pytest.raises(SkillCreationError) as failure:
            await builds(cache).run(MethodReview, "method_review", "Same scoped review.", payload,
                check, produce, decision_identity={"contract": contract.model_dump(), "candidate": "same"})
        assert failure.value.code == "method_review_rejected"
    assert len(calls) == produce.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_tail", "duplicate_status", "duplicate_target", "bad_fence", "truncated", "multiple_documents"])
async def test_structural_damage_cannot_erase_an_already_observed_real_target_negative(damage):
    form = as_form(canonical())
    form["requirements"]["R003"]["status"] = "missing"
    answer, metadata = json.dumps(form), {}
    if damage == "missing_tail":
        answer = answer[:-1]
    elif damage == "duplicate_status":
        answer = answer.replace('"status": "missing"', '"status": "missing", "status": "preserved"', 1)
    elif damage == "duplicate_target":
        answer = answer.replace('"R003": {', '"R003": {"status":"missing","explanation":"The original method is missing."}, "R003": {', 1)
    elif damage == "bad_fence":
        answer = "```json\n" + answer
    elif damage == "truncated":
        metadata = {"finish_reason": "length"}
    else:
        answer += "\n{}"
    gateway, calls, _ = make_gateway([AIMessage(content=answer, response_metadata=metadata), as_form(canonical())])
    with pytest.raises(SkillCreationError) as failure:
        await generate(gateway)
    assert failure.value.code == "method_review_rejected" and len(calls) == 1


@pytest.mark.asyncio
async def test_complete_fence_and_explanation_quoting_other_judgments_do_not_change_votes():
    expected = canonical()
    expected["requirements"][0]["explanation"] = 'Example only: {"requirements":{"R003":{"status":"missing"}}}; the actual implementation is preserved.'
    expected["implementation_reason"] = 'The quoted legacy example "implementation_appropriate":false is not this review verdict.'
    answer = "```json\n" + json.dumps(as_form(expected)) + "\n```"
    gateway, calls, _ = make_gateway([AIMessage(content=answer)])
    assert (await generate(gateway)).model_dump() == expected
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_canonical_explanation_patch_preserves_all_false_statuses_and_host_ids():
    expected = canonical()
    expected["requirements"][0].update(status="missing", explanation="")
    expected["implementation_appropriate"] = False
    expected.pop("implementation_reason")
    spec = repair_spec(expected, MethodReview)
    patch = repairs(spec, explanation="The original source requirement is not implemented by this candidate.")
    gateway, calls, _ = make_gateway([as_form(expected), patch])
    result = await generate(gateway)
    repaired = deepcopy(expected)
    repaired["requirements"][0]["explanation"] = patch["repairs"][0]["value"]
    repaired["implementation_reason"] = patch["repairs"][0]["value"]
    assert result.model_dump() == repaired and len(calls) == 2
    request = requests(calls)[1]
    assert request["schema"]["title"] == "SchemaFieldRepairs"
    assert request["previous_candidate"] == expected
    assert request["repair_feedback"]["document_schema"] == MethodReview.model_json_schema()
    assert {tuple(item["location"]) for item in request["repair_feedback"]["field_repairs"]} == {
        ("requirements", 0, "explanation"), ("implementation_reason",)}


@pytest.mark.asyncio
async def test_explanation_repair_cannot_replace_scoped_negative_with_positive_form():
    original = canonical()
    original["implementation_appropriate"] = False
    original.pop("implementation_reason")
    gateway, calls, _ = make_gateway([as_form(original), as_form(canonical()), as_form(canonical())])
    with pytest.raises(SkillCreationError) as failure:
        await generate(gateway)
    assert failure.value.code == "method_review_rejected" and len(calls) == 3


@pytest.mark.asyncio
async def test_old_unscoped_false_remains_sticky_while_wrong_protocol_is_never_directly_accepted():
    old = canonical()
    old["implementation_appropriate"] = False
    old["sources"] = None
    gateway, calls, _ = make_gateway([old, canonical()])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(MethodReview, "method_review", "Legacy review.", {})
    assert failure.value.code == "method_review_rejected" and len(calls) == 1

    # A new form never interprets an old array reply as a valid positive form.
    for appropriate in (True, False):
        wrong_protocol = canonical()
        wrong_protocol["implementation_appropriate"] = appropriate
        gateway, calls, _ = make_gateway([wrong_protocol, wrong_protocol, as_form(canonical())])
        with pytest.raises(SkillCreationError) as failure:
            await generate(gateway)
        assert failure.value.code == "invalid_review_scope" and len(calls) == 2
