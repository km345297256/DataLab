"""Synthetic privacy and scientific-method regressions; no user data or model."""
import json

import pytest

from app.domain.services.skills.skill_creation.privacy import (
    PrivacyContext, PrivateValue, ensure_context, redact_document, scan_document,
)


def test_context_preserves_sensitive_provenance_without_treating_every_filename_as_identity():
    events = [{"id": "event-private-17", "message": "Use an HDF5 reader and export summary.csv.",
               "attachments": [{"file_id": "source-file-17", "filename": "measurements-patient17.csv",
                                "file_path": "/custom-store/account17/measurements-patient17.csv"}],
               "function_args": {"password": "synthetic-private-key", "code": "token = runtime_token\nthreshold = 0.05"}}]
    context = PrivacyContext.collect(events, session_id="session-private-17", user_id="researcher17")
    values = {(entry.value, entry.category) for entry in context.entries}
    assert ("measurements-patient17.csv", "source_filename") in values
    assert ("synthetic-private-key", "credential") in values
    assert ("source-file-17", "identity") in values
    assert "summary.csv" not in context.private_values
    assert "runtime_token" not in context.private_values
    assert context.scan_document({"goal": "Use an HDF5 reader and export summary.csv."}) == []
    assert "synthetic-private-key" not in repr(context)


def test_context_finds_typed_markers_in_nested_tool_results_without_exporting_results():
    context = PrivacyContext.collect([{"function_result": {"data": {
        "workspace_id": "workspace-personal-private", "access_token": "private-access-value",
        "files": [{"filename": "private-experiment.tsv", "path": "private-stage/experiment.tsv"}],
        "rows": [{"value": 3.14, "id": "method-unit"}],
    }}}], session_id="session-17", user_id="user-17")
    assert {"workspace-personal-private", "private-access-value", "private-experiment.tsv",
            "private-stage/experiment.tsv"} <= context.private_values
    assert "method-unit" not in context.private_values
    assert context.scan_document({"steps": [{"action": "Summarize method-unit by mean and variance."}]}) == []


@pytest.mark.parametrize("text", [
    r"Validate extension with r'\.(?:csv|tsv|xlsx)$'; allow summary.csv output.",
    "Use HDF5, XML, viewBox, PNG and sklearn; compute OLS with HC3 covariance.",
    "The parsing token is a field separator. An empty token=",  # never borrow another field
    "Bind token={auth_token}, session_id={current_session} and --output {output}/summary.csv.",
    "Write --output {output}/tables/summary.csv and figures to <output_dir>/figures/qa.png.",
    "Use token=args.token and session_id=params.session_id; output remains parameterized.",
    "Check PNG dimensions and report .csv/.tsv input formats.",
    "Use Benjamini-Hochberg FDR correction: alpha = 0.05; compare treatment/control.",
])
def test_scientific_names_regex_and_runtime_contracts_are_not_private(text):
    assert PrivacyContext().scan_document({"steps": [{"action": text}]}) == []


def test_fields_are_never_concatenated_and_no_json_serialization_changes_detection():
    document = {"parameters": [{"description": "Use token=", "constraints": "Whitespace splits fields."}],
                "steps": [{"action": "filename: ", "decision": "summary.csv is a portable output name."}]}
    context = PrivacyContext()
    assert not context.scan_document(document)
    assert context.redact_document(document) == document


