"""Host source units, rather than generated prose, define frozen requirements."""
import copy

import pytest

from app.domain.services.skills.method_contract import MethodContract, validate_contract
from app.domain.services.skills.skill_creation.requirement_form import requirement_form_adapter
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_session_skill_creator import requirement_wire_response
from test_skill_creation_gateway import make_gateway, requests


SOURCES = [
    {"id": "S004", "kind": "request", "text": "名称可以包含两端空白，只有缺失名称才拒绝输入。"},
    {"id": "S019", "kind": "method", "text": "保持输入字段顺序，并保留命名空间。"},
]


def form():
    payload = {"sources": copy.deepcopy(SOURCES), "successful_tool_capabilities": []}
    adapter = requirement_form_adapter(MethodContract, "method_requirements", payload)
    assert adapter is not None
    document = {"requirements": [{"kind": "method", "unit_ids": [
        item["id"] for item in adapter.source_units]}], "excluded_sources": []}
    return payload, adapter, document


@pytest.mark.asyncio
async def test_gateway_freezes_host_source_wording_and_host_requirement_identifiers():
    payload, adapter, wire = form()
    gateway, calls, usage = make_gateway([wire])
    result = await gateway.generate(MethodContract, "method_requirements", "Select original method units.",
        payload, lambda value: validate_contract(value, SOURCES))
    observed = requests(calls)
    assert len(observed) == usage.record_from_message.await_count == 1
    assert observed[0]["source_units"] == adapter.source_units
    assert observed[0]["schema"] == adapter.request_schema()
    assert result.model_dump() == adapter.document(wire)
    assert result.requirements[0].id == "R001"
    assert result.requirements[0].source_ids == ["S004", "S019"]
    assert result.requirements[0].statement == "\n".join(item["text"] for item in adapter.source_units)
    assert "strip" not in result.requirements[0].statement
    assert payload == {"sources": SOURCES, "successful_tool_capabilities": []}


@pytest.mark.asyncio
async def test_new_protocol_cannot_freeze_model_authored_statement_even_with_valid_units():
    payload, _, wire = form()
    bad = copy.deepcopy(wire)
    bad["requirements"][0]["statement"] = "调用 strip 删除名称两端空白后判断是否为空。"
    gateway, calls, _ = make_gateway([bad, bad, wire])
    with pytest.raises(SkillCreationError) as error:
        await gateway.generate(MethodContract, "method_requirements", "Select original method units.",
            payload, lambda value: validate_contract(value, SOURCES))
    assert error.value.code == "invalid_requirement_evidence"
    assert len(calls) == 2
    assert not any(event.status == "passed" for event in gateway.attempts)


@pytest.mark.asyncio
async def test_unknown_unit_receives_bounded_wire_repair_without_inventing_a_requirement():
    payload, adapter, wire = form()
    bad = copy.deepcopy(wire)
    bad["requirements"][0]["unit_ids"].append("U999")
    gateway, calls, _ = make_gateway([bad, wire])
    result = await gateway.generate(MethodContract, "method_requirements", "Select original method units.",
        payload, lambda value: validate_contract(value, SOURCES))
    assert result.model_dump() == adapter.document(wire)
    observed = requests(calls)
    assert len(observed) == 2
    assert all(item["source_units"] == adapter.source_units for item in observed)
    assert all(item["schema"] == adapter.request_schema() for item in observed)
    assert observed[1]["repair_feedback"]["reason"] == "invalid_requirement_evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["source_coverage", "method_coverage"])
async def test_domain_repair_never_presents_compiled_contract_as_authored_unit_form(problem):
    sources = ([{"id": "S004", "kind": "method", "text": "使用 HC3 估计协方差。使用 Holm 校正多重比较。"}]
               if problem == "method_coverage" else copy.deepcopy(SOURCES))
    payload = {"sources": sources, "successful_tool_capabilities": []}
    adapter = requirement_form_adapter(MethodContract, "method_requirements", payload)
    assert len(adapter.source_units) >= 2
    bad = {"requirements": [{"kind": "method", "unit_ids": [adapter.source_units[0]["id"]]}],
           "excluded_sources": []}
    complete = {"requirements": [{"kind": "method", "unit_ids": [
        item["id"] for item in adapter.source_units]}], "excluded_sources": []}
    gateway, calls, _ = make_gateway([bad, complete])
    result = await gateway.generate(MethodContract, "method_requirements", "Select original method units.",
        payload, lambda value: validate_contract(value, sources))
    assert result.model_dump() == adapter.document(complete)
    observed = requests(calls)
    assert len(observed) == 2
    assert observed[1]["repair_feedback"]["reason"] == (
        "method_identifiers_lost" if problem == "method_coverage" else "missing_source_coverage")
    assert observed[1]["schema"] == adapter.request_schema()
    assert observed[1]["source_units"] == adapter.source_units
    assert "source_units" in observed[1]["repair_feedback"]["hint"]
    assert "unit_ids" in observed[1]["repair_feedback"]["hint"]
    # A retry may retain the validated original wire form or simply ask for a
    # new selection. The compiled domain contract is not a valid wire example.
    previous = observed[1].get("previous_candidate")
    assert previous is None or previous == bad
    if previous is not None:
        assert all(set(row) == {"kind", "unit_ids"} for row in previous["requirements"])


@pytest.mark.asyncio
async def test_no_host_sources_keeps_the_legacy_canonical_contract_protocol():
    canonical = {"requirements": [{"id": "R017", "kind": "method",
        "statement": "保持输入字段顺序，并保留命名空间。", "source_ids": ["S019"]}], "excluded_sources": []}
    gateway, calls, _ = make_gateway([canonical])
    result = await gateway.generate(MethodContract, "method_requirements", "Retain legacy fixtures.", {})
    assert result.model_dump() == canonical
    assert "source_units" not in requests(calls)[0]


def test_fixture_encoder_uses_only_matching_source_units_and_keeps_exclusions():
    _, adapter, _ = form()
    canonical = {"requirements": [{"id": "R001", "kind": "constraint",
        "statement": "Fixture prose is not used to compile the new contract.", "source_ids": ["S019"]}],
        "excluded_sources": [{"source_id": "S004", "reason": "duplicate",
            "explanation": "This deliberate fixture exclusion must remain available to semantic tests."}]}
    original = copy.deepcopy(canonical)
    request = {"stage": "method_requirements", "source_units": adapter.source_units,
               "schema": adapter.request_schema()}
    encoded = requirement_wire_response(request, canonical)
    assert encoded == {"requirements": [{"kind": "constraint", "unit_ids": [
        unit["id"] for unit in adapter.source_units if unit["source_id"] == "S019"]}],
        "excluded_sources": canonical["excluded_sources"]}
    assert canonical == original
    assert requirement_wire_response({"stage": "method_requirements"}, canonical) is canonical
    assert requirement_wire_response({**request, "stage": "workflow"}, canonical) is canonical


@pytest.mark.parametrize("refs", [["S019", "S019"], ["S999"]])
def test_fixture_encoder_refuses_to_repair_duplicate_or_unknown_source_ids(refs):
    _, adapter, _ = form()
    canonical = {"requirements": [{"id": "R001", "kind": "method",
        "statement": "Preserve fixture semantics.", "source_ids": refs}], "excluded_sources": []}
    with pytest.raises(AssertionError):
        requirement_wire_response({"stage": "method_requirements", "source_units": adapter.source_units,
            "schema": adapter.request_schema()}, canonical)
