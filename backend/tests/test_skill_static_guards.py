"""Realistic synthesis forms must preserve methods and isolated outputs."""
import ast
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.skill_creation import service as creator_service
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import (
    CoverageError, MethodContract, validate_contract, validate_coverage,
)
from app.domain.services.skills.program_method_evidence import method_program_references
from app.domain.services.skills.session_skill_creator import (
    _PRIVATE, HelperScript,
)
from app.domain.services.skills.skill_creation.script_policy import (
    SkillScriptValidationError, validate_script as _validate_script,
)


def _contract(statement):
    return MethodContract(requirements=[{
        "id": "R001", "kind": "method", "statement": statement, "source_ids": ["S001"],
    }])


@pytest.mark.parametrize("verb", ["Clean", "Validate", "Summarize", "Inspect", "Calculate",
                                  "Fit", "Plot", "Extract", "Preserve", "Reject"])
def test_ordinary_english_workflow_verb_does_not_block_chinese_method_extraction(verb):
    sources = [{"id": "S001", "kind": "request", "text": f"{verb} the CSV observations using HC3."}]
    validate_contract(_contract("按照原要求处理 CSV 观测，并保留 HC3 计算方法。"), sources)


def test_capitalized_action_filter_does_not_remove_scientific_acronym():
    sources = [{"id": "S001", "kind": "request", "text": "Apply CLEAN to the selected observations."}]
    with pytest.raises(CoverageError, match="method_identifiers_lost"):
        validate_contract(_contract("按要求处理所选数据并报告结果。"), sources)


def test_identifier_case_in_code_or_prose_does_not_change_the_method():
    contract = _contract("使用 HC3 协方差与 Holm 校正，读取 CSV 数据。")
    sources = [{"id": "S001", "kind": "request", "text": "Validate the CSV observations using HC3 and Holm."}]
    validate_contract(_contract("处理 csv 观测，使用 hc3 协方差和 holm 校正。"), sources)
    validate_coverage(contract, {"steps": [{"action": "使用 hc3 协方差和 holm 校正，读取 csv 数据。"}],
        "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action"]}]})


def test_source_overlap_feedback_also_identifies_uncovered_sources():
    payload = _contract("完整保留数据验证与分析方法，不复制结果。").model_dump()
    payload["excluded_sources"] = [{"source_id": "S001", "reason": "observed_result",
                                    "explanation": "错误地将已映射的方法来源再次排除。"}]
    contract = MethodContract.model_validate(payload)
    with pytest.raises(CoverageError) as error:
        validate_contract(contract, [{"id": "S001", "kind": "request", "text": "分析数据。"},
                                     {"id": "S002", "kind": "goal", "text": "验证输入。"}])
    assert error.value.code == "source_exclusion_overlap"
    assert error.value.identifiers == ["S001"]
    assert error.value.details == {"overlapping_source_ids": ["S001"], "missing_source_ids": ["S002"]}


@pytest.mark.parametrize("unknown", ["S999", "private-sensitive-invalid-source"])
def test_unknown_exclusion_feedback_never_echoes_arbitrary_model_values(unknown):
    contract = MethodContract.model_validate({"requirements": [{"id": "R001", "kind": "method",
        "statement": "完整保留数据验证与分析方法。", "source_ids": ["S001"]}],
        "excluded_sources": [{"source_id": unknown, "reason": "duplicate", "explanation": "错误引用了不存在的来源编号。"}]})
    with pytest.raises(CoverageError) as error:
        validate_contract(contract, [{"id": "S001", "kind": "request", "text": "分析数据。"}])
    assert error.value.code == "unknown_source_exclusion"
    assert error.value.identifiers == (["S999"] if unknown == "S999" else [])
    assert "private-sensitive" not in json.dumps(error.value.details)


@pytest.mark.parametrize("literal", [
    "b'participant,measurement\\nExample Person,12.4\\n'",
    "b'Example Person' + b' private observation'",
    "{'observations': b'Example Person,12.4'}",
])
def test_byte_literals_never_retain_original_observations_in_method_references(literal):
    source = "import numpy as np\nraw = " + literal + "\nresult = np.mean(values)\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    references = method_program_references([{
        "schema": "program-method-evidence/v1", "source_digest": digest,
        "source_snapshot": {"version": 1, "encoding": "utf-8", "content": source,
                            "size_bytes": len(source.encode()), "sha256": digest},
    }], private_values=set(), private_pattern=_PRIVATE)
    rendered = json.dumps(references)
    assert "Example Person" not in rendered and "12.4" not in rendered
    assert "CURRENT_TASK" in references[0]["source"]
    assert "np.mean(values)" in references[0]["source"]


