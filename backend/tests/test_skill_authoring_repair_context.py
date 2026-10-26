"""A broken repair response cannot erase the last safely reviewable draft."""
import copy
import json

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.skill_creation.gateway import RepairRequired
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.utils.model_response import ModelEmptyResponseError, ModelIncompleteResponseError
from test_skill_creation_gateway import (
    PRIVATE, VALID, SmallRecipe, check_privacy, make_gateway, requests,
)
from test_skill_creation_source_response import SOURCE, REQUEST
from test_skill_review_schema_repair import review_case, repair_spec, repairs
from test_skill_workflow_output_repair_gateway import multistage_draft


def transport_failure(kind, document):
    if kind == "extra_closing_brace":
        return AIMessage(content=json.dumps(document) + "}"), "invalid_json"
    if kind == "empty_message":
        return AIMessage(content=""), "incomplete_response"
    if kind == "truncated_message":
        return AIMessage(content='{"unfinished":', response_metadata={"finish_reason": "length"}), "output_truncated"
    if kind == "driver_empty":
        return ModelEmptyResponseError(), "incomplete_response"
    return ModelIncompleteResponseError("max_tokens"), "output_truncated"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["extra_closing_brace", "empty_message", "truncated_message", "driver_empty", "driver_truncated"])
async def test_workflow_transport_error_retains_redacted_authoring_form_and_unrechecked_binding(kind):
    original, privacy = multistage_draft()
    # A protected default cannot authorize leaf patches. This case exercises
    # retained full-authoring context; exact workflow patches are tested apart.
    original["parameters"][0]["default"] = "old_result.csv"
    before = copy.deepcopy(original)
    repaired = json.loads(json.dumps(original).replace("old_result.csv", "output_001.csv"))
    malformed, reason = transport_failure(kind, original)
    gateway, calls, _ = make_gateway([original, malformed, repaired], privacy=privacy)
    result = await gateway.generate(WorkflowDraft, "workflow", "Preserve all methods.",
                                   {"original_requirements": ["Never change the method."]}, check_privacy(privacy))
    observed = requests(calls)
    previous, current = observed[1], observed[2]
    assert result.model_dump(mode="json") == repaired and original == before
    assert current["repair_feedback"]["reason"] == reason
    assert current["repair_feedback"]["prior_candidate_status"] == "not_revalidated"
    assert current["repair_feedback"]["prior_unrechecked_feedback"] == previous["repair_feedback"]
    assert current["previous_candidate"] == previous["previous_candidate"]
    assert "portable_output_bindings" in current["repair_feedback"]["prior_unrechecked_feedback"]["details"]
    assert set(current["previous_candidate"]) == set(WorkflowDraft.model_fields)
    assert "coverage" not in current["previous_candidate"] and "scripts" not in current["previous_candidate"]
    assert "old_result.csv" not in json.dumps(observed)
    assert all(row["original_requirements"] == ["Never change the method."] for row in observed)
    assert "尚未重新核验" in calls[2][0].content
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_distinct_transport_failures_reuse_one_context_without_nested_history():
    private = {**VALID, "description": "Description owned by " + PRIVATE}
    gateway, calls, _ = make_gateway([private, AIMessage(content="not JSON"), ModelEmptyResponseError(), VALID])
    await gateway.generate(SmallRecipe, "workflow", "Keep method.", {}, check_privacy(gateway.privacy))
    rows = requests(calls)
    assert len(rows) == 4
    for row in rows[2:]:
        assert row["previous_candidate"] == rows[1]["previous_candidate"]
        assert row["repair_feedback"]["prior_unrechecked_feedback"] == rows[1]["repair_feedback"]
        assert "prior_unrechecked_feedback" not in row["repair_feedback"]["prior_unrechecked_feedback"]


@pytest.mark.asyncio
async def test_repeated_transport_error_keeps_original_retry_bound():
    private = {**VALID, "description": "Description owned by " + PRIVATE}
    gateway, calls, _ = make_gateway([private, AIMessage(content="not JSON"), AIMessage(content="not JSON"), VALID])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SmallRecipe, "workflow", "Keep method.", {}, check_privacy(gateway.privacy))
    assert failure.value.code == "invalid_json" and len(calls) == 3
    assert [event.reason for event in gateway.attempts if event.status == "repairing"] == [
        "private_package_content", "invalid_json", "invalid_json"]


@pytest.mark.asyncio
async def test_new_parseable_schema_failure_clears_old_domain_diagnostics():
    private = {**VALID, "description": "Description owned by " + PRIVATE}
    gateway, calls, _ = make_gateway([private, {"name": "valid-name"}, VALID])
    await gateway.generate(SmallRecipe, "workflow", "Keep method.", {}, check_privacy(gateway.privacy))
    current = requests(calls)[2]
    assert current["repair_feedback"]["reason"] == "invalid_schema"
    assert "prior_unrechecked_feedback" not in current["repair_feedback"]
    assert "previous_candidate" not in current


@pytest.mark.asyncio
async def test_new_checked_candidate_replaces_both_prior_draft_and_diagnostic():
    private = {**VALID, "description": "Description owned by " + PRIVATE}
    second = {**VALID, "description": "Second complete candidate needs a separate domain correction."}
    gateway, calls, _ = make_gateway([private, second, AIMessage(content="not JSON"), VALID])
    def check(value):
        check_privacy(gateway.privacy)(value)
        if value.description == second["description"]:
            raise SkillPackageValidationError("duplicate_skill_parameters")
    await gateway.generate(SmallRecipe, "workflow", "Keep method.", {}, check)
    current = requests(calls)[3]
    assert current["previous_candidate"] == second
    assert current["repair_feedback"]["prior_unrechecked_feedback"]["reason"] == "duplicate_skill_parameters"
    assert "privacy_issues" not in json.dumps(current["repair_feedback"])


