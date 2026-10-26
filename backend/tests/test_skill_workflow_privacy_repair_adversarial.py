"""Independent synthetic attacks on host-owned workflow string repair scope.

These tests use no user-session artifacts, provider calls or scientific-oracle
fixtures. A successful merge remains subject to the ordinary complete gates.
"""
from copy import deepcopy
import json

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import validate_coverage
from app.domain.services.skills.skill_creation.schema_repair import (
    SchemaRepairRejected, apply_schema_repairs, build_existing_string_repair_spec,
)
from app.domain.services.skills.skill_creation.workflow_authoring import (
    WorkflowDraft, check_workflow_draft,
)
from app.domain.services.skills.skill_creation.workflow_privacy_repair import (
    apply_workflow_privacy_repairs, build_workflow_privacy_repair_spec,
    workflow_privacy_response_schema,
)
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from test_skill_creation_gateway import make_gateway, requests, check_privacy
from test_skill_workflow_authoring import contract, draft_payload, script_declaration


PRIVATE = "private-study-observations.csv"
PRIVACY = PrivacyContext((PrivateValue(PRIVATE, "source_filename"),))


def prepared(count=1):
    payload = draft_payload(max(count, 2))
    for step in payload["steps"][:count]:
        step["action"] += " Read " + PRIVATE + " as the selected input."
    draft = WorkflowDraft.model_validate(payload)
    spec = build_workflow_privacy_repair_spec(draft, PRIVACY, stage="workflow")
    assert spec is not None
    patch = {"repairs": [{"field_id": item.field_id, "action": "set",
                          "value": draft.steps[item.path[1]].action.replace(PRIVATE, "selected input")}
                         for item in spec.fields]}
    return draft, spec, patch


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def test_full_scan_selects_seventy_three_distinct_leaves_even_when_public_feedback_is_capped():
    draft, spec, patch = prepared(73)
    assert len(PRIVACY.scan_document(draft)) == 32
    assert [item.field_id for item in spec.fields] == [f"F{n:03d}" for n in range(1, 74)]
    assert [item.path for item in spec.fields] == [("steps", n, "action") for n in range(73)]
    schema = workflow_privacy_response_schema(spec)
    merged = apply_workflow_privacy_repairs(draft, spec, schema.model_validate(patch))
    expected = draft.model_dump(mode="json")
    for step in expected["steps"]:
        step["action"] = step["action"].replace(PRIVATE, "selected input")
    assert canonical(merged) == canonical(expected)
    assert not PRIVACY.scan_document(merged, limit=None)
    assert PRIVATE not in canonical(spec.public_fields())


def test_uneditable_match_after_sixty_seven_prose_leaves_cancels_the_entire_patch_authority():
    draft, _, _ = prepared(67)
    payload = draft.model_dump(mode="json")
    script = script_declaration()
    payload["implementation"] = {"kind": "parameterized_scripts", "reason": "Use the declared calculation script.",
                                 "scripts": [script]}
    privacy = PrivacyContext((*PRIVACY.entries, PrivateValue(script["path"], "identity")))
    draft = WorkflowDraft.model_validate(payload)
    assert len(privacy.scan_document(draft)) == 32
    assert any(issue.location == ("implementation", "scripts", 0, "path")
               for issue in privacy.scan_document(draft, limit=None))
    assert build_workflow_privacy_repair_spec(draft, privacy, stage="workflow") is None


@pytest.mark.parametrize("mutation", [
    lambda value: value["parameters"][0].update(required=1),
    lambda value: value["parameters"][0].update(default=""),
    lambda value: value["steps"].reverse(),
    lambda value: value["steps"][-1]["requirement_ids"].append("R002"),
    lambda value: value["outputs"][0].update(format="JSON"),
    lambda value: value["validation_cases"][0].update(expected="A different numerical expectation."),
])
def test_any_unselected_value_type_order_or_semantics_change_breaks_the_original_binding(mutation):
    draft, spec, patch = prepared(2)
    changed = draft.model_dump(mode="json")
    mutation(changed)
    with pytest.raises(SchemaRepairRejected) as error:
        apply_workflow_privacy_repairs(changed, spec, patch)
    assert error.value.reason == "candidate_changed"


