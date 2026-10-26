"""Only proven output column names cross the computation-blind boundary."""
import json

import pytest

from app.domain.services.skills.script_public_interface import public_script_interface
from app.domain.services.skills.skill_creation.checkpoints import content_digest
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_validation import ScriptValidationInterface, SyntheticValidationPlan, validate_validation_plan
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources


SOURCE = '''import argparse
import csv
import json
from pathlib import Path
PRIMARY = "entity"

def summarize(rows):
    features = sorted({row["feature"] for row in rows})
    columns = [PRIMARY] + features
    alias = columns
    result = {"columns": alias, "answer": [987654321, "PRIVATE_SCIENTIFIC_ANSWER"],
              "metadata": {"fieldnames": ["NOT_A_PUBLIC_COLUMN"]}}
    return result, features

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result, features = summarize([])
    report = {"pivot": result}
    output = Path(args.output)
    with (output / "wide.csv").open("w") as handle:
        writer = csv.writer(handle)
        writer.writerow([PRIMARY] + features)
        for row in []:
            writer.writerow([row[PRIMARY]] + ["" if row[field] is None else row[field] for field in features])
    with (output / "report.json").open("w") as handle:
        json.dump(report, handle)
'''


def columns_shape(source=SOURCE):
    return public_script_interface(source)["json_outputs"][0]["shape"]["properties"]["pivot"]["properties"]["columns"]


def validate(assertion, source=SOURCE):
    raw = plan_payload()
    for case in raw["scripts"][0]["cases"]:
        if case["expected_exit_code"] == 0:
            case["assertions"] = [assertion]
    plan = SyntheticValidationPlan.model_validate(raw)
    interface = ScriptValidationInterface.model_validate({**interfaces()[0], **public_script_interface(source)})
    validate_validation_plan(plan, method_contract(), sources(), [interface])


@pytest.mark.parametrize("sequence", ["[PRIMARY] + features", "[PRIMARY, *features]"])
def test_proven_csv_header_prefix_survives_aliases_function_return_and_unpacking(sequence):
    source = SOURCE.replace("[PRIMARY] + features", sequence)
    assert columns_shape(source) == {"type": "array", "items": {"type": "unknown"}, "column_name_prefix": ["entity"]}
    encoded = json.dumps(public_script_interface(source))
    for secret in ("987654321", "PRIVATE_SCIENTIFIC_ANSWER", "NOT_A_PUBLIC_COLUMN", "summarize", "sorted", "row[", "PRIMARY"):
        assert secret not in encoded


def test_dictwriter_fieldnames_with_header_is_a_public_column_schema():
    start = SOURCE.index("        writer = csv.writer(handle)")
    end = SOURCE.index('    with (output / "report.json")', start)
    source = SOURCE[:start] + '''        names = [PRIMARY, *features]
        alias = names
        writer = csv.DictWriter(handle, fieldnames=alias)
        writer.writeheader()
        writer.writerows([])
''' + SOURCE[end:]
    assert columns_shape(source)["column_name_prefix"] == ["entity"]


def member_header_source():
    start = SOURCE.index('    with (output / "wide.csv")')
    return SOURCE[:start] + '''    with (output / "report.json").open("w") as handle:
        json.dump(report, handle)
    columns = report["pivot"]["columns"]
    names = columns
    with (output / "wide.csv").open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        writer.writerows([])
'''


def test_nested_dictionary_member_aliases_and_function_returns_share_the_csv_column_schema():
    # Serializing the report first does not mutate it. The exact same nested
    # column sequence then supplies DictWriter's header after local aliases.
    assert columns_shape(member_header_source())["column_name_prefix"] == ["entity"]


@pytest.mark.parametrize("change", [
    '    columns = report["pivot"][args.member]',
    '    report["pivot"]["columns"].reverse()\n    columns = report["pivot"]["columns"]',
    '    report["pivot"]["columns"] = []\n    columns = report["pivot"]["columns"]',
    '    alter_report(report)\n    columns = report["pivot"]["columns"]',
])
def test_unknown_member_or_mutated_dictionary_does_not_authorize_a_fixed_prefix(change):
    source = member_header_source().replace('    columns = report["pivot"]["columns"]', change)
    assert "column_name_prefix" not in columns_shape(source)


