"""Connect authoring progress to existing durable events or REST audit records."""
import asyncio
import logging

from app.domain.models.audit import AuditStatus
from app.domain.models.skill_creation_progress import SkillCreationProgressView
from app.domain.services.skills.skill_creation_error import SkillCreationError

logger = logging.getLogger(__name__)


async def record_creation_audit(audit, *, owner_id, session_id, call_id, progress):
    await audit.record(actor_user_id=owner_id, action="skill.creation_progress",
        resource_type="skill_creation", resource_id=call_id, session_id=session_id,
        status=AuditStatus.FAILED if progress.status in {"failed", "cancelled"} else AuditStatus.SUCCESS,
        metadata=progress.model_dump(mode="json"))


class SkillCreationProgressRecorder:
    def __init__(self, sink):
        self.sink = sink
        self.latest = SkillCreationProgressView(stage="source_analysis", status="running", attempt=0)

    async def record(self, progress):
        # Project only host-authored enums and counters. In particular, do not
        # serialize the callback object or any attached candidate/diagnostics.
        view = SkillCreationProgressView(stage=progress.stage, status=progress.status,
            attempt=progress.attempt, code=getattr(progress, "reason", getattr(progress, "code", None)))
        self.latest = view
        await self.sink(view)

    async def _terminal(self, *, stage, status, code=None):
        attempt = self.latest.attempt if self.latest.stage == stage else 0
        try:
            async with asyncio.timeout(2):
                await self.record(SkillCreationProgressView(stage=stage, status=status, attempt=attempt, code=code))
        except Exception as exc:
            # Preserve the original failure/cancellation or a committed draft.
            # Normal running progress still fails visibly if persistence fails.
            logger.warning("Skill creation terminal progress unavailable error_type=%s", type(exc).__name__)

    async def run(self, operation):
        try:
            result = await operation(self.record)
        except asyncio.CancelledError:
            await self._terminal(stage=self.latest.stage, status="cancelled")
            raise
        except SkillCreationError as exc:
            await self._terminal(stage=exc.stage, status="failed", code=exc.code)
            raise
        except Exception:
            await self._terminal(stage=self.latest.stage, status="failed")
            raise
        await self._terminal(stage="package_storage", status="passed" if result is not None else "failed")
        return result
