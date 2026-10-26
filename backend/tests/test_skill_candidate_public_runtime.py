"""The tested and repaired skill must satisfy the same original public schema."""
import json
import hashlib
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills import skill_script_verification as verification
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_reference_public_columns import source
from test_skill_reference_public_runtime import files_with_columns, observed_receipt
from test_skill_script_verification import generation, recipe, revalidate
from test_skill_validation_plan import method_contract, sources


@pytest.fixture
def original_candidate(monkeypatch):
    async def independent_reference(plan, contract, method_sources, interfaces, **kwargs):
        plan = SyntheticValidationPlan.model_validate(plan.model_dump(mode='json'))
        return plan.model_copy(deep=True), {'status': 'isolated_reference_test', 'corrections': [], 'execution': {'cases': []},
            'corrected_plan_sha256': hashlib.sha256(worker._json(plan.model_dump())).hexdigest()}
    monkeypatch.setattr(verification, 'verify_reference_plan', independent_reference)
    candidate = recipe()
    candidate.scripts[0].content = source()
    return candidate


@pytest.mark.asyncio
async def test_candidate_interface_failure_repairs_code_against_unchanged_contract_and_plan(monkeypatch, original_candidate):
    generate, calls = generation()
    executions = []
    async def execute(programs, plan, *, public_output_contract):
        executions.append((dict(programs), plan.model_dump(mode='json'), public_output_contract.model_dump(mode='json')))
        def files(case):
            return files_with_columns(case, ['mean'] if len(executions) == 1 and case.kind == 'boundary' else ['id', 'mean'])
        return observed_receipt(programs, plan, public_output_contract, files=files)
    monkeypatch.setattr(worker, 'validate_skill_scripts', execute)
    review = AsyncMock()
    returned, frozen, receipt = await verification.verify_skill_script_cases(original_candidate, method_contract(), sources(),
        generate=generate, revalidate=revalidate, review=review)
    assert receipt['status'] == 'passed' and receipt['public_output_verification']['status'] == 'passed'
    assert len(executions) == 2 and executions[0][0] != executions[1][0]
    assert executions[0][1] == executions[1][1] == frozen
    assert executions[0][2] == executions[1][2]
    repair = next(call for call in calls if call['stage'] == 'synthetic_script_repair')
    observation = repair['payload']['observed_validation']
    assert observation['public_output_verification']['status'] == 'failed'
    assert 'contract_sha256' not in observation['public_output_verification']
    assert 'source_sha256' not in observation and 'plan_sha256' not in observation
    assert 'public_prefix_mismatch' in json.dumps(observation)
    assert returned.scripts[0].content != original_candidate.scripts[0].content
    review.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_after_repair', [False, True])
async def test_missing_candidate_public_evidence_never_reports_success(monkeypatch, original_candidate, missing_after_repair):
    generate, calls = generation()
    executions = []
    async def execute(programs, plan, *, public_output_contract):
        executions.append(plan.model_dump(mode='json'))
        files = (lambda case: files_with_columns(case, ['mean'])) if missing_after_repair and len(executions) == 1 else files_with_columns
        receipt = observed_receipt(programs, plan, public_output_contract, files=files)
        if not missing_after_repair or len(executions) == 2:
            receipt.pop('public_output_verification')
        return receipt
    monkeypatch.setattr(worker, 'validate_skill_scripts', execute)
    review = AsyncMock()
    with pytest.raises(SkillCreationError) as failure:
        await verification.verify_skill_script_cases(original_candidate, method_contract(), sources(),
            generate=generate, revalidate=revalidate, review=review)
    assert (failure.value.stage, failure.value.code) == ('synthetic_execution', 'synthetic_execution_failed')
    assert len(executions) == (2 if missing_after_repair else 1)
    assert sum(call['stage'] == 'synthetic_script_repair' for call in calls) == int(missing_after_repair)
    if missing_after_repair:
        review.assert_awaited_once()  # review precedes the repaired execution
    else:
        review.assert_not_awaited()
