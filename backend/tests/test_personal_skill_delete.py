import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.application.errors.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.application.services.skill_deletion_service import SkillDeletionService, get_skill_deletion_service
from app.domain.models.skill import Skill, SkillScope
from app.domain.models.user import User
from app.domain.services.audit_service import get_audit_service
from app.domain.services.skills.registry import SkillRegistry
from app.infrastructure.repositories.skill_reference_repository import MongoSkillReferenceRepository
from app.infrastructure.repositories.skill_repository import MongoSkillRepository
from app.interfaces.api import skill_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers
from test_skill_drafts import create_draft, make_service


class Repository:
    def __init__(self):
        self.rows = {}
        self.fail_delete = False
        self.lost_ack = False
        self.enter_delete = None
        self.release_delete = None

    async def save(self, skill):
        existing = next((item for item in self.rows.values() if item.path == skill.path), None)
        if existing:
            skill.id = existing.id
        self.rows[skill.id] = skill.model_copy(deep=True)
        return skill

    async def get_by_id(self, skill_id):
        skill = self.rows.get(skill_id)
        return skill.model_copy(deep=True) if skill else None

    async def list_accessible(self, owner):
        return [skill.model_copy(deep=True) for skill in self.rows.values()
                if skill.scope == SkillScope.GLOBAL or (skill.owner_user_id or skill.user_id) == owner]

    async def delete_owned(self, skill, owner):
        assert (skill.owner_user_id or skill.user_id) == owner
        if self.enter_delete:
            self.enter_delete.set()
            await self.release_delete.wait()
        if self.fail_delete:
            raise RuntimeError('db unavailable')
        self.rows.pop(skill.id)
        if self.lost_ack:
            raise RuntimeError('lost acknowledgement')


class References:
    def __init__(self, drafts=None):
        self.users = {}
        self.drafts = drafts
        self.fail_detach = False

    async def snapshot(self, skill, owner):
        return copy.deepcopy(self.users), copy.deepcopy(self.drafts.rows if self.drafts else {})

    async def detach(self, skill, owner, snapshot):
        for key, values in self.users.items():
            if key == owner or skill.id in values['installed_skill_ids']:
                values['installed_skill_ids'] = [item for item in values['installed_skill_ids'] if item != skill.id]
                values['auto_enabled_skills'] = [name for name in values['auto_enabled_skills'] if name.strip().casefold() != skill.name.casefold()]
        if self.drafts:
            for draft in self.drafts.rows.values():
                if draft.owner_id == owner and draft.installed_skill_id == skill.id:
                    draft.installed_skill_id = None
        if self.fail_detach:
            raise RuntimeError('reference write failed')

    async def restore(self, skill, owner, snapshot):
        self.users = snapshot[0]
        if self.drafts:
            self.drafts.rows = snapshot[1]


async def setup_skill(tmp_path, *, shared=False):
    repository = Repository()
    registry = SkillRegistry(str(tmp_path / 'skills'), user_id='alice', repository=repository)
    skill = await registry.save_generated_skill(name='report', description='Report', triggers=[], content='# Report',
        user_id='alice', created_from_session_id='session')
    if shared:
        skill = await registry.change_scope(skill, SkillScope.GLOBAL, 'alice')
    references = References()
    references.users = {
        'alice': {'installed_skill_ids': [skill.id, 'other'], 'auto_enabled_skills': [' REPORT ', 'other']},
        'bob': {'installed_skill_ids': ['unrelated'], 'auto_enabled_skills': ['report']},
    }
    return skill, registry, repository, references, SkillDeletionService(repository, references)


@pytest.mark.asyncio
@pytest.mark.parametrize('shared', [False, True])
async def test_owned_delete_removes_package_references_and_never_resurrects(tmp_path, shared):
    skill, registry, repo, refs, service = await setup_skill(tmp_path, shared=shared)
    stale = SkillRegistry(str(tmp_path / 'skills'), user_id='alice', repository=repo)
    await stale.load()
    await service.delete('alice', skill.id, registry)
    assert not Path(skill.path).parent.exists()
    assert await repo.get_by_id(skill.id) is None
    assert refs.users['alice'] == {'installed_skill_ids': ['other'], 'auto_enabled_skills': ['other']}
    assert refs.users['bob']['auto_enabled_skills'] == ['report']
    await stale.sync_files_to_repository()
    assert stale.list_skills() == [] and repo.rows == {}
    reloaded = SkillRegistry(str(tmp_path / 'skills'), user_id='alice', repository=repo)
    await reloaded.load()
    assert reloaded.list_skills() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['other-owner', 'conflicting-owner', 'official', 'missing', 'legacy-owner'])