def test_unknown_dictionary_spread_does_not_prove_selected_column_member():
    source = member_header_source().replace('report = {"pivot": result}', 'report = {"pivot": result, **external_fields}')
    # The unknown spread may replace pivot entirely; preserve that uncertainty.
    assert "column_name_prefix" not in json.dumps(public_script_interface(source))


@pytest.mark.parametrize("replacement", [
    '        if features:\n            writer.writerow([PRIMARY] + features)',
    '        writer.writerow(["SCIENTIFIC_RESULT"] + features)',
])
def test_conditional_or_unrelated_csv_header_cannot_authorize_json_array_literals(replacement):
    source = SOURCE.replace('        writer.writerow([PRIMARY] + features)', replacement)
    assert "column_name_prefix" not in columns_shape(source)


def test_data_row_must_select_same_column_sequence_to_prove_writer_header_role():
    source = SOURCE.replace('for field in features])', 'for field in reversed(features)])')
    assert "column_name_prefix" not in columns_shape(source)


def test_import_shadowing_does_not_masquerade_as_csv_schema():
    source = SOURCE.replace('    result, features = summarize([])', '    csv = another_library\n    result, features = summarize([])')
    assert "column_name_prefix" not in columns_shape(source)


def test_mutation_through_alias_prevents_claiming_stable_column_prefix():
    source = SOURCE.replace('    result = {"columns": alias', '    alias.reverse()\n    result = {"columns": alias')
    assert "column_name_prefix" not in columns_shape(source)


def test_columns_field_name_alone_never_exposes_constant_data_values():
    source = SOURCE[:SOURCE.index('    with (output / "wide.csv")')] + '''    with (output / "report.json").open("w") as handle:
        json.dump(report, handle)
'''
    assert "column_name_prefix" not in columns_shape(source)
    assert "NOT_A_PUBLIC_COLUMN" not in json.dumps(public_script_interface(source))


def test_renaming_json_metadata_does_not_change_proven_column_semantics():
    source = SOURCE.replace('"columns": alias', '"layout": alias')
    shape = public_script_interface(source)["json_outputs"][0]["shape"]["properties"]["pivot"]["properties"]["layout"]
    assert shape["column_name_prefix"] == ["entity"]


@pytest.mark.parametrize("expected", ["entity", "measure"])
def test_plan_requires_exact_public_column_name_at_fixed_prefix_index(expected):
    assertion = {"type": "json_value", "path": "report.json", "pointer": "/pivot/columns/0", "expected": {"value": expected}}
    if expected == "entity":
        validate(assertion)
    else:
        with pytest.raises(SkillPackageValidationError, match="validation_output_field_unknown"):
            validate(assertion)


@pytest.mark.parametrize("assertion", [
    {"type": "json_value", "pointer": "/pivot/columns/0", "expected": {"value": 1}},
    {"type": "json_value", "pointer": "/pivot/columns/0/child", "expected": {"value": "entity"}},
    {"type": "json_array_length", "pointer": "/pivot/columns", "length": 0},
    {"type": "json_array_length", "pointer": "/pivot/columns/0", "length": 1},
])
def test_fixed_prefix_cannot_be_replaced_with_value_type_nesting_or_shorter_length(assertion):
    with pytest.raises(SkillPackageValidationError, match="validation_output_field_unknown"):
        validate({"path": "report.json", **assertion})


def test_dynamic_tail_and_scientific_values_remain_independent_oracles():
    validate({"type": "json_value", "path": "report.json", "pointer": "/pivot/columns/1", "expected": {"value": "measure"}})
    validate({"type": "json_array_length", "path": "report.json", "pointer": "/pivot/columns", "length": 1})
    validate({"type": "json_value", "path": "report.json", "pointer": "/pivot/answer/0", "expected": {"value": 123}})


