import json
from uuid import uuid4

from langchain.tools import tool
from typing import ClassVar

from app.domain.models.tool_result import ToolResult
from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.skills.skill_creation.service import create_skill_from_session
from app.domain.services.skills.skill_creation.progress import SkillCreationProgressRecorder, record_creation_audit
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.registry import SkillRegistry
from app.domain.services.tools.base import BaseToolkit


class SkillToolkit(BaseToolkit):
    name: str = "skill"
    # Preparation tools only. Fast-path analysis cannot create/install a skill.
    dataset_fast_path_tool_names: ClassVar[set[str]] = {
        "skill_list", "skill_read", "skill_list_resources",
        "skill_read_reference", "skill_read_script",
    }
    # Whole references only: never silently shorten a scientific method. Larger
    # files remain explicitly discoverable through skill_read_reference.
    MAX_INLINE_REFERENCE_BYTES: ClassVar[int] = 32_000
    EXECUTION_VALIDATION_STATES: ClassVar[set[str]] = {
        "passed", "failed", "not_run", "unavailable", "unsupported", "not_applicable",
    }

    def __init__(
        self,
        registry: SkillRegistry,
        session_id: str | None = None,
        user_id: str | None = None,
        session_repository: SessionRepository | None = None,
        llm_overrides: dict | None = None,
        usage_context: dict | None = None,
    ):
        super().__init__()
        self.registry = registry
        self.session_id = session_id
        self.user_id = user_id
        self.session_repository = session_repository
        self.llm_overrides = dict(llm_overrides or {})
        self.usage_context = dict(usage_context or {})

    def _validation_summary(self, name: str) -> dict:
        """Project package records without promoting static/self-claimed checks.

        The registry reads the same pinned package bytes as skill/script reads.
        Imported packages can contain arbitrary records, so this is never a
        host-verification receipt or evidence about the current user's data.
        """
        summary = {
            "state": "unknown",
            "source": "references/validation.json",
            "scope": "unknown",
            "recorded_only": True,
            "current_data_validated": False,
            "case_counts": {},
        }
        success, content = self.registry.read_reference(name, "validation.json")
        if not success:
            summary["reason"] = "validation_reference_unavailable"
            return summary
        try:
            payload = json.loads(content)
        except (ValueError, RecursionError):
            summary["reason"] = "validation_reference_invalid"
            return summary
        execution = payload.get("execution") if isinstance(payload, dict) else None
        if not isinstance(execution, dict):
            # Earlier packages have top-level status/checks for static checks.
            # Those do not say whether any synthetic case was actually run.
            summary["reason"] = "execution_record_missing"
            return summary
        state = execution.get("status")
        if not isinstance(state, str) or state not in self.EXECUTION_VALIDATION_STATES or execution.get("scope") != "synthetic":
            summary["reason"] = "execution_record_invalid"
            return summary
        summary["state"] = state
        summary["scope"] = "synthetic"
        cases = execution.get("cases")
        if isinstance(cases, list):
            for case in cases:
                if (isinstance(case, dict) and isinstance(case.get("kind"), str)
                        and case["kind"] in {"normal", "boundary", "invalid"}):
                    kind = case["kind"]
                    summary["case_counts"][kind] = summary["case_counts"].get(kind, 0) + 1
        return summary

    @tool
    async def skill_list(self) -> ToolResult:
        """List local agent Skills only. Skills are prompt/workflow guidance, not MCP servers and not MCP tools. Do not use this for MCP questions."""
        skills = [
            {
                "name": skill.name,
                "description": skill.description,
                "triggers": skill.triggers,
                "package_id": skill.manifest.package_id if skill.manifest else None,
                "version": skill.manifest.version if skill.manifest else None,
                "resources": {
                    "scripts": skill.scripts,
                    "references": skill.references,
                    "templates": skill.templates,
                },
            }
            for skill in self.registry.list_skills()
        ]
        return ToolResult(success=True, data=skills)

    @tool
    async def skill_read(self, name: str) -> ToolResult:
        """Read a local Skill's full instructions, complete bounded references and recorded synthetic-validation state. Inspect deferred references and scripts before use. Package records do not verify current data; an unknown state is not a pass. Skills are not MCP tools."""
        skill = self.registry.get_skill(name)
        if not skill:
            return ToolResult(success=False, message=f"Skill not found: {name}")
        content = self.registry.read_skill_body(name)
        references = {}
        deferred_references = []
        remaining_bytes = self.MAX_INLINE_REFERENCE_BYTES
        for filename in skill.references:
            success, reference = self.registry.read_reference(name, filename)
            byte_count = len(reference.encode("utf-8")) if success else 0
            if success and byte_count <= remaining_bytes:
                references[filename] = reference
                remaining_bytes -= byte_count
            else:
                deferred_references.append(filename)
        return ToolResult(
            success=True,
            data={
                "name": skill.name,
                "description": skill.description,
                "content": content or skill.content,
                "contract": skill.manifest.model_dump(mode="json") if skill.manifest else None,
                "reference_contents": references,
                "deferred_references": deferred_references,
                "implementation": "scripts_available" if skill.scripts else "method_only",
                "validation": self._validation_summary(name),
                "resources": {
                    "scripts": skill.scripts,
                    "references": skill.references,
                    "templates": skill.templates,
                },
            },
        )

    @tool
    async def skill_list_resources(self, name: str) -> ToolResult:
        """List scripts, references, and templates available in a local agent Skill package."""
        resources = self.registry.list_skill_resources(name)
        if resources is None:
            return ToolResult(success=False, message=f"Skill not found: {name}")
        skill = self.registry.get_skill(name)
        return ToolResult(
            success=True,
            data={
                "name": name,
                "resources": resources,
                "scripts": skill.scripts if skill else [],
                "references": skill.references if skill else [],
                "templates": skill.templates if skill else [],
            },
        )

    @tool
    async def skill_read_reference(self, skill_name: str, ref_filename: str) -> ToolResult:
        """Read a reference file from a local agent Skill references/ directory. Call skill_read first to discover references."""
        success, content = self.registry.read_reference(skill_name, ref_filename)
        return ToolResult(
            success=success,
            message=None if success else content,
            data={
                "skill_name": skill_name,
                "ref_filename": ref_filename,
                "content": content,
            } if success else None,
        )

    @tool
    async def skill_read_script(self, skill_name: str, script_filename: str) -> ToolResult:
        """Read a local Skill script and its package's recorded validation state before use. Unknown/failed scripts need small method-derived synthetic checks and repair before processing user data. Reading or explaining a script does not authorize running it."""
        success, content = self.registry.read_script_content(skill_name, script_filename)
        return ToolResult(
            success=success,
            message=None if success else content,
            data={
                "skill_name": skill_name,
                "script_filename": script_filename,
                "content": content,
                "validation": self._validation_summary(skill_name),
            } if success else None,
        )

    @tool
    async def skill_create_from_session(self) -> ToolResult:
        """Prepare a private downloadable Skill draft from the latest completed analysis when the user explicitly requests creation. The result card explains the method and lets the user choose Add to my skills or Download; creation never installs or enables the skill. No commands are replayed. Static checks do not validate the workflow on new data."""
        if not self.session_id or not self.user_id or not self.session_repository:
            failure = SkillCreationError("source_analysis", "session_unavailable")
            return self._creation_failure_result(failure)
        call_id = self.current_tool_call_id() or "skill-creator-" + uuid4().hex
        async def progress_sink(progress):
            sink = getattr(self, "creation_progress_sink", None)
            if sink is not None:
                await sink(ToolEvent(tool_call_id=call_id, tool_name="skill",
                    function_name="skill_create_from_session", function_args={}, status=ToolStatus.CALLING,
                    skill_creation_progress=progress))
            else:
                from app.domain.services.audit_service import get_audit_service
                await record_creation_audit(get_audit_service(), owner_id=self.user_id,
                    session_id=self.session_id, call_id=call_id, progress=progress)
        recorder = SkillCreationProgressRecorder(progress_sink)
        try:
            draft = await recorder.run(lambda on_progress: create_skill_from_session(
                session_id=self.session_id,
                user_id=self.user_id,
                session_repository=self.session_repository,
                registry=self.registry,
                llm_overrides=self.llm_overrides,
                usage_context=self.usage_context,
                on_progress=on_progress,
            ))
        except SkillCreationError as exc:
            # Only known, host-authored diagnostics are recoverable. A bug or
            # an unknown storage outcome must not become a false success.
            return self._creation_failure_result(exc)

        return ToolResult(
            success=True,
            message=f"Skill draft created: {draft.name}. Awaiting the user's choice to add it to their skills.",
            data=draft.public_view().model_dump(mode="json"),
        )

    @staticmethod
    def _creation_failure_result(error: SkillCreationError) -> ToolResult:
        return ToolResult(success=False, message=error.public_message, data={
            "skill_creation_failure": {
                "stage": error.stage, "code": error.code,
                "message": error.public_message, "retryable": error.retryable,
            },
        })