async def test_owner_authorization_is_not_permission_to_write_shared_skill(tmp_path, mode):
    skill, registry, repo, refs, service = await setup_skill(tmp_path)
    if mode == 'legacy-owner':
        repo.rows[skill.id].owner_user_id = None
        await service.delete('alice', skill.id, registry)
        assert not Path(skill.path).exists()
        return
    if mode == 'conflicting-owner':
        repo.rows[skill.id].owner_user_id = 'bob'
    if mode == 'official':
        repo.rows[skill.id].owner_user_id = repo.rows[skill.id].user_id = None
    with pytest.raises(ForbiddenError if mode == 'official' else NotFoundError):
        await service.delete('bob' if mode == 'other-owner' else 'alice',
                             'missing' if mode == 'missing' else skill.id, registry)
    assert Path(skill.path).exists() and len(repo.rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['outside', 'other-owner-dir', 'root', 'nested', 'package-link', 'file-link', 'trash-link'])
async def test_unsafe_package_paths_cannot_delete_other_files(tmp_path, mode):
    skill, registry, repo, refs, service = await setup_skill(tmp_path)
    original = Path(skill.path)
    if mode == 'outside':
        changed = tmp_path / 'outside' / 'SKILL.md'
    elif mode == 'other-owner-dir':
        changed = registry._user_dir('bob') / 'package' / 'SKILL.md'
    elif mode == 'root':
        changed = registry._user_dir('alice') / 'SKILL.md'
    elif mode == 'nested':
        changed = registry._user_dir('alice') / 'nested' / 'package' / 'SKILL.md'
    elif mode == 'package-link':
        changed = registry._user_dir('alice') / 'link' / 'SKILL.md'
        changed.parent.symlink_to(original.parent, target_is_directory=True)
    elif mode == 'file-link':
        changed = registry._user_dir('alice') / 'linkfile' / 'SKILL.md'
        changed.parent.mkdir()
        changed.symlink_to(original)
    else:
        changed = original
        (original.parent.parent / '.skill-trash').symlink_to(tmp_path, target_is_directory=True)
    if not changed.exists():
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text('# Must survive')
    repo.rows[skill.id].path = str(changed)
    with pytest.raises(BadRequestError):
        await service.delete('alice', skill.id, registry)
    assert changed.exists() and original.exists() and len(repo.rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['database', 'references'])
async def test_delete_failure_restores_files_and_only_removed_references(tmp_path, failure):
    skill, registry, repo, refs, service = await setup_skill(tmp_path)
    original = copy.deepcopy(refs.users)
    repo.fail_delete = failure == 'database'
    refs.fail_detach = failure == 'references'
    with pytest.raises(RuntimeError):
        await service.delete('alice', skill.id, registry)
    assert Path(skill.path).exists() and await repo.get_by_id(skill.id)
    assert refs.users == original
    await registry.load()
    assert registry.get_skill('report')


@pytest.mark.asyncio
async def test_lost_database_ack_does_not_restore_a_committed_deletion(tmp_path):
    skill, registry, repo, refs, service = await setup_skill(tmp_path)
    repo.lost_ack = True
    await service.delete('alice', skill.id, registry)
    assert not Path(skill.path).exists() and repo.rows == {}
    assert refs.users['alice']['installed_skill_ids'] == ['other']


@pytest.mark.asyncio
async def test_concurrent_stale_sync_waits_for_delete_and_cannot_upsert_it(tmp_path):
    skill, registry, repo, refs, service = await setup_skill(tmp_path)
    stale = SkillRegistry(str(tmp_path / 'skills'), user_id='alice', repository=repo)
    await stale.load()
    repo.enter_delete, repo.release_delete = asyncio.Event(), asyncio.Event()
    deletion = asyncio.create_task(service.delete('alice', skill.id, registry))
    await repo.enter_delete.wait()
    sync = asyncio.create_task(stale.sync_files_to_repository())
    await asyncio.sleep(0)
    assert not sync.done()
    repo.release_delete.set()
    await asyncio.gather(deletion, sync)
    assert repo.rows == {} and stale.list_skills() == []


