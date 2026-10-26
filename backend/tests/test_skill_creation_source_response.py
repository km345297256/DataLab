"""Source is a complete artifact, not JSON-escaped code plus model-owned identity."""
import ast
import copy
import json
from unittest.mock import Mock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.skill_creation.models import HelperScript, ScriptRequest
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.prompts import SYSTEM_PROMPT
from app.domain.services.skills.skill_creation.script_authoring import generate_modular_script
from app.domain.services.skills.skill_creation.script_policy import validate_script
from app.domain.services.skills.skill_creation.source_response import SourceResponseError, source_response_adapter
from test_skill_creation_gateway import PRIVATE, make_gateway, requests
from test_skill_creation_script_authoring import CONTRACT, REQUEST, PLAN, COUNT, MAIN


SOURCE = "\n\n".join([*PLAN["imports"], COUNT["content"], MAIN["content"],
                          'if __name__ == "__main__":\n    main()']) + "\n"
METADATA = {key: REQUEST[key] for key in ("path", "purpose", "invocation")}


def adapter():
    return source_response_adapter(HelperScript, "implementation", {"script_request": REQUEST})


@pytest.mark.parametrize("answer", [SOURCE, f"```python\n{SOURCE}\n```", f"```py\n{SOURCE}\n```",
                                     f"```\n{SOURCE}\n```", json.dumps({**METADATA, "content": SOURCE})],
                         ids=["raw", "python-fence", "py-fence", "bare-fence", "exact-legacy-envelope"])
def test_only_complete_source_or_exact_legacy_envelope_binds_host_metadata(answer):
    document = adapter().document(answer)
    assert {key: document[key] for key in METADATA} == METADATA
    assert ast.dump(ast.parse(document["content"])) == ast.dump(ast.parse(SOURCE))


@pytest.mark.parametrize("answer", [
    "Here is the source:\n```python\n" + SOURCE + "\n```",
    "```python\n" + SOURCE,
    "```python\n" + SOURCE + "\n```\nExplanation follows.",
    "```python\n" + SOURCE + "\n```\n```python\nprint('second')\n```",
    "```json\n" + json.dumps({**METADATA, "content": SOURCE}) + "\n```",
    json.dumps({"content": SOURCE}),
    json.dumps({**METADATA, "content": SOURCE, "additional": "unexpected"}),
    json.dumps({**METADATA, "content": 123}),
    json.dumps({**METADATA, "path": "scripts/foreign.py", "content": SOURCE}),
    json.dumps({**METADATA, "purpose": "Rewrite accepted purpose", "content": SOURCE}),
    json.dumps({**METADATA, "invocation": "python scripts/foreign.py", "content": SOURCE}),
    '{"content":"ignored",' + json.dumps({**METADATA, "content": SOURCE})[1:],
    "# Only a comment, no implementation.",
    "def main():\n    if True:",
    "return 'invalid module-level control flow'",
], ids=["prose-prefix", "unfinished-fence", "prose-suffix", "multiple-blocks", "json-fence", "missing-metadata",
        "extra-metadata", "nontext-source", "foreign-path", "foreign-purpose", "foreign-invocation",
        "duplicate-key", "comments-only", "unfinished-python", "invalid-control-flow"])
def test_never_extracts_partial_source_or_silently_changes_metadata(answer):
    with pytest.raises(SourceResponseError):
        adapter().document(answer)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["implementation", "synthetic_script_repair", "synthetic_build_repair", "synthetic_method_repair", "synthetic_reference_implementation"])
async def test_raw_source_has_no_json_schema_and_runs_all_original_gates(stage):
    check = Mock()
    gateway, calls, usage = make_gateway([AIMessage(content=SOURCE, response_metadata={"finish_reason": "stop"})])
    gateway.system = SYSTEM_PROMPT
    payload = {"script_request": copy.deepcopy(REQUEST), "method_contract": CONTRACT}
    if stage in {"synthetic_script_repair", "synthetic_build_repair", "synthetic_method_repair"}:
        payload = {"script": {**METADATA, "content": SOURCE}, "frozen_validation_plan": {"cases": ["unchanged"]}}
    before = copy.deepcopy(payload)
    result = await gateway.generate(HelperScript, stage, "Preserve the complete analysis.", payload, check)
    request = requests(calls)[0]
    assert "schema" not in request
    assert request["response_format"] == {
        "type": "python_source", "artifact": "script", "host_metadata": METADATA, "complete_source_required": True}
    assert "只返回一个完整 Python 源文件" in calls[0][0].content
    assert "严格按给定 JSON Schema 返回单个 JSON 对象" not in calls[0][0].content
    assert payload == before
    check.assert_called_once_with(result)
    assert usage.record_from_message.await_count == 1
    assert result.model_dump(exclude={"content"}) == METADATA
    validate_script(result)


