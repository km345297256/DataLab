"""The artificial replay cache persists production records without user Redis."""
import copy
import stat
from unittest.mock import AsyncMock, Mock

import pytest

from acceptance.skill_creator_checkpoint_state import (
    ArtificialCheckpointCache, load_private_state, save_private_state,
)
from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_skill_creation_checkpoints import Artifact, generated, scope


def test_same_artificial_events_preserve_production_privacy_scope_across_explicit_invocations():
    from app.domain.models.event import MessageEvent, DoneEvent, PlanEvent
    from app.domain.models.plan import Plan, Step
    from app.domain.services.skills.skill_creation.privacy import PrivacyContext
    from app.domain.services.skills.skill_creation.checkpoints import content_digest
    from acceptance.skill_creator_real_model import stabilize_artificial_event_identity
    def events():
        return [MessageEvent(role="user", message="Preserve this authored method and input requirement.", seq=1),
            PlanEvent(plan=Plan(goal="Keep this method.", steps=[Step(id="artificial-step")]), status="completed", seq=2), DoneEvent(seq=3)]
    first, second = events(), events()
    assert first[0].id != second[0].id
    def material(values):
        documents = [{key: value for key, value in event.model_dump().items() if key not in {"id", "timestamp"}} for event in values]
        for doc in documents:
            if "plan" in doc:
                doc["plan"].pop("id")
        return documents
    before = material(first)
    metadata = stabilize_artificial_event_identity(first, "complex")
    assert metadata == stabilize_artificial_event_identity(second, "complex")
    after = material(first)
    assert before == after
    privacy = lambda values: content_digest([(entry.category, entry.value) for entry in
        PrivacyContext.collect(values, session_id="artificial-session-complex", user_id="artificial-owner-complex").entries])
    assert privacy(first) == privacy(second)
    stabilize_artificial_event_identity(second, "paired")
    assert privacy(first) != privacy(second)


@pytest.mark.asyncio
async def test_private_artificial_state_roundtrip_revalidates_production_checkpoint(tmp_path):
    path = tmp_path / "private" / "state.json"
    cache = ArtificialCheckpointCache("complex", persist=lambda value: save_private_state(path, value, "complex"))
    producer = AsyncMock(return_value=Artifact(content="Portable scientific method."))
    check = Mock()
    await generated(BuildCheckpoints(cache, scope()), producer, check=check)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    restored = ArtificialCheckpointCache("complex", load_private_state(path, "complex"))
    await generated(BuildCheckpoints(restored, scope()), producer, check=check)
    assert producer.await_count == 1 and check.call_count == 2
    assert restored.stats["hits"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"owner_id": "other"}, {"session_id": "other"},
    {"source": {"method": "different"}}, {"code_version": "new-release"}])
async def test_private_transport_retains_production_scope_isolation(change):
    cache = ArtificialCheckpointCache("complex")
    producer = AsyncMock(return_value=Artifact(content="Portable method."))
    await generated(BuildCheckpoints(cache, scope()), producer)
    restored = ArtificialCheckpointCache("complex", cache.snapshot())
    await generated(BuildCheckpoints(restored, scope(**change)), producer)
    assert producer.await_count == 2


@pytest.mark.asyncio
async def test_artificial_cache_ttl_and_copy_boundary_survive_storage():
    clock = [100]
    cache = ArtificialCheckpointCache("complex", clock=lambda: clock[0])
    key = "datalab:skill-build:v1:artifact:" + "a" * 64
    value = {"answer": [1]}
    await cache.set(key, value, ttl=5)
    value["answer"].append(2)
    read = await cache.get(key)
    read["answer"].append(3)
    restored = ArtificialCheckpointCache("complex", cache.snapshot(), clock=lambda: clock[0])
    assert await restored.get(key) == {"answer": [1]}
    clock[0] = 105
    assert await restored.get(key) is None


@pytest.mark.asyncio
async def test_saved_latest_negative_reference_resumes_in_separate_invocation(tmp_path):
    from test_skill_reference_revisions import Gateway, program, responder, run
    path = tmp_path / "private" / "state.json"
    cache = ArtificialCheckpointCache("complex", persist=lambda value: save_private_state(path, value, "complex"))
    first = Gateway(responder([program(-2), program(-1)], ["changed", "changed"]))
    with pytest.raises(SkillCreationError):
        await run(first, cache)
    restored = ArtificialCheckpointCache("complex", load_private_state(path, "complex"))
    second = Gateway(responder([program(0)], ["preserved"]))
    (_, receipt), _ = await run(second, restored)
    assert receipt["status"] == "verified"
    assert [stage for stage, _ in second.calls] == ["synthetic_reference_implementation", "synthetic_reference_review"]
    assert second.calls[0][1]["reference_revision"]["reference_scripts"][0]["content"] == program(-1)
    assert restored.stats["hits"] > 0


def test_private_state_rejects_other_scenario_world_readable_and_symlink(tmp_path):
    path = tmp_path / "private" / "state.json"
    value = {"version": 1, "scenario": "complex", "entries": {}}
    save_private_state(path, value, "complex")
    with pytest.raises(AssertionError):
        load_private_state(path, "paired")
    path.chmod(0o644)
    with pytest.raises(AssertionError):
        load_private_state(path, "complex")
    path.chmod(0o600)
    link = path.parent / "link.json"
    link.symlink_to(path)
    with pytest.raises(AssertionError):
        load_private_state(link, "complex")
    path.parent.chmod(0o755)
    with pytest.raises(AssertionError):
        save_private_state(path, copy.deepcopy(value), "complex")
