"""Durable public-data discovery and retrieval, independent of agent/sandbox life."""
import asyncio
import copy
import errno
import hashlib
import logging
import re
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import PurePosixPath
from uuid import uuid4

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.open_data_relevance import relevance_fingerprint, assess_open_data_relevance
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.data_candidate import CandidateInput, identity
from app.domain.models.data_library import CreateLibraryAsset
from app.domain.models.open_data import (
    AcquisitionInput, AcquisitionJob, OpenDataCandidate, OpenDataFile, OpenDataFileOption,
    OpenDataSearch, ResolvedFiles, SearchInput,
)
from app.interfaces.schemas.file import public_filename, _redact_absolute_paths

logger = logging.getLogger(__name__)
MAX_RESULTS = 200
VERIFY_CONCURRENCY = 5
MAX_REFILL_PAGES = 3
VERIFICATION_TTL_SECONDS = 300
SOURCE_ROUND_TIMEOUT = 120
RELEVANCE_PENDING_NOTE = "部分候选的主题匹配暂未确认，可继续重试。"


class LeaseLost(Exception):
    pass


def safe_error(error):
    reason = getattr(error, "reason", "")
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
        reason = "storage_write_insufficient"
    if getattr(error, "code", "") in {"XMinioStorageFull", "XMinioAdminBucketQuotaExceeded", "InsufficientStorage", "QuotaExceeded"}:
        reason = "storage_write_insufficient"
    return {
        "rate_limited": "数据源限流，请稍后重试。",
        "storage_insufficient": "暂存空间不足，请释放空间后重试；已下载片段会保留。",
        "storage_write_insufficient": "可用存储不足，可释放空间后重试；此文件尚未完成保存。",
        "storage_upload_failed": "文件尚未完成保存，请重试；已下载片段会保留。",
        "storage_protocol_limit": "当前存储服务无法保存此体积的文件，需要扩展存储能力；此文件尚未完成保存。",
        "download_integrity_failed": "文件完整性校验失败，未交付此文件。",
        "invalid_download_package": "数据包校验失败，未交付此文件。",
        "expanded_file_too_large": "解压校验超过安全读取范围，未交付此文件。",
        "restricted": "此来源需要访问授权，请查看公开来源说明。",
        "restricted_access": "此来源需要访问授权，请查看公开来源说明。",
        "english_query_required": "此来源暂需英文检索词；查询规划未能完成转换，请补充简短英文关键词。",
        "invalid_file_selection": "来源文件列表已变化，请重新查看文件列表。",
        "download_timeout_or_unavailable": "数据源连接超时或暂不可用，可重试。",
    }.get(reason, "数据源调用未完成，可稍后重试；尚未确认取得完整数据。")


def public_file(info):
    return OpenDataFile(file_id=info.file_id, filename=public_filename(info.filename),
                        size=info.size, content_type=info.content_type, upload_date=info.upload_date).model_dump()


def clean_candidate(raw, source_id):
    """Provider-private metadata is persisted, never serialized to the browser."""
    candidate = copy.deepcopy(raw)
    external_id = str(candidate.get("external_id", ""))[:200]
    if not external_id:
        raise ValueError("missing_candidate_identity")
    candidate.update(id=identity([source_id, external_id])[:32], source_id=source_id, external_id=external_id)
    # Only a completed server verification may set these fields. Provider
    # metadata and a provider's "downloadable" label are not proof.
    candidate.update(availability_verified_at=None, available_file_count=0,
                     relevance_status=None, relevance_reason=None)
    candidate["source_url"] = CandidateInput.public_source(candidate.get("source_url", ""))
    for key, maximum in (("title", 400), ("description", 8000), ("license", 500)):
        if candidate.get(key) is not None:
            candidate[key] = _redact_absolute_paths(str(candidate[key]))[:maximum]
    candidate["coverage"] = {str(key)[:80]: _redact_absolute_paths(str(value))[:500]
                             for key, value in candidate.get("coverage", {}).items()
                             if str(key) in {"temporal", "spatial", "time", "geography", "population", "variables", "period", "region", "unit", "granularity", "organism", "sample_count", "library_strategy"}}
    for key in ("match_notes", "limitations", "system_notes", "formats"):
        candidate[key] = [_redact_absolute_paths(str(value))[:500] for value in candidate.get(key, [])[:20]]
    public = OpenDataCandidate.model_validate(candidate).model_dump()
    return {**public, "metadata": candidate.get("metadata", {}), "_source_system_notes": public["system_notes"]}


def verified_dataset_identity(candidate, options):
    """Collapse an indexed DOI and its repository only with identical files.

    Titles or a concept DOI alone cannot prove that versions/subsets coincide.
    This private key never replaces persisted candidate/acquisition identifiers.
    """
    doi = str(candidate.get("metadata", {}).get("doi") or "").strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    if not re.fullmatch(r'10\.\d{4,9}/[^\s<>"?#]{1,180}', doi) or not options:
        return None
    # File IDs bind the repository version/manifest. Matching checksums for
    # the bounded probe subset alone cannot prove that two versions coincide.
    if all(option.get("source_id") and option.get("external_id") and option.get("id") for option in options):
        files = sorted((option["source_id"], option["external_id"], option["id"]) for option in options)
    else:
        return None
    return identity([doi, files])


