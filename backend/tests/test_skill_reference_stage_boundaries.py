"""Reference failures cannot authorize changes to the tested implementation."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_skill_creation_gateway import make_gateway
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_creation_pipeline import review_payload
from test_skill_creation_source_response import SOURCE, METADATA
from app.domain.services.skills.skill_creation.models import HelperScript


@pytest.mark.asyncio
async def test_reference_rejection_survives_bad_explanation_and_retry_without_becoming_method_revision():
    candidate = review_payload()
    candidate["requirements"][0].update(status="missing", explanation="")
    positive = deepcopy(candidate)
    positive["requirements"][0].update(status="preserved", explanation="The complete mathematical requirement is preserved.")
    gateway, calls, _ = make_gateway([candidate, positive, positive])
    async def produce():
        return await gateway.generate(MethodReview, "synthetic_reference_review", "Review reference independently.", {})
    producer, cache = AsyncMock(side_effect=produce), MemoryCache()
    for _ in range(2):
        with pytest.raises(SkillCreationError) as failure:
            await builds(cache).run(MethodReview, "synthetic_reference_review", "Review reference.", {}, None,
                                    producer, decision_identity="same-independent-reference")
        assert failure.value.code == "reference_calculation_unconfirmed"
        assert failure.value.stage == "synthetic_reference_review"
    producer.assert_awaited_once()
    assert len(calls) == 3


@pytest.mark.parametrize("stage,code", [
    ("synthetic_reference_review", "reference_calculation_unconfirmed"),
    ("synthetic_reference_execution", "reference_execution_unavailable"),
])
def test_reference_error_identifies_unconfirmed_test_basis_without_claiming_tested_code_failed(stage, code):
    error = SkillCreationError(stage, code)
    assert "参考" in error.public_message
    assert "脚本在合成用例中运行或结果校验失败" not in error.public_message


@pytest.mark.asyncio
async def test_unconfirmed_reference_authoring_is_not_a_scientific_negative_vote():
    cache = MemoryCache()
    candidate = HelperScript.model_validate({**METADATA, "content": SOURCE})
    producer = AsyncMock(side_effect=[SkillCreationError("synthetic_reference_implementation",
                                                       "reference_calculation_unconfirmed"), candidate])
    with pytest.raises(SkillCreationError):
        await builds(cache).run(HelperScript, "synthetic_reference_implementation", "Create reference.", {}, None, producer)
    recovered = await builds(cache).run(HelperScript, "synthetic_reference_implementation", "Create reference.", {}, None, producer)
    assert recovered == candidate and producer.await_count == 2