@pytest.mark.asyncio
async def test_host_error_named_invalid_json_does_not_impersonate_unparsed_transport():
    private = {**VALID, "description": "Description owned by " + PRIVATE}
    second = {**VALID, "description": "Parsed candidate with a host-defined schema issue."}
    gateway, calls, _ = make_gateway([private, second, VALID])
    def check(value):
        check_privacy(gateway.privacy)(value)
        if value.description == second["description"]:
            raise RepairRequired("invalid_json")
    await gateway.generate(SmallRecipe, "workflow", "Keep method.", {}, check)
    current = requests(calls)[2]
    assert current["repair_feedback"] == {"reason": "invalid_json", "fields": []}
    assert "previous_candidate" not in current


@pytest.mark.asyncio
async def test_raw_source_empty_response_keeps_host_metadata_and_redacted_complete_source():
    private_source = SOURCE + "\n# " + PRIVATE
    gateway, calls, _ = make_gateway([AIMessage(content=private_source), ModelEmptyResponseError(),
        {"repairs": [{"block_id": "B001", "replacement": ""}]}])
    result = await gateway.generate(HelperScript, "implementation", "Preserve method.",
                                   {"script_request": REQUEST}, check_privacy(gateway.privacy))
    previous, current = requests(calls)[1:]
    assert result.content == SOURCE + "\n"  # Only the selected last comment is removed.
    assert current["previous_candidate"] == previous["previous_candidate"]
    assert current["schema"] == previous["schema"]
    assert current["schema"]["title"] == "SourceBlockRepairs" and "response_format" not in current
    assert PRIVATE not in json.dumps(requests(calls))
    assert current["repair_feedback"]["source_repair_context"] == previous["repair_feedback"]["source_repair_context"]
    assert current["repair_feedback"]["source_repair_context"]["feedback"]["reason"] == "private_package_content"


@pytest.mark.asyncio
async def test_repair_context_never_crosses_generate_calls():
    private = {**VALID, "description": "Description owned by " + PRIVATE}
    gateway, calls, _ = make_gateway([private, VALID, AIMessage(content="not JSON"), VALID])
    for _ in range(2):
        await gateway.generate(SmallRecipe, "workflow", "Keep method.", {}, check_privacy(gateway.privacy))
    current = requests(calls)[3]
    assert "previous_candidate" not in current
    assert "prior_unrechecked_feedback" not in current["repair_feedback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["method", "oracle"])
async def test_frozen_explanation_repair_remains_separate_and_retains_negative(kind):
    candidate, schema, stage, check, _ = review_case(kind, text="")
    patch = repairs(repair_spec(candidate, schema))
    gateway, calls, _ = make_gateway([candidate, AIMessage(content="not JSON"), patch])
    result = await gateway.generate(schema, stage, "Review independently.", {}, check)
    current = requests(calls)[2]
    assert current["schema"]["title"] == "SchemaFieldRepairs"
    assert current["previous_candidate"] == candidate
    assert "prior_unrechecked_feedback" not in current["repair_feedback"]
    assert result.requirements[0].status == "missing" if kind == "method" else result.verdict() == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["method", "oracle"])
async def test_malformed_negative_review_remains_terminal_without_authoring_repair(kind):
    candidate, schema, stage, _, code = review_case(kind, text="The source contains a concrete method contradiction.")
    gateway, calls, _ = make_gateway([AIMessage(content=json.dumps(candidate)[:-1]), candidate])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(schema, stage, "Review independently.", {})
    assert failure.value.code == code and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [
    "{", "[", '{"unfinished":', '[{"unfinished":', '{"unfinished":[1,2',
    json.dumps(VALID)[:-1], json.dumps(VALID) + "}", json.dumps(VALID) + json.dumps(VALID),
    '{"name":"first","name":"second","description":"Duplicate keys are ambiguous."}',
    '{"name":"candidate","description":NaN}',
    '{"name":"candidate","description":Infinity}',
    '{"name":"candidate","description":1e9999}',
    "```json\n" + json.dumps(VALID)[:-1] + "\n```",
    "Explanation before document " + json.dumps(VALID),
])
async def test_only_complete_unambiguous_json_can_replace_authoring_context(partial):
    original = {**VALID, "description": "Description owned by " + PRIVATE}
    gateway, calls, _ = make_gateway([original, AIMessage(content=partial), VALID])
    result = await gateway.generate(SmallRecipe, "workflow", "Keep methods.", {}, check_privacy(gateway.privacy))
    first, current = requests(calls)[1:]
    assert result.model_dump() == VALID
    assert current["repair_feedback"]["reason"] == "invalid_json"
    assert current["previous_candidate"] == first["previous_candidate"]
    assert current["repair_feedback"]["prior_unrechecked_feedback"] == first["repair_feedback"]
    assert PRIVATE not in json.dumps(requests(calls))


@pytest.mark.asyncio
@pytest.mark.parametrize("wrap", [lambda text: text, lambda text: "```json\n" + text + "\n```",
                                   lambda text: "```\n" + text + "\n```"])
async def test_complete_json_document_and_single_fence_still_pass_every_check(wrap):
    gateway, calls, _ = make_gateway([AIMessage(content=wrap(json.dumps(VALID)))])
    checked = []
    def check(value):
        checked.append(value.model_dump())
        check_privacy(gateway.privacy)(value)
    result = await gateway.generate(SmallRecipe, "workflow", "Keep methods.", {}, check)
    assert result.model_dump() == VALID and checked == [VALID] and len(calls) == 1