@pytest.mark.asyncio
async def test_raw_python_preserves_large_nested_strings_and_scientific_code_without_json_escaping():
    strings = '\n'.join(f'    label_{i} = f"literal {{len(rows)}}; quote \\\"{i}\\\""' for i in range(450))
    source = SOURCE.replace("def count_rows(rows):\n", "def count_rows(rows):\n" + strings + "\n")
    assert len(source) > 20000
    gateway, calls, _ = make_gateway([AIMessage(content=source)])
    result = await gateway.generate(HelperScript, "implementation", "Keep all methods", {"script_request": REQUEST})
    assert result.content == source.strip()
    assert "label_449" in result.content
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_private_invalid_source_is_safely_repaired_and_rechecked(caplog):
    invalid = "# " + PRIVATE + "\ndef broken(:\n    pass"
    gateway, calls, _ = make_gateway([AIMessage(content=invalid),
        {"repairs": [{"block_id": "B001", "replacement": SOURCE}]}])
    result = await gateway.generate(HelperScript, "implementation", "Keep all methods", {"script_request": REQUEST},
        lambda value: validate_portable_document(value, gateway.privacy))
    repair = requests(calls)[1]
    assert repair["repair_feedback"]["reason"] == "script_syntax_invalid"
    assert repair["repair_feedback"]["details"] == {"source_issue": "invalid_python_syntax", "line": 2, "end_line": 2}
    assert repair["schema"]["title"] == "SourceBlockRepairs" and "response_format" not in repair
    assert PRIVATE not in json.dumps(repair) and PRIVATE not in caplog.text
    assert result.content == SOURCE  # Host merge must not trim replacement bytes.


@pytest.mark.asyncio
async def test_source_admission_preserves_code_without_claiming_input_write_safety():
    source = SOURCE.replace("Path(args.output).write_text", "Path(args.input).write_text")
    gateway, calls, _ = make_gateway([AIMessage(content=source)])
    result = await gateway.generate(HelperScript, "implementation", "Keep all methods",
                                    {"script_request": REQUEST}, validate_script)
    # This checks source transport and static admission only. Input protection
    # must be confirmed by the isolated worker, not an inferred AST shape.
    assert result.content == source.strip()
    assert len(calls) == 1
    assert result.model_dump(exclude={"content"}) == METADATA


@pytest.mark.asyncio
async def test_modular_recovery_uses_raw_functions_with_host_ids_and_frozen_interfaces():
    gateway, calls, _ = make_gateway([PLAN, AIMessage(content=COUNT["content"]), AIMessage(content=MAIN["content"])])
    result = await generate_modular_script(gateway, HelperScript, "implementation", "Preserve all methods",
        {"script_request": REQUEST}, None, CONTRACT)
    observed = requests(calls)
    assert "schema" in observed[0]
    assert all("schema" not in row for row in observed[1:])
    assert [row["response_format"]["host_metadata"] for row in observed[1:]] == [{"unit_id": "U001"}, {"unit_id": "U002"}]
    assert COUNT["content"] in result.content and MAIN["content"].strip() in result.content
    validate_script(result)


@pytest.mark.asyncio
async def test_truncated_raw_script_never_uses_partial_answer_and_recovers_complete_units():
    partial = AIMessage(content=SOURCE + "# " + PRIVATE, response_metadata={"finish_reason": "length"})
    gateway, calls, _ = make_gateway([partial, PLAN, AIMessage(content=COUNT["content"]), AIMessage(content=MAIN["content"])])
    result = await gateway.generate(HelperScript, "implementation", "Preserve all methods",
        {"script_request": REQUEST}, validate_script, contract=CONTRACT)
    assert [row.get("authoring_phase") for row in requests(calls)] == [None, "plan", "unit", "unit"]
    assert PRIVATE not in json.dumps(requests(calls)) and PRIVATE not in result.content


def long_request():
    return ScriptRequest.model_validate({**REQUEST,
        "purpose": "Read the explicitly selected current input and count parsed records without modifying source data. " * 6,
        "invocation": "python scripts/count_rows.py --input " + "CURRENT_INPUT_" * 40 + " --output CURRENT_OUTPUT",
    }).model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["implementation", "synthetic_script_repair", "synthetic_build_repair", "synthetic_method_repair"])
@pytest.mark.parametrize("structured", [False, True])
async def test_valid_long_planned_metadata_remains_exact_when_source_is_bound(stage, structured):
    request = long_request()
    metadata = {key: request[key] for key in METADATA}
    assert all(len(metadata[key]) > 400 for key in ("purpose", "invocation"))
    payload = {"script_request": request}
    if stage in {"synthetic_script_repair", "synthetic_build_repair", "synthetic_method_repair"}:
        payload = {"script": {**metadata, "content": SOURCE}}
    before = copy.deepcopy(payload)
    response = json.dumps({**metadata, "content": SOURCE}) if structured else SOURCE
    gateway, calls, _ = make_gateway([AIMessage(content=response)])
    result = await gateway.generate(HelperScript, stage, "Keep the complete interface", payload, validate_script)
    assert result.model_dump(exclude={"content"}) == metadata
    assert requests(calls)[0]["response_format"]["host_metadata"] == metadata
    assert payload == before


@pytest.mark.asyncio
async def test_long_metadata_survives_complete_function_recovery_without_reauthoring_identity():
    request = long_request()
    gateway, calls, _ = make_gateway([AIMessage(content=SOURCE, response_metadata={"finish_reason": "length"}),
        PLAN, AIMessage(content=COUNT["content"]), AIMessage(content=MAIN["content"])])
    result = await gateway.generate(HelperScript, "implementation", "Keep complete methods",
        {"script_request": request}, validate_script, contract=CONTRACT)
    assert result.model_dump(exclude={"content"}) == {key: request[key] for key in METADATA}
    assert len(calls) == 4


@pytest.mark.parametrize("stage", ["workflow", "method_review", "synthetic_validation_plan", "package_validation"])
def test_other_stages_do_not_silently_switch_to_source_output(stage):
    assert source_response_adapter(HelperScript, stage, {"script_request": REQUEST}) is None
