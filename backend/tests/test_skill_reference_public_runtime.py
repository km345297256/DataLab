"""Unknown static shape needs separate real-output evidence, never a new oracle."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills import skill_reference_verification as reference
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.script_public_interface import public_script_interface
from app.domain.services.skills.skill_public_output_contract import (
    check_public_output, public_case_status, public_contract_receipt,
    public_output_path, public_rules_for_case,
)
from test_skill_reference_public_columns import declared, source
from test_skill_reference_verification import host_receipt, method_review, output_files
from test_skill_validation_plan import method_contract, sources
from test_skill_validation_review import typed


def files_with_columns(case, columns=None):
    files = output_files(case)
    files['status.json'] = json.dumps({'status': 'ok', 'columns': columns or ['id', 'mean']}).encode()
    return files


def observed_receipt(programs, plan, contract, *, files=files_with_columns):
    """Use production parsers for a host-side execution fixture."""
    result = host_receipt(programs, plan, files=files)
    cases = []
    for script in plan.scripts:
        if script.support != 'supported':
            continue
        for case in script.cases:
            rules = public_rules_for_case(contract, script.script_path, case)
            if not rules:
                continue
            checks = [check_public_output(rule, files(case)[public_output_path(rule, case)]) for rule in rules]
            status = public_case_status(checks)
            cases.append({'script_path': script.script_path, 'id': case.id, 'status': status, 'checks': checks})
            if status == 'failed':
                for entry in result['cases']:
                    if (entry['script_path'], entry['id']) == (script.script_path, case.id):
                        entry.update(status='failed', reason='public_output_contract_mismatch')
                result['status'] = 'failed'
    result['public_output_verification'] = public_contract_receipt(contract, plan, cases)
    return result


def generate_reference(code=None):
    calls = []
    async def generate(schema, stage, instruction, payload, check):
        calls.append((stage, copy.deepcopy(payload)))
        document = ({**payload['script_request'], 'content': code or source(report_columns='[name for name in columns]')}
                    if stage == 'synthetic_reference_implementation' else method_review(payload))
        value = schema.model_validate(document)
        check(value)
        return value
    return generate, calls


@pytest.mark.asyncio
async def test_unknown_static_shape_executes_once_with_separate_public_contract():
    generate, calls = generate_reference()
    original = typed()
    frozen = original.model_dump(mode='json')
    seen = []
    async def execute(programs, plan, *, public_output_contract):
        seen.append(public_output_contract.model_dump(mode='json'))
        assert plan.model_dump(mode='json') == frozen
        return observed_receipt(programs, plan, public_output_contract)
    result, receipt = await reference.verify_reference_plan(original, method_contract(), sources(), [declared()],
        generate=generate, execute=execute)
    assert result.model_dump(mode='json') == frozen == original.model_dump(mode='json')
    assert receipt['status'] == 'verified' and receipt['corrections'] == []
    assert receipt['execution']['public_output_verification']['status'] == 'passed'
    assert len(seen) == 1 and len(calls) == 2
    assert seen[0]['rules'][0]['prefix'] == ['id', 'mean']
    assert 'public_output_contract' not in calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['missing_receipt', 'prefix_failure', 'contract_hash', 'source_hash', 'case_missing'])
async def test_unconfirmed_public_interface_stops_before_any_scientific_correction(monkeypatch, damage):
    generate, calls = generate_reference()
    correction = AsyncMock()
    # The synchronous correction function must never be entered on this path.
    monkeypatch.setattr(reference, 'correct_reference_expectations', correction)
    executions = []
    async def execute(programs, plan, *, public_output_contract):
        executions.append(plan.model_dump(mode='json'))
        files = (lambda case: files_with_columns(case, ['mean'])) if damage == 'prefix_failure' else files_with_columns
        result = observed_receipt(programs, plan, public_output_contract, files=files)
        if damage == 'missing_receipt': result.pop('public_output_verification')
        elif damage == 'contract_hash': result['public_output_verification']['contract_sha256'] = 'a' * 64
        elif damage == 'source_hash': result['source_sha256'] = {}
        elif damage == 'case_missing': result['public_output_verification']['cases'].pop()
        return result
    with pytest.raises(SkillCreationError) as failure:
        await reference.verify_reference_plan(typed(), method_contract(), sources(), [declared()],
            generate=generate, execute=execute)
    assert (failure.value.stage, failure.value.code) == ('synthetic_reference_execution', 'reference_calculation_unconfirmed')
    correction.assert_not_called()
    assert len(calls) == 2 and len(executions) == 1


@pytest.mark.asyncio
async def test_corrected_science_is_reconfirmed_with_identical_public_contract_and_unmodified_plan_identity():
    generate, calls = generate_reference()
    plan = typed()
    plan.scripts[0].cases[0].assertions[0].fields['mean'].value = 99
    original = plan.model_dump(mode='json')
    seen = []
    async def execute(programs, candidate, *, public_output_contract):
        result = observed_receipt(programs, candidate, public_output_contract)
        seen.append((dict(programs), candidate.model_dump(mode='json'), public_output_contract.model_dump(mode='json'), result))
        return result
    corrected, receipt = await reference.verify_reference_plan(plan, method_contract(), sources(), [declared()],
        generate=generate, execute=execute)
    assert len(seen) == 2 and len(calls) == 2
    assert seen[0][0] == seen[1][0] and seen[0][2] == seen[1][2]
    assert seen[0][1] == original == plan.model_dump(mode='json')
    assert seen[1][1] == corrected.model_dump(mode='json')
    assert receipt['initial_execution']['plan_sha256'] != receipt['execution']['plan_sha256']
    assert receipt['initial_execution']['public_output_verification']['contract_sha256'] == receipt['execution']['public_output_verification']['contract_sha256']
    assert len(receipt['corrections']) == 1 and receipt['corrections'][0]['target_type'] == 'csv_field'


@pytest.mark.asyncio
async def test_second_execution_cannot_drop_or_change_public_interface_receipt():
    generate, calls = generate_reference()
    plan = typed()
    plan.scripts[0].cases[0].assertions[0].fields['mean'].value = 99
    runs = []
    async def execute(programs, candidate, *, public_output_contract):
        runs.append(candidate)
        result = observed_receipt(programs, candidate, public_output_contract)
        if len(runs) == 2: result.pop('public_output_verification')
        return result
    with pytest.raises(SkillCreationError):
        await reference.verify_reference_plan(plan, method_contract(), sources(), [declared()], generate=generate, execute=execute)
    assert len(runs) == 2 and len(calls) == 2


@pytest.mark.asyncio
async def test_public_contract_does_not_reclassify_infrastructure_outage_as_repairable_reference():
    generate, calls = generate_reference()
    execute = AsyncMock(return_value={'status': 'unavailable', 'reason': 'sandbox_image_not_configured'})
    with pytest.raises(SkillCreationError) as failure:
        await reference.verify_reference_plan(typed(), method_contract(), sources(), [declared()], generate=generate, execute=execute)
    assert failure.value.code == 'reference_execution_unavailable'
    assert len(calls) == 2


def test_actual_034_original_source_is_unknown_not_a_proven_column_conflict():
    directory = Path(__file__).parent / 'fixtures' / 'skill_creator_public_runtime'
    code = (directory / 'reference034.py.txt').read_text()
    provenance = json.loads((directory / 'provenance.json').read_text())
    assert hashlib.sha256(code.encode()).hexdigest() == provenance['sha256']
    assert provenance['source_unchanged'] is True
    assert 'payload = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)' in code
    assert 'report_name = resolve_output_name(args.report_name, DEFAULT_REPORT_NAME, "--report-name")' in code
    assert public_script_interface(code)['json_outputs'] == []
    from test_skill_public_json_whitespace import actual_sources
    original_candidate, _ = actual_sources()
    assert reference._validate_reference_column_contract(code, public_script_interface(original_candidate)) is False
    # The unknown static result is deliberately not a claim that execution or
    # scientific calculations succeeded; independent runtime evidence gates it.