@pytest.mark.parametrize("value,category", [
    ("/Users/researcher/private-input.csv", "host_path"),
    ("/custom-volume/research/account1/input.csv", "host_path"),
    ("/tmp", "host_path"),
    (r"C:\Users\researcher\private-input.csv", "host_path"),
    (r"\\private-host\share\file.csv", "host_path"),
    ("~/private/file.csv", "host_path"),
    ("https://example.invalid/private.csv?token=synthetic-private", "url"),
    ("s3://private-bucket/account/file.csv", "url"),
    ("file:///private/file.csv", "url"),
    ("1fa82c70-2fa9-4ff5-a8d3-56f6554bb1a2", "identity"),
    ("62709f846cc74e37afc10c1133b49baa", "identity"),
    ("ghp_syntheticprivatevalue123", "credential"),
    ("sk-synthetic-private-value", "credential"),
    ("Authorization: Bearer synthetic.private.value", "credential"),
    ("api_key='private-key-value'", "credential"),
    ("password: real-but-synthetic-secret", "credential"),
    ("session_id=actual-session-value", "identity"),
])
def test_concrete_identifiers_paths_and_credentials_still_fail_closed(value, category):
    context = PrivacyContext()
    document = {"steps": [{"action": value}]}
    issues = context.scan_document(document)
    assert any(issue.category == category and issue.location == ("steps", 0, "action") for issue in issues)
    assert value not in json.dumps(context.redact_document(document))
    assert value not in json.dumps([issue.as_dict() for issue in issues])


def test_source_filename_matching_is_lexical_and_preserves_unrelated_method_identifiers():
    context = PrivacyContext((PrivateValue("ols", "identity"), PrivateValue("report.csv", "source_filename")))
    assert context.scan_document({"description": "Use tools and myreport.csv output."}) == []
    assert context.scan_document({"description": "Summarize report.csv with tools."})[0].category == "source_filename"
    assert context.scan_document({"goal": "source owner is ols"})[0].category == "identity"


@pytest.mark.parametrize("rhs", ["runtime_token", "args.token", "options.token", "config['token']", "get_token()", "None", "False", "''"])
def test_code_variable_assignments_are_not_credentials(rhs):
    code = f"token = {rhs}\nresult = normalize(token)\n"
    document = {"scripts": [{"content": code}]}
    assert PrivacyContext().scan_document(document) == []


@pytest.mark.parametrize("code", [
    "token = 'literal-sensitive-value'",
    "token = 'literal-' + suffix",
    "token = f'literal-sensitive-{suffix}'",
    "request(token='literal-sensitive-value')",
    "def request(token='literal-sensitive-value'):\n    return token",
    "token = construct('literal-sensitive-value')",
    "# token = private-comment-value\ntoken = args.token",
])
def test_python_ast_exemptions_cannot_hide_literal_credentials(code):
    context = PrivacyContext()
    assert any(issue.category == "credential" for issue in context.scan_document({"scripts": [{"content": code}]}))


def test_known_values_always_override_runtime_reference_or_placeholder_grammar():
    context = PrivacyContext((PrivateValue("args.token", "credential"), PrivateValue("{auth_token}", "credential")))
    issues = context.scan_document({"scripts": [{"content": "token = args.token"}], "goal": "token={auth_token}"})
    assert len(issues) == 2
    assert all(issue.category == "credential" for issue in issues)


@pytest.mark.parametrize("code", [
    r"source = '\x2fUsers\x2falice\x2fprivate.csv'",
    r"source = '\u002fUsers/alice/private.csv'",
    r"source = '\x73\x65\x73\x73\x69\x6f\x6e-private'",
    "source = '/Users/' 'alice/private.csv'",
])
def test_decoded_or_concatenated_python_literals_cannot_conceal_private_content(code):
    context = PrivacyContext.from_legacy({"session-private"})
    document = {"scripts": [{"content": code}]}
    assert context.scan_document(document)
    safe = context.redact_document(document)
    assert not context.scan_document(safe)


def test_unicode_python_positions_do_not_corrupt_diagnostics_or_repair():
    context = PrivacyContext()
    document = {"scripts": [{"content": "说明 = '科学'; token = args.token\n说明 = '输入'; source = '/Users/private/input.csv'\n"}]}
    issues = context.scan_document(document)
    assert all(issue.line == 2 for issue in issues)
    safe = context.redact_document(document)
    assert "说明 = '科学'; token = args.token" in safe["scripts"][0]["content"]
    assert "说明 = '输入';" in safe["scripts"][0]["content"]


