"""The source-edit wire cannot widen scope or lose the newest checked draft."""
import json

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import CoverageError
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.utils.model_response import ModelEmptyResponseError, ModelIncompleteResponseError
from test_skill_creation_gateway import make_gateway, requests


REQUEST = {"path": "scripts/analyze.py", "purpose": "Analyze the currently selected input.",
           "invocation": "python scripts/analyze.py --input current --output result"}
PRIVATE = "original-output.csv"
SOURCE = ("# This prefix and suffix must remain byte-for-byte unchanged.\n"
          "def calculate(values):\n    return sum(values)\n\n"
          "OUTPUT = 'original-output.csv'\n\n"
          "def describe():\n    return 'the current reusable result'\n")
PATCH = {"repairs": [{"block_id": "B001", "replacement": "OUTPUT = 'artifact_001.csv'\n"}]}
REPAIRED = SOURCE.strip().replace("original-output.csv", "artifact_001.csv")


def privacy():
    return PrivacyContext((PrivateValue(PRIVATE, "source_filename"),),
                          generated_output_filenames=frozenset({PRIVATE}))


def check(value):
    validate_portable_document(value, privacy())


def broken_reply(kind):
    if kind == "empty":
        return AIMessage(content="")
    if kind == "driver_empty":
        return ModelEmptyResponseError()
    if kind == "length":
        return AIMessage(content=json.dumps(PATCH), response_metadata={"finish_reason": "length"})
    if kind == "driver_length":
        return ModelIncompleteResponseError("max_tokens")
    texts = {
        "partial": '{"repairs":[',
        "extra_document": json.dumps(PATCH) + "{}",
        "duplicate_field": '{"repairs":[],"repairs":' + json.dumps(PATCH["repairs"]) + "}",
        "wrong_id": json.dumps({"repairs": [{"block_id": "B099", "replacement": ""}]}),
        "extra_metadata": json.dumps({**PATCH, "path": "scripts/foreign.py"}),
        "nonstring": json.dumps({"repairs": [{"block_id": "B001", "replacement": 3}]}),
    }
    return AIMessage(content=texts[kind])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["empty", "driver_empty", "length", "driver_length", "partial",
                                   "extra_document", "duplicate_field", "wrong_id", "extra_metadata", "nonstring"])
async def test_failed_patch_retains_one_bound_original_and_diagnostic(kind, monkeypatch):
    async def forbidden_modular(*args, **kwargs):
        pytest.fail("A broken local response cannot reset the budget through modular generation")
    monkeypatch.setattr("app.domain.services.skills.skill_creation.script_authoring.generate_modular_script", forbidden_modular)
    gateway, calls, _ = make_gateway([AIMessage(content=SOURCE), broken_reply(kind), PATCH], privacy=privacy())
    result = await gateway.generate(HelperScript, "implementation", "Preserve the whole method.",
                                   {"script_request": REQUEST}, check)
    assert result.content == REPAIRED
    first, second = requests(calls)[1:]
    assert first["schema"]["title"] == second["schema"]["title"] == "SourceBlockRepairs"
    assert first["previous_candidate"] == second["previous_candidate"]
    assert first["repair_feedback"]["source_blocks"] == second["repair_feedback"]["source_blocks"]
    assert first["repair_feedback"]["source_repair_context"] == second["repair_feedback"]["source_repair_context"]
    original_feedback = second["repair_feedback"]["source_repair_context"]["feedback"]
    assert original_feedback["reason"] == "private_package_content"
    assert original_feedback["details"]["output_repair_references"][0]["role"] == "generated_output"
    assert "prior_unrechecked_feedback" not in original_feedback
    assert PRIVATE not in json.dumps(requests(calls))
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_valid_patch_followed_by_new_domain_failure_retains_new_source_after_empty_reply():
    final = REPAIRED.replace("return sum(values)", "return sum(values) + 0")
    gateway, calls, _ = make_gateway([AIMessage(content=SOURCE), PATCH, ModelEmptyResponseError(),
                                     AIMessage(content=final)], privacy=privacy())
    def validate(value):
        check(value)
        if value.content == REPAIRED:
            raise CoverageError("method_identifiers_lost", ["R001"])
    result = await gateway.generate(HelperScript, "implementation", "Preserve the whole method.",
                                   {"script_request": REQUEST}, validate)
    assert result.content == final
    current = requests(calls)[3]
    assert current["response_format"]["type"] == "python_source"
    assert "schema" not in current
    assert current["previous_candidate"]["content"] == REPAIRED
    assert "source_blocks" not in current["repair_feedback"]
    assert current["repair_feedback"]["prior_unrechecked_feedback"]["reason"] == "method_identifiers_lost"
    assert PRIVATE not in json.dumps(requests(calls))


@pytest.mark.asyncio
async def test_repeated_noop_source_patches_do_not_reset_identical_diagnostic_budget():
    noop = {"repairs": [{"block_id": "B001", "replacement": "OUTPUT = 'original-output.csv'\n"}]}
    gateway, calls, _ = make_gateway([AIMessage(content=SOURCE), noop, PATCH], privacy=privacy())
    with pytest.raises(SkillCreationError) as error:
        await gateway.generate(HelperScript, "implementation", "Preserve the whole method.",
                               {"script_request": REQUEST}, check)
    assert error.value.code == "private_package_content"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_scientific_rejection_of_merged_source_stops_without_replacing_the_review():
    gateway, calls, _ = make_gateway([AIMessage(content=SOURCE), PATCH, AIMessage(content=REPAIRED)], privacy=privacy())
    def validate(value):
        check(value)
        raise CoverageError("method_review_rejected", ["R001"])
    with pytest.raises(SkillCreationError) as error:
        await gateway.generate(HelperScript, "implementation", "Preserve the whole method.",
                               {"script_request": REQUEST}, validate)
    assert error.value.code == "method_review_rejected"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_all_fifty_three_real_matches_are_selected_despite_bounded_public_diagnostics():
    names = [f"historical-{index:03}.csv" for index in range(53)]
    source = "# Current computation parameters\n" + "".join(
        f"result_{index} = {name!r}\n# keep separator {index}\n" for index, name in enumerate(names))
    context = PrivacyContext(tuple(PrivateValue(name, "source_filename") for name in names),
                             generated_output_filenames=frozenset(names))
    patch = {"repairs": [{"block_id": f"B{index+1:03d}",
                          "replacement": f"result_{index} = 'portable-{index:03}.csv'\n"}
                         for index in range(53)]}
    expected = "# Current computation parameters\n" + "".join(
        f"result_{index} = 'portable-{index:03}.csv'\n# keep separator {index}\n" for index in range(53))
    gateway, calls, _ = make_gateway([AIMessage(content=source), patch], privacy=context)
    result = await gateway.generate(HelperScript, "implementation", "Preserve the whole method.",
        {"script_request": REQUEST}, lambda value: validate_portable_document(value, context))
    request = requests(calls)[1]
    assert len(request["repair_feedback"]["details"]["privacy_issues"]) <= 20
    assert len(request["repair_feedback"]["source_blocks"]) == 53
    assert result.content == expected.strip()
    assert not context.scan_document(result)