class OpenDataService:
    def __init__(self, repository, sources, files, sessions, provenance, library, planner=None, relevance=None):
        self.repository, self.sources, self.files = repository, sources, files
        self.sessions, self.provenance, self.library = sessions, provenance, library
        self.planner = planner
        self.relevance = relevance or assess_open_data_relevance
        # This limits simultaneous model calls, never the number of candidates.
        self._relevance_slots = asyncio.Semaphore(2)

    @staticmethod
    def _relevance_proof(query, candidate):
        proof = candidate.get("_relevance", {})
        fingerprint = relevance_fingerprint(query, candidate)
        return proof if (all(proof.get(key) == value for key, value in fingerprint.items())
                         and proof.get("status") in {"related", "unrelated", "uncertain"}) else {}

    @classmethod
    def _is_related(cls, payload, candidate):
        return cls._relevance_proof(payload["query"], candidate).get("status") == "related"

    @classmethod
    def _eligible(cls, payload, candidate):
        return cls._has_verification(candidate) and cls._is_related(payload, candidate)

    async def _assess_candidates(self, row, candidates, checkpoint, *, attempted=None):
        """Persist decisions progressively. Model failure remains retryable."""
        query = row["payload"]["query"]
        attempted = attempted if attempted is not None else set()
        pending = [item for item in candidates if item["id"] not in attempted
                   and self._relevance_proof(query, item).get("status") not in {"related", "unrelated"}]
        for offset in range(0, len(pending), 10):
            batch = pending[offset:offset + 10]
            try:
                async with self._relevance_slots:
                    decisions = await self.relevance(query, batch, user_id=row["owner_id"], session_id=row["session_id"])
            except (LeaseLost, asyncio.CancelledError):
                raise
            except Exception:
                decisions = {}
            for candidate in batch:
                fingerprint = relevance_fingerprint(query, candidate)
                proof = decisions.get(candidate["id"], {})
                if (not isinstance(proof, dict) or any(proof.get(key) != value for key, value in fingerprint.items())
                        or proof.get("status") not in {"related", "unrelated", "uncertain"}):
                    proof = {**fingerprint, "status": "uncertain", "score": 0,
                             "reason": "暂未完成主题匹配，可重试。", "evidence": []}
                candidate["_relevance"] = proof
                attempted.add(candidate["id"])
            await checkpoint()

    async def _owner(self, owner, session):
        if await self.sessions.find_owned_by_id_and_user_id(session, owner) is None:
            raise NotFoundError("探索不存在或不可访问")

    async def _row(self, kind, owner, session, task_id):
        await self._owner(owner, session)
        row = await self.repository.get(kind, owner, session, task_id)
        if row is None:
            raise NotFoundError("查找或获取任务不存在")
        return row

    @staticmethod
    def _has_verification(candidate):
        proof = candidate.get("_availability_verification", {})
        return (proof.get("version") == 1 and proof.get("method") == "registered_source_probe"
                and isinstance(proof.get("checked_at"), datetime)
                and isinstance(proof.get("proof_token"), str) and bool(re.fullmatch(r"[a-f0-9]{32}", proof["proof_token"])))

    @staticmethod
    def _apply_verified_summary(candidate, options, checked_at):
        formats = set()
        licenses = {str(option["license"]) for option in options if option.get("license")}
        if len(licenses) == 1 and all(option.get("license") for option in options):
            candidate["license"] = _redact_absolute_paths(licenses.pop())[:500]
        for option in options:
            suffixes = PurePosixPath(str(option.get("source_filename") or option["filename"])).suffixes
            suffix = "".join(suffixes[-2:]) if len(suffixes) > 1 and suffixes[-1].lower() in {".gz", ".bz2", ".xz"} else "".join(suffixes[-1:])
            suffix = suffix.lower().lstrip(".")
            if re.fullmatch(r"[a-z0-9]{1,12}(?:\.[a-z0-9]{1,8})?", suffix):
                formats.add(suffix)
        # Older persisted candidates mixed DataLab behavior with source content.
        # Migrate known generated phrases at the shared public boundary, without
        # rewriting the source's own limitations or retaining obsolete byte caps.
        limitations, system_notes = [], []
        technical_prefixes = ("仅展示本次已读取内容的", "读取样本仅确认", "可读性核验只读取", "核验不代表",
                              "仅核验部分文件", "文件解析最多返回", "候选记录不代表文件已获取",
                              "DataCite 提供 DOI 元数据", "获取后由官方 API 观测值生成")
        legacy_notes = [note for note in candidate.get("limitations", []) if not str(note).startswith("仅展示本次已读取内容的")]
        for note in legacy_notes + [note for option in options for note in option.get("limitations", [])[:10]]:
            note = _redact_absolute_paths(str(note))[:500]
            if note.startswith(("当前已核验 ", "已核验 ")):
                continue
            if note.startswith(("单文件最多 64 MiB", "候选记录不代表文件已获取", "文件超过 64 MiB", "所选文件超过 128 MiB")) or note.startswith("可读性核验只读取") and "64 MiB" in note:
                if "双端测序需保留配对文件" in note:
                    limitations.append("双端测序需保留配对文件。")
                # Obsolete system limits should not survive as source claims.
                if note.startswith("可读性核验只读取"):
                    system_notes.append(re.sub(r"[，；]?\s*(?:并执行|执行)?\s*64 MiB 上限", "", note))
                continue
            (system_notes if note.startswith(technical_prefixes) else limitations).append(note)
        # Probe-specific warnings belong to the current proof. Re-resolving a
        # record must not keep an earlier probe's failures or unchecked count.
        source_notes = candidate.get("_source_system_notes", [note for note in candidate.get("system_notes", [])
            if not str(note).startswith("仅展示本次已读取内容的")])
        for note in source_notes + [note for option in options for note in option.get("system_notes", [])[:10]]:
            note = _redact_absolute_paths(str(note))[:500]
            if not note.startswith(("当前已核验 ", "已核验 ")) and "64 MiB" not in note and "128 MiB" not in note:
                system_notes.append(note)
        candidate.update(access="downloadable", availability_verified_at=checked_at, available_file_count=len(options),
            formats=sorted(formats), size=sum(option["size"] for option in options) if all(option.get("size") is not None for option in options) else None,
            limitations=list(dict.fromkeys(limitations))[:20],
            system_notes=list(dict.fromkeys([f"已核验 {len(options)} 个可获取文件"] + system_notes))[:20])

    async def _public_search(self, row):
        """One projection for every endpoint, including legacy persisted rows."""
        payload = copy.deepcopy(row["payload"])
        raw = payload.get("results", [])
        # Upgrade completed history in the durable worker, not in a polling
        # HTTP request. Keep raw records and acquisition references intact.
        if (any(not self._relevance_proof(payload["query"], item) for item in raw)
                and payload["status"] not in {"queued", "searching"}):
            payload.update(_relevance_refresh=True, _relevance_previous_status=payload["status"],
                           status="queued", updated_at=datetime.now(UTC))
            changed = await self.repository.mutate(row, payload, {"ready", "partial", "failed"})
            row = changed or await self.repository.get("search", row["owner_id"], row["session_id"], payload["id"])
            payload = copy.deepcopy(row["payload"])
            raw = payload.get("results", [])
        relevance_pending = {item["id"] for item in raw
            if self._relevance_proof(payload["query"], item).get("status") not in {"related", "unrelated"}}
        for pending in payload.get("_verification_pending", {}).values():
            relevance_pending.update(item["id"] for item in pending
                if self._relevance_proof(payload["query"], item).get("status") not in {"related", "unrelated"})
        payload["relevance_pending_count"] = len(relevance_pending)
        proofs = await self.repository.get_verified_options(row["owner_id"], row["session_id"], payload["id"],
            [candidate["id"] for candidate in raw if self._has_verification(candidate)])
        visible, legacy, revoked = [], False, False
        for candidate in raw:
            relevance = self._relevance_proof(payload["query"], candidate)
            if relevance.get("status") == "unrelated":
                continue
            if not self._has_verification(candidate):
                legacy = True
                continue
            if relevance.get("status") != "related":
                continue
            proof = proofs.get(candidate["id"], {})
            options = [item for item in proof.get("options", []) if item.get("available") is True]
            if (not options or not isinstance(proof.get("verified_at"), datetime)
                    or proof.get("generation", -1) < candidate["_availability_verification"].get("generation", 0)
                    or proof.get("proof_token") != candidate["_availability_verification"]["proof_token"]):
                revoked = True
                continue
            self._apply_verified_summary(candidate, options, proof["verified_at"])
            candidate.update(relevance_status="related",
                relevance_reason=_redact_absolute_paths(str(relevance.get("reason", "")))[:300])
            visible.append(candidate)
        # A deterministic tie break keeps polling, pagination and provider
        # response speed from reshuffling equally relevant records.
        visible.sort(key=lambda item: (-item["_relevance"].get("score", 0), -item["_relevance"].get("rank", 0),
                                      item["title"].casefold(), item["id"]))
        payload["results"] = visible
        for source in payload["sources"]:
            source["count"] = sum(candidate["source_id"] == source["id"] for candidate in visible)
        notes = payload.setdefault("notes", [])
        if legacy:
            notes.append("已隐藏旧版尚未核验文件可获取性的记录；可继续核验，历史获取任务及已保存文件仍保留。")
        if revoked:
            notes.append("部分记录的文件当前未能核验为可获取，已暂不展示；可继续重试核验，已取得的文件仍保留。")
        payload["notes"] = list(dict.fromkeys(notes))
        retryable = legacy or revoked or relevance_pending or any(payload.get("_verification_pending", {}).values())
        payload["has_more"] = bool(payload.get("has_more") or retryable) and len(visible) < MAX_RESULTS
        return OpenDataSearch.model_validate(payload)

    async def create_search(self, owner, session_id, request: SearchInput):
        await self._owner(owner, session_id)
        catalog = {entry["id"]: entry for entry in self.sources.catalog()}
        source_ids = request.source_ids
        if request.parent_id:
            parent = await self._row("search", owner, session_id, request.parent_id)
            if source_ids is None and parent["payload"].get("_explicit_sources"):
                source_ids = [source["id"] for source in parent["payload"]["sources"]]
        selected = list(dict.fromkeys(source_ids if source_ids is not None else list(catalog)))
        if not selected or any(key not in catalog for key in selected):
            raise BadRequestError("请选择已提供的数据源")
        now = datetime.now(UTC)
        payload = dict(id=uuid4().hex, session_id=session_id, query=request.query, parent_id=request.parent_id,
                       status="queued", sources=[dict(id=key, title=catalog[key]["title"], status="queued", count=0) for key in selected],
                       results=[], has_more=False, created_at=now, updated_at=now,
                       _pending_sources=selected, _cursors={}, _planned=False,
                       _verification_pending={}, _fetch_sources=list(selected),
                       _explicit_sources=source_ids is not None)
        row = await self.repository.insert("search", owner, session_id, payload)
        await self.sessions.ensure_discovery_title(session_id, owner, _redact_absolute_paths(request.query)[:80])
        return await self._public_search(row)

    async def list_searches(self, owner, session_id):
        await self._owner(owner, session_id)
        rows = await self.repository.list("search", owner, session_id)
        return [await self._public_search(row) for row in rows]

    async def get_search(self, owner, session_id, search_id):
        return await self._public_search(await self._row("search", owner, session_id, search_id))

    async def continue_search(self, owner, session_id, search_id):
        row = await self._row("search", owner, session_id, search_id)
        payload = copy.deepcopy(row["payload"])
        if payload["status"] in {"queued", "searching"}:
            return await self._public_search(row)
        public = await self._public_search(row)
        if public.status in {"queued", "searching"}:
            return public
        visible = {candidate.id for candidate in public.results}
        verification = payload.setdefault("_verification_pending", {})
        for candidate in payload["results"]:
            if candidate["id"] not in visible and self._relevance_proof(payload["query"], candidate).get("status") != "unrelated":
                queue = verification.setdefault(candidate["source_id"], [])
                if candidate["id"] not in {item["id"] for item in queue}:
                    queue.append(candidate)
        fetch = [source["id"] for source in payload["sources"] if payload["_cursors"].get(source["id"])
                 or (source["status"] == "failed" and not verification.get(source["id"]))]
        pending = [source["id"] for source in payload["sources"] if source["id"] in fetch or verification.get(source["id"])]
        if not pending or len(visible) >= MAX_RESULTS:
            raise BadRequestError("没有更多结果，请补充条件后重新查找")
        payload.update(status="queued", _pending_sources=pending, _fetch_sources=fetch, updated_at=datetime.now(UTC))
        changed = await self.repository.mutate(row, payload, {"ready", "partial", "failed"})
        return await self._public_search(changed or await self._row("search", owner, session_id, search_id))

    @staticmethod
    def _candidate(row, candidate_id):
        candidate = next((item for item in row["payload"]["results"] if item["id"] == candidate_id), None)
        if candidate is None:
            raise NotFoundError("所选数据不属于本次查找")
        return candidate

    async def resolve(self, owner, session_id, search_id, candidate_id):
        row = await self._row("search", owner, session_id, search_id)
        candidate = self._candidate(row, candidate_id)
        proof_token = candidate.get("_availability_verification", {}).get("proof_token") or uuid4().hex
        try:
            options = await self._verify_options(candidate, complete=True)
        except Exception as error:
            # A timeout/rate limit does not disprove the earlier byte probe.
            # Retain its original timestamp so the card remains retryable; the
            # acquisition TTL still requires fresh evidence before queuing an
            # old selection, and every download rechecks its source manifest.
            transient = isinstance(error, TimeoutError) or getattr(error, "reason", None) in {
                "rate_limited", "upstream_unavailable", "download_timeout_or_unavailable"}
            if not transient:
                await self.repository.put_options(owner, session_id, search_id, candidate_id, [], verified_at=datetime.now(UTC),
                    generation=row.get("lease_generation", 0), proof_token=proof_token)
            raise BadRequestError(safe_error(error)) from None
        saved = await self.repository.put_options(owner, session_id, search_id, candidate_id, options,
            verified_at=datetime.now(UTC), generation=row.get("lease_generation", 0), proof_token=proof_token)
        if not saved:
            raise BadRequestError("查找状态已更新，请重新查看文件列表")
        summary = options[0].get("_verification_summary", {}) if options else {}
        return ResolvedFiles(files=[OpenDataFileOption.model_validate(item) for item in options],
            verification_pending_count=summary.get("verification_pending_count", 0),
            unavailable_file_count=summary.get("unavailable_file_count", 0))

    async def _verify_options(self, candidate, *, complete=False):
        # Search only needs quick evidence of readable data. Expanding a record
        # must check its entire manifest without a batch-size/time cutoff.
        if complete:
            raw = await self.sources.verify_all(candidate)
        else:
            async with asyncio.timeout(50):
                raw = await self.sources.verify(candidate)
        options = []
        seen = set()
        for option in raw:
            item = copy.deepcopy(option)
            if item.get("available") is not True:
                continue
            item["filename"] = public_filename(item.get("filename"))[:200]
            if not isinstance(item.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item["id"]):
                raise SourceConnectorError("invalid_response")
            if item["id"] in seen:
                continue
            size = item.get("size")
            if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
                continue
            item["reason"] = _redact_absolute_paths(str(item.get("reason", "")))[:500]
            for key in ("kind", "group_id", "group_role"):
                if item.get(key) is not None:
                    item[key] = _redact_absolute_paths(str(item[key]))[:200]
            OpenDataFileOption.model_validate(item)
            options.append(item)
            seen.add(item["id"])
        return options

    async def create_acquisition(self, owner, session_id, search_id, request: AcquisitionInput):
        search = await self._row("search", owner, session_id, search_id)
        candidate = self._candidate(search, request.candidate_id)
        proof = (await self.repository.get_verified_options(owner, session_id, search_id, [request.candidate_id])).get(request.candidate_id, {})
        checked_at = proof.get("verified_at")
        age = (datetime.now(UTC) - checked_at.replace(tzinfo=UTC)).total_seconds() if isinstance(checked_at, datetime) else float("inf")
        if age > VERIFICATION_TTL_SECONDS:
            await self.resolve(owner, session_id, search_id, request.candidate_id)
            proof = (await self.repository.get_verified_options(owner, session_id, search_id, [request.candidate_id])).get(request.candidate_id, {})
        if proof.get("generation", -1) < candidate.get("_availability_verification", {}).get("generation", 0):
            raise BadRequestError("文件核验证明已更新，请重新查看文件列表")
        marker = candidate.get("_availability_verification", {})
        if marker.get("proof_token") and proof.get("proof_token") != marker["proof_token"]:
            raise BadRequestError("文件核验证明已更新，请重新查看文件列表")
        options = {item["id"]: item for item in proof.get("options", [])}
        if any(key not in options or not options[key].get("available") for key in request.file_ids):
            raise BadRequestError("文件选择已失效或不可获取，请重新查看文件列表")
        selected = [options[key] for key in request.file_ids]
        now = datetime.now(UTC)
        job_id = identity([owner, session_id, search_id, request.candidate_id, sorted(request.file_ids)])[:32]
        payload = dict(id=job_id, search_id=search_id, candidate_id=request.candidate_id, session_id=session_id,
                       status="queued", items=[dict(id=item["id"], filename=item["filename"], size=item.get("size"),
                       bytes_received=0, status="queued", _storage_key=uuid4().hex) for item in selected], files=[], created_at=now, updated_at=now,
                       _candidate=candidate, _options=selected, _asset_id=f"{int(now.timestamp() * 1000):012x}{job_id[:20]}")
        row = await self.repository.insert("acquisition", owner, session_id, payload)
        return AcquisitionJob.model_validate(row["payload"])

    async def list_acquisitions(self, owner, session_id, search_id=None):
        await self._owner(owner, session_id)
        if search_id:
            await self._row("search", owner, session_id, search_id)
        return [AcquisitionJob.model_validate(row["payload"]) for row in await self.repository.list("acquisition", owner, session_id, search_id)]

    async def get_acquisition(self, owner, session_id, job_id):
        return AcquisitionJob.model_validate((await self._row("acquisition", owner, session_id, job_id))["payload"])

    async def cancel_acquisition(self, owner, session_id, job_id):
        for _ in range(5):
            row = await self._row("acquisition", owner, session_id, job_id)
            if row["status"] not in {"queued", "downloading"}:
                return AcquisitionJob.model_validate(row["payload"])
            payload = copy.deepcopy(row["payload"])
            payload.update(status="cancelled", updated_at=datetime.now(UTC))
            for item in payload["items"]:
                if item["status"] != "ready":
                    item.update(status="failed", message="已取消；已取得的文件仍保留")
            changed = await self.repository.mutate(row, payload, {"queued", "downloading"})
            if changed:
                return AcquisitionJob.model_validate(changed["payload"])
        raise BadRequestError("进度正在更新，请重试取消")

    async def retry_acquisition(self, owner, session_id, job_id):
        row = await self._row("acquisition", owner, session_id, job_id)
        payload = copy.deepcopy(row["payload"])
        if payload["status"] in {"queued", "downloading", "ready"}:
            return AcquisitionJob.model_validate(payload)
        for item in payload["items"]:
            if item["status"] != "ready":
                item.update(status="queued", message=None, bytes_received=0)
        payload.update(status="queued", error=None, updated_at=datetime.now(UTC))
        changed = await self.repository.mutate(row, payload, {"partial", "failed", "cancelled"})
        return AcquisitionJob.model_validate((changed or await self._row("acquisition", owner, session_id, job_id))["payload"])

    async def _checkpoint(self, row, payload):
        payload["updated_at"] = datetime.now(UTC)
        if not await self.repository.checkpoint(row, payload):
            raise LeaseLost()

    async def _search(self, row):
        payload = copy.deepcopy(row["payload"])
        # Historical relevance migration is independent from fetching new
        # pages or downloading files. Decisions survive restarts per batch.
        async def relevance_checkpoint():
            await self._checkpoint(row, payload)

        queued_ids = {item["id"] for items in payload.get("_verification_pending", {}).values() for item in items}
        history = [item for item in payload.get("results", [])
                   if payload.get("_relevance_refresh") or item["id"] not in queued_ids]
        if history:
            await self._assess_candidates(row, history, relevance_checkpoint)
        if payload.pop("_relevance_refresh", False):
            previous = payload.pop("_relevance_previous_status", "ready")
            payload["status"] = "partial" if any(
                self._relevance_proof(payload["query"], item).get("status") == "uncertain"
                for item in payload["results"]) else previous
            await self._checkpoint(row, payload)
            return
        from app.application.services.open_data_query_planner import PLAN_VERSION
        if payload.get("_planned") and payload.get("_plan_version") != PLAN_VERSION:
            # Old cursors belong to the old source query (which may have been
            # an unrelated provider default). Never reuse them after replanning.
            payload.update(_planned=False, _cursors={},
                           _pending_sources=[source["id"] for source in payload["sources"]],
                           _fetch_sources=[source["id"] for source in payload["sources"]])
        if not payload.get("_planned"):
            try:
                planner = self.planner
                if planner is None:
                    from app.application.services.open_data_query_planner import plan_open_data_query
                    planner = plan_open_data_query
                async with asyncio.timeout(35):
                    plan = await planner(payload["query"], [source["id"] for source in payload["sources"]],
                                         user_id=row["owner_id"], session_id=row["session_id"],
                                         explicit_sources=payload.get("_explicit_sources", False))
                payload["effective_query"] = str(plan.get("search_query") or payload["query"])[:1000]
                source_queries = plan.get("source_queries", {})
                payload["_source_queries"] = {source["id"]: source_queries[source["id"]].strip()
                    for source in payload["sources"] if isinstance(source_queries, dict)
                    and isinstance(source_queries.get(source["id"]), str)
                    and 0 < len(source_queries[source["id"]].strip()) <= 500}
                payload["notes"] = [_redact_absolute_paths(str(note))[:500] for note in plan.get("notes", [])[:10]]
                payload["_query_intent"] = plan.get("query_intent", {})
                unavailable = plan.get("unavailable_sources", {})
                payload["_unavailable_sources"] = {source["id"]: _redact_absolute_paths(str(unavailable[source["id"]]))[:500]
                    for source in payload["sources"] if isinstance(unavailable, dict) and source["id"] in unavailable}
                if not payload.get("_explicit_sources") and isinstance(plan.get("source_ids"), list):
                    selected = set(plan["source_ids"])
                    filtered = [source for source in payload["sources"] if source["id"] in selected]
                    payload["sources"] = filtered
                    payload["_pending_sources"] = [source["id"] for source in filtered]
            except Exception:
                # Planner failures must not fan out to every specialized
                # observation service using unrelated provider defaults.
                from app.application.services.open_data_query_planner import fallback_plan
                plan = fallback_plan(payload["query"], [source["id"] for source in payload["sources"]],
                                     explicit_sources=payload.get("_explicit_sources", False))
                payload.update(effective_query=plan["search_query"], notes=plan["notes"],
                               _source_queries=plan["source_queries"], _unavailable_sources=plan.get("unavailable_sources", {}))
                if not payload.get("_explicit_sources"):
                    payload["sources"] = [source for source in payload["sources"] if source["id"] in plan["source_ids"]]
                    payload["_pending_sources"] = [source["id"] for source in payload["sources"]]
            if len(payload["effective_query"]) > 500:
                payload["effective_query"] = payload["effective_query"][:500]
                payload.setdefault("notes", []).append("数据源查询限 500 字符，本轮仅使用查询词前 500 字符；后续条件未用于检索。原始需求完整保留，可补充更简短条件继续查找。")
            payload["_planned"] = True
            payload["_plan_version"] = PLAN_VERSION
            await self._checkpoint(row, payload)
        verification = payload.setdefault("_verification_pending", {})
        payload.setdefault("_fetch_sources", list(payload["_pending_sources"]))
        # Old in-flight searches also migrate through verification. Historical
        # completed rows enter here only after the user's continue action.
        for candidate in payload["results"]:
            if not self._has_verification(candidate) and candidate["source_id"] in payload["_pending_sources"]:
                queue = verification.setdefault(candidate["source_id"], [])
                if candidate["id"] not in {item["id"] for item in queue}:
                    queue.append(candidate)
        pending_note = "部分候选的文件可获取性暂未确认，已暂不展示；可继续重试核验。"
        empty_note = "本轮尚未找到与需求相关且已核验可获取的实体数据文件；元数据线索不计入结果。可继续查找或调整条件。"
        payload["notes"] = [note for note in payload.get("notes", []) if note not in {pending_note, empty_note}]
        checkpoint_lock, verify_slots, query_slots = asyncio.Lock(), asyncio.Semaphore(VERIFY_CONCURRENCY), asyncio.Semaphore(3)
        relevance_attempted = set()

        async def checkpoint():
            async with checkpoint_lock:
                # Child sources share the live payload, but a database write
                # receives an immutable snapshot under the same lease version.
                await self._checkpoint(row, copy.deepcopy(payload))

        async def source_round(source_id):
            source = next(item for item in payload["sources"] if item["id"] == source_id)
            unavailable = payload.get("_unavailable_sources", {}).get(source_id)
            if unavailable:
                source.update(status="ready", count=0, error=None, warnings=[unavailable])
                payload["_pending_sources"].remove(source_id)
                payload["_cursors"].pop(source_id, None)
                if source_id in payload["_fetch_sources"]:
                    payload["_fetch_sources"].remove(source_id)
                await checkpoint()
                return
            source.update(status="searching", error=None, warnings=[])
            start_count = sum(item["source_id"] == source_id and self._eligible(payload, item) for item in payload["results"])
            failures = []
            await checkpoint()
            try:
                async with asyncio.timeout(SOURCE_ROUND_TIMEOUT):
                    if verification.get(source_id):
                        failures.extend(await self._verify_pending(row, payload, source, verify_slots, checkpoint, relevance_attempted))
                    fetch = source_id in payload["_fetch_sources"]
                    pages = 0
                    while (fetch and not any(error != RELEVANCE_PENDING_NOTE for error in failures) and pages < MAX_REFILL_PAGES
                           and sum(item["source_id"] == source_id and self._eligible(payload, item) for item in payload["results"]) <= start_count
                           and sum(self._eligible(payload, item) for item in payload["results"]) < MAX_RESULTS):
                        cursor = payload["_cursors"].get(source_id, "")
                        async with query_slots:
                            async with asyncio.timeout(90):
                                query = payload.get("_source_queries", {}).get(source_id) or payload["effective_query"]
                                page = await self.sources.search(source_id, query, cursor)
                        pages += 1
                        queue = verification.setdefault(source_id, [])
                        known = {item["id"] for item in payload["results"] if self._eligible(payload, item)} | {item["id"] for item in queue}
                        for raw in page["results"]:
                            candidate = clean_candidate(raw, source_id)
                            if candidate["id"] not in known:
                                queue.append(candidate)
                                known.add(candidate["id"])
                        next_cursor = page.get("next_cursor") or ""
                        if next_cursor == cursor:
                            next_cursor = ""
                        payload["_cursors"][source_id] = next_cursor
                        if source_id in payload["_fetch_sources"]:
                            payload["_fetch_sources"].remove(source_id)
                        warnings = [_redact_absolute_paths(str(note))[:500] for note in page.get("warnings", [])[:10]]
                        source["warnings"] = list(dict.fromkeys(source["warnings"] + warnings))
                        # The cursor and unverified page are durable before any
                        # probes start, so a restart can resume this exact page.
                        await checkpoint()
                        failures.extend(await self._verify_pending(row, payload, source, verify_slots, checkpoint, relevance_attempted))
                        fetch = bool(next_cursor)
                    source["status"] = "partial" if failures or source["warnings"] else "ready"
            except (LeaseLost, asyncio.CancelledError):
                raise
            except Exception as error:
                failures.append(safe_error(error))
                source["status"] = "partial" if verification.get(source_id) or source.get("count") else "failed"
            source["count"] = sum(item["source_id"] == source_id and self._eligible(payload, item) for item in payload["results"])
            if failures:
                source["error"] = "；".join(dict.fromkeys(failures))[:1000]
                source["warnings"] = list(dict.fromkeys(source["warnings"] + [
                    RELEVANCE_PENDING_NOTE if all(error == RELEVANCE_PENDING_NOTE for error in failures) else pending_note]))
            elif source["warnings"]:
                source["error"] = "；".join(source["warnings"])
            payload["notes"] = list(dict.fromkeys(payload.get("notes", []) + source["warnings"]))[:30]
            payload["_pending_sources"].remove(source_id)
            payload["has_more"] = bool(any(payload["_cursors"].values()) or any(verification.values()))
            await checkpoint()

        tasks = [asyncio.create_task(source_round(source_id)) for source_id in list(payload["_pending_sources"])]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        failed = sum(source["status"] == "failed" for source in payload["sources"])
        incomplete = any(source["status"] in {"failed", "partial"} for source in payload["sources"])
        payload["status"] = "failed" if payload["sources"] and failed == len(payload["sources"]) else "partial" if incomplete else "ready"
        count = sum(self._eligible(payload, item) for item in payload["results"])
        if not count:
            payload["notes"] = list(dict.fromkeys(payload.get("notes", []) + [empty_note]))
        if count >= MAX_RESULTS:
            payload["has_more"] = False
            payload["notes"] = list(dict.fromkeys(payload.get("notes", []) + [f"本次查找最多保留 {MAX_RESULTS} 个候选；请缩小条件后发起新查找。 "]))
        await checkpoint()

    async def _verify_pending(self, row, payload, source, slots, checkpoint, relevance_attempted=None):
        source_id = source["id"]
        pending = payload["_verification_pending"][source_id]
        await self._assess_candidates(row, list(pending), checkpoint, attempted=relevance_attempted)
        pending[:] = [item for item in pending if self._relevance_proof(payload["query"], item).get("status") != "unrelated"]
        ready = [item for item in pending if self._is_related(payload, item)]
        failures = [RELEVANCE_PENDING_NOTE] if len(ready) < len(pending) else []
        await checkpoint()

        async def probe(candidate):
            async with slots:
                try:
                    return candidate, await self._verify_options(candidate), None
                except Exception as error:
                    return candidate, [], error

        tasks = [asyncio.create_task(probe(candidate)) for candidate in ready]
        try:
            for future in asyncio.as_completed(tasks):
                candidate, options, error = await future
                await checkpoint()
                checked_at = datetime.now(UTC)
                proof_token = uuid4().hex
                if not await self.repository.put_options(row["owner_id"], row["session_id"], payload["id"], candidate["id"], options,
                        verified_at=checked_at, generation=row.get("lease_generation", 0), proof_token=proof_token):
                    raise LeaseLost()
                payload["results"] = [item for item in payload["results"] if item["id"] != candidate["id"]]
                if error is not None:
                    failures.append(safe_error(error))
                else:
                    pending[:] = [item for item in pending if item["id"] != candidate["id"]]
                    if options and sum(self._eligible(payload, item) for item in payload["results"]) < MAX_RESULTS:
                        relevance = candidate["_relevance"]
                        candidate = clean_candidate(candidate, source_id)
                        candidate["_relevance"] = relevance
                        self._apply_verified_summary(candidate, options, checked_at)
                        candidate["_availability_verification"] = {"version": 1, "method": "registered_source_probe", "checked_at": checked_at,
                            "generation": row.get("lease_generation", 0), "proof_token": proof_token}
                        candidate["_dataset_identity"] = verified_dataset_identity(candidate, options)
                        duplicate = candidate["_dataset_identity"] and any(
                            item.get("_dataset_identity") == candidate["_dataset_identity"]
                            and self._eligible(payload, item) for item in payload["results"])
                        if not duplicate:
                            payload["results"].append(candidate)
                source["count"] = sum(item["source_id"] == source_id and self._eligible(payload, item) for item in payload["results"])
                await checkpoint()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return failures

    @staticmethod
    def _validate_stream(stream, *, rewind=True, stop=None):
        if rewind:
            stream.seek(0)
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(1024 * 1024):
            if stop is not None and stop.is_set():
                raise InterruptedError("stream validation cancelled")
            size += len(chunk)
            digest.update(chunk)
        if not size:
            raise SourceConnectorError("download_integrity_failed")
        if rewind:
            stream.seek(0)
        return size, digest.hexdigest()

    async def _stream_digest(self, stream, *, rewind=True):
        # A cancelled to_thread keeps running. Stop at a chunk boundary and
        # join it before closing the file and releasing the transfer lock.
        stop = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(self._validate_stream, stream, rewind=rewind, stop=stop))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            # Lease loss and worker shutdown can both cancel this coroutine.
            # Every join must remain shielded until the blocking reader exits.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            with suppress(Exception):
                task.result()
            raise

    async def _cleanup_transfer(self, item):
        # The item must already have a durable ready checkpoint. Cleanup is
        # idempotent and never turns a successfully saved object into a failure.
        try:
            await self.sources.cleanup_transfer(item["_storage_key"])
        except Exception:
            logger.warning("Open-data staging cleanup deferred for saved item", exc_info=True)

    async def _acquire(self, row):
        payload = copy.deepcopy(row["payload"])
        owner, session = row["owner_id"], row["session_id"]
        options = {item["id"]: item for item in payload["_options"]}
        for item in payload["items"]:
            if item["status"] == "ready":
                await self._cleanup_transfer(item)
                continue
            item.update(status="downloading", message=None, bytes_received=0)
            await self._checkpoint(row, payload)
            try:
                item_key = item["_storage_key"]
                saved_id = await self.repository.find_materialized_file(owner, item_key)
                info = await self.files.get_file_info(saved_id, owner) if saved_id else None
                if info is not None:
                    # Upload metadata alone is not authoritative evidence. The
                    # expected digest was checkpointed from verified source bytes
                    # before storage; re-read a recovered object against it.
                    expected = item.get("_verified", {})
                    item["message"] = "正在校验文件"
                    await self._checkpoint(row, payload)
                    stream, current = await self.files.download_file(info.file_id, owner)
                    try:
                        size, digest = await self._stream_digest(stream, rewind=False)
                    finally:
                        try:
                            stream.close()
                        finally:
                            release = getattr(stream, "release_conn", None)
                            if callable(release):
                                release()
                    if (current.user_id != owner or size != expected.get("size") or digest != expected.get("sha256")
                            or info.size != size or (info.metadata or {}).get("source_url") != expected.get("source_url")):
                        raise SourceConnectorError("download_integrity_failed")
                if info is None:
                    last_progress = 0.0
                    async def progress(received, total=None):
                        nonlocal last_progress
                        item["bytes_received"] = received
                        if total is not None:
                            item["size"] = total
                        if time.monotonic() - last_progress >= 0.5:
                            await self._checkpoint(row, payload)
                            last_progress = time.monotonic()
                    stream, filename, mime, metadata = await self.sources.download(
                        payload["_candidate"], options[item["id"]], progress, transfer_key=item_key)
                    try:
                        item["message"] = "正在校验文件"
                        await self._checkpoint(row, payload)
                        size, digest = await self._stream_digest(stream)
                        if metadata.get("sha256") != digest:
                            raise SourceConnectorError("download_integrity_failed")
                        await self._checkpoint(row, payload)
                        filename = item["id"][:8] + "_" + public_filename(filename)[-180:]
                        source_url = CandidateInput.public_source(metadata.get("source_url") or payload["_candidate"]["source_url"])
                        metadata = {key: metadata[key] for key in ("connector_id", "external_id", "provider_checksum", "provider_checksum_verified", "acquired_at", "snapshot_rows", "snapshot_page_count", "source_last_updated") if key in metadata}
                        metadata.update(source="data_connector", source_url=source_url, sha256=digest,
                                        checksum_origin="computed_locally", open_data_item_key=item_key)
                        item["_verified"] = dict(size=size, sha256=digest, source_url=source_url)
                        item["message"] = "正在保存文件"
                        await self._checkpoint(row, payload)
                        info = await self.files.upload_file(stream, filename, owner, mime, metadata)
                    finally:
                        stream.close()
                if info.user_id != owner or not info.size:
                    raise SourceConnectorError("download_integrity_failed")
                metadata = info.metadata or {}
                digest = metadata.get("sha256", "")
                expected = item.get("_verified", {})
                if (not re.fullmatch(r"[a-f0-9]{64}", digest) or digest != expected.get("sha256")
                        or info.size != expected.get("size") or metadata.get("source_url") != expected.get("source_url")):
                    raise SourceConnectorError("download_integrity_failed")
                await self._checkpoint(row, payload)
                await self.provenance.save_acquisition(owner, session, "open-data:" + item_key,
                    dict(file_id=info.file_id, filename=info.filename, size=info.size, sha256=digest,
                         source_url=CandidateInput.public_source(metadata["source_url"])))
                await self._checkpoint(row, payload)
                await self.sessions.upsert_connector_file(session, owner, info)
                await self._checkpoint(row, payload)
                file = public_file(info)
                item.update(status="ready", bytes_received=info.size, size=info.size, file=file,
                            message="已下载并完成文件校验；分析适用性尚待确认")
                payload["files"] = [existing for existing in payload["files"] if existing["file_id"] != file["file_id"]] + [file]
                await self._checkpoint(row, payload)
                await self._cleanup_transfer(item)
                # Save each success before starting another transfer. A later
                # cancellation/failure must not strand completed personal data.
                await self._save_asset(row, payload)
            except (LeaseLost, asyncio.CancelledError):
                raise
            except Exception as error:
                item.update(status="failed", message=safe_error(error))
                await self._checkpoint(row, payload)
        if payload["files"] and (not payload.get("asset_id") or payload.get("error")):
            await self._save_asset(row, payload)
        failed = any(item["status"] != "ready" for item in payload["items"])
        payload["status"] = "partial" if payload["files"] and (failed or payload.get("error")) else "ready" if payload["files"] else "failed"
        await self._checkpoint(row, payload)

    async def _save_asset(self, row, payload):
        try:
            await self._checkpoint(row, payload)
            asset = await self.library.create(row["owner_id"], CreateLibraryAsset(
                name=payload["_candidate"]["title"][:200], source_type="open_data", session_id=row["session_id"],
                file_ids=[file["file_id"] for file in payload["files"]],
                description="开放数据获取结果；已保存实际文件，尚未判断其是否满足具体分析要求。"),
                acquisition_id=payload["_asset_id"])
            payload.update(asset_id=asset.id, error=None)
            await self._checkpoint(row, payload)
        except (LeaseLost, asyncio.CancelledError):
            raise
        except Exception:
            payload["error"] = "文件已保存在当前探索，保存到我的数据尚未完成，可重试。"
            await self._checkpoint(row, payload)

    async def process_next(self):
        row = await self.repository.claim()
        if row is None:
            return False
        lost = asyncio.Event()
        async def run():
            await self._owner(row["owner_id"], row["session_id"])
            await (self._search(row) if row["kind"] == "search" else self._acquire(row))
        work = asyncio.create_task(run())
        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                try:
                    valid = await self.repository.renew(row)
                except Exception:
                    valid = False
                if not valid:
                    lost.set()
                    work.cancel()
                    return
        beat = asyncio.create_task(heartbeat())
        try:
            await work
        except LeaseLost:
            pass
        except asyncio.CancelledError:
            work.cancel()
            with suppress(asyncio.CancelledError, LeaseLost):
                await work
            if not lost.is_set():
                raise
        except Exception:
            payload = copy.deepcopy(row["payload"])
            payload.update(status="failed", error="任务未完成，可重试。")
            with suppress(LeaseLost):
                await self._checkpoint(row, payload)
            logger.warning("Open data task failed kind=%s", row["kind"])
        finally:
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat
        return True


