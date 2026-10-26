"""One blind program-build repair cannot chase a scientific expected value."""
import copy
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_reference_verification import verify_reference_plan
from test_skill_creation_checkpoint_integration import Gateway, pipeline, verifier
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_reference_revisions import program, responder
from test_skill_reference_verification import host_receipt
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import typed


def build_failure(programs, plan, *, exception_type='TypeError'):
    value = host_receipt(programs, plan)
    value['status'] = 'failed'
    case = value['cases'][0]
    case.pop('checks')
    case.update(status='failed', reason='program_runtime_failure', actual_exit_code=1,
                runtime_error_type=exception_type, input_unchanged=True,
                diagnostic='TypeError: PRIVATE_STDERR_OR_OBSERVED_ANSWER')
    return value


async def run(gateway, execute, *, plan=None, cache=None, declared=None):
    build = pipeline(cache if cache is not None else MemoryCache(), gateway, verifier())
    build.contract = method_contract()
    return await verify_reference_plan(plan or typed(), method_contract(), sources(), declared or interfaces(),
        generate=build.generate, execute=execute, checkpoints=build.checkpoints)


@pytest.mark.asyncio
async def test_only_own_program_and_typed_build_failure_reach_the_one_runtime_repair():
    gateway = Gateway(responder([program(-1), program(0)], ['preserved', 'preserved']))
    plan = typed()
    original = plan.model_dump(mode='json')
    executions = []
    async def execute(programs, candidate):
        executions.append((dict(programs), candidate.model_dump(mode='json')))
        return build_failure(programs, candidate) if len(executions) == 1 else host_receipt(programs, candidate)
    corrected, receipt = await run(gateway, execute, plan=plan)
    assert corrected.model_dump(mode='json') == original == plan.model_dump(mode='json')
    assert len(executions) == 2 and executions[0][1] == executions[1][1] == original
    assert executions[0][0] != executions[1][0]
    first, review, repair, second_review = [payload for _, payload in gateway.calls]
    assert set(repair) == set(first) | {'runtime_repair'}
    assert 'reference_revision' not in repair
    assert repair['runtime_repair'] == {'reference_scripts': review['reference_scripts'], 'failures': [
        {'script_path': 'scripts/group_summary.py', 'case_id': 'group-normal', 'exception_type': 'TypeError'}]}
    for key in ('method_contract', 'sources', 'script_interfaces', 'blind_cases'):
        assert first[key] == repair[key] == second_review[key]
    assert 'PRIVATE_STDERR_OR_OBSERVED_ANSWER' not in json.dumps(repair)
    assert receipt['runtime_build_recovery']['failed_execution']['source_sha256'] != receipt['reference_source_sha256']
    assert receipt['initial_execution']['source_sha256'] == receipt['reference_source_sha256']
    assert receipt['runtime_build_recovery']['failed_execution']['plan_sha256'] == receipt['initial_execution']['plan_sha256']
    assert receipt['status'] == 'verified'