def test_json_pointers_are_only_exempt_in_declared_coverage_locations():
    context = PrivacyContext()
    assert context.scan_document({"coverage": [{"locations": ["/steps/0/action", "/scripts/0/content"]}]}) == []
    # Privacy does not adjudicate which schema locations scientifically prove
    # coverage; the coverage validator reports these as invalid locations.
    assert context.scan_document({"coverage": [{"locations": ["/applicability/0", "/implementation/scripts/0"]}]}) == []
    assert context.scan_document({"steps": [{"action": "Read /steps/0/action"}]})
    assert context.scan_document({"coverage": [{"locations": ["/Users/private/input.csv"]}]})
    known = PrivacyContext((PrivateValue("/steps/0/action", "host_path"),))
    assert known.scan_document({"coverage": [{"locations": ["/steps/0/action"]}]})


def test_safe_diagnostics_never_echo_untrusted_keys_values_or_code():
    document = {"scripts": [{"content": "import csv\n# inspect /Users/private/input.csv\n"}],
                "untrusted-account-17": {"api_key": "synthetic-private-value"}}
    context = PrivacyContext()
    issues = [issue.as_dict() for issue in context.scan_document(document)]
    assert {"location": ["scripts", 0, "content"], "category": "host_path", "line": 2,
            "hint": "Replace the original absolute path with an explicit current-input or output parameter."} in issues
    result = json.dumps(issues)
    for private in ("untrusted-account-17", "synthetic-private-value", "input.csv", "import csv"):
        assert private not in result


def test_private_dictionary_keys_are_detected_and_redacted_without_collision():
    document = {"/Users/private/a.csv": "OLS", "/Users/private/b.csv": "HC3"}
    context = PrivacyContext()
    assert len(context.scan_document(document)) == 2
    safe = context.redact_document(document)
    assert len(safe) == 2 and set(safe.values()) == {"OLS", "HC3"}
    assert "/Users" not in json.dumps(safe)


def test_redaction_is_consistent_idempotent_and_preserves_scientific_structure():
    context = PrivacyContext.collect([{"attachments": [{"filename": "subject17.csv"}]}],
                                     session_id="session-17", user_id="person-17")
    original = {"steps": [{"action": "Read subject17.csv from /Users/person-17/input.csv; use OLS + HC3.",
                           "verification": "FDR family includes all treatment-control comparisons."}],
                "outputs": [{"name": "summary.csv", "format": "csv"}],
                "coverage": [{"locations": ["/steps/0/action"]}]}
    safe = context.redact_document(original)
    assert safe == context.redact_document(safe)
    assert not context.scan_document(safe)
    assert original["outputs"] == safe["outputs"]
    assert original["coverage"] == safe["coverage"]
    assert original["steps"][0]["verification"] == safe["steps"][0]["verification"]
    assert "OLS + HC3" in safe["steps"][0]["action"]


def test_legacy_compatibility_uses_new_field_scanner_without_losing_private_values():
    legacy = {"session-owned", "private-secret-value", "source.csv"}
    assert ensure_context(ensure_context(legacy)).private_values == legacy
    document = {"goal": "session-owned uses private-secret-value with source.csv"}
    assert len(scan_document(document, legacy)) == 2  # identity and filename categories
    assert not any(value in json.dumps(redact_document(document, legacy)) for value in legacy)


def test_distinct_inputs_keep_stable_opaque_aliases_and_join_roles_across_fields():
    events = [{"attachments": [{"filename": "measurements.csv", "file_path": "/private/source/measurements.csv"},
                               {"filename": "cultures.csv", "file_path": "/private/source/cultures.csv"}]}]
    context = PrivacyContext.collect(events, session_id="session-17", user_id="person-17")
    document = {"research_request": "cultures.csv包含分组信息；measurements.csv包含测量值。",
                "steps": [{"action": "读取/private/source/cultures.csv，关联/private/source/measurements.csv；以sample_id作多对一连接。",
                           "verification": "cultures.csv的sample_id必须唯一，measurements.csv保留所有重复测量。"}]}
    safe = context.redact_document(document)
    assert safe["research_request"] == "{input_file_1}包含分组信息；{input_file_2}包含测量值。"
    assert safe["steps"][0]["action"] == "读取{input_file_1}，关联{input_file_2}；以sample_id作多对一连接。"
    assert safe["steps"][0]["verification"] == "{input_file_1}的sample_id必须唯一，{input_file_2}保留所有重复测量。"
    assert not context.scan_document(safe)
    assert context.redact_document(safe) == safe
    reversed_context = PrivacyContext(tuple(reversed(context.entries)))
    assert reversed_context.redact_document(document) == safe