def _script(imports, write):
    return HelperScript(path="scripts/analyze.py", purpose="Produce a parameterized analysis result",
        invocation="python scripts/analyze.py --input INPUT --output OUTPUT", content=f"""import argparse
from pathlib import Path
{imports}
parser = argparse.ArgumentParser()
parser.add_argument('--input')
parser.add_argument('--output')
args = parser.parse_args()
if Path(args.input).resolve() == Path(args.output).resolve():
    raise ValueError('input and output must differ')
{write}
""")


@pytest.mark.parametrize(("imports", "statement"), [
    ("from datalab_json import write_json", "write_json(DEST, {'value': 1})"),
    ("from datalab_json import write_json as export", "export(path=DEST, payload={'value': 1})"),
    ("from numpy import savetxt as export", "export(DEST, [1, 2])"),
    ("from numpy import savetxt as export\nwriter = export", "writer(DEST, [1, 2])"),
    ("import numpy as np\nwriter = np.save", "writer(file=DEST, arr=[1, 2])"),
    ("from matplotlib.pyplot import savefig as export", "export(fname=DEST)"),
])
def test_writer_alias_shape_is_deferred_to_real_execution(imports, statement):
    _validate_script(_script(imports, statement.replace("DEST", "args.input")))
    _validate_script(_script(imports, statement.replace("DEST", "args.output")))


def test_standard_json_dump_uses_second_argument_and_output_handle_provenance():
    _validate_script(_script("from json import dump", """with open(args.output, 'w') as target:
    dump({'value': 1}, target)
"""))


def test_write_order_requires_real_execution_and_method_review():
    script = _script("from datalab_json import write_json", "")
    script.content = script.content.replace("if Path(args.input)",
        "write_json(args.output, {'value': 1})\nif Path(args.input)")
    _validate_script(script)


@pytest.mark.parametrize("input_expression,output_expression", [
    ("Path(args.input).resolve()", "Path(args.output).resolve()"),
    ("str(Path(args.input).resolve())", "str(Path(args.output).resolve())"),
])
def test_resolved_path_alias_guard_and_pure_compute_helper_are_supported(input_expression, output_expression):
    script = _script("import warnings", "Path(args.output).write_text(str(calculate()))")
    script.content = script.content.replace("parser = argparse.ArgumentParser()", "def calculate():\n    return 2 + 2\n\nparser = argparse.ArgumentParser()")
    script.content = script.content.replace("if Path(args.input).resolve() == Path(args.output).resolve():",
        f"input_path = {input_expression}\noutput_path = {output_expression}\n"
        "resolved_input = input_path\nif resolved_input == output_path:")
    _validate_script(script)


@pytest.mark.parametrize("input_expression,output_expression", [
    ("Path(args.input)", "Path(args.output)"),
    ("Path(args.input).absolute()", "Path(args.output).absolute()"),
    ("Path(args.input).resolve()", "Path(args.input).resolve()"),
])
def test_path_alias_shape_does_not_establish_runtime_safety(input_expression, output_expression):
    script = _script("", "Path(args.output).write_text('result')")
    script.content = script.content.replace("if Path(args.input).resolve() == Path(args.output).resolve():",
        f"input_path = {input_expression}\noutput_path = {output_expression}\nif input_path == output_path:")
    _validate_script(script)


def test_helper_write_layout_is_admitted_without_echoing_source():
    script = _script("", "emit_results(args.output)")
    script.content = script.content.replace("parser = argparse.ArgumentParser()",
        "def emit_results(destination):\n    Path(destination).write_text('private-source-marker')\n\nparser = argparse.ArgumentParser()")
    _validate_script(script)


def _script_with_parser_wrapper():
    script = _script("", "")
    script.content = '''import argparse
from pathlib import Path

def parse_args(argv=None):
    """Declare this invocation's inputs and isolated output."""
    parser = argparse.ArgumentParser(description='Analyze the selected data')
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--tolerance', type=float, default=1e-6)
    return parser.parse_args(argv)

def main(argv=None):
    args = parse_args(argv)
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output).resolve()
    if input_path == output_dir:
        raise ValueError('input and output must differ')
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'summary.csv').write_text('metric,value\\ncount,1\\n')

if __name__ == '__main__':
    main()
'''
    return script


