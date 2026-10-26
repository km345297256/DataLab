"""Durable work survives refresh/restart and cannot publish after cancellation."""
import asyncio
import copy
import errno
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient
from pymongo.errors import DuplicateKeyError

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.data_library_service import DataLibraryService
from app.application.services.open_data_service import OpenDataService, run_open_data_worker
from app.application.services.open_data_relevance import relevance_fingerprint
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.file import FileInfo
from app.domain.models.open_data import AcquisitionInput, SearchInput
from app.infrastructure.repositories.open_data_repository import OpenDataRepository
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.infrastructure.models.documents import SessionDocument
from app.interfaces.api import open_data_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers


def field(row, path):
    for part in path.split("."):
        if not isinstance(row, dict):
            return None
        row = row.get(part)
    return row


def matches(row, query):
    for key, wanted in query.items():
        if key == "$or":
            if not any(matches(row, item) for item in wanted):
                return False
            continue
        value = field(row, key)
        if isinstance(wanted, dict):
            for op, operand in wanted.items():
                if op == "$in" and value not in operand:
                    return False
                if op == "$lte" and (value is None or value > operand):
                    return False
                if op == "$ne" and value == operand:
                    return False
                if op == "$exists" and (value is not None) != operand:
                    return False
        elif value != wanted:
            return False
    return True


def change(row, update):
    for key, value in update.get("$set", {}).items():
        parts, target = key.split("."), row
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = copy.deepcopy(value)
    for key, value in update.get("$inc", {}).items():
        row[key] = row.get(key, 0) + value
    for key in update.get("$unset", {}):
        row.pop(key, None)


class Cursor:
    def __init__(self, rows): self.rows = copy.deepcopy(rows)
    def sort(self, key, direction):
        self.rows.sort(key=lambda row: field(row, key), reverse=direction < 0)
        return self
    def limit(self, count): self.rows = self.rows[:count]; return self
    async def to_list(self, length): return self.rows[:length]


class Collection:
    def __init__(self): self.rows = {}
    async def create_index(self, *args, **kwargs): pass
    async def insert_one(self, row):
        if row["_id"] in self.rows: raise DuplicateKeyError("duplicate")
        self.rows[row["_id"]] = copy.deepcopy(row)
    async def find_one(self, query):
        return copy.deepcopy(next((row for row in self.rows.values() if matches(row, query)), None))
    def find(self, query): return Cursor([row for row in self.rows.values() if matches(row, query)])
    async def update_one(self, query, update):
        row = next((row for row in self.rows.values() if matches(row, query)), None)
        if row is not None: change(row, update)
        return SimpleNamespace(matched_count=int(row is not None))
    async def find_one_and_update(self, query, update, **kwargs):
        rows = [row for row in self.rows.values() if matches(row, query)]
        if kwargs.get("sort"):
            key, direction = kwargs["sort"][0]
            rows.sort(key=lambda row: field(row, key), reverse=direction < 0)
        if not rows: return None
        change(rows[0], update)
        return copy.deepcopy(rows[0])
    async def replace_one(self, query, row, **kwargs):
        existing = self.rows.get(row["_id"])
        if existing is not None and not matches(existing, query):
            if kwargs.get("upsert"):
                raise DuplicateKeyError("duplicate")
            return SimpleNamespace(matched_count=0)
        self.rows[row["_id"]] = copy.deepcopy(row)
        return SimpleNamespace(matched_count=int(existing is not None))


class Database(dict):
    def __missing__(self, key): self[key] = Collection(); return self[key]


class Files:
    def __init__(self, database):
        self.database, self.infos, self.contents = database, {}, {}
        self.crash_once = False
        self.uploads = 0
    async def upload_file(self, stream, filename, owner, mime, metadata):
        key, data = "minio:" + uuid4().hex, stream.read()
        self.uploads += 1
        info = FileInfo(file_id=key, filename=filename, user_id=owner, size=len(data), content_type=mime,
                        upload_date=datetime.now(UTC), metadata=metadata, file_path="/srv/private/file",
                        file_url="http://minio.internal/private")
        self.infos[key], self.contents[key] = info, data
        await self.database["stored_files"].insert_one(dict(_id=key, file_id=key, user_id=owner, metadata=metadata))
        if self.crash_once:
            self.crash_once = False
            raise asyncio.CancelledError()
        return info.model_copy(deep=True)
    async def get_file_info(self, key, owner):
        info = self.infos.get(key)
        return info.model_copy(deep=True) if info and info.user_id == owner else None
    async def download_file_range(self, key, owner, *, offset, length):
        info = await self.get_file_info(key, owner)
        if info is None: raise FileNotFoundError()
        return self.contents[key][offset:offset + length], info
    async def download_file(self, key, owner):
        info = await self.get_file_info(key, owner)
        if info is None: raise FileNotFoundError()
        return BytesIO(self.contents[key]), info


class LibraryRepository:
    def __init__(self): self.rows, self.receipts, self.claims = {}, {}, set()
    async def retain_file(self, owner, file_id, asset_id): self.claims.add((owner, file_id, asset_id)); return True
    async def release_file(self, owner, file_id, asset_id): self.claims.discard((owner, file_id, asset_id))
    async def acquisitions(self, owner, files): return {key: self.receipts[(owner, key)] for key in files if (owner, key) in self.receipts}
    async def merge_acquisition(self, owner, payload):
        saved = self.rows.setdefault((owner, payload["id"]), copy.deepcopy(payload))
        merged = {item["file_id"]: item for item in saved["files"] + payload["files"]}
        saved["files"] = list(merged.values())
        return copy.deepcopy(saved)


class Sources:
    def __init__(self):
        self.calls, self.search_calls = [], []
        self.verify_calls = []
        self.fail = set()
        self.block = False
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.transfer_keys, self.cleaned, self.streams = [], [], []
    def catalog(self): return [dict(id="zenodo", title="Zenodo"), dict(id="worldbank", title="World Bank")]
    async def search(self, source, query, cursor):
        self.search_calls.append((source, query, cursor))
        if source in self.fail: raise SourceConnectorError("upstream_unavailable")
        return dict(results=[dict(external_id=cursor or "first", title="Data", description="location /srv/private/secret",
            source_url="https://zenodo.org/records/123?token=SECRET", access="needs_resolution",
            metadata={"token": "SECRET", "path": "/srv/private"}, coverage={"storage_path": "/srv/private"})],
            next_cursor="second" if not cursor else "")
    async def resolve(self, candidate):
        return [dict(id="file-" + str(i), filename=f"table{i}.csv", size=None, content_type="text/csv", available=True,
                     reason="", url="https://zenodo.org/private?token=SECRET") for i in range(2)]
    async def verify(self, candidate):
        self.verify_calls.append(candidate["external_id"])
        return await self.resolve(candidate)
    async def verify_all(self, candidate):
        return await self.verify(candidate)
    async def download(self, candidate, option, progress, *, transfer_key=None):
        self.calls.append(option["id"])
        self.transfer_keys.append((option["id"], transfer_key))
        if self.block:
            self.started.set()
            await self.release.wait()
        if option["id"] in self.fail: raise SourceConnectorError("download_timeout_or_unavailable")
        data = ("x,y\n1," + option["id"] + "\n").encode()
        await progress(len(data), None)
        stream = BytesIO(data)
        self.streams.append(stream)
        return stream, option["filename"], "text/csv", dict(sha256=hashlib.sha256(data).hexdigest(),
            source_url=candidate["source_url"], connector_id="zenodo", source="data_connector")
    async def cleanup_transfer(self, key):
        assert all(stream.closed for stream in self.streams)
        self.cleaned.append(key)


@pytest.mark.asyncio
async def test_source_specific_queries_survive_worker_restart_and_pagination(env):
    async def multilingual(query, sources, **kwargs):
        return dict(search_query="education", source_ids=["zenodo"], notes=[],
                    source_queries={"zenodo": "教育", "worldbank": "education employment", "evil": "ignored"})
    env.service.planner = multilingual
    created = await env.service.create_search("alice", "s", SearchInput(query="教育与就业", source_ids=["zenodo", "worldbank"]))
    await env.service.process_next()
    first = await env.service.get_search("alice", "s", created.id)
    assert {item.id for item in first.sources} == {"zenodo", "worldbank"}
    assert {(source, query) for source, query, _ in env.sources.search_calls} == {
        ("zenodo", "教育"), ("worldbank", "education employment")}
    restored = env.new_service()
    await restored.continue_search("alice", "s", created.id)
    await restored.process_next()
    assert {(source, query) for source, query, cursor in env.sources.search_calls if cursor} == {
        ("zenodo", "教育"), ("worldbank", "education employment")}