@pytest.mark.asyncio
async def test_disconnected_client_cannot_leave_partial_deletion(tmp_path):
    skill, registry, repo, refs, service = await setup_skill(tmp_path)
    repo.enter_delete, repo.release_delete = asyncio.Event(), asyncio.Event()
    deletion = asyncio.create_task(service.delete('alice', skill.id, registry))
    await repo.enter_delete.wait()
    deletion.cancel()
    repo.release_delete.set()
    with pytest.raises(asyncio.CancelledError):
        await deletion
    assert repo.rows == {} and not Path(skill.path).exists()


@pytest.mark.asyncio
async def test_generated_draft_remains_downloadable_and_requires_explicit_reinstall(tmp_path):
    drafts = make_service()
    draft = await create_draft(drafts)
    repo = Repository()
    registry = SkillRegistry(str(tmp_path / 'skills'), user_id='alice', repository=repo)
    installed = await drafts.install('alice', draft.id, registry)
    refs = References(drafts.repository)
    await SkillDeletionService(repo, refs).delete('alice', installed.installed_skill_id, registry)
    result = await drafts.get('alice', draft.id)
    assert not result.public_view().installed
    original_payload, _ = await drafts.download('alice', draft.id)
    await registry.load()
    assert registry.list_skills() == [] and repo.rows == {}
    again = await drafts.install('alice', draft.id, registry)
    assert again.public_view().installed and len(repo.rows) == 1
    assert (await drafts.download('alice', draft.id))[0] == original_payload
    assert drafts.storage.upload_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('audit_failure', [False, True])