@pytest.mark.parametrize("mutate", [
    lambda patch: patch["repairs"][0].update(value={"replacement": "selected input"}),
    lambda patch: patch["repairs"][0].update(value=["selected input"]),
    lambda patch: patch["repairs"][0].update(value=float("nan")),
    lambda patch: patch["repairs"][0].update(value=True),
    lambda patch: patch["repairs"][0].update(action="remove"),
    lambda patch: patch["repairs"][0].update(path=["steps"]),
    lambda patch: patch["repairs"][0].update(field_id="steps.0.action"),
    lambda patch: patch.update(steps=[]),
    lambda patch: patch["repairs"].append(deepcopy(patch["repairs"][0])),
    lambda patch: patch["repairs"][1].update(field_id=patch["repairs"][0]["field_id"]),
])
def test_response_cannot_choose_a_path_container_operation_or_new_edit(mutate):
    draft, spec, patch = prepared(2)
    before = canonical(draft.model_dump(mode="json"))
    mutate(patch)
    with pytest.raises(SchemaRepairRejected):
        apply_workflow_privacy_repairs(draft, spec, patch)
    assert canonical(draft.model_dump(mode="json")) == before


def test_string_engine_preserves_every_unselected_python_scalar_without_normalization():
    draft, spec, patch = prepared()
    values = {"flag": True, "integer": 1, "float": 1.0, "negative_zero": -0.0,
              "null": None, "sequence": [False, 0, 0.0, "0"]}
    original = {"selected": "private text", "frozen": values}
    direct = build_existing_string_repair_spec(original, [("selected",)], hint="Host-selected text only.")
    correction = workflow_privacy_response_schema(spec).model_validate(patch)
    result = apply_schema_repairs(original, direct, correction)
    assert canonical(result["frozen"]) == canonical(values)
    assert type(result["frozen"]["flag"]) is bool
    assert type(result["frozen"]["integer"]) is int
    assert type(result["frozen"]["float"]) is float
    for key, value in [("flag", 1), ("integer", 1.0), ("float", 1), ("negative_zero", 0.0)]:
        changed = deepcopy(original)
        changed["frozen"][key] = value
        with pytest.raises(SchemaRepairRejected) as error:
            apply_schema_repairs(changed, direct, correction)
        assert error.value.reason == "candidate_changed"


@pytest.mark.parametrize("location", [("frozen",), ("items", True), ("items", -1),
                                      ("items", 1), ("absent",), ("selected", "nested")])
def test_host_string_engine_will_not_bind_a_container_bool_index_or_nonexistent_leaf(location):
    assert build_existing_string_repair_spec(
        {"selected": "text", "frozen": {"value": "text"}, "items": ["text"]}, [location], hint="Repair") is None


