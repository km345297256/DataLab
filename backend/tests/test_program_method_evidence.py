"""Successful source evidence is owner-bound, private and tied to exact events."""
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.program_attempt import ProgramAttemptView
from app.domain.services.program_method_evidence import private_method_evidence_for_event, validated_method_evidence
from app.infrastructure.models.documents import SessionDocument, SessionEventDocument
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.interfaces.schemas.event import EventMapper


def evidence():
    source = 'print("private-method-source")\n'
    digest = hashlib.sha256(source.encode()).hexdigest()
    return {'schema': 'program-method-evidence/v1', 'source_digest': digest,
            'source_snapshot': {'version': 1, 'encoding': 'utf-8', 'content': source,
                                'size_bytes': len(source.encode()), 'sha256': digest},
            'operation_id': 'host-operation-1', 'script_path': '/home/ubuntu/scripts/model.py'}


def event():
    e = ToolEvent(seq=7, tool_call_id='native-call', tool_name='shell', function_name='program_run',
                  function_args={}, status=ToolStatus.CALLED, function_result={'success': True},
                  program_attempt=ProgramAttemptView(identity='a'*64, state='succeeded', returncode=0))
    e._program_method_evidence = evidence()
    return e


@pytest.mark.asyncio
async def test_private_source_never_enters_model_payload_history_or_sse():
    e = event()
    assert private_method_evidence_for_event(e) == evidence()
    projected = await EventMapper.event_to_sse_event(e)
    assert 'private-method-source' not in e.model_dump_json()
    assert 'private-method-source' not in projected.model_dump_json()
    assert ToolEvent.model_validate(e.model_dump())._program_method_evidence is None


def test_public_tool_result_cannot_claim_private_source_and_failed_attempt_cannot_save_it():
    e = event()
    e._program_method_evidence = None
    e.function_result = {'success': True, 'program_method_evidence': evidence()}
    assert private_method_evidence_for_event(e) is None
    e._program_method_evidence = evidence()
    e.program_attempt = ProgramAttemptView(identity='a'*64, state='failed', returncode=1)
    assert private_method_evidence_for_event(e) is None
    e = event()
    e._program_method_evidence['source_snapshot']['content'] += 'changed'
    assert private_method_evidence_for_event(e) is None


@pytest.mark.asyncio
async def test_private_evidence_is_persisted_beside_immutable_event(monkeypatch):
    stored = {}
    async def insert(query, update, **kwargs):
        stored.update(copy.deepcopy(update['$setOnInsert']))
        return stored
    collection = SimpleNamespace(find_one=AsyncMock(return_value=None), find_one_and_update=insert)
    monkeypatch.setattr(SessionEventDocument, 'get_pymongo_collection', classmethod(lambda cls: collection))
    repo = MongoSessionRepository()
    repo.reserve_event_sequence = AsyncMock(return_value=7)
    repo._mark_user_event_progress = AsyncMock()
    await repo.add_event('session', event())
    assert stored['program_method_evidence'] == evidence()
    assert 'private-method-source' not in json.dumps(stored['event'], default=str)


@pytest.mark.asyncio
async def test_only_owner_and_selected_turn_events_can_read_sources(monkeypatch):
    calls = []
    class Cursor:
        def sort(self, *args): return self
        async def to_list(self, **kwargs):
            return [{'seq': 7, 'program_method_evidence': evidence()}]
    class Events:
        def find(self, query, projection):
            calls.append(query)
            return Cursor()
    async def owner(query, projection):
        return {'_id': 'owner'} if query == {'session_id': 'session', 'user_id': 'owner'} else None
    monkeypatch.setattr(SessionDocument, 'get_pymongo_collection', classmethod(lambda cls: SimpleNamespace(find_one=owner)))
    monkeypatch.setattr(SessionEventDocument, 'get_pymongo_collection', classmethod(lambda cls: Events()))
    repo = MongoSessionRepository()
    assert await repo.get_successful_program_sources('session', 'collaborator', [7]) == []
    assert calls == []
    sources = await repo.get_successful_program_sources('session', 'owner', [7, 7, True, -1])
    assert sources == [{**evidence(), 'event_seq': 7}]
    assert calls[0]['session_id'] == 'session'
    assert calls[0]['seq'] == {'$in': [7]}