async def test_normal_delete_api_is_owner_only_and_does_not_leak_path(tmp_path, monkeypatch, audit_failure):
    skill, registry, repo, refs, service = await setup_skill(tmp_path)
    current = User(id='alice', fullname='Alice', email='alice@example.test')
    app = FastAPI()
    app.include_router(skill_routes.router, prefix='/api/v1')
    register_exception_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_skill_deletion_service] = lambda: service
    audit = SimpleNamespace(record=AsyncMock(side_effect=RuntimeError("audit unavailable") if audit_failure else None))
    app.dependency_overrides[get_audit_service] = lambda: audit
    monkeypatch.setattr(skill_routes, '_registry', lambda _: registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        current.id = 'bob'
        denied = await client.delete('/api/v1/skills/' + skill.id)
        assert denied.status_code == 404
        current.id = 'alice'
        response = await client.delete('/api/v1/skills/' + skill.id)
        assert response.status_code == 200 and response.json()['data'] == {}
        assert str(tmp_path) not in response.text
    assert audit.record.await_args.kwargs['action'] == 'skill.delete'
    assert 'path' not in audit.record.await_args.kwargs['metadata']


@pytest.mark.asyncio
async def test_reference_repository_uses_atomic_narrow_updates_and_compensation():
    refs = MongoSkillReferenceRepository()
    users, drafts = SimpleNamespace(update_many=AsyncMock(), update_one=AsyncMock()), SimpleNamespace(update_many=AsyncMock())
    class FakeReferences(MongoSkillReferenceRepository):
        @property
        def users(self): return users
        @property
        def drafts(self): return drafts
    refs = FakeReferences()
    skill = Skill(id='id', name='Report.v2+', path='/unused', content='', user_id='alice')
    snapshots = ([{'user_id': 'alice', 'installed_skill_ids': ['id', 'keep'],
                   'auto_enabled_skills': [' REPORT.v2+ ', 'keep']}], [{'_id': 'draft'}])
    await refs.detach(skill, 'alice', snapshots)
    query, update = users.update_many.await_args.args
    assert query == {'user_id': {'$in': ['alice']}}
    assert set(update['$pull']) == {'installed_skill_ids', 'auto_enabled_skills'}
    assert set(update['$set']) == {'updated_at'}
    import re
    pattern = update['$pull']['auto_enabled_skills']['$regex']
    assert re.search(pattern, ' REPORT.v2+ ', re.I) and not re.search(pattern, 'reportXv22', re.I)
    await refs.restore(skill, 'alice', snapshots)
    assert users.update_one.await_args.args == ({'user_id': 'alice'}, {'$addToSet': {
        'installed_skill_ids': 'id', 'auto_enabled_skills': {'$each': [' REPORT.v2+ ']}}})
    assert drafts.update_many.await_args.args[0]['installed_skill_id'] is None


@pytest.mark.asyncio
async def test_database_delete_rechecks_actual_owner_and_path(monkeypatch):
    from app.infrastructure.repositories import skill_repository
    collection = SimpleNamespace(delete_one=AsyncMock(return_value=SimpleNamespace(deleted_count=1)))
    monkeypatch.setattr(skill_repository, 'SkillDocument', SimpleNamespace(get_pymongo_collection=lambda: collection))
    skill = Skill(id='id', name='x', path='/managed/SKILL.md', content='', owner_user_id='alice')
    await MongoSkillRepository().delete_owned(skill, 'alice')
    query = collection.delete_one.await_args.args[0]
    assert query['skill_id'] == skill.id and query['path'] == skill.path
    assert query['$or'] == [{'owner_user_id': 'alice'}, {'owner_user_id': {'$in': [None, '']}, 'user_id': 'alice'}]


@pytest.mark.asyncio
async def test_draft_get_reconciles_stale_installed_marker_without_automatic_install(tmp_path):
    drafts = make_service()
    draft = await create_draft(drafts)
    repo = Repository()
    registry = SkillRegistry(str(tmp_path / 'skills'), user_id='alice', repository=repo)
    installed = await drafts.install('alice', draft.id, registry)
    skill = await repo.get_by_id(installed.installed_skill_id)
    repo.rows.clear()  # Older/admin deletion may not have cleared draft metadata.
    import shutil
    shutil.rmtree(Path(skill.path).parent)
    drafts.skill_repository = repo
    async def clear(owner, draft_id, expected):
        value = drafts.repository.rows[(owner, draft_id)]
        if value.installed_skill_id == expected:
            value.installed_skill_id = None
    drafts.repository.clear_installed = AsyncMock(side_effect=clear)
    refreshed = await drafts.get('alice', draft.id)
    assert not refreshed.public_view().installed
    assert repo.rows == {} and not list(tmp_path.rglob('SKILL.md'))
    drafts.repository.clear_installed.assert_awaited_once_with('alice', draft.id, installed.installed_skill_id)
    assert (await drafts.download('alice', draft.id))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['update', 'delete'])
async def test_admin_mutation_waits_for_personal_delete_before_reading_document(tmp_path, monkeypatch, operation):
    from app.domain.models.user import UserRole
    from app.interfaces.api import admin_routes
    from app.interfaces.schemas.admin import AdminSkillUpdateRequest
    skill, registry, repo, refs, service = await setup_skill(tmp_path)
    class Field:
        def __eq__(self, value): return value
    find = AsyncMock(side_effect=repo.get_by_id)
    monkeypatch.setattr(admin_routes, 'SkillDocument', SimpleNamespace(skill_id=Field(), find_one=find))
    admin = User(id='admin', fullname='Admin', email='admin@example.test', role=UserRole.ADMIN)
    repo.enter_delete, repo.release_delete = asyncio.Event(), asyncio.Event()
    deletion = asyncio.create_task(service.delete('alice', skill.id, registry))
    await repo.enter_delete.wait()
    if operation == 'update':
        pending = admin_routes.update_skill(skill.id, AdminSkillUpdateRequest(), admin, SimpleNamespace(record=AsyncMock()))
    else:
        pending = admin_routes.delete_skill(skill.id, admin, SimpleNamespace(record=AsyncMock()))
    admin_mutation = asyncio.create_task(pending)
    await asyncio.sleep(0)
    assert not admin_mutation.done()
    find.assert_not_awaited()
    repo.release_delete.set()
    await deletion
    with pytest.raises(NotFoundError):
        await admin_mutation
    find.assert_awaited_once_with(skill.id)
    assert repo.rows == {} and not Path(skill.path).exists()
