"""One serialized JSON document keeps its schema across static JSON whitespace."""
import hashlib
import json
from pathlib import Path

import pytest

from app.domain.services.skills.script_public_interface import public_script_interface
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_reference_verification import _validate_reference_column_contract
from test_skill_public_column_names import GLOBAL_COLUMNS_SOURCE, validate


def projected(expression, *, preparation="", source=None):
    source = source or GLOBAL_COLUMNS_SOURCE
    source = source.replace('    (Path(args.output) / "report.json").write_text(json.dumps(report))',
        preparation + '    (Path(args.output) / "report.json").write_text(' + expression + ')')
    return public_script_interface(source)


@pytest.mark.parametrize("expression", [
    'json.dumps(report) + "\\n"',
    '"\\t " + json.dumps(report)',
    '"\\r\\n" + (json.dumps(report) + "\\n \\t")',
    'json.dumps(obj=report) + ""',
])
def test_only_json_whitespace_does_not_erase_serialized_structure(expression):
    assert projected(expression) == public_script_interface(GLOBAL_COLUMNS_SOURCE)


def test_read_only_serialization_aliases_keep_shape_without_scientific_values():
    source = GLOBAL_COLUMNS_SOURCE.replace('"columns": COLUMNS',
        '"columns": COLUMNS, "answer": [987654321, "SCIENTIFIC_ANSWER_CANARY"]')
    result = projected('alias + "\\n"', preparation='    text = json.dumps(report)\n    alias = text\n', source=source)
    shape = result['json_outputs'][0]['shape']['properties']['pivot']['properties']
    assert shape['columns']['column_name_prefix'] == ['entity']
    assert shape['answer'] == {'type': 'array', 'items': {'type': 'unknown'}}
    serialized = json.dumps(result)
    assert '987654321' not in serialized and 'SCIENTIFIC_ANSWER_CANARY' not in serialized


def test_imported_json_module_alias_is_proven():
    source = GLOBAL_COLUMNS_SOURCE.replace('import json', 'import json as json_api')
    result = projected('json_api.dumps(report) + "\\n"', source=source)
    assert result['json_outputs'][0]['shape']['properties']['pivot']['properties']['columns']['column_name_prefix'] == ['entity']


@pytest.mark.parametrize("expression", [
    '"{}" + "\\n"',
    '" " + "\\n"',
    'json.dumps(report) + "other"',
    'json.dumps(report) + "\\v"',
    'json.dumps(report) + "\\u00a0"',
    'json.dumps(report) + suffix',
    'json.dumps(report) + json.dumps(report)',
    'json.dumps(report).strip() + "\\n"',
    'json.dumps(report, cls=CustomEncoder) + "\\n"',
    'json.dumps(report, default=custom_serializer) + "\\n"',
    'json.dumps(report, **options) + "\\n"',
    'other.dumps(report) + "\\n"',
])
def test_arbitrary_text_dynamic_suffixes_other_operations_and_multiple_documents_remain_unknown(expression):
    assert projected(expression)['json_outputs'] == []


@pytest.mark.parametrize("preparation", [
    '    text = json.dumps(report)\n    alias = text\n    alias += suffix\n',
    '    text = json.dumps(report)\n    alias = text\n    alias = unknown_text\n',
])
def test_rebound_or_modified_serialization_alias_is_not_read_only(preparation):
    assert projected('alias + "\\n"', preparation=preparation)['json_outputs'] == []


@pytest.mark.parametrize("shadow", ['json = other_module\n', 'def json():\n    pass\n'])
def test_shadowed_json_binding_does_not_authorize_whitespace_projection(shadow):
    source = GLOBAL_COLUMNS_SOURCE.replace('def main(args):', shadow + '\ndef main(args):')
    assert projected('json.dumps(report) + "\\n"', source=source)['json_outputs'] == []


def test_replaced_json_serializer_cannot_prove_standard_json_structure():
    source = GLOBAL_COLUMNS_SOURCE.replace('def main(args):', 'json.dumps = arbitrary_text\n\ndef main(args):')
    assert projected('json.dumps(report) + "\\n"', source=source)['json_outputs'] == []


def test_whitespace_expression_depth_is_bounded():
    expression = 'json.dumps(report)' + ' + "\\n"' * 80
    assert projected(expression)['json_outputs'] == []


def actual_sources():
    directory = Path(__file__).parent / 'fixtures' / 'skill_creator_json_whitespace'
    provenance = json.loads((directory / 'provenance.json').read_text())
    assert provenance['source_unchanged'] is True
    material = {}
    for entry in provenance['files']:
        source = (directory / entry['file']).read_text()
        assert hashlib.sha256(source.encode()).hexdigest() == entry['sha256']
        material[entry['artifact']] = source
    return material['022-method_review-request.json'], material['038-synthetic_reference_review-request.json']


def test_actual_022_candidate_recovers_json_contract_while_038_needs_runtime_prefix_evidence():
    candidate, reference = actual_sources()
    assert 'report_path.write_text(report_text + "\\n", encoding="utf-8")' in candidate
    interface = public_script_interface(candidate)
    assert len(interface['json_outputs']) == 1
    output = interface['json_outputs'][0]
    assert output['path'] == '@output/report.json' and output['filename_argument'] == '--report-name'
    pivot = output['shape']['properties']['pivot']['properties']
    assert pivot['columns']['column_name_prefix'] == ['id']
    _validate_reference_column_contract(candidate, interface)
    assert _validate_reference_column_contract(reference, interface) is False
    # Dynamic columns do not statically prove presence or absence of 'id'.
    # The separate host contract still rejects outputs whose prefix omits it.
    from app.domain.services.skills.skill_public_output_contract import compile_public_output_contract, check_public_output
    declared = {**interface, 'script_path': 'scripts/longitudinal_report.py',
                'input_contract': [], 'output_contract': [],
                'arguments': [flag for option in interface['cli_options'] for flag in option['flags']]}
    contract = compile_public_output_contract([declared])
    rule = next(rule for rule in contract.rules if rule.prefix == ['id'])
    assert check_public_output(rule, b'{"pivot":{"columns":["x","z"]}}')['status'] == 'failed'
    # The same extracted public contract also constrains plan authoring.
    validate({'type': 'json_value', 'path': 'report.json', 'pointer': '/pivot/columns/0',
              'expected': {'value': 'id'}}, source=candidate)
    with pytest.raises(SkillPackageValidationError, match='validation_output_field_unknown'):
        validate({'type': 'json_value', 'path': 'report.json', 'pointer': '/pivot/columns/0',
                  'expected': {'value': 'feature'}}, source=candidate)
