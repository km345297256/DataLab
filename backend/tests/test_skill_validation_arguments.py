"""CLI data stays byte-for-byte intact; only host references are expanded."""
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from app.domain.services.skills.skill_creation.schema_diagnostics import _schema_diagnostics
from app.domain.services.skills.skill_validation import (
    SyntheticValidationPlan, is_input_alias_rejection_case, validate_validation_plan,
)
from app.domain.services.skills.skill_validation_arguments import (
    ARGUMENT_ERROR_HINTS, classify_argument, input_output_reference_alias, validate_arguments,
)
from app.infrastructure.external.sandbox.skill_validation_worker import _argv
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources


def argv(value):
    return ["--input", "{input:table.csv}", "--output", "{output}/summary.csv", "--fields-json", value]


@pytest.mark.parametrize("token", [
    '{"sample":"sid","group":"grp","value":"val"}',
    ' { "sample" : "sid", "groups" : ["grp", "batch"], "threshold":1.0000 } ',
    '{"columns":{"sample":"编号","group":"分组"},"strict":true,"optional":null}',
    '["sid", "grp", "val", 0, 1.25, false, null, {"key":"field"}]',
    '{\n  "sample": "sid",\n  "value": "val"\n}',
    '{}', '[]',
])
def test_json_looking_configuration_is_one_unchanged_argv_value(token):
    arguments = argv(token)
    before = copy.deepcopy(arguments)
    assert classify_argument(token, {"table.csv"}) == "literal"
    validate_arguments(arguments, {"table.csv"})
    assert arguments == before
    # Exercise the actual worker substitution boundary: only top-level file
    # placeholders are substituted, never JSON members or textual escapes.
    rendered = _argv(SimpleNamespace(argv=arguments), "/validation/case")
    assert rendered[-1] == token
    assert len(rendered) == len(arguments)
    assert rendered[1] == "/validation/case/table.csv"
    assert rendered[3] == "/home/ubuntu/output/summary.csv"


@pytest.mark.parametrize("token", [
    '{"sample":"sid","group":"grp","value":"val"}', "", "\n", "[A-Z]+", r"\s+", "mg/L", "{1,3}",
])
def test_real_configuration_interface_does_not_modify_cases_assertions_or_oracles(token):
    payload = plan_payload()
    for case in payload["scripts"][0]["cases"]:
        case["argv"] += ["--fields-json", token]
    before = copy.deepcopy(payload)
    parsed = SyntheticValidationPlan.model_validate(payload)
    declared = interfaces()
    declared[0]["arguments"].append("--fields-json")
    validate_validation_plan(parsed, method_contract(), sources(), declared)
    assert payload == before
    for original, accepted in zip(before["scripts"][0]["cases"], parsed.scripts[0].cases):
        assert accepted.argv == original["argv"]
        assert accepted.oracle_explanation == original["oracle_explanation"]
        assert accepted.input_files[0].model_dump() == original["input_files"][0]
        assert accepted.assertions == SyntheticValidationPlan.model_validate(before).scripts[0].cases[
            before["scripts"][0]["cases"].index(original)].assertions