@pytest.mark.asyncio
async def test_duplicate_doi_requires_identical_verified_file_set_and_keeps_acquisition(env):
    original = env.sources.search
    async def indexed(*args):
        result = await original(*args)
        result["next_cursor"] = ""
        result["results"][0]["metadata"]["doi"] = "10.5281/zenodo.123"
        return result
    env.sources.search = indexed
    async def files(candidate):
        return [dict(id="file-0", filename="data.csv", size=12, checksum="md5:" + "a" * 32,
                     available=True, source_id="zenodo", external_id="123")]
    env.sources.verify = files
    created = await env.service.create_search("alice", "s", SearchInput(query="climate"))
    await env.service.process_next()
    result = await env.service.get_search("alice", "s", created.id)
    assert len(result.results) == 1
    candidate = result.results[0]
    job = await env.service.create_acquisition("alice", "s", created.id,
        AcquisitionInput(candidate_id=candidate.id, file_ids=["file-0"]))
    assert job.candidate_id == candidate.id
    assert sum(source.count for source in result.sources) == 1


def test_dedup_does_not_merge_versions_subsets_or_title_only_matches():
    from app.application.services.open_data_service import verified_dataset_identity
    candidate = {"metadata": {"doi": "https://doi.org/10.5281/zenodo.123"}}
    one = [{"source_id": "harvard-dataverse", "external_id": "doi:10.7910/DVN/ABCDE", "id": "v1-file1",
            "checksum": "md5:" + "a" * 32, "size": 10}]
    two = one + [{**one[0], "id": "v1-file2"}]
    key = verified_dataset_identity(candidate, one)
    assert key and key != verified_dataset_identity(candidate, two)
    assert key != verified_dataset_identity({"metadata": {"doi": "10.5281/zenodo.124"}}, one)
    assert key != verified_dataset_identity(candidate, [{**one[0], "id": "v2-file1"}])
    assert verified_dataset_identity({"title": "Same title"}, one) is None
    assert verified_dataset_identity(candidate, [{"id": "unqualified-file"}]) is None


async def planner(query, sources, **kwargs): return dict(search_query=query, source_ids=sources, notes=[])


async def related_candidates(query, candidates, **kwargs):
    # Storage/probe lifecycle tests isolate the separately tested semantic model.
    return {item["id"]: {**relevance_fingerprint(query, item), "status": "related", "score": 3,
        "reason": "测试数据符合本次研究需求。", "evidence": [{"field": "title", "quote": item["title"]}]}
        for item in candidates}


def relevance_decisions(query, candidates, statuses):
    return {item["id"]: {**relevance_fingerprint(query, item),
        "status": statuses[item["external_id"]][0], "score": statuses[item["external_id"]][1],
        "reason": "元数据提供了所需主题的研究材料。", "evidence": [{"field": "title", "quote": item["title"]}]}
        for item in candidates}


@pytest.fixture
def env():
    database = Database()
    repository, files, sources, library_repo = OpenDataRepository(database), Files(database), Sources(), LibraryRepository()
    owned = {("alice", "s"): SimpleNamespace(files=[], title=None), ("alice", "s2"): SimpleNamespace(files=[], title=None)}
    async def find(session, owner): return owned.get((owner, session))
    async def upsert(session, owner, info):
        current = owned[(owner, session)]
        current.files = [file for file in current.files if file.file_id != info.file_id] + [info]
    async def receipt(owner, session, key, payload): library_repo.receipts[(owner, payload["file_id"])] = payload
    async def title(session, owner, value):
        if owned[(owner, session)].title is None:
            owned[(owner, session)].title = value
    sessions = SimpleNamespace(find_owned_by_id_and_user_id=find, upsert_connector_file=upsert, ensure_discovery_title=title)
    provenance = SimpleNamespace(save_acquisition=receipt)
    library = DataLibraryService(library_repo, files, sessions)
    def service(): return OpenDataService(repository, sources, files, sessions, provenance, library, planner, related_candidates)
    return SimpleNamespace(database=database, repository=repository, sources=sources, files=files,
                           library_repo=library_repo, service=service(), new_service=service)


async def prepared(env, *, files=("file-0", "file-1")):
    search = await env.service.create_search("alice", "s", SearchInput(query="climate", source_ids=["zenodo"]))
    assert search.status == "queued"
    await env.service.process_next()
    search = await env.service.get_search("alice", "s", search.id)
    candidate = search.results[0]
    options = await env.service.resolve("alice", "s", search.id, candidate.id)
    request = AcquisitionInput(candidate_id=candidate.id, file_ids=list(files))
    job = await env.service.create_acquisition("alice", "s", search.id, request)
    return search, options, job, request