def invalid_transport(kind, candidate):
    complete = json.dumps(candidate, ensure_ascii=False)
    if kind == "extra_closer":
        return AIMessage(content=complete + "}"), "invalid_json"
    if kind == "missing_root_closer":
        return AIMessage(content=complete[:-1]), "invalid_json"
    if kind == "missing_nested_closers":
        return AIMessage(content=complete[:-3]), "invalid_json"
    if kind == "missing_value":
        return AIMessage(content='{"unfinished":'), "invalid_json"
    if kind == "second_document":
        return AIMessage(content=complete + "\n" + complete), "invalid_json"
    if kind == "duplicate_root_key":
        return AIMessage(content=complete[:-1] + ', "name": "covariance-analysis"}'), "invalid_json"
    if kind == "duplicate_nested_key":
        return AIMessage(content=complete.replace('"required": true', '"required": true, "required": true', 1)), "invalid_json"
    if kind in {"NaN", "Infinity", "-Infinity", "1e309"}:
        return AIMessage(content=complete.replace('"required": true', '"required": ' + kind, 1)), "invalid_json"
    if kind == "fence_missing_root_closer":
        return AIMessage(content="```json\n" + complete[:-1] + "\n```"), "invalid_json"
    if kind == "fence_plus_commentary":
        return AIMessage(content="```json\n" + complete + "\n```\nA second unstructured answer."), "invalid_json"
    if kind == "two_fences":
        return AIMessage(content="```json\n" + complete + "\n```\n```json\n{}\n```"), "invalid_json"
    if kind == "actual_finish_length":
        # Even syntactically complete bytes are not complete provider output
        # when the actual finish reason reports truncation.
        return AIMessage(content=complete, response_metadata={"finish_reason": "length"}), "output_truncated"
    raise AssertionError("Unknown artificial transport case")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "extra_closer", "missing_root_closer", "missing_nested_closers", "missing_value",
    "second_document", "duplicate_root_key", "duplicate_nested_key", "NaN", "Infinity",
    "-Infinity", "1e309", "fence_missing_root_closer", "fence_plus_commentary", "two_fences",
    "actual_finish_length",
])
async def test_merged_candidate_and_coverage_feedback_survive_the_next_malformed_response(kind):
    original = draft_payload(2)
    original["steps"][-1]["action"] += " Read " + PRIVATE + " as selected input."
    draft = WorkflowDraft.model_validate(original)
    spec = build_workflow_privacy_repair_spec(draft, PRIVACY, stage="workflow")
    assert spec is not None
    patch = {"repairs": [{"field_id": spec.fields[0].field_id, "action": "set",
                          "value": "Compute a generic covariance for the selected input."}]}
    merged = apply_workflow_privacy_repairs(draft, spec, patch)
    final = deepcopy(merged)
    final["steps"][-1]["action"] = "Compute HC3 covariance for the selected input."
    checked = []
    method = contract()

    def check(value):
        checked.append(value.model_dump(mode="json"))
        check_privacy(PRIVACY)(value)
        check_workflow_draft(value, method, lambda recipe: validate_coverage(method, recipe.model_dump()))

    malformed, reason = invalid_transport(kind, merged)
    gateway, calls, _ = make_gateway([original, patch, malformed, final], privacy=PRIVACY)
    result = await gateway.generate(WorkflowDraft, "workflow", "Preserve the original methods.",
                                    {"method_contract": method.model_dump(mode="json")}, check)
    rows = requests(calls)
    assert len(rows) == 4 and result.model_dump(mode="json") == final
    assert rows[1]["schema"]["title"] == "WorkflowPrivacyFieldRepairs"
    assert rows[2]["schema"]["title"] == rows[3]["schema"]["title"] == "WorkflowDraft"
    assert rows[2]["previous_candidate"] == rows[3]["previous_candidate"] == merged
    assert rows[2]["repair_feedback"]["reason"] == "method_identifiers_lost"
    assert rows[3]["repair_feedback"]["reason"] == reason
    assert rows[3]["repair_feedback"]["prior_unrechecked_feedback"] == rows[2]["repair_feedback"]
    assert "privacy_issues" not in canonical(rows[3]["repair_feedback"])
    assert "field_repairs" not in canonical(rows[2:])
    assert PRIVATE not in canonical(rows)
    assert [row["method_contract"] for row in rows] == [method.model_dump(mode="json")] * 4
    assert checked == [draft.model_dump(mode="json"), merged, final]


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["json", "JSON", ""])
async def test_one_complete_fenced_document_keeps_unicode_and_goes_through_normal_validation(fence):
    candidate = draft_payload()
    candidate["description"] += " Preserve Unicode separators \u2028 and \u2029 in the complete explanation."
    expected = WorkflowDraft.model_validate(candidate).model_dump(mode="json")
    body = " \n```" + fence + "\n" + json.dumps(candidate, ensure_ascii=False) + "\n```\n "
    seen = []
    method = contract()

    def check(value):
        check_privacy(PRIVACY)(value)
        check_workflow_draft(value, method, lambda recipe: validate_coverage(method, recipe.model_dump()))
        seen.append(value.model_dump(mode="json"))

    gateway, calls, _ = make_gateway([AIMessage(content=body)], privacy=PRIVACY)
    result = await gateway.generate(WorkflowDraft, "workflow", "Keep the exact methods.", {}, check)
    assert result.model_dump(mode="json") == expected
    assert seen == [expected] and len(calls) == 1