@pytest.mark.parametrize("token,rule", [
    ('{"value":0,"value":1}', "json_duplicate_key"),
    ('{"fields":{"sample":"sid","sample":"other"}}', "json_duplicate_key"),
    (r'{"key":0,"\u006bey":1}', "json_duplicate_key"),
    ('{"threshold":NaN}', "json_nonfinite"),
    ('{"threshold":Infinity}', "json_nonfinite"),
    ('{"threshold":-Infinity}', "json_nonfinite"),
    ('{"threshold":1e9999}', "json_nonfinite"),
    ('{"fields":{"source":"/Users/private/input.csv"}}', "json_path"),
    ('{"fields":["../outside.csv"]}', "json_path"),
    ('{"source":"relative/input.csv"}', "json_path"),
    (r'{"source":"\u002fUsers\u002fprivate\u002finput.csv"}', "json_path"),
    (r'{"source":"C:\\private\\input.csv"}', "json_path"),
    ('{"source":"C:private.csv"}', "json_path"),
    ('{"source":"~/input.csv"}', "json_path"),
    ('{"source":"https://example.invalid/input.csv"}', "json_path"),
    (r'{"source":"https:\/\/example.invalid\/input.csv"}', "json_path"),
    ('{"source":"mailto:private@example.invalid"}', "json_path"),
    ('{"source":"file:input.csv"}', "json_path"),
    ('{"/Users/private/key":"field"}', "json_path"),
    ('{"source":"{input:table.csv}"}', "json_reference"),
    ('["{output}"]', "json_reference"),
    ('{"source":"{output}/summary.csv"}', "json_reference"),
    ('{"{input:table.csv}":"value"}', "json_reference"),
    (r'{"source":"\u007binput:table.csv\u007d"}', "json_reference"),
    ('{"sample":', "json_invalid"),
    ("{'sample':'sid'}", "json_invalid"),
    ('{"sample":"sid",}', "json_invalid"),
    ('{"sample":"sid"} trailing', "json_invalid"),
    ('[1,2,]', "json_invalid"),
    (r'{"sample":"\x73id"}', "json_invalid"),
    (r'{"sample":"\ud800"}', "json_invalid"),
    (r'{"sample":"sid\u0000"}', "json_control_character"),
    (r'{"sample":"sid\n"}', "json_control_character"),
])
def test_former_json_guesses_are_unchanged_cli_data_not_host_interpreted(token, rule):
    # The old guessed error category documents the migration; it is no longer
    # a transport policy. The tested CLI must interpret/reject these strings.
    assert "validation_argument_" + rule not in ARGUMENT_ERROR_HINTS
    validate_arguments(argv(token), {"table.csv"})
    assert classify_argument(token, {"table.csv"}) == "literal"
    assert _argv(SimpleNamespace(argv=argv(token)), "/validation/case")[-1] == token


@pytest.mark.parametrize("arguments", [
    ["--input", '{"sample":"sid"}', "--output", "{output}"],
    ["--input", "{input:table.csv}", "--output", '["result"]'],
    ["--input", "{output}", "--output", "{output}"],
    ["--input", "{input:table.csv}", "--output", "{input:table.csv}"],
    ["--input", "{input:table.csv}", "--input", "{input:table.csv}", "--output", "{output}"],
    ["--input", "{input:table.csv}", "--output"],
    ["--input", "", "--output", "{output}"],
    ["--input", "{input:table.csv}", "--output", ""],
])
def test_input_output_bindings_never_accept_json_scalar_or_duplicate_flags(arguments):
    with pytest.raises(PydanticCustomError) as failure:
        validate_arguments(arguments, {"table.csv"})
    assert failure.value.type == "validation_argument_io_binding"


@pytest.mark.parametrize("token,rule", [
    ("{input:missing.csv}", "input_reference"),
    ("{input:table.csv}/extra", "input_reference"),
    ("{output}/../outside.csv", "output_reference"),
    ("{output}//outside.csv", "output_reference"),
    ("{output}/", "output_reference"),
    ("{output}outside.csv", "output_reference"),
    ("value\x00suffix", "control_character"),
    ("value\ud800suffix", "unicode"),
    ("value\udfffsuffix", "unicode"),
])
def test_reserved_references_and_untransportable_values_are_rejected(token, rule):
    with pytest.raises(PydanticCustomError) as failure:
        classify_argument(token, {"table.csv"})
    assert failure.value.type == "validation_argument_" + rule


def test_schema_feedback_uses_only_fixed_argument_hint_not_rejected_value():
    payload = plan_payload()
    secret = "private-untrusted-field"
    payload["scripts"][0]["cases"][0]["argv"] += ["--fields-json", secret + "\x00"]
    with pytest.raises(ValidationError) as failure:
        SyntheticValidationPlan.model_validate(payload)
    diagnostics = _schema_diagnostics(failure.value, SyntheticValidationPlan)
    issue = next(item for item in diagnostics if item["type"] == "validation_argument_control_character")
    assert issue["hint"] == ARGUMENT_ERROR_HINTS[issue["type"]]
    assert secret not in json.dumps(diagnostics)


