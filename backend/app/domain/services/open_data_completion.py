"""Confirm durable acquisition submissions without inventing sandbox artifacts."""
import asyncio
import re
from dataclasses import dataclass, field

from app.domain.models.event import ToolEvent, ToolStatus


@dataclass
class AcquisitionEvidence:
    jobs: list[dict] = field(default_factory=list)
    unconfirmed: bool = False
    needs_handoff: bool = False


async def read_acquisition_evidence(events, *, owner, session, message, service=None):
    """A tool response is just a locator; the owned persistent job is authority."""
    evidence = AcquisitionEvidence()
    if not events:
        return evidence
    if service is None:
        try:
            from app.application.services.open_data_service import get_open_data_service
            service = get_open_data_service()
        except Exception:
            evidence.unconfirmed = True
            return evidence
    handed_off = {
        info.file_id for info in message.attachment_file_infos
        if info.file_id in message.attachment_file_ids and info.file_path in message.attachments
        and isinstance(info.file_path, str) and info.file_path.startswith("/home/ubuntu/")
    }
    # An explicit retry of the same selection replaces its earlier failed
    # attempt. A failure for another selection must remain visible.
    latest = {}
    for index, event in enumerate(events):
        if (not isinstance(event, ToolEvent) or event.tool_name != "data_discovery"
                or event.function_name != "data_acquire" or event.status != ToolStatus.CALLED):
            continue
        arguments = event.function_args or {}
        try:
            selection = arguments["file_ids"]
            if not isinstance(selection, list) or not selection or not all(isinstance(value, str) for value in selection):
                raise ValueError()
            key = (arguments["search_id"], arguments["candidate_id"], tuple(sorted(selection)))
            latest[key] = event
        except (KeyError, TypeError, ValueError):
            latest[("invalid", index)] = event
    seen = set()
    for event in latest.values():
        try:
            result = event.function_result
            if not isinstance(result, dict):
                result = result.model_dump() if callable(getattr(result, "model_dump", None)) else None
            if not isinstance(result, dict):
                raise ValueError("invalid tool result")
            if result.get("success") is not True:
                evidence.unconfirmed = True
                continue
            data, arguments = result.get("data"), event.function_args or {}
            job_id = data.get("id") if isinstance(data, dict) else None
            if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
                raise ValueError("missing job")
            async with asyncio.timeout(10):
                job = await service.get_acquisition(owner, session, job_id)
            payload = job.model_dump(mode="json")
            if (payload["id"] != job_id or payload["session_id"] != session
                    or payload["search_id"] != arguments.get("search_id")
                    or payload["candidate_id"] != arguments.get("candidate_id")
                    or set(item["id"] for item in payload["items"]) != set(arguments.get("file_ids") or [])
                    or not payload["items"]
                    or payload["status"] not in {"queued", "downloading", "ready", "partial"}):
                raise ValueError("unconfirmed job")
            file_ids = {item["file_id"] for item in payload["files"]}
            if payload["status"] != "ready" or not file_ids or not file_ids.issubset(handed_off):
                evidence.needs_handoff = True
            if job_id not in seen:
                evidence.jobs.append({"id": job_id, "status": payload["status"]})
                seen.add(job_id)
        except Exception:
            evidence.unconfirmed = True
    return evidence


def acquisition_notice(evidence):
    if evidence.jobs and not evidence.unconfirmed:
        if all(job["status"] == "ready" for job in evidence.jobs):
            return "获取任务已完成，实际文件已保存在数据卡片和我的数据中。文件是否适合当前分析仍需检查；可从卡片加入分析输入后继续。"
        return "已确认提交数据获取任务。文件正在后台获取或部分文件尚未完成，请查看数据卡片中的实际进度；本轮尚未完成下载或数据分析。完成后可将文件加入分析输入继续。"
    if evidence.jobs:
        return "部分数据获取任务已确认提交，成功任务的进度仍保留在数据卡片中；另有获取请求尚未确认成功。请核对卡片后只重试未完成的选择，本轮未确认全部下载完成，也未完成数据分析。"
    return "尚未确认数据获取任务已成功提交，请查看数据卡片状态后再决定是否重试。本轮未确认下载完成，也未进行数据分析。"