@pytest.mark.parametrize("constructor_import,constructor", [
    ("import argparse", "argparse.ArgumentParser"),
    ("import argparse as cli", "cli.ArgumentParser"),
    ("from argparse import ArgumentParser as Parser", "Parser"),
])
def test_simple_parser_wrapper_supports_realistic_main_path_guard(constructor_import, constructor):
    script = _script_with_parser_wrapper()
    script.content = script.content.replace("import argparse", constructor_import).replace("argparse.ArgumentParser", constructor)
    _validate_script(script)


@pytest.mark.parametrize("replacement", [
    "namespace = parser.parse_args(argv)\n    namespace.output = namespace.input\n    return namespace",
    "return argparse.Namespace(input='input.csv', output='output.csv')",
    "return parser.parse_args(argv, namespace=argparse.Namespace())",
    "if argv is None:\n        return parser.parse_args()\n    return parser.parse_args(argv)",
])
def test_parser_wrapper_effects_require_execution_evidence(replacement):
    script = _script_with_parser_wrapper()
    script.content = script.content.replace("return parser.parse_args(argv)", replacement)
    _validate_script(script)


@pytest.mark.parametrize("change", [
    lambda source: source.replace("parser = argparse.ArgumentParser", "parser = custom_parser"),
    lambda source: source.replace("def main(argv=None):", "parse_args = another_parser\n\ndef main(argv=None):"),
    lambda source: source.replace("def parse_args(argv=None):", "@alter_namespace\ndef parse_args(argv=None):"),
    lambda source: source.replace("if __name__ == '__main__':", "def parse_args(argv=None):\n    return None\n\nif __name__ == '__main__':"),
])
def test_parser_binding_is_not_guessed_by_static_admission(change):
    script = _script_with_parser_wrapper()
    script.content = change(script.content)
    _validate_script(script)


@pytest.mark.parametrize("change", [
    lambda source: source.replace("if input_path == output_dir:", "if args.input == args.output:"),
    lambda source: source.replace("(output_dir / 'summary.csv')", "input_path"),
])
def test_parser_wrapper_io_semantics_are_not_proven_statically(change):
    script = _script_with_parser_wrapper()
    script.content = change(script.content)
    _validate_script(script)


@pytest.mark.asyncio
@pytest.mark.parametrize("private_comment, expected_code", [
    ("", None),
    ("/Users/alice/private/current.csv", "private_package_content"),
    ("private-secret-value", "private_package_content"),
    ("session-owned owner", "private_package_content"),
])
async def test_model_repair_receives_specific_safe_script_feedback(tmp_path, monkeypatch, private_comment, expected_code):
    from app.core.config import Settings
    from app.domain.services.skills import session_skill_creator as creator
    from app.domain.services.skills.registry import SkillRegistry
    from test_session_skill_creator import Repository, helper_script, recipe_payload, review_wire_response, stage_response

    payload = recipe_payload()
    example = helper_script()
    payload["implementation"] = {"kind": "parameterized_scripts", "reason": "按照已完成方法生成可参数化实现，并保持所有验证。",
        "scripts": [{"path": example["path"], "purpose": example["purpose"], "invocation": example["invocation"],
                     "requirement_ids": ["R001"], "implementation_source_ids": []}]}
    calls = []
    authored_sources = []
    verified_sources = []

    async def invoke(messages, **kwargs):
        request = json.loads(messages[1].content)
        calls.append(request)
        blocks = request.get("repair_feedback", {}).get("source_blocks")
        if request["stage"] == "implementation" and blocks is not None:
            # Only the diagnosed private comment is editable. Errors without
            # a host-selected source scope keep the ordinary full-source stub.
            return AIMessage(content=json.dumps({"repairs": [
                {"block_id": block["block_id"], "replacement": ""} for block in blocks]}))
        result = stage_response(messages, payload)
        if request["stage"] == "implementation" and "repair_feedback" not in request:
            result["content"] = result["content"].replace(
                "if Path(a.input).resolve() == Path(a.output).resolve():", "if a.input == a.output:")
            result["content"] += "\n# generated-candidate-comment\n# " + private_comment + "\n"
            authored_sources.append(result["content"])
        return AIMessage(content=json.dumps(review_wire_response(messages, result), ensure_ascii=False))

    monkeypatch.setattr(creator_service, "get_build_checkpoints", lambda **kwargs: None)
    monkeypatch.setattr(creator_service, "get_settings", lambda: Settings(_env_file=None, api_key="unit-test-placeholder"))
    monkeypatch.setattr(creator_service, "create_chat_model", lambda *args, **kwargs: SimpleNamespace(ainvoke=invoke))
    monkeypatch.setattr(creator_service.TokenUsageService, "record_from_message", AsyncMock())
    async def verification(recipe, *args, **kwargs):
        # This regression isolates static validation feedback; executable
        # synthetic-oracle/repair integration is covered separately.
        verified_sources.extend(script.content for script in recipe.scripts)
        return recipe, None, {"status": "not_run", "scope": "synthetic", "cases": []}
    monkeypatch.setattr(creator_service, "verify_skill_script_cases", verification)
    draft_service = SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(name="draft")))
    monkeypatch.setattr(creator_service, "get_skill_draft_service", lambda: draft_service)

    await creator.create_skill_from_session("session-owned", "owner", Repository(),
        SkillRegistry(str(tmp_path / "skills"), user_id="owner"))

    repairs = [request["repair_feedback"] for request in calls if "repair_feedback" in request]
    if expected_code is None:
        assert repairs == []  # Different guard layout is not a generation error.
    else:
        assert len(repairs) == 1 and repairs[0]["reason"] == expected_code
        issues = repairs[0]["details"]["privacy_issues"]
        assert issues and all(issue["location"] == ["content"] for issue in issues)
        assert all(issue["category"] in {"host_path", "identity", "credential"} for issue in issues)
        assert all(issue["line"] > 0 and issue["hint"] for issue in issues)
        repaired_request = next(request for request in calls if "repair_feedback" in request)
        assert repaired_request["schema"]["title"] == "SourceBlockRepairs"
        assert "response_format" not in repaired_request
        assert len(repairs[0]["source_blocks"]) == 1
        candidate = repaired_request["previous_candidate"]
        assert "generated-candidate-comment" in candidate["content"]
        assert "if a.input == a.output:" in candidate["content"]
        assert "--input" in candidate["content"] and "--output" in candidate["content"]
        assert verified_sources == [authored_sources[0].replace("# " + private_comment + "\n", "")]
        compile(verified_sources[0], "<rechecked-synthetic-source>", "exec")
    serialized = json.dumps(calls)
    for private in ("/Users/alice", "private-secret-value", "session-owned", '"owner"'):
        assert private not in serialized
    draft_service.create.assert_awaited_once()