@pytest.mark.asyncio
@pytest.mark.parametrize('exception', ['TypeError', 'NameError', 'UnboundLocalError', 'AttributeError'])
async def test_only_fixed_runtime_error_classes_can_enter_build_repair(exception):
    gateway = Gateway(responder([program(-1), program(0)], ['preserved', 'preserved']))
    calls = []
    async def execute(programs, plan):
        calls.append(programs)
        return build_failure(programs, plan, exception_type=exception) if len(calls) == 1 else host_receipt(programs, plan)
    await run(gateway, execute)
    assert gateway.calls[2][1]['runtime_repair']['failures'][0]['exception_type'] == exception


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', [
    'scientific_failure', 'interface_failure', 'checks_read', 'unsafe_input', 'semantic_exit',
    'unknown_error', 'environment_error', 'timeout', 'signal', 'wrong_hash', 'wrong_plan',
    'duplicate_case', 'missing_case', 'invalid_input_case',
])
async def test_observed_answers_environment_or_untrusted_execution_cannot_trigger_runtime_rewrite(defect):
    gateway = Gateway(responder([program(-1)], ['preserved']))
    def receipt(programs, plan):
        result = build_failure(programs, plan)
        case = result['cases'][0]
        if defect == 'scientific_failure':
            result['cases'][1].update(status='failed', reason='assertion_mismatch')
            result['cases'][1]['checks'][0]['passed'] = False
        elif defect == 'interface_failure': result['cases'][1].update(status='failed', reason='public_output_contract_mismatch')
        elif defect == 'checks_read': case['checks'] = [{'id': 'assertion_1', 'passed': True, 'actual': 98765}]
        elif defect == 'unsafe_input': case['input_unchanged'] = False
        elif defect == 'semantic_exit': case['reason'] = 'exit_code_mismatch'
        elif defect == 'unknown_error': case['runtime_error_type'] = 'ValueError'
        elif defect == 'environment_error': case['runtime_error_type'] = 'ImportError'
        elif defect == 'timeout': case['actual_exit_code'] = 124
        elif defect == 'signal': case['actual_exit_code'] = -9
        elif defect == 'wrong_hash': result['source_sha256'] = {}
        elif defect == 'wrong_plan': result['plan_sha256'] = 'f' * 64
        elif defect == 'duplicate_case': result['cases'].append(copy.deepcopy(case))
        elif defect == 'missing_case': result['cases'].pop()
        elif defect == 'invalid_input_case':
            result['cases'][0] = host_receipt(programs, plan)['cases'][0]
            result['cases'][2].update(status='failed', reason='program_runtime_failure', actual_exit_code=1,
                                      runtime_error_type='TypeError')
        return result
    execute = AsyncMock(side_effect=receipt)
    with pytest.raises(SkillCreationError):
        await run(gateway, execute)
    assert len(gateway.calls) == 2
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_program_crash_does_not_get_another_repair():
    gateway = Gateway(responder([program(-1), program(0)], ['preserved', 'preserved']))
    execute = AsyncMock(side_effect=build_failure)
    with pytest.raises(SkillCreationError):
        await run(gateway, execute)
    assert len(gateway.calls) == 4 and execute.await_count == 2


@pytest.mark.asyncio
async def test_unchanged_runtime_repair_does_not_repoll_review_and_can_be_reauthored_on_retry():
    cache = MemoryCache()
    gateway = Gateway(responder([program(-1), '# comment only\n' + program(-1), program(0)], ['preserved', 'preserved']))
    runs = []
    async def execute(programs, plan):
        runs.append(programs)
        return host_receipt(programs, plan) if set(programs.values()) == {program(0)} else build_failure(programs, plan)
    with pytest.raises(SkillCreationError):
        await run(gateway, execute, cache=cache)
    assert len(gateway.calls) == 3
    assert await builds(cache).get_reference_revision() is None
    _, receipt = await run(gateway, execute, cache=cache)
    assert receipt['status'] == 'verified'
    assert gateway.calls[2][1] == gateway.calls[3][1]
    assert [stage for stage, _ in gateway.calls].count('synthetic_reference_review') == 2


@pytest.mark.asyncio
async def test_negative_review_of_runtime_repair_saves_latest_candidate_and_stops_before_execution():
    cache = MemoryCache()
    gateway = Gateway(responder([program(-1), program(0), program(1)], ['preserved', 'changed', 'preserved']))
    execute = AsyncMock(side_effect=build_failure)
    with pytest.raises(SkillCreationError) as failure:
        await run(gateway, execute, cache=cache)
    assert failure.value.stage == 'synthetic_reference_review'
    execute.assert_awaited_once()
    saved = await builds(cache).get_reference_revision()
    assert saved.reference_scripts[0].content == program(0)
    await run(gateway, AsyncMock(side_effect=host_receipt), cache=cache)
    assert gateway.calls[4][1]['reference_revision']['reference_scripts'][0]['content'] == program(0)
    assert 'runtime_repair' not in gateway.calls[4][1]