@pytest.mark.parametrize("token", [
    "", "\n", "\r\n", "  spacing  ", "中文\u2028line\u2029end", "[A-Z]+", "{1,3}",
    r"\s+", "mg/L", "/absolute/synthetic.csv", "relative/input.csv", r"C:\synthetic\input.csv",
    "--field={input:table.csv}", "prefix{output}/result", "prefix{input:table.csv}",
    '{"path":"{input:table.csv}"}', ' [invalid json ',
])
def test_ordinary_data_is_unchanged_even_when_it_resembles_a_path_or_expression(token):
    arguments = argv(token)
    validate_arguments(arguments, {"table.csv"})
    assert classify_argument(token, {"table.csv"}) == "literal"
    rendered = _argv(SimpleNamespace(argv=arguments), "/validation/case")
    assert rendered[-1].encode("utf-8") == token.encode("utf-8")
    assert len(rendered) == len(arguments)


@pytest.mark.parametrize("token", ["{input:table.csv}/suffix", "{output}suffix", "{output}/../escape"])
def test_worker_never_partially_expands_a_malformed_reserved_reference(token):
    with pytest.raises(PydanticCustomError):
        _argv(SimpleNamespace(argv=argv(token)), "/validation/case")


def test_schema_feedback_does_not_echo_an_unknown_custom_error_message():
    class UnrelatedModel(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def reject(cls, value):
            raise PydanticCustomError("unknown_validation_rule", "private error detail " + value)

    with pytest.raises(ValidationError) as failure:
        UnrelatedModel(name="synthetic-sensitive-value")
    diagnostics = _schema_diagnostics(failure.value, UnrelatedModel)
    assert diagnostics == [{"location": ["name"], "type": "unknown_validation_rule"}]
    assert "synthetic-sensitive-value" not in json.dumps(diagnostics)


def alias_rejection_payload():
    payload = plan_payload()
    for case in payload["scripts"][0]["cases"]:
        case["argv"][3] = "{output}/summary.csv"
    negative = payload["scripts"][0]["cases"][2]
    negative["argv"][3] = negative["argv"][1]
    negative["description"] = "Reject identical input and output before writing."
    negative["oracle_explanation"] = "Identical input and output must produce nonzero exit; all input bytes remain unchanged."
    return payload


def test_exact_same_input_alias_is_an_explicit_negative_probe_not_output_authorization():
    payload = alias_rejection_payload()
    frozen = copy.deepcopy(payload)
    plan = SyntheticValidationPlan.model_validate(payload)
    negative = plan.scripts[0].cases[2]
    assert is_input_alias_rejection_case(negative)
    assert negative.input_unchanged is True and negative.assertions == []
    declared = interfaces()
    declared[0]["output_mode"] = "file"
    validate_validation_plan(plan, method_contract(), sources(), declared)
    assert payload == frozen
    assert negative.argv == frozen["scripts"][0]["cases"][2]["argv"]
    rendered = _argv(negative, "/validation/synthetic-inputs")
    assert rendered[1] == rendered[3] == "/validation/synthetic-inputs/table.csv"


def test_pure_argument_checker_requires_opt_in_for_the_exact_same_declared_input():
    arguments = ["--input", "{input:table.csv}", "--output", "{input:table.csv}"]
    assert input_output_reference_alias(arguments, {"table.csv"})
    with pytest.raises(PydanticCustomError) as failure:
        validate_arguments(arguments, {"table.csv"})
    assert failure.value.type == "validation_argument_io_binding"
    validate_arguments(arguments, {"table.csv"}, reject_input_alias_only=True)


@pytest.mark.parametrize("mutation", [
    "normal", "boundary", "zero_exit", "output_assertion", "other_input", "unknown_input",
    "absolute_path", "relative_path", "nested_json", "input_alias_suffix", "duplicate_output", "changed_input",
])
def test_same_input_probe_cannot_relax_other_kind_exit_assertion_or_binding_contracts(mutation):
    payload = alias_rejection_payload()
    negative = payload["scripts"][0]["cases"][2]
    if mutation in {"normal", "boundary"}:
        negative["kind"] = mutation
    elif mutation == "zero_exit":
        negative["expected_exit_code"] = 0
    elif mutation == "output_assertion":
        negative["assertions"] = copy.deepcopy(payload["scripts"][0]["cases"][0]["assertions"])
    elif mutation == "other_input":
        other = copy.deepcopy(negative["input_files"][0])
        other["path"] = "other.csv"
        negative["input_files"].append(other)
        negative["argv"][3] = "{input:other.csv}"
    elif mutation == "unknown_input":
        negative["argv"][1] = negative["argv"][3] = "{input:unknown.csv}"
    elif mutation == "absolute_path":
        negative["argv"][1] = negative["argv"][3] = "/Users/private/input.csv"
    elif mutation == "relative_path":
        negative["argv"][1] = negative["argv"][3] = "table.csv"
    elif mutation == "nested_json":
        negative["argv"][3] = '{"path":"{input:table.csv}"}'
    elif mutation == "input_alias_suffix":
        negative["argv"][3] += "/extra.csv"
    elif mutation == "duplicate_output":
        negative["argv"] += ["--output", "{input:table.csv}"]
    elif mutation == "changed_input":
        negative["input_unchanged"] = False
    with pytest.raises(ValidationError):
        SyntheticValidationPlan.model_validate(payload)


def test_interface_file_binding_exception_does_not_apply_to_generic_invalid_directory_output():
    payload = alias_rejection_payload()
    payload["scripts"][0]["cases"][2]["argv"][3] = "{output}"
    plan = SyntheticValidationPlan.model_validate(payload)
    assert not is_input_alias_rejection_case(plan.scripts[0].cases[2])
    declared = interfaces()
    declared[0]["output_mode"] = "file"
    with pytest.raises(ValueError, match="validation_output_binding_invalid"):
        validate_validation_plan(plan, method_contract(), sources(), declared)


def test_safe_binding_hint_explains_rejection_probe_without_rewriting_test_intent():
    hint = ARGUMENT_ERROR_HINTS["validation_argument_io_binding"]
    assert "invalid case" in hint and "nonzero exit" in hint and "no assertions" in hint
    assert "output_empty/stderr_contains" in hint
    assert "Scientific CSV/JSON output assertions are not allowed" in hint
    assert "isolated output workspace, never the read-only input mount" in hint
    assert "no output assertions" not in hint
    assert "exactly the same declared fixture" in hint


@pytest.mark.parametrize("assertions", [
    [{"type": "output_empty"}],
    [{"type": "stderr_contains", "expected": "input"}],
    [{"type": "output_empty"}, {"type": "stderr_contains", "expected": "input"}],
])
def test_same_input_rejection_allows_only_declared_safety_postconditions(assertions):
    payload = alias_rejection_payload()
    payload["scripts"][0]["cases"][2]["assertions"] = copy.deepcopy(assertions)
    original = copy.deepcopy(payload)
    plan = SyntheticValidationPlan.model_validate(payload)
    case = plan.scripts[0].cases[2]
    declared = interfaces()
    declared[0]["output_mode"] = "file"
    validate_validation_plan(plan, method_contract(), sources(), declared)
    assert is_input_alias_rejection_case(case)
    assert [item.model_dump() for item in case.assertions] == assertions
    assert case.input_unchanged is True and payload == original
    rendered = _argv(case, "/validation/synthetic-inputs")
    assert rendered[1] == rendered[3] == "/validation/synthetic-inputs/table.csv"


@pytest.mark.parametrize("mutation", ["scientific_value", "different_input"])
def test_safety_postconditions_do_not_authorize_scientific_values_or_cross_input_alias(mutation):
    payload = alias_rejection_payload()
    case = payload["scripts"][0]["cases"][2]
    case["assertions"] = [{"type": "output_empty"}]
    if mutation == "scientific_value":
        case["assertions"].extend(copy.deepcopy(payload["scripts"][0]["cases"][0]["assertions"]))
    else:
        other = copy.deepcopy(case["input_files"][0])
        other["path"] = "other.csv"
        case["input_files"].append(other)
        case["argv"][3] = "{input:other.csv}"
    with pytest.raises(ValidationError) as failure:
        SyntheticValidationPlan.model_validate(payload)
    if mutation == "different_input":
        diagnostic = next(item for item in _schema_diagnostics(failure.value, SyntheticValidationPlan)
                          if item["type"] == "validation_argument_io_binding")
        assert diagnostic["hint"] == ARGUMENT_ERROR_HINTS["validation_argument_io_binding"]
        assert "output_empty/stderr_contains" in diagnostic["hint"]
