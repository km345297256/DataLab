"""Repair termination follows real host observations, not movable line labels."""
from dataclasses import asdict
import json
import logging

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.repair_progress import privacy_failure_snapshot
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.utils.model_response import ModelEmptyResponseError, ModelIncompleteResponseError
from test_skill_creation_gateway import SmallRecipe, VALID, check_privacy, make_gateway, requests
from test_skill_creation_source_response import REQUEST
from test_skill_source_repair_gateway import setup
from test_skill_review_schema_repair import review_case


FIRST = "first-private-source.csv"
SECOND = "different-private-source.csv"
CONTEXT = PrivacyContext((PrivateValue(FIRST, "source_filename"), PrivateValue(SECOND, "source_filename")))
PREFIX = "print(3)  # Preserve the complete scientific computation and input contract.\n"


def replacement(text):
    return {"repairs": [{"block_id": "B001", "replacement": text}]}


async def generate_source(gateway):
    return await gateway.generate(HelperScript, "implementation", "Preserve the complete calculation.",
                                  {"script_request": REQUEST}, check_privacy(gateway.privacy))


@pytest.mark.asyncio
async def test_moving_same_private_value_to_another_source_line_stops_without_leaking_identity(caplog):
    caplog.set_level(logging.INFO)
    gateway, calls, _, _ = setup([
        AIMessage(content=PREFIX + f"# {FIRST}"),
        replacement(f"# Changed explanation and padding.\n\n# {FIRST}"),
        replacement("# Safe final explanation."),
    ], context=CONTEXT)
    with pytest.raises(SkillCreationError) as failure:
        await generate_source(gateway)
    assert failure.value.code == "private_package_content" and len(calls) == 2
    assert '"line": 2' in caplog.text and '"line": 4' in caplog.text
    assert FIRST not in caplog.text and SECOND not in caplog.text
    assert FIRST not in json.dumps(requests(calls))
    assert "privacy_snapshot" not in json.dumps(requests(calls))
    assert FIRST not in json.dumps([asdict(event) for event in gateway.attempts])


@pytest.mark.asyncio
async def test_removing_one_same_line_occurrence_is_progress_despite_identical_public_diagnostic():
    gateway, calls, _, _ = setup([
        AIMessage(content=PREFIX + f"# {FIRST} then {FIRST}"),
        replacement(f"# {FIRST}"),
        replacement("# Current parameters determine the input."),
    ], context=CONTEXT)
    result = await generate_source(gateway)
    first, second = requests(calls)[1:]
    assert len(calls) == 3 and FIRST not in result.content
    assert first["repair_feedback"]["details"]["privacy_issues"] == second["repair_feedback"]["details"]["privacy_issues"]


@pytest.mark.asyncio
async def test_different_private_values_in_same_field_and_category_each_receive_repair():
    gateway, calls, _, _ = setup([
        AIMessage(content=PREFIX + f"# {FIRST}"),
        replacement(f"# {SECOND}"),
        replacement("# No historical input binding remains."),
    ], context=CONTEXT)
    result = await generate_source(gateway)
    assert len(calls) == 3 and FIRST not in result.content and SECOND not in result.content


@pytest.mark.asyncio
async def test_same_value_in_distinct_authored_fields_is_not_one_diagnostic():
    gateway, calls, _ = make_gateway([
        {**VALID, "description": f"Historical input {FIRST}"},
        {**VALID, "name": FIRST},
        VALID,
    ], privacy=CONTEXT)
    result = await gateway.generate(SmallRecipe, "workflow", "Preserve the method.", {}, check_privacy(CONTEXT))
    assert len(calls) == 3 and result.model_dump() == VALID


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", [
    AIMessage(content="not JSON"), ModelEmptyResponseError(),
    ModelIncompleteResponseError("max_tokens"), {"repairs": []},
], ids=["invalid_json", "empty", "truncated", "invalid_patch_schema"])
async def test_broken_response_does_not_clear_prior_private_problem(interruption):
    gateway, calls, _, _ = setup([
        AIMessage(content=PREFIX + f"# {FIRST}"), interruption,
        replacement(f"# Only the line moved.\n# {FIRST}"),
        replacement("# Valid result should never be requested."),
    ], context=CONTEXT)
    with pytest.raises(SkillCreationError) as failure:
        await generate_source(gateway)
    assert failure.value.code == "private_package_content" and len(calls) == 3


@pytest.mark.asyncio
async def test_adding_another_problem_without_resolving_original_is_not_progress():
    gateway, calls, _, _ = setup([
        AIMessage(content=PREFIX + f"# {FIRST}"),
        replacement(f"# {FIRST} and newly introduced {SECOND}"),
        replacement("# Valid result should never be requested."),
    ], context=CONTEXT)
    with pytest.raises(SkillCreationError) as failure:
        await generate_source(gateway)
    assert failure.value.code == "private_package_content" and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["method", "oracle"])
async def test_adverse_independent_review_is_terminal_even_with_private_explanation(kind):
    document, schema, stage, _, code = review_case(kind, text=f"The concrete method contradicts {FIRST}.")
    gateway, calls, _ = make_gateway([document, document], privacy=CONTEXT)
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(schema, stage, "Review independently.", {}, check_privacy(CONTEXT))
    assert failure.value.code == code and len(calls) == 1


@pytest.mark.parametrize("source", [
    'value = "changed first-private-source\\x2ecsv text"',
    "value = ('first-private-'\n 'source.csv')",
    "value = ('first-private-'\n # Preserve this literal\n 'source.csv')",
    "value = ('first-private-'\r\n\r\n 'source.csv')",
])
def test_private_identity_ignores_python_literal_encoding_and_unrelated_literal_text(source):
    plain = privacy_failure_snapshot({"content": f'value = "prefix {FIRST} suffix"'}, CONTEXT)
    escaped = privacy_failure_snapshot({"content": source}, CONTEXT)
    assert plain is not None and escaped is not None
    assert plain.problems == escaped.problems
    assert sum(plain.problems.values()) == 1
    assert FIRST not in repr(plain) and SECOND not in repr(escaped)


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_reencoding_same_value_as_adjacent_multiline_literals_is_not_progress(newline):
    multiline = newline.join(["VALUE = ('first-private-'", " # Preserve the output declaration", "", " 'source.csv')"])
    gateway, calls, _, _ = setup([
        AIMessage(content=PREFIX + f"VALUE={FIRST!r}"),
        replacement(multiline),
        replacement("VALUE = None"),
    ], context=CONTEXT)
    with pytest.raises(SkillCreationError) as failure:
        await generate_source(gateway)
    assert failure.value.code == "private_package_content" and len(calls) == 2


def test_complete_private_observation_is_not_truncated_to_public_diagnostic_limit():
    repeated = privacy_failure_snapshot({"content": "\n".join(f"# {FIRST}" for _ in range(40))}, CONTEXT)
    reduced = privacy_failure_snapshot({"content": "\n".join(f"# {FIRST}" for _ in range(39))}, CONTEXT)
    assert sum(repeated.problems.values()) == 40
    assert sum(reduced.problems.values()) == 39