def test_paths_only_share_source_alias_when_basename_is_unambiguous():
    context = PrivacyContext((PrivateValue("a/data.csv", "source_filename"),
                              PrivateValue("b/data.csv", "source_filename"),
                              PrivateValue("single.csv", "source_filename")))
    assert context.redact_text("a/data.csv b/data.csv") == "{input_file_1} {input_file_2}"
    assert context.redact_text("/private/source/data.csv") == "{input_path}"
    assert context.redact_text("/private/source/single.csv") == "{input_file_3}"
    assert context.redact_text(r"C:\private\single.csv") == "{input_file_3}"
    assert context.redact_text("/private/source/unknown.csv") == "{input_path}"


def test_python_escaped_source_filename_keeps_its_alias():
    context = PrivacyContext((PrivateValue("cultures.csv", "source_filename"),
                              PrivateValue("measurements.csv", "source_filename")))
    document = {"scripts": [{"content": r"source = '\x63ultures.csv'"}]}
    safe = context.redact_document(document)
    assert "{input_file_1}" in safe["scripts"][0]["content"]
    assert not context.scan_document(safe)


@pytest.mark.parametrize("directive", ["#!/usr/bin/env python3", "#!/usr/bin/env python", "#!/usr/bin/python3", "#!/usr/bin/python"])
def test_exact_standard_python_shebang_is_an_interpreter_directive(directive):
    document = {"scripts": [{"content": directive + "\nimport csv\n"}]}
    context = PrivacyContext()
    assert context.scan_document(document) == []
    assert context.redact_document(document) == document
    context = PrivacyContext.collect([{"function_args": {"code": document["scripts"][0]["content"]}}],
                                     session_id="session-17", user_id="person-17")
    assert context.scan_document(document) == []


@pytest.mark.parametrize("code", [
    "#!/usr/bin/env python3 --private=/private/input.csv\n",
    "#!/usr/bin/env PRIVATE_TOKEN=secret python3\n",
    "#!/usr/bin/env python3 -c 'secret'\n",
    "#!/home/private/python3\n",
    "#!/usr/bin/private-python3\n",
    "#!/usr/bin/python37\n",
    "\n#!/usr/bin/env python3\n",
    "# note\n#!/usr/bin/env python3\n",
    "#!/usr/bin/env python3\nsource = '/usr/bin/private.csv'\n",
])
def test_shebang_classification_does_not_exempt_arguments_other_paths_or_later_lines(code):
    assert PrivacyContext().scan_document({"scripts": [{"content": code}]})


def test_known_private_path_and_prose_are_never_exempted_as_shebang():
    directive = "#!/usr/bin/env python3\n"
    assert PrivacyContext().scan_document({"steps": [{"action": directive}]})
    context = PrivacyContext((PrivateValue("/usr/bin/env", "host_path"),))
    assert context.scan_document({"scripts": [{"content": directive}]})


@pytest.mark.parametrize("pointer", ["", "/summary/count", "/home/private", "/a~1b/~0metric", "/统计/0/均值"])
def test_synthetic_assertion_pointer_uses_rfc6901_semantics(pointer):
    document = {"scripts": [{"cases": [{"assertions": [{"pointer": pointer}]}]}]}
    context = PrivacyContext()
    assert context.scan_document(document) == []
    assert context.redact_document(document) == document
    if pointer:
        known = PrivacyContext((PrivateValue(pointer, "host_path"),))
        assert known.scan_document(document)


def test_synthetic_pointer_exemption_does_not_apply_to_other_fields_or_invalid_escapes():
    context = PrivacyContext()
    assert context.scan_document({"steps": [{"action": "/summary/count"}]})
    assert context.scan_document({"assertions": [{"explanation": "/home/private"}]})
    assert context.scan_document({"assertions": [{"pointer": "/home/private~bad"}]})