@pytest.mark.asyncio
@pytest.mark.parametrize('mixed_interface_failure', [False, True])
async def test_public_contract_is_preserved_and_actual_interface_disagreement_prevents_build_repair(mixed_interface_failure):
    from test_skill_reference_public_columns import declared, source
    from test_skill_reference_public_runtime import observed_receipt, files_with_columns
    from app.domain.services.skills.skill_public_output_contract import public_contract_receipt, failed_public_case, public_rules_for_case
    gateway = Gateway(responder([source(), source() + '\nbuild_repaired = True\n'], ['preserved', 'preserved']))
    executions = []
    async def execute(programs, plan, *, public_output_contract):
        executions.append((plan.model_dump(mode='json'), public_output_contract.model_dump(mode='json')))
        result = observed_receipt(programs, plan, public_output_contract,
            files=lambda case: files_with_columns(case, ['mean'] if mixed_interface_failure and case.kind == 'boundary' else ['id', 'mean']))
        if len(executions) == 1:
            result['cases'][0] = build_failure(programs, plan)['cases'][0]
            result['status'] = 'failed'
            public_cases = result['public_output_verification']['cases']
            public_cases[0].update(status='failed', checks=failed_public_case(
                public_rules_for_case(public_output_contract, plan.scripts[0].script_path, plan.scripts[0].cases[0]),
                reason='public_execution_unconfirmed'))
            result['public_output_verification'] = public_contract_receipt(public_output_contract, plan, public_cases)
        return result
    if mixed_interface_failure:
        with pytest.raises(SkillCreationError):
            await run(gateway, execute, declared=[declared()])
        assert len(executions) == 1 and len(gateway.calls) == 2
    else:
        _, receipt = await run(gateway, execute, declared=[declared()])
        assert receipt['status'] == 'verified'
        assert len(executions) == 2 and executions[0] == executions[1]
        assert len(gateway.calls) == 4


@pytest.mark.asyncio
async def test_scientific_correction_reconfirmation_crash_never_opens_build_recovery():
    gateway = Gateway(responder([program(0)], ['preserved']))
    plan = typed()
    plan.scripts[0].cases[0].assertions[0].fields['mean'].value = 99
    executions = []
    async def execute(programs, candidate):
        executions.append(candidate.model_dump(mode='json'))
        return host_receipt(programs, candidate) if len(executions) == 1 else build_failure(programs, candidate)
    with pytest.raises(SkillCreationError):
        await run(gateway, execute, plan=plan)
    assert len(executions) == 2 and len(gateway.calls) == 2


def test_actual_034_host_receipt_qualifies_without_exposing_successful_scientific_observations():
    from pathlib import Path
    from app.domain.services.skills.skill_reference_verification import _reference_runtime_failures
    from app.domain.services.skills.skill_public_output_contract import PublicOutputContract, validate_public_contract_receipt
    from app.domain.services.skills.skill_validation import SyntheticValidationPlan
    directory = Path(__file__).parent / 'fixtures' / 'skill_creator_public_runtime'
    recorded = json.loads((directory / 'reference034-execution.json').read_text())
    plan = SyntheticValidationPlan.model_validate(recorded['plan'])
    source = (directory / 'reference034.py.txt').read_text()
    programs = {'scripts/longitudinal_report.py': source}
    contract = PublicOutputContract.model_validate(recorded['contract'])
    validate_public_contract_receipt(recorded['receipt'], contract, programs, plan, require_passed=False)
    failures = _reference_runtime_failures(recorded['receipt'], plan, programs)
    assert [failure.model_dump() for failure in failures] == [{
        'script_path': 'scripts/longitudinal_report.py', 'case_id': 'normal_full_methods', 'exception_type': 'TypeError'}]