@lru_cache()
def get_open_data_service():
    from app.application.services.data_library_service import DataLibraryService
    from app.infrastructure.external.data_sources.open_data_sources import get_open_data_sources
    from app.infrastructure.external.file.factory import get_file_storage
    from app.infrastructure.repositories.mongo_data_candidate_repository import MongoDataCandidateRepository
    from app.infrastructure.repositories.mongo_data_library_repository import MongoDataLibraryRepository
    from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
    from app.infrastructure.repositories.open_data_repository import OpenDataRepository
    files, sessions = get_file_storage(), MongoSessionRepository()
    return OpenDataService(OpenDataRepository(), get_open_data_sources(), files, sessions,
                           MongoDataCandidateRepository(), DataLibraryService(MongoDataLibraryRepository(), files, sessions))


async def run_open_data_worker(*, service=None, concurrency=3, poll_interval=1):
    service = service or get_open_data_service()
    while True:
        try:
            await service.repository.ensure_indexes()
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Open data worker will retry initialization")
            await asyncio.sleep(3)
    async def consume():
        while True:
            try:
                if not await service.process_next():
                    await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Open data worker will retry after an infrastructure failure")
                await asyncio.sleep(3)
    async with asyncio.TaskGroup() as group:
        for _ in range(max(1, min(concurrency, 4))):
            group.create_task(consume())