def test_dynamic_code_and_attributes_are_not_rejected_by_their_spelling():
    script = _script("", "Path(args.output).write_text('result')")
    script.content += "\n# source-specific-secret-comment\neval('source-specific-secret')\nvalue.__private_marker\n"
    _validate_script(script)


def test_builtin_type_name_read_is_safe_for_error_diagnostics():
    script = _script("", "Path(args.output).write_text(str(type(42).__name__))")
    _validate_script(script)


@pytest.mark.parametrize("prefix, expression", [
    ("type = lambda value: value", "type(42).__name__"),
    ("def describe(type):\n    return type(42).__name__", "42"),
    ("from typing import Any as type", "type(42).__name__"),
    ("class type:\n    pass", "type(42).__name__"),
    ("try:\n    raise ValueError()\nexcept ValueError as type:\n    diagnostic = type(42).__name__", "42"),
    ("match 42:\n    case type:\n        diagnostic = type(42).__name__", "42"),
    ("", "value.__name__"),
    ("", "type(42).__name__.__class__"),
    ("", "type(42).__mro__"),
    ("", "type(*values).__name__"),
])
def test_dunder_access_does_not_require_a_special_builtin_type_exception(prefix, expression):
    script = _script(prefix, f"Path(args.output).write_text(str({expression}))")
    _validate_script(script)


def _reused_handle_script(first="input", second="output", rebind=""):
    script = _script("import json", "")
    script.content += (f"\nwith Path(args.{first}).open('r') as handle:\n    value = json.load(handle)\n"
        f"with Path(args.{second}).open('w') as handle:\n"
        + ("    " + rebind + "\n" if rebind else "")
        + "    json.dump(value, handle)\n")
    return script


def test_reusing_with_handle_name_reads_input_then_writes_only_output():
    _validate_script(_reused_handle_script())


def test_output_with_handle_is_not_polluted_by_an_unrelated_function_scope():
    script = _reused_handle_script()
    script.content = script.content.replace("parser = argparse.ArgumentParser()",
        "def inspect_input(args):\n    with Path(args.input).open('r') as handle:\n        return json.load(handle)\n\nparser = argparse.ArgumentParser()")
    _validate_script(script)


@pytest.mark.parametrize("first, second, rebind", [
    ("output", "input", ""),
    ("input", "output", "handle = Path(args.input).open('w')"),
    ("input", "output", "handle = args.input"),
])
def test_reassigned_file_handle_requires_actual_readonly_worker_checks(first, second, rebind):
    _validate_script(_reused_handle_script(first, second, rebind))