def test_chinese_prose_cannot_hide_known_filename_identity_or_absolute_path():
    context = PrivacyContext((PrivateValue("private-user", "identity"), PrivateValue("结果.csv", "source_filename"),
                              PrivateValue("/custom/input.csv", "host_path")))
    value = "由private-user生成结果.csv，并读取/custom/input.csv，保留OLS方法。"
    safe = context.redact_text(value)
    assert safe == "由{source_identity}生成{input_file_1}，并读取{input_path}，保留OLS方法。"
    assert not context.scan_document({"goal": safe})


@pytest.mark.parametrize("field", ["explanation", "implementation_reason"])
def test_review_can_cite_only_exact_workflow_schema_pointers(field):
    text = "R001由 /steps/0/action 和 /scripts/0/content 落实；/parameters/1/constraints 定义输入限制。"
    document = {"requirements": [{field: text}]}
    context = PrivacyContext()
    assert not context.scan_document(document)
    assert context.redact_document(document) == document
    known = PrivacyContext((PrivateValue("/steps/0/action", "host_path"),))
    assert known.scan_document(document)


@pytest.mark.parametrize("path", [
    "/home/private", "/script/path/filename", "/scripts/analysis.py",
    "/scripts/private/content", "/scripts/0/private.csv", "/steps/0/action/private.csv",
    "/steps/0/action-private.csv", "/steps/0/private_field", "/parameters/1/api_key",
    "/outputs/summary/content", "/dependencies/0/private.csv",
])
def test_review_pointer_grammar_does_not_exempt_actual_paths_or_unknown_fields(path):
    context = PrivacyContext()
    assert context.scan_document({"requirements": [{"explanation": "方法使用 " + path}]})
    assert context.scan_document({"implementation_reason": "方法使用 " + path})


def test_review_pointer_role_does_not_extend_to_code_or_general_prose():
    context = PrivacyContext()
    assert context.scan_document({"steps": [{"action": "方法使用 /scripts/0/content"}]})
    assert context.scan_document({"scripts": [{"content": "path = '/scripts/0/content'"}]})


@pytest.mark.parametrize("text", [
    "非法 scale（布尔/非数值/NaN/Infinity/<=0）、字段映射缺失或重复、",
    "按组统计样本数/均值/标准差/standard_error，保留数据单位。",
    "支持输入/输出/CSV/JSON/Parquet 格式检查。",
    "検証: 真偽値/非数値/欠損/NaN/Infinity、異常時に終了。",
    "Check bool/non_numeric/NaN/Infinity; reject nonpositive scale.",
    "Évaluer moyenne/variance/écart_type et conserver les unités.",
])
def test_multilingual_scientific_slash_lists_are_not_host_paths(text):
    context = PrivacyContext()
    document = {"steps": [{"action": text}], "scripts": [{"content": f'"""{text}"""\nvalue = 1\n'}]}
    assert not context.scan_document(document)
    assert context.redact_document(document) == document


@pytest.mark.parametrize("text", [
    "Read /custom/input.csv before computation.",
    "读取 /custom/input.csv 后计算。",
    "读取/Users/private/input.csv，保持源数据只读。",
    "读取/home/private/input.csv，保持源数据只读。",
    "读取/tmp/input.csv，保持源数据只读。",
    'input_path = "/custom/input.csv"',
    'input_path = "/数据目录/原始文件.csv"',
])
def test_explicit_paths_and_common_roots_remain_private_after_unicode_boundary_fix(text):
    context = PrivacyContext()
    assert context.scan_document({"scripts": [{"content": text}]})


def test_known_nonstandard_source_path_still_matches_without_prose_whitespace():
    context = PrivacyContext.collect([{"function_args": {"file_path": "/custom/input.csv"}}],
                                     session_id="session-17", user_id="person-17")
    text = "读取/custom/input.csv，布尔/非数值/NaN/Infinity均需校验。"
    safe = context.redact_text(text)
    assert safe == "读取{input_path}，布尔/非数值/NaN/Infinity均需校验。"
    assert not context.scan_document(safe)
