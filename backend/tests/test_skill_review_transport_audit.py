"""Independent adversarial checks for closure recovery and sticky decisions."""
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.skill_creation.gateway import _raw_review_negative_code
from app.domain.services.skills.skill_creation.review_transport import (
    ReviewTransportRejected, _tokens, recover_review_closures, review_transport_proof,
)
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_creation_gateway import make_gateway
from test_skill_review_json_boundary import candidate


@pytest.mark.parametrize("kind", ["method", "reference", "oracle"])
def test_every_single_missing_object_closer_either_preserves_the_exact_document_or_is_rejected(kind):
    document, schema, stage, check, _ = candidate(kind)
    answer = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    recovered = 0
    for token in _tokens(answer):
        if token.raw != "}":
            continue
        damaged = answer[:token.start] + answer[token.end:]
        code = _raw_review_negative_code(damaged, schema, stage)
        try:
            value, proof = recover_review_closures(damaged, schema, negative_code=code)
        except ReviewTransportRejected:
            continue
        check(value)
        recovered += 1
        # The independently known original is the only permitted outcome.
        # In particular, no sibling assessment may move to another target.
        assert value.model_dump(mode="json") == document
        assert [item.model_dump() for item in proof.edits] == [
            {"operation": "insert", "offset": token.start, "token": "}"}]
    if kind == "oracle":
        assert recovered > 0


@pytest.mark.parametrize("kind", ["method", "reference", "oracle"])
@pytest.mark.parametrize("change", [
    {"configuration": {"model": "another-independent-review-model"}},
    {"code_version": "review-transport-next-version"},
])
@pytest.mark.asyncio
async def test_recovered_negative_and_proof_remain_sticky_across_provider_and_code_changes(kind, change):
    document, schema, stage, check, _ = candidate(kind)
    gateway, calls, _ = make_gateway([AIMessage(content=json.dumps(document) + "}")])
    async def produce():
        return await gateway.generate(schema, stage, "Review", {}, check)
    producer = AsyncMock(side_effect=produce)
    cache = MemoryCache()
    first = await builds(cache).run(schema, stage, "Review", {}, check, producer,
                                   decision_identity="same-scientific-candidate-and-targets")
    second = await builds(cache, **change).run(schema, stage, "New presentation only", {}, check, producer,
                                             decision_identity="same-scientific-candidate-and-targets")
    assert second.model_dump(mode="json") == first.model_dump(mode="json") == document
    assert review_transport_proof(second) == review_transport_proof(first)
    assert producer.await_count == len(calls) == 1
    assert len([key for key in cache.entries if ":adverse:" in key]) == 1