@pytest.mark.asyncio
async def test_large_selection_is_fully_verified_downloaded_and_archived_without_count_cap(env):
    count = 43
    options = [dict(id=f"file-{i}", filename=f"table{i}.csv", size=None,
                    content_type="text/csv", available=True, reason="") for i in range(count)]
    complete_calls = []
    active = maximum = 0
    original_info = env.service.library._info
    async def bounded_info(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0)
            return await original_info(*args, **kwargs)
        finally:
            active -= 1
    env.service.library._info = bounded_info
    async def quick(candidate):
        return copy.deepcopy(options[:6])
    async def complete(candidate):
        complete_calls.append(candidate["id"])
        return copy.deepcopy(options)
    env.sources.verify, env.sources.verify_all = quick, complete
    search, resolved, job, _ = await prepared(env, files=[item["id"] for item in options])
    assert len(search.results) == 1 and search.results[0].available_file_count == 6
    assert len(resolved.files) == len(job.items) == count
    assert resolved.limits["max_files"] is None and complete_calls == [job.candidate_id]
    await env.new_service().process_next()
    final = await env.service.get_acquisition("alice", "s", job.id)
    assert final.status == "ready" and final.error is None and final.asset_id
    assert len(final.items) == len(final.files) == env.files.uploads == count
    assert set(env.sources.calls) == {item["id"] for item in options}
    assert all(item.status == "ready" for item in final.items)
    assert len(env.sources.cleaned) == count
    assert maximum == 4 and active == 0
    assert len(env.library_repo.rows) == 1
    saved = next(iter(env.library_repo.rows.values()))
    assert len(saved["files"]) == len(env.library_repo.receipts) == count
    assert {file["file_id"] for file in saved["files"]} == {file.file_id for file in final.files}
    session = await env.service.sessions.find_owned_by_id_and_user_id("s", "alice")
    assert {file.file_id for file in session.files} == {file.file_id for file in final.files}
    for file in final.files:
        data = env.files.contents[file.file_id]
        assert data.startswith(b"x,y\n1,file-") and data.endswith(b"\n")
        receipt = env.library_repo.receipts[("alice", file.file_id)]
        assert receipt["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_complete_resolve_does_not_cut_off_at_500_and_rejects_unproven_selection(env):
    options = [dict(id=f"file-{i}", filename=f"data{i}.csv", available=True) for i in range(503)]
    async def complete(candidate):
        return options + [dict(id="not-readable", filename="private.csv", available=False)]
    env.sources.verify_all = complete
    search, resolved, job, _ = await prepared(env, files=["file-502"])
    assert len(resolved.files) == 503 and job.items[0].id == "file-502"
    for forged in ("not-readable", "not-in-manifest"):
        request = AcquisitionInput(candidate_id=job.candidate_id,
            file_ids=[f"file-{i}" for i in range(43)] + [forged])
        with pytest.raises(BadRequestError, match="文件选择已失效"):
            await env.service.create_acquisition("alice", "s", search.id, request)
    assert len(await env.service.list_acquisitions("alice", "s")) == 1


@pytest.mark.asyncio
async def test_complete_resolve_exposes_pending_count_without_private_probe_details(env):
    async def complete(candidate):
        return [dict(id="file-0", filename="data.csv", available=True,
            url="https://zenodo.org/private", _verification_summary={
                "verification_pending_count": 1, "unavailable_file_count": 2})]
    env.sources.verify_all = complete
    _, resolved, _, _ = await prepared(env, files=["file-0"])
    assert resolved.verification_pending_count == 1 and resolved.unavailable_file_count == 2
    assert len(resolved.files) == 1
    assert "_verification_summary" not in resolved.model_dump_json() and "url" not in resolved.model_dump_json()


def test_large_acquisition_still_requires_unique_nonempty_file_selection():
    from pydantic import ValidationError
    for files in ([], [f"file-{i}" for i in range(43)] + ["file-42"]):
        with pytest.raises(ValidationError):
            AcquisitionInput(candidate_id="candidate", file_ids=files)


@pytest.mark.asyncio
async def test_search_restart_pagination_and_private_metadata_projection(env):
    env.sources.fail.add("worldbank")
    search = await env.service.create_search("alice", "s", SearchInput(query="climate"))
    restarted = env.new_service()
    assert (await restarted.list_searches("alice", "s"))[0].id == search.id
    await restarted.process_next()
    first = await restarted.get_search("alice", "s", search.id)
    assert first.status == "partial" and first.has_more
    encoded = first.model_dump_json()
    assert "SECRET" not in encoded and "/srv/private" not in encoded and "metadata" not in encoded
    env.sources.fail.clear()
    await restarted.continue_search("alice", "s", search.id)
    await env.new_service().process_next()
    final = await restarted.get_search("alice", "s", search.id)
    assert final.status == "ready" and len(final.results) == 3
    assert ("zenodo", "climate", "second") in env.sources.search_calls
    assert len({item.id for item in final.results}) == 3


@pytest.mark.asyncio
async def test_partial_download_retry_keeps_success_and_one_library_asset(env):
    search, options, job, request = await prepared(env)
    assert options.limits["max_files"] is None and "url" not in options.model_dump_json()
    assert options.limits["max_file_bytes"] is None and options.limits["max_total_bytes"] is None
    env.sources.fail.add("file-1")
    await env.service.process_next()
    partial = await env.service.get_acquisition("alice", "s", job.id)
    assert partial.status == "partial" and len(partial.files) == 1 and partial.asset_id
    assert "file_path" not in partial.model_dump_json() and "minio.internal" not in partial.model_dump_json()
    assert (await env.service.create_acquisition("alice", "s", search.id, request)).id == job.id
    env.sources.fail.clear()
    await env.service.retry_acquisition("alice", "s", job.id)
    await env.new_service().process_next()
    final = await env.service.get_acquisition("alice", "s", job.id)
    assert final.status == "ready" and len(final.files) == 2 and final.asset_id == partial.asset_id
    assert env.sources.calls.count("file-0") == 1 and env.files.uploads == 2
    assert len(env.library_repo.rows) == 1
    assert len(next(iter(env.library_repo.rows.values()))["files"]) == 2
    keys = dict(env.sources.transfer_keys)
    assert all(key == keys[file_id] for file_id, key in env.sources.transfer_keys)
    assert set(env.sources.cleaned) == set(keys.values())


@pytest.mark.asyncio
async def test_upload_checkpoint_gap_recovers_object_after_worker_restart(env):
    _, _, job, _ = await prepared(env, files=("file-0",))
    env.files.crash_once = True
    with pytest.raises(asyncio.CancelledError): await env.service.process_next()
    stored = env.database["open_data_tasks"].rows["acquisition:" + job.id]
    assert stored["status"] == "downloading" and env.files.uploads == 1
    assert not env.sources.cleaned and all(stream.closed for stream in env.sources.streams)
    stored["lease_until"] = 0
    await env.new_service().process_next()
    done = await env.service.get_acquisition("alice", "s", job.id)
    assert done.status == "ready" and env.files.uploads == 1 and env.sources.calls == ["file-0"]
    assert env.sources.cleaned == [env.sources.transfer_keys[0][1]]


@pytest.mark.asyncio
async def test_cancel_fences_inflight_download_and_retry_can_resume(env):
    _, _, job, _ = await prepared(env, files=("file-0",))
    env.sources.block = True
    task = asyncio.create_task(env.service.process_next())
    await env.sources.started.wait()
    cancelled = await env.service.cancel_acquisition("alice", "s", job.id)
    assert cancelled.status == "cancelled"
    env.sources.release.set()
    await task
    assert (await env.service.get_acquisition("alice", "s", job.id)).status == "cancelled"
    assert env.files.uploads == 0
    assert not env.sources.cleaned
    env.sources.block = False
    await env.service.retry_acquisition("alice", "s", job.id)
    await env.service.process_next()
    assert (await env.service.get_acquisition("alice", "s", job.id)).status == "ready"
    assert env.sources.transfer_keys[0] == env.sources.transfer_keys[1]
    assert env.sources.cleaned == [env.sources.transfer_keys[0][1]]


@pytest.mark.asyncio
async def test_atomic_claim_and_expired_lease_reject_old_worker(env):
    search = await env.service.create_search("alice", "s", SearchInput(query="rain", source_ids=["zenodo"]))
    claims = await asyncio.gather(env.repository.claim(), env.repository.claim())
    assert sum(item is not None for item in claims) == 1
    old = next(item for item in claims if item is not None)
    env.database["open_data_tasks"].rows["search:" + search.id]["lease_until"] = 0
    new = await env.repository.claim()
    assert new["lease_token"] != old["lease_token"]
    stale = copy.deepcopy(old["payload"])
    stale["status"] = "ready"
    assert not await env.repository.checkpoint(old, stale)
    assert not await env.repository.renew(old)


@pytest.mark.asyncio
async def test_owner_scope_and_invalid_selection(env):
    search, _, job, request = await prepared(env, files=("file-0",))
    for owner, session in (("bob", "s"), ("administrator", "s"), ("alice", "s2")):
        with pytest.raises(NotFoundError): await env.service.get_search(owner, session, search.id)
        with pytest.raises(NotFoundError): await env.service.get_acquisition(owner, session, job.id)
        with pytest.raises(NotFoundError): await env.service.continue_search(owner, session, search.id)
        with pytest.raises(NotFoundError): await env.service.resolve(owner, session, search.id, request.candidate_id)
        with pytest.raises(NotFoundError): await env.service.create_acquisition(owner, session, search.id, request)
        with pytest.raises(NotFoundError): await env.service.cancel_acquisition(owner, session, job.id)
        with pytest.raises(NotFoundError): await env.service.retry_acquisition(owner, session, job.id)
    assert (await env.service.get_acquisition("alice", "s", job.id)).status == "queued"
    with pytest.raises(BadRequestError):
        await env.service.create_acquisition("alice", "s", search.id, AcquisitionInput(candidate_id=job.candidate_id, file_ids=["forged"]))


@pytest.mark.asyncio
async def test_routes_return_durable_ids_and_owner_scoped_results(env):
    app = FastAPI()
    app.include_router(open_data_routes.router)
    register_exception_handlers(app)
    async def user(x_user: str = Header("alice")): return SimpleNamespace(id=x_user)
    app.dependency_overrides[get_current_user] = user
    app.dependency_overrides[open_data_routes.get_open_data_service] = lambda: env.service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/sessions/s/data-searches", json={"query": "weather", "source_ids": ["zenodo"]})
        assert created.status_code == 200 and created.json()["data"]["status"] == "queued"
        sid = created.json()["data"]["id"]
        assert (await client.get(f"/sessions/s/data-searches/{sid}", headers={"x-user": "bob"})).status_code == 404
        assert (await client.post("/sessions/s/data-searches", json={"query": "x", "url": "http://localhost"})).status_code == 422
        assert (await client.get("/sessions/s/data-searches")).json()["data"][0]["id"] == sid


@pytest.mark.asyncio
async def test_query_plan_preserves_explicit_sources_and_original_long_query(env):
    plans = []
    async def selected(query, sources, **kwargs):
        plans.append((query, sources, kwargs))
        return dict(search_query=query, source_ids=["zenodo", "unknown"], notes=[])
    env.service.planner = selected
    query = "climate " * 100
    search = await env.service.create_search("alice", "s", SearchInput(query=query, source_ids=["zenodo", "worldbank"]))
    await env.service.process_next()
    found = await env.service.get_search("alice", "s", search.id)
    assert found.query == query.strip() and len(found.effective_query) == 500
    assert any("500" in note for note in found.notes)
    assert {source.id for source in found.sources} == {"zenodo", "worldbank"}
    assert all(len(call[1]) == 500 for call in env.sources.search_calls)
    await env.service.continue_search("alice", "s", search.id)
    await env.service.process_next()
    assert len(plans) == 1 and plans[0][2] == dict(user_id="alice", session_id="s", explicit_sources=True)
    automatic = await env.service.create_search("alice", "s", SearchInput(query="rain"))
    await env.service.process_next()
    assert [source.id for source in (await env.service.get_search("alice", "s", automatic.id)).sources] == ["zenodo"]
    refinement = await env.service.create_search("alice", "s", SearchInput(query="rain monthly", parent_id=search.id))
    await env.service.process_next()
    assert {source.id for source in (await env.service.get_search("alice", "s", refinement.id)).sources} == {"zenodo", "worldbank"}
    automatic_refinement = await env.service.create_search("alice", "s", SearchInput(query="weather", parent_id=automatic.id))
    # An automatic parent leaves all catalog sources available to fresh planning.
    assert {source.id for source in automatic_refinement.sources} == {"zenodo", "worldbank"}


@pytest.mark.asyncio
async def test_source_warning_is_persisted_as_partial_with_visible_explanation(env):
    original = env.sources.search
    async def warned(*args):
        page = await original(*args)
        return {**page, "warnings": ["此来源仅返回首批指标，覆盖尚不完整。"]}
    env.sources.search = warned
    search = await env.service.create_search("alice", "s", SearchInput(query="rain", source_ids=["worldbank"]))
    await env.service.process_next()
    result = await env.new_service().get_search("alice", "s", search.id)
    assert result.status == "partial" and result.results
    assert result.sources[0].status == "partial"
    assert result.sources[0].warnings[0] in result.notes


@pytest.mark.asyncio
async def test_cancel_after_one_success_keeps_personal_asset(env):
    _, _, job, _ = await prepared(env)
    original = env.sources.download
    async def block_second(candidate, option, progress, *, transfer_key=None):
        if option["id"] == "file-1":
            env.sources.block = True
        return await original(candidate, option, progress, transfer_key=transfer_key)
    env.sources.download = block_second
    task = asyncio.create_task(env.service.process_next())
    await asyncio.wait_for(env.sources.started.wait(), 2)
    current = await env.service.cancel_acquisition("alice", "s", job.id)
    assert current.status == "cancelled" and len(current.files) == 1 and current.asset_id
    assert len(env.library_repo.rows) == 1
    env.sources.release.set()
    await task
    assert env.files.uploads == 1
    assert (await env.service.get_acquisition("alice", "s", job.id)).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_during_receipt_does_not_publish_stale_session_or_success(env):
    _, _, job, _ = await prepared(env, files=("file-0",))
    started, release = asyncio.Event(), asyncio.Event()
    original = env.service.provenance.save_acquisition
    async def blocked(*args):
        started.set()
        await release.wait()
        await original(*args)
    env.service.provenance.save_acquisition = blocked
    task = asyncio.create_task(env.service.process_next())
    await asyncio.wait_for(started.wait(), 2)
    await env.service.cancel_acquisition("alice", "s", job.id)
    release.set()
    await task
    current = await env.service.get_acquisition("alice", "s", job.id)
    assert current.status == "cancelled" and not current.files and not env.library_repo.rows
    session = await env.service.sessions.find_owned_by_id_and_user_id("s", "alice")
    assert not session.files
    # The completed upload/receipt remains recoverable; resuming does not repeat it.
    await env.service.retry_acquisition("alice", "s", job.id)
    await env.service.process_next()
    assert (await env.service.get_acquisition("alice", "s", job.id)).status == "ready"
    assert env.files.uploads == 1 and env.sources.calls == ["file-0"]


@pytest.mark.asyncio
async def test_recovery_rehash_rejects_same_size_modified_object(env):
    _, _, job, _ = await prepared(env, files=("file-0",))
    env.files.crash_once = True
    with pytest.raises(asyncio.CancelledError):
        await env.service.process_next()
    key = next(iter(env.files.contents))
    data = env.files.contents[key]
    env.files.contents[key] = b"!" + data[1:]
    env.database["open_data_tasks"].rows["acquisition:" + job.id]["lease_until"] = 0
    await env.new_service().process_next()
    failed = await env.service.get_acquisition("alice", "s", job.id)
    assert failed.status == "failed" and "校验失败" in failed.items[0].message
    assert not env.library_repo.receipts and not env.library_repo.rows


@pytest.mark.asyncio
async def test_library_failure_retries_save_without_redownload(env):
    _, _, job, _ = await prepared(env, files=("file-0",))
    original = env.service.library.create
    async def unavailable(*args, **kwargs):
        raise OSError("temporary database failure")
    env.service.library.create = unavailable
    await env.service.process_next()
    partial = await env.service.get_acquisition("alice", "s", job.id)
    assert partial.status == "partial" and partial.files and partial.error
    env.service.library.create = original
    await env.service.retry_acquisition("alice", "s", job.id)
    await env.service.process_next()
    done = await env.service.get_acquisition("alice", "s", job.id)
    assert done.status == "ready" and done.asset_id and not done.error
    assert env.files.uploads == 1 and env.sources.calls == ["file-0"]


@pytest.mark.asyncio
async def test_worker_retries_initialization_and_runs_bounded_pool():
    class WorkerService:
        def __init__(self):
            self.repository = self
            self.index_calls = self.active = self.maximum = 0
            self.all_started = asyncio.Event()
        async def ensure_indexes(self):
            self.index_calls += 1
            if self.index_calls == 1:
                raise OSError("temporary startup database failure")
        async def process_next(self):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 3:
                self.all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.active -= 1
    service = WorkerService()
    task = asyncio.create_task(run_open_data_worker(service=service, concurrency=3))
    try:
        await asyncio.wait_for(service.all_started.wait(), 5)
        assert service.index_calls == 2 and service.maximum == 3
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service.active == 0


@pytest.mark.asyncio
async def test_discovery_title_only_sets_empty_owned_nonmanual_session(monkeypatch):
    collection = Collection()
    for session, owner, title, manual in (("empty", "alice", None, False), ("named", "alice", "My title", False),
            ("manual", "alice", "", True), ("foreign", "bob", None, False)):
        await collection.insert_one(dict(_id=session, session_id=session, user_id=owner, title=title, title_manually_set=manual))
    monkeypatch.setattr(SessionDocument, "get_pymongo_collection", lambda: collection)
    repository = MongoSessionRepository()
    for session in collection.rows:
        await repository.ensure_discovery_title(session, "alice", "Rainfall discovery")
    assert collection.rows["empty"]["title"] == "Rainfall discovery"
    assert collection.rows["named"]["title"] == "My title"
    assert collection.rows["manual"]["title"] == "" and collection.rows["foreign"]["title"] is None


@pytest.mark.asyncio
async def test_materialized_file_recovery_respects_owner_and_storage_id_conventions(env):
    from bson import ObjectId
    from app.infrastructure.external.file.hybridfile import HybridFileStorage
    object_id = ObjectId()
    await env.database["fs.files"].insert_one(dict(_id=object_id, metadata=dict(user_id="alice", open_data_item_key="grid")))
    key = await env.repository.find_materialized_file("alice", "grid")
    assert key == str(object_id)
    minio, gridfs = object(), object()
    assert HybridFileStorage(minio, gridfs)._select_storage(key) is gridfs
    await env.database["stored_files"].insert_one(dict(_id="bad", file_id="dataset:private", user_id="alice", metadata=dict(open_data_item_key="bad")))
    assert await env.repository.find_materialized_file("alice", "bad") is None
    assert await env.repository.find_materialized_file("bob", "grid") is None


def raw_candidate(external_id="record", **changes):
    return dict(external_id=external_id, title="Observations", source_url="https://zenodo.org/records/123",
                access="downloadable", **changes)


@pytest.mark.asyncio
async def test_search_admits_only_probed_files_and_refills_empty_pages_with_cursor(env):
    pages = []
    async def search(source, query, cursor):
        page = int(cursor or "1")
        pages.append(page)
        return dict(results=[raw_candidate(str(page), availability_verified_at=datetime.now(UTC), available_file_count=999,
            _availability_verification={"version": 1, "method": "registered_source_probe", "checked_at": datetime.now(UTC)})], next_cursor=str(page + 1))
    async def verify(candidate):
        if candidate["external_id"] != "3":
            return []
        options = await env.sources.resolve(candidate)
        options[0]["system_notes"] = ["仅核验部分文件，不代表仓库中的全部数据。"]
        return options
    env.sources.search, env.sources.verify = search, verify
    created = await env.service.create_search("alice", "s", SearchInput(query="climate", source_ids=["zenodo"]))
    assert not created.results
    await env.service.process_next()
    result = await env.service.get_search("alice", "s", created.id)
    assert pages == [1, 2, 3] and result.has_more and len(result.results) == 1
    candidate = result.results[0]
    assert candidate.external_id == "3" and candidate.access == "downloadable"
    assert candidate.availability_verified_at and candidate.available_file_count == 2
    assert "仅核验部分文件，不代表仓库中的全部数据。" in candidate.system_notes
    assert "仅核验部分文件，不代表仓库中的全部数据。" not in candidate.limitations
    assert result.sources[0].count == 1
    assert "_availability_verification" not in result.model_dump_json() and "url" not in (await env.service.resolve("alice", "s", result.id, candidate.id)).model_dump_json()
    # Successful discovery already saved its verified choices; no second list
    # request is needed to submit a known option through the service contract.
    job = await env.service.create_acquisition("alice", "s", result.id, AcquisitionInput(candidate_id=candidate.id, file_ids=["file-0"]))
    assert job.status == "queued"


@pytest.mark.asyncio
async def test_raw_downloadable_label_and_provider_forged_marker_cannot_pass_gate(env):
    async def search(*args):
        return dict(results=[raw_candidate(availability_verified_at=datetime.now(UTC), available_file_count=10,
            _availability_verification={"version": 1, "method": "registered_source_probe", "checked_at": datetime.now(UTC)})], next_cursor="")
    async def verify(candidate): return []
    env.sources.search, env.sources.verify = search, verify
    created = await env.service.create_search("alice", "s", SearchInput(query="climate", source_ids=["zenodo"]))
    await env.service.process_next()
    result = await env.service.get_search("alice", "s", created.id)
    assert result.status == "ready" and not result.results and result.sources[0].count == 0
    assert not result.has_more and any("元数据线索不计入结果" in note for note in result.notes)


@pytest.mark.asyncio
async def test_transient_probe_failure_is_retryable_and_does_not_fetch_more_pages(env):
    calls = 0
    async def verify(candidate):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SourceConnectorError("rate_limited")
        return await env.sources.resolve(candidate)
    env.sources.verify = verify
    created = await env.service.create_search("alice", "s", SearchInput(query="climate", source_ids=["zenodo"]))
    await env.service.process_next()
    partial = await env.service.get_search("alice", "s", created.id)
    assert partial.status == "partial" and not partial.results and partial.has_more
    assert len(env.sources.search_calls) == 1 and "限流" in partial.sources[0].error
    await env.service.continue_search("alice", "s", created.id)
    await env.new_service().process_next()
    complete = await env.service.get_search("alice", "s", created.id)
    assert complete.status == "ready" and len(complete.results) == 1 and complete.has_more
    assert len(env.sources.search_calls) == 1 and calls == 2


@pytest.mark.asyncio
async def test_verified_other_source_is_persisted_while_slow_source_is_still_probing(env):
    started, release, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def search(source, query, cursor): return dict(results=[raw_candidate(source)], next_cursor="")
    async def verify(candidate):
        if candidate["source_id"] == "zenodo":
            started.set()
            await release.wait()
        return await env.sources.resolve(candidate)
    checkpoint = env.repository.checkpoint
    async def observed(row, payload):
        result = await checkpoint(row, payload)
        if result and any(item.get("source_id") == "worldbank" for item in payload.get("results", [])):
            published.set()
        return result
    env.sources.search, env.sources.verify, env.repository.checkpoint = search, verify, observed
    created = await env.service.create_search("alice", "s", SearchInput(query="climate"))
    task = asyncio.create_task(env.service.process_next())
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(published.wait(), 2)
        progress = await env.service.get_search("alice", "s", created.id)
        assert progress.status == "searching" and [item.source_id for item in progress.results] == ["worldbank"]
        assert next(item for item in progress.sources if item.id == "worldbank").count == 1
    finally:
        release.set()
        await task
    final = await env.service.get_search("alice", "s", created.id)
    assert final.status == "ready" and len(final.results) == 2


@pytest.mark.asyncio
async def test_verification_pool_is_bounded_and_cancels_children_on_shutdown(env):
    active = maximum = 0
    full = asyncio.Event()
    async def search(source, query, cursor): return dict(results=[raw_candidate(str(i)) for i in range(12)], next_cursor="")
    async def verify(candidate):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 5:
            full.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
    env.sources.search, env.sources.verify = search, verify
    created = await env.service.create_search("alice", "s", SearchInput(query="climate", source_ids=["zenodo"]))
    task = asyncio.create_task(env.service.process_next())
    await asyncio.wait_for(full.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert maximum == 5 and active == 0
    stored = env.database["open_data_tasks"].rows["search:" + created.id]
    assert len(stored["payload"]["_verification_pending"]["zenodo"]) == 12
    assert not (await env.service.get_search("alice", "s", created.id)).results


@pytest.mark.asyncio
async def test_probe_deadline_preserves_pending_page_for_continuation(env, monkeypatch):
    active = 0
    async def verify(candidate):
        nonlocal active
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
    env.sources.verify = verify
    monkeypatch.setattr("app.application.services.open_data_service.SOURCE_ROUND_TIMEOUT", 0.03)
    created = await env.service.create_search("alice", "s", SearchInput(query="climate", source_ids=["zenodo"]))
    await env.service.process_next()
    partial = await env.service.get_search("alice", "s", created.id)
    assert partial.status == "partial" and partial.has_more and not partial.results and active == 0
    assert len(env.sources.search_calls) == 1
    env.sources.verify = env.sources.resolve
    await env.service.continue_search("alice", "s", created.id)
    await env.service.process_next()
    assert len((await env.service.get_search("alice", "s", created.id)).results) == 1
    assert len(env.sources.search_calls) == 1


@pytest.mark.asyncio
async def test_restart_resumes_durable_unverified_page_without_repeating_search(env):
    started = asyncio.Event()
    async def blocked(candidate):
        started.set()
        await asyncio.Event().wait()
    env.sources.verify = blocked
    created = await env.service.create_search("alice", "s", SearchInput(query="climate", source_ids=["zenodo"]))
    task = asyncio.create_task(env.service.process_next())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = env.database["open_data_tasks"].rows["search:" + created.id]
    assert stored["payload"]["_cursors"]["zenodo"] == "second" and stored["payload"]["_verification_pending"]["zenodo"]
    stored["lease_until"] = 0
    env.sources.verify = env.sources.resolve
    await env.new_service().process_next()
    result = await env.service.get_search("alice", "s", created.id)
    assert result.status == "ready" and len(result.results) == 1 and result.has_more
    assert len(env.sources.search_calls) == 1


@pytest.mark.asyncio
async def test_legacy_projection_hides_unverified_candidate_but_preserves_old_files(env):
    search, _, job, _ = await prepared(env, files=("file-0",))
    await env.service.process_next()
    ready = await env.service.get_acquisition("alice", "s", job.id)
    assert ready.files and ready.asset_id
    stored = env.database["open_data_tasks"].rows["search:" + search.id]
    for candidate in stored["payload"]["results"]:
        candidate.pop("_availability_verification", None)
    single = await env.service.get_search("alice", "s", search.id)
    listed = (await env.service.list_searches("alice", "s"))[0]
    assert not single.results and not listed.results and single.sources[0].count == listed.sources[0].count == 0
    assert single.has_more and any("旧版" in note for note in single.notes)
    assert (await env.service.list_acquisitions("alice", "s", search.id))[0].files == ready.files
    queued = await env.service.continue_search("alice", "s", search.id)
    assert queued.status == "queued" and not queued.results and queued.sources[0].count == 0
    await env.service.process_next()
    verified = await env.service.get_search("alice", "s", search.id)
    assert len(verified.results) == 1 and verified.results[0].availability_verified_at
    assert (await env.service.get_acquisition("alice", "s", job.id)).files == ready.files


@pytest.mark.asyncio
async def test_resolve_reverification_failure_revokes_stale_options_and_public_result(env):
    search, _, job, request = await prepared(env, files=("file-0",))
    async def unavailable(candidate): raise SourceConnectorError("invalid_response")
    env.sources.verify = unavailable
    with pytest.raises(BadRequestError):
        await env.service.resolve("alice", "s", search.id, job.candidate_id)
    result = await env.service.get_search("alice", "s", search.id)
    assert not result.results and result.sources[0].count == 0 and result.has_more
    assert await env.repository.get_options("alice", "s", search.id, job.candidate_id) == []
    with pytest.raises(BadRequestError):
        await env.service.create_acquisition("alice", "s", search.id, request)
    # Existing durable work remains independently readable after a candidate
    # disappears from the current source-verification projection.
    assert (await env.service.get_acquisition("alice", "s", job.id)).id == job.id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [SourceConnectorError("rate_limited"),
    SourceConnectorError("upstream_unavailable"), SourceConnectorError("download_timeout_or_unavailable"), TimeoutError()])
async def test_transient_resolve_keeps_retryable_card_without_renewing_expired_proof(env, failure):
    search, _, job, _ = await prepared(env, files=("file-0",))
    previous_verify = env.sources.verify
    proof = (await env.repository.get_verified_options("alice", "s", search.id, [job.candidate_id]))[job.candidate_id]
    checked_at = datetime.now(UTC) - timedelta(minutes=6)
    await env.repository.put_options("alice", "s", search.id, job.candidate_id, proof["options"],
        verified_at=checked_at, generation=proof["generation"], proof_token=proof["proof_token"])
    async def unavailable(candidate):
        raise failure
    env.sources.verify = unavailable
    with pytest.raises(BadRequestError):
        await env.service.resolve("alice", "s", search.id, job.candidate_id)
    visible = await env.service.get_search("alice", "s", search.id)
    listed = (await env.service.list_searches("alice", "s"))[0]
    assert visible.results[0].id == listed.results[0].id == job.candidate_id
    assert visible.results[0].availability_verified_at == checked_at
    retained = (await env.repository.get_verified_options("alice", "s", search.id, [job.candidate_id]))[job.candidate_id]
    assert retained["options"] == proof["options"] and retained["verified_at"] == checked_at
    selection = AcquisitionInput(candidate_id=job.candidate_id, file_ids=["file-1"])
    with pytest.raises(BadRequestError):
        await env.service.create_acquisition("alice", "s", search.id, selection)
    assert len(await env.service.list_acquisitions("alice", "s")) == 1
    env.sources.verify = previous_verify
    resolved = await env.service.resolve("alice", "s", search.id, job.candidate_id)
    assert len(resolved.files) == 2
    retry = await env.service.create_acquisition("alice", "s", search.id, selection)
    assert retry.id != job.id and retry.status == "queued"


@pytest.mark.asyncio
async def test_confirmed_empty_resolution_revokes_old_proof(env):
    search, _, job, request = await prepared(env, files=("file-0",))
    async def empty(candidate):
        return []
    env.sources.verify = empty
    assert not (await env.service.resolve("alice", "s", search.id, job.candidate_id)).files
    assert not (await env.service.get_search("alice", "s", search.id)).results
    with pytest.raises(BadRequestError):
        await env.service.create_acquisition("alice", "s", search.id, request)


@pytest.mark.asyncio
async def test_delayed_expired_worker_cannot_replace_new_generation_options(env):
    entered, release = asyncio.Event(), asyncio.Event()
    put_options, writes = env.repository.put_options, 0
    async def delayed(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            entered.set()
            await release.wait()
        return await put_options(*args, **kwargs)
    async def old_verify(candidate):
        return [{**(await env.sources.resolve(candidate))[0], "id": "old-file"}]
    async def new_verify(candidate):
        return [{**(await env.sources.resolve(candidate))[0], "id": "new-file"}]
    env.repository.put_options, env.sources.verify = delayed, old_verify
    search = await env.service.create_search("alice", "s", SearchInput(query="climate", source_ids=["zenodo"]))
    old_worker = asyncio.create_task(env.service.process_next())
    await asyncio.wait_for(entered.wait(), 2)
    env.database["open_data_tasks"].rows["search:" + search.id]["lease_until"] = 0
    env.sources.verify = new_verify
    try:
        await env.new_service().process_next()
        current = await env.service.get_search("alice", "s", search.id)
        assert current.status == "ready" and len(current.results) == 1
    finally:
        release.set()
        await old_worker
    candidate = current.results[0]
    options = await env.repository.get_options("alice", "s", search.id, candidate.id)
    assert [item["id"] for item in options] == ["new-file"]
    with pytest.raises(BadRequestError):
        await env.service.create_acquisition("alice", "s", search.id, AcquisitionInput(candidate_id=candidate.id, file_ids=["old-file"]))
    job = await env.service.create_acquisition("alice", "s", search.id, AcquisitionInput(candidate_id=candidate.id, file_ids=["new-file"]))
    assert job.status == "queued"


@pytest.mark.asyncio
async def test_uncommitted_probe_token_cannot_advertise_or_authorize_options(env):
    search, _, job, request = await prepared(env, files=("file-0",))
    row = await env.repository.get("search", "alice", "s", search.id)
    options = await env.repository.get_options("alice", "s", search.id, job.candidate_id)
    assert await env.repository.put_options("alice", "s", search.id, job.candidate_id, options,
        verified_at=datetime.now(UTC), generation=row["lease_generation"], proof_token=uuid4().hex)
    result = await env.service.get_search("alice", "s", search.id)
    assert not result.results and result.sources[0].count == 0
    with pytest.raises(BadRequestError):
        await env.service.create_acquisition("alice", "s", search.id, request)


@pytest.mark.asyncio
async def test_candidate_size_formats_and_latest_summary_come_from_verified_files(env):
    async def search(*args):
        return dict(results=[raw_candidate(size=10 * 1024**3, formats=["pdf", "zip"])], next_cursor="")
    async def csv(candidate):
        return [dict(id="data", filename="observations.CSV", size=1024, available=True, reason="", content_type="text/csv")]
    env.sources.search, env.sources.verify = search, csv
    created = await env.service.create_search("alice", "s", SearchInput(query="climate", source_ids=["zenodo"]))
    await env.service.process_next()
    candidate = (await env.service.get_search("alice", "s", created.id)).results[0]
    assert candidate.size == 1024 and candidate.formats == ["csv"] and candidate.available_file_count == 1
    async def snapshot(candidate):
        return [dict(id="data", filename="observations.json", size=None, available=True, reason="", content_type="application/json")]
    env.sources.verify = snapshot
    await env.service.resolve("alice", "s", created.id, candidate.id)
    updated = (await env.service.get_search("alice", "s", created.id)).results[0]
    assert updated.size is None and updated.formats == ["json"] and updated.available_file_count == 1


def test_verification_timestamp_from_mongo_keeps_utc_in_browser_contract():
    from app.domain.models.open_data import OpenDataCandidate
    candidate = OpenDataCandidate(id="record", source_id="zenodo", external_id="123", title="Data",
        source_url="https://zenodo.org/records/123", availability_verified_at=datetime(2026, 10, 21, 2, 15))
    assert candidate.model_dump(mode="json")["availability_verified_at"] == "2026-10-21T02:15:00Z"


@pytest.mark.asyncio
@pytest.mark.parametrize("known_sizes", [True, False])
async def test_large_files_and_batch_stream_without_fixed_byte_limit(env, tmp_path, known_sizes):
    """Real 65 MiB streams and a >128 MiB job, without allocating whole files."""
    size = 65 * 1024**2 + 17
    path = tmp_path / "observations.csv"
    with path.open("wb") as stream:
        stream.write(b"x,y\n")
        stream.truncate(size)
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            sha.update(chunk)
    resolve = env.sources.resolve
    async def verify(candidate):
        return [{**item, "size": size if known_sizes else None} for item in await resolve(candidate)]
    env.sources.verify = verify
    streams = []
    class BoundedReader:
        def __init__(self): self.stream = path.open("rb")
        def read(self, length=-1):
            assert 0 < length <= 1024**2, "acquisition must not read an entire large file"
            return self.stream.read(length)
        def seek(self, *args): return self.stream.seek(*args)
        def close(self): self.stream.close()
        @property
        def closed(self): return self.stream.closed
    async def download(candidate, option, progress, *, transfer_key=None):
        assert transfer_key and len(transfer_key) == 32
        env.sources.transfer_keys.append((option["id"], transfer_key))
        await progress(size, size if known_sizes else None)
        stream = BoundedReader()
        streams.append(stream)
        return stream, option["filename"], "text/csv", dict(sha256=sha.hexdigest(), source_url=candidate["source_url"])
    async def upload(stream, filename, owner, mime, metadata):
        digest, received = hashlib.sha256(), 0
        while chunk := stream.read(1024**2):
            received += len(chunk)
            digest.update(chunk)
        assert received == size and digest.hexdigest() == sha.hexdigest()
        key = "minio:" + uuid4().hex
        info = FileInfo(file_id=key, filename=filename, user_id=owner, size=received, content_type=mime,
                        upload_date=datetime.now(UTC), metadata=metadata)
        env.files.infos[key], env.files.contents[key] = info, b"x,y\n"
        env.files.uploads += 1
        await env.database["stored_files"].insert_one(dict(_id=key, file_id=key, user_id=owner, metadata=metadata))
        return info
    async def ranged(key, owner, *, offset, length):
        info = await env.files.get_file_info(key, owner)
        with path.open("rb") as stream:
            stream.seek(offset)
            return stream.read(length), info
    env.sources.download, env.files.upload_file = download, upload
    env.files.download_file_range = ranged
    _, resolved, job, _ = await prepared(env)
    assert resolved.limits == {"max_file_bytes": None, "max_total_bytes": None, "max_files": None}
    await env.service.process_next()
    done = await env.service.get_acquisition("alice", "s", job.id)
    assert done.status == "ready" and len(done.files) == 2 and done.asset_id
    assert all(file.size > 64 * 1024**2 for file in done.files)
    assert sum(file.size for file in done.files) > 128 * 1024**2
    assert all(stream.closed for stream in streams) and env.files.uploads == 2
    assert set(env.sources.cleaned) == {key for _, key in env.sources.transfer_keys}


@pytest.mark.asyncio
async def test_no_arbitrary_limit_when_verifying_large_known_options(env):
    async def verify(candidate):
        return [{**item, "size": 2**50} for item in await env.sources.resolve(candidate)]
    env.sources.verify = verify
    search, resolved, job, _ = await prepared(env)
    candidate = search.results[0]
    assert candidate.size == 2**51 and candidate.available_file_count == 2
    assert all(item.size == 2**50 and item.available for item in resolved.files)
    assert job.status == "queued" and all(item.size == 2**50 for item in job.items)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [SourceConnectorError("storage_insufficient"), OSError(errno.ENOSPC, "disk full")])
async def test_resource_shortage_is_retryable_and_keeps_transfer_identity(env, error):
    _, _, job, _ = await prepared(env, files=("file-0",))
    original = env.sources.download
    seen = []
    async def shortage(candidate, option, progress, *, transfer_key=None):
        seen.append(transfer_key)
        raise error
    env.sources.download = shortage
    await env.service.process_next()
    failed = await env.service.get_acquisition("alice", "s", job.id)
    assert failed.status == "failed" and "不足" in failed.items[0].message
    assert "释放空间后重试" in failed.items[0].message and not env.sources.cleaned
    env.sources.download = original
    await env.service.retry_acquisition("alice", "s", job.id)
    await env.new_service().process_next()
    assert (await env.service.get_acquisition("alice", "s", job.id)).status == "ready"
    assert seen == [env.sources.transfer_keys[0][1]] and env.sources.cleaned == seen


@pytest.mark.asyncio
async def test_cleanup_only_after_file_receipt_session_and_ready_are_durable(env):
    _, _, job, _ = await prepared(env, files=("file-0",))
    original = env.sources.cleanup_transfer
    async def cleanup(key):
        row = await env.repository.get("acquisition", "alice", "s", job.id)
        item = row["payload"]["items"][0]
        assert item["status"] == "ready" and item["_storage_key"] == key
        file_id = item["file"]["file_id"]
        assert ("alice", file_id) in env.library_repo.receipts
        session = await env.service.sessions.find_owned_by_id_and_user_id("s", "alice")
        assert file_id in {file.file_id for file in session.files}
        await original(key)
    env.sources.cleanup_transfer = cleanup
    await env.service.process_next()
    assert (await env.service.get_acquisition("alice", "s", job.id)).status == "ready"
    assert env.sources.cleaned == [env.sources.transfer_keys[0][1]]


@pytest.mark.asyncio
async def test_legacy_system_notes_migrate_without_relabelling_source_restrictions(env):
    search, _, _, _ = await prepared(env, files=("file-0",))
    saved = env.database["open_data_tasks"].rows["search:" + search.id]["payload"]["results"][0]
    saved["limitations"] = ["仅覆盖某一地区的观测数据。", "当前已核验 2 个可获取文件；来源记录的标题与说明可能描述更大的数据集合。",
        "单文件最多 64 MiB；双端测序需保留配对文件。", "读取样本仅确认当前可读；完整性、校验和及实际分析适用性仍需获取后确认。"]
    result = await env.service.get_search("alice", "s", search.id)
    candidate = result.results[0]
    assert candidate.limitations == ["仅覆盖某一地区的观测数据。", "双端测序需保留配对文件。"]
    assert candidate.system_notes[0] == "已核验 2 个可获取文件"
    assert any(note.startswith("读取样本") for note in candidate.system_notes)
    assert "64 MiB" not in result.model_dump_json() and "来源记录的标题" not in result.model_dump_json()
    assert (await env.service.list_searches("alice", "s"))[0].results[0] == candidate


@pytest.mark.asyncio
async def test_restart_after_ready_checkpoint_cleans_staging_without_redownload(env):
    _, _, job, _ = await prepared(env, files=("file-0",))
    checkpoint = env.repository.checkpoint
    crashed = False
    async def stop_after_ready(row, payload):
        nonlocal crashed
        result = await checkpoint(row, payload)
        if result and row["kind"] == "acquisition" and payload["items"][0]["status"] == "ready" and not crashed:
            crashed = True
            raise asyncio.CancelledError()
        return result
    env.repository.checkpoint = stop_after_ready
    with pytest.raises(asyncio.CancelledError):
        await env.service.process_next()
    assert not env.sources.cleaned and env.files.uploads == 1
    row = env.database["open_data_tasks"].rows["acquisition:" + job.id]
    assert row["payload"]["items"][0]["status"] == "ready" and row["status"] == "downloading"
    row["lease_until"] = 0
    await env.new_service().process_next()
    final = await env.service.get_acquisition("alice", "s", job.id)
    assert final.status == "ready" and final.asset_id
    assert env.sources.calls == ["file-0"] and env.files.uploads == 1
    assert env.sources.cleaned == [env.sources.transfer_keys[0][1]]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_hash_joins_reader_before_closing_transfer_file(env, cancel_count):
    entered, release = threading.Event(), threading.Event()
    class SlowStream:
        closed = False
        def seek(self, position): pass
        def read(self, length):
            assert length == 1024**2
            entered.set()
            assert release.wait(2)
            assert not self.closed, "transfer lock must remain held while a hash thread reads"
            return b"x"
        def close(self): self.closed = True
    stream = SlowStream()
    async def hash_and_close():
        try:
            await env.service._stream_digest(stream)
        finally:
            stream.close()
    task = asyncio.create_task(hash_and_close())
    assert await asyncio.to_thread(entered.wait, 2)
    try:
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
            assert not stream.closed and not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


def test_object_store_capacity_error_is_safe_and_does_not_claim_file_saved():
    from app.application.services.open_data_service import safe_error
    message = safe_error(SimpleNamespace(code="XMinioStorageFull"))
    assert "可用存储不足" in message and "尚未完成保存" in message


@pytest.mark.asyncio
async def test_reverification_replaces_prior_probe_system_warnings(env):
    async def verify(candidate):
        return [{**item, "system_notes": ["仅展示本次已读取内容的 2 个文件；另有 2 个文件核验失败。"]}
                for item in await env.sources.resolve(candidate)]
    env.sources.verify = verify
    search, _, _, _ = await prepared(env)
    assert any("核验失败" in note for note in search.results[0].system_notes)
    env.sources.verify = env.sources.resolve
    await env.service.resolve("alice", "s", search.id, search.results[0].id)
    candidate = (await env.service.get_search("alice", "s", search.id)).results[0]
    assert candidate.system_notes == ["已核验 2 个可获取文件"]


@pytest.mark.asyncio
async def test_hash_and_storage_phases_are_persisted_before_slow_work(env):
    _, _, job, _ = await prepared(env, files=("file-0",))
    digest, upload = env.service._stream_digest, env.files.upload_file
    stages = []
    async def hashing(*args, **kwargs):
        current = await env.service.get_acquisition("alice", "s", job.id)
        assert current.status == "downloading" and current.items[0].message == "正在校验文件"
        stages.append(current.items[0].message)
        return await digest(*args, **kwargs)
    async def saving(*args, **kwargs):
        current = await env.service.get_acquisition("alice", "s", job.id)
        assert current.status == "downloading" and current.items[0].message == "正在保存文件"
        assert not current.files
        stages.append(current.items[0].message)
        return await upload(*args, **kwargs)
    env.service._stream_digest, env.files.upload_file = hashing, saving
    await env.service.process_next()
    done = await env.service.get_acquisition("alice", "s", job.id)
    assert stages == ["正在校验文件", "正在保存文件"]
    assert done.status == "ready" and done.files


@pytest.mark.asyncio
async def test_relevance_filters_before_file_probe_and_orders_by_fit_not_completion(env):
    async def search(*args):
        return dict(results=[{**raw_candidate(key), 'title': title} for key, title in [
            ('surname', 'Last names by language region'), ('context', 'Tibetan dictionary'),
            ('corpus', 'Tibetan linguistic corpus')]], next_cursor='')
    calls = []
    async def assess(query, candidates, **kwargs):
        calls.append(query)
        return relevance_decisions(query, candidates, {'surname': ('unrelated', 0),
            'context': ('related', 2), 'corpus': ('related', 3)})
    original = env.sources.verify
    async def verify(candidate):
        if candidate['external_id'] == 'corpus':
            await asyncio.sleep(.01)
        return await original(candidate)
    env.sources.search, env.sources.verify, env.service.relevance = search, verify, assess
    created = await env.service.create_search('alice', 's', SearchInput(query='藏文语言研究', source_ids=['zenodo']))
    await env.service.process_next()
    found = await env.service.get_search('alice', 's', created.id)
    assert calls == ['藏文语言研究']
    assert env.sources.verify_calls == ['context', 'corpus']
    assert [item.external_id for item in found.results] == ['corpus', 'context']
    assert all(item.relevance_status == 'related' and item.relevance_reason for item in found.results)
    assert found.sources[0].count == 2 and not found.has_more
    assert '_relevance' not in found.model_dump_json()


@pytest.mark.asyncio
async def test_uncertain_relevance_is_retryable_and_does_not_claim_no_matches(env):
    async def search(*args):
        return dict(results=[raw_candidate('one')], next_cursor='')
    async def offline(*args, **kwargs):
        raise TimeoutError()
    env.sources.search, env.service.relevance = search, offline
    created = await env.service.create_search('alice', 's', SearchInput(query='water quality', source_ids=['zenodo']))
    await env.service.process_next()
    found = await env.service.get_search('alice', 's', created.id)
    assert found.status == 'partial' and not found.results and found.has_more
    assert found.relevance_pending_count == 1 and not env.sources.verify_calls
    assert '主题匹配' in found.sources[0].error
    env.service.relevance = related_candidates
    await env.service.continue_search('alice', 's', created.id)
    await env.service.process_next()
    found = await env.service.get_search('alice', 's', created.id)
    assert len(found.results) == 1 and found.relevance_pending_count == 0
    assert len(env.sources.search_calls) == 0  # custom search; no provider refetch needed


@pytest.mark.asyncio
async def test_history_relevance_upgrade_runs_in_worker_and_preserves_downloads(env):
    search, candidate, job, _ = await prepared(env, files=('file-0',))
    await env.service.process_next()
    original_job = await env.service.get_acquisition('alice', 's', job.id)
    stored = env.database['open_data_tasks'].rows['search:' + search.id]
    for item in stored['payload']['results']:
        item.pop('_relevance')
    calls = []
    async def unrelated(query, candidates, **kwargs):
        calls.append(len(candidates))
        return relevance_decisions(query, candidates, {item['external_id']: ('unrelated', 0) for item in candidates})
    env.service.relevance = unrelated
    before_search, before_verify = len(env.sources.search_calls), len(env.sources.verify_calls)
    pending = await env.service.get_search('alice', 's', search.id)
    assert pending.status == 'queued' and pending.relevance_pending_count == 1 and not pending.results
    assert not calls  # GET only enqueues, including concurrent repeated reads.
    await env.service.list_searches('alice', 's')
    await env.service.process_next()
    found = await env.service.get_search('alice', 's', search.id)
    assert calls == [1] and not found.results and found.relevance_pending_count == 0
    assert len(env.sources.search_calls) == before_search and len(env.sources.verify_calls) == before_verify
    assert (await env.service.get_acquisition('alice', 's', job.id)).files == original_job.files
    assert (await env.repository.get('search', 'alice', 's', search.id))['payload']['results'][0]['id'] == job.candidate_id
    await env.service.get_search('alice', 's', search.id)
    assert not await env.service.process_next() and calls == [1]


@pytest.mark.asyncio
async def test_changed_candidate_metadata_invalidates_relevance_without_reusing_old_decision(env):
    search, _, _, _ = await prepared(env)
    stored = env.database['open_data_tasks'].rows['search:' + search.id]
    stored['payload']['results'][0]['description'] = 'A different population and research purpose'
    result = await env.service.get_search('alice', 's', search.id)
    assert result.status == 'queued' and result.relevance_pending_count == 1 and not result.results


@pytest.mark.asyncio
async def test_provider_cannot_supply_its_own_relevance_proof(env):
    async def search(*args):
        return dict(results=[raw_candidate('one', relevance_status='related', relevance_reason='Forged',
            _relevance={'status': 'related', 'score': 3})], next_cursor='')
    async def assess(query, candidates, **kwargs):
        assert all(item.get('_relevance') is None and item.get('relevance_status') is None for item in candidates)
        return relevance_decisions(query, candidates, {'one': ('unrelated', 0)})
    env.sources.search, env.service.relevance = search, assess
    created = await env.service.create_search('alice', 's', SearchInput(query='real research', source_ids=['zenodo']))
    await env.service.process_next()
    found = await env.service.get_search('alice', 's', created.id)
    assert not found.results and not env.sources.verify_calls


@pytest.mark.asyncio
async def test_empty_automatic_route_and_unavailable_explicit_source_never_use_defaults(env):
    async def plan(query, sources, **kwargs):
        return dict(search_query='Tibetan language', source_ids=[], source_queries={},
                    unavailable_sources={source: '需要具体指标与地域' for source in sources})
    env.service.planner = plan
    automatic = await env.service.create_search('alice', 's', SearchInput(query='藏文研究'))
    await env.service.process_next()
    found = await env.service.get_search('alice', 's', automatic.id)
    assert found.status == 'ready' and not found.sources and not found.results
    explicit = await env.service.create_search('alice', 's2', SearchInput(query='藏文研究', source_ids=['worldbank']))
    await env.service.process_next()
    found = await env.service.get_search('alice', 's2', explicit.id)
    assert found.sources[0].id == 'worldbank' and found.sources[0].warnings == ['需要具体指标与地域']
    assert not env.sources.search_calls


@pytest.mark.asyncio
async def test_unrelated_legacy_without_file_proof_does_not_offer_impossible_retry(env):
    search, _, job, _ = await prepared(env)
    stored = env.database['open_data_tasks'].rows['search:' + search.id]
    stored['payload']['_cursors'] = {}
    stored['payload']['has_more'] = False
    for item in stored['payload']['results']:
        item.pop('_availability_verification')
        item.pop('_relevance')
    async def reject(query, candidates, **kwargs):
        return relevance_decisions(query, candidates, {item['external_id']: ('unrelated', 0) for item in candidates})
    env.service.relevance = reject
    await env.service.get_search('alice', 's', search.id)
    # prepared() also leaves an acquisition queued; complete it independently.
    await env.service.process_next()
    await env.service.process_next()
    found = await env.service.get_search('alice', 's', search.id)
    assert not found.results and not found.has_more and found.relevance_pending_count == 0


@pytest.mark.asyncio
async def test_uncertain_history_retry_evaluates_candidate_once_after_mongo_roundtrip(env):
    from bson import BSON
    search, _, job, _ = await prepared(env)
    await env.service.process_next()
    stored = env.database['open_data_tasks'].rows['search:' + search.id]
    for item in stored['payload']['results']:
        item['_relevance']['status'] = 'uncertain'
    await env.service.continue_search('alice', 's', search.id)
    stored = env.database['open_data_tasks'].rows['search:' + search.id]
    # Mongo creates independent objects for the raw history and pending queue.
    stored['payload'] = BSON(BSON.encode(stored['payload'])).decode()
    calls = []
    async def assess(query, candidates, **kwargs):
        calls.extend(item['id'] for item in candidates)
        return await related_candidates(query, candidates)
    env.service.relevance = assess
    await env.service.process_next()
    assert calls.count(job.candidate_id) == 1
    assert (await env.service.get_search('alice', 's', search.id)).results


@pytest.mark.asyncio
async def test_uncertain_candidate_does_not_block_later_relevant_page_or_repeat_same_round(env):
    assessments, pages = [], []
    async def search(source, query, cursor):
        pages.append(cursor)
        return dict(results=[raw_candidate('good' if cursor else 'vague')], next_cursor='' if cursor else 'next')
    async def assess(query, candidates, **kwargs):
        assessments.extend(item['external_id'] for item in candidates)
        return relevance_decisions(query, candidates, {'vague': ('uncertain', 1), 'good': ('related', 3)})
    env.sources.search, env.service.relevance = search, assess
    created = await env.service.create_search('alice', 's', SearchInput(query='public health', source_ids=['zenodo']))
    await env.service.process_next()
    found = await env.service.get_search('alice', 's', created.id)
    assert pages == ['', 'next'] and assessments == ['vague', 'good']
    assert [item.external_id for item in found.results] == ['good']
    assert found.relevance_pending_count == 1 and found.has_more
    assert env.sources.verify_calls == ['good']
