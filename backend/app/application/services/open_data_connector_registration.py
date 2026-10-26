"""Executable catalog entries for general public-data discovery sources."""
import asyncio
import time
from datetime import UTC, datetime

from pydantic import Field

from app.application.services.data_connector_service import RegisteredConnector, RegisteredOperation
from app.application.services.open_data_service import OpenDataService, clean_candidate
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.data_candidate import StrictRecord
from app.domain.models.data_connector import ConnectorManifest, ConnectorOperation, ConnectorResource, ConnectorResult
from app.domain.models.open_data_registry import connector_sources, source_domains
from app.interfaces.schemas.file import _redact_absolute_paths


class OpenDataConnectorSearchInput(StrictRecord):
    query: str = Field(min_length=1, max_length=500, description="简短数据主题关键词；跨轮保持原词以复用游标。")
    cursor: str = Field(default="", max_length=500, description="上一页返回的 next_cursor；首次查找留空。")


# Compatibility for callers that imported the original tuple catalog. Its
# content now comes from the same trusted definitions as automatic discovery.
DEFINITIONS = tuple((source.id, source.connector_title, source.description, list(source_domains(source)), source.limitation)
                    for source in connector_sources())


def open_data_registration(sources, source_id: str) -> RegisteredConnector:
    definition = next((source for source in connector_sources() if source.id == source_id), None)
    if definition is None:
        raise ValueError("Unsupported public-data registration")
    title, description = definition.connector_title, definition.description
    domains, limitation = list(source_domains(definition)), definition.limitation
    version = "1.2.0"

    async def search(owner, session, args):
        deadline = time.monotonic() + 55
        page = await sources.search(source_id, args.query, args.cursor)
        warnings = [_redact_absolute_paths(str(note))[:500] for note in page.get("warnings", [])[:10]]
        semaphore = asyncio.Semaphore(6)

        async def qualify(raw):
            async with semaphore:
                candidate = clean_candidate(raw, source_id)
                options = [option for option in await sources.verify(candidate) if option.get("available") is True]
                if not options:
                    return None
                OpenDataService._apply_verified_summary(candidate, options, datetime.now(UTC))
                candidate["availability_verified_at"] = candidate["availability_verified_at"].isoformat()
                return candidate

        tasks = [asyncio.create_task(qualify(raw)) for raw in page["results"][:20]]
        try:
            done, pending = await asyncio.wait(tasks, timeout=max(0, deadline - time.monotonic())) if tasks else (set(), set())
            incomplete = bool(pending) or any(task.exception() is not None for task in done)
            if incomplete:
                warnings.append("部分数据的实际文件尚未完成核验，已隐藏；可重新查找以重试。")
            candidates = [task.result() for task in tasks if task in done and task.exception() is None and task.result()]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if warnings and not candidates:
            raise SourceConnectorError("incomplete_search")
        resources = []
        for candidate in candidates:
            # Deliberate projection: source-private metadata, resolved URLs,
            # credentials and host paths never enter a generic tool result.
            metadata = {key: candidate[key] for key in (
                "description", "access", "formats", "size", "license", "coverage", "match_notes", "limitations", "system_notes",
                "availability_verified_at", "available_file_count")}
            metadata["system_notes"] = list(dict.fromkeys(metadata["system_notes"] + warnings))
            resources.append(ConnectorResource(id=candidate["external_id"], external_id=candidate["external_id"],
                title=candidate["title"][:200], source_url=candidate["source_url"], retrieved_at=int(time.time()), metadata=metadata))
        return ConnectorResult(connector_id=source_id, connector_version=version, operation="search", resources=resources,
            next_cursor=page.get("next_cursor") or "", total_count=None)

    descriptor = ConnectorOperation(name="search", title="查找可获取数据", description=description + " 只展示已核验实际数据的记录；每页最多 20 条，可用返回的游标继续。",
        input_schema=OpenDataConnectorSearchInput.model_json_schema(), output_schema=ConnectorResult.model_json_schema(),
        effects=["network"], timeout_seconds=60)
    operation = RegisteredOperation(descriptor, OpenDataConnectorSearchInput, search)
    manifest = ConnectorManifest(id=source_id, version=version, title=title, description=description, transport="native", auth=definition.authentication,
        domains=domains, operations=[descriptor], limitations=[limitation,
            "核验成功不代表已下载或符合分析需求；最多等待 60 秒，持续查找与文件获取请使用聊天中的数据查找功能。"])
    return RegisteredConnector(manifest, {"search": operation})


def open_data_registrations(sources=None) -> list[RegisteredConnector]:
    if sources is None:
        from app.infrastructure.external.data_sources.open_data_sources import get_open_data_sources
        sources = get_open_data_sources()
    return [open_data_registration(sources, definition.id) for definition in connector_sources()]