def test_removing_fixed_prefix_changes_the_frozen_public_interface():
    assert content_digest(public_script_interface(SOURCE)) != content_digest(public_script_interface(
        SOURCE.replace('[PRIMARY] + features', 'features')))


GLOBAL_COLUMNS_SOURCE = '''import csv
import json
from pathlib import Path
COLUMNS = ["entity"]

def main(args):
    report = {"pivot": {"columns": COLUMNS}}
    with (Path(args.output) / "wide.csv").open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
    (Path(args.output) / "report.json").write_text(json.dumps(report))
'''


def test_unchanged_global_column_list_keeps_proven_prefix():
    assert columns_shape(GLOBAL_COLUMNS_SOURCE)["column_name_prefix"] == ["entity"]


def test_known_csv_writer_only_reads_the_global_header_list():
    source = GLOBAL_COLUMNS_SOURCE.replace(
        'writer = csv.DictWriter(handle, fieldnames=COLUMNS)\n        writer.writeheader()',
        'writer = csv.writer(handle)\n        writer.writerow(COLUMNS)\n        for row in []:\n            writer.writerow([row[field] for field in COLUMNS])')
    assert columns_shape(source)["column_name_prefix"] == ["entity"]


@pytest.mark.parametrize("mutation", [
    '    COLUMNS[0] = "sample"',
    '    COLUMNS.reverse()',
    '    alias = COLUMNS\n    alias[0] = "sample"',
    '    holder = {"names": COLUMNS}\n    holder["names"][0] = "sample"',
    '    alias = get_columns()\n    alias.append("sample")',
    '    alter_columns(COLUMNS)',
])
def test_visible_global_list_mutation_or_unknown_callee_cannot_prove_original_prefix(mutation):
    source = GLOBAL_COLUMNS_SOURCE.replace('def main(args):',
        'def get_columns():\n    return COLUMNS\n\ndef main(args):\n' + mutation)
    assert "column_name_prefix" not in json.dumps(public_script_interface(source))


@pytest.mark.parametrize("placement", ["helper", "module"])
def test_global_column_mutation_outside_writer_function_invalidates_prefix(placement):
    if placement == "helper":
        source = GLOBAL_COLUMNS_SOURCE.replace('def main(args):',
            'def prepare_columns():\n    COLUMNS[0] = "sample"\n\ndef main(args):\n    prepare_columns()')
    else:
        # Module initialization can run after the function's lexical body.
        source = GLOBAL_COLUMNS_SOURCE + '\nCOLUMNS[0] = "sample"\n'
    assert "column_name_prefix" not in json.dumps(public_script_interface(source))


def test_global_alias_does_not_bypass_original_list_mutation_proof():
    source = GLOBAL_COLUMNS_SOURCE.replace('def main(args):', 'ALIAS = COLUMNS\n\ndef main(args):\n    COLUMNS[0] = "sample"')
    source = source.replace('"columns": COLUMNS', '"columns": ALIAS').replace('fieldnames=COLUMNS', 'fieldnames=ALIAS')
    assert "column_name_prefix" not in json.dumps(public_script_interface(source))


def test_immutable_global_tuple_remains_proven_when_passed_to_reader():
    source = GLOBAL_COLUMNS_SOURCE.replace('["entity"]', '("entity",)').replace(
        'def main(args):', 'def main(args):\n    consume_columns(COLUMNS)')
    assert columns_shape(source)["column_name_prefix"] == ["entity"]


@pytest.mark.parametrize("initial", ['["entity"]', '("entity",)'])
def test_explicit_global_rebinding_invalidates_mutable_and_immutable_prefixes(initial):
    source = GLOBAL_COLUMNS_SOURCE.replace('["entity"]', initial).replace('def main(args):',
        'def replace_columns():\n    global COLUMNS\n    COLUMNS = ("sample",)\n\ndef main(args):\n    replace_columns()')
    assert "column_name_prefix" not in json.dumps(public_script_interface(source))
