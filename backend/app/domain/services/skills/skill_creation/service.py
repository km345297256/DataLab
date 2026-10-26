"""Application boundary for creating an owned, uninstalled skill draft."""
from dataclasses import dataclass
from functools import lru_cache, partial
import hashlib
import json
import logging
from pathlib import Path
from typing import Callable

from app.core.config import get_settings
from app.application.services.skill_draft_service import SkillDraftCapacityError, get_skill_draft_service
from app.domain.models.skill_draft import SkillDraft
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.model_runtime import model_response_validation
from app.domain.services.skills.registry import SkillRegistry
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
from app.domain.services.token_usage_service import TokenUsageService
from app.infrastructure.external.llm import create_chat_model
from .evidence import prepare_analysis_context
from .gateway import SynthesisGateway
from .model_policy import resolve_skill_model_policy
from .package import build_skill_package
from .pipeline import SkillCreationPipeline
from .prompts import SYSTEM_PROMPT
from .checkpoints import BuildCheckpoints, BuildScope, content_digest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CreationDependencies:
    settings_factory: Callable
    model_factory: Callable
    draft_service_factory: Callable
    usage_recorder_factory: Callable
    response_validation: Callable
    script_verifier: Callable
    checkpoint_factory: Callable | None = None

    @classmethod
    def production(cls):
        return cls(get_settings, create_chat_model, get_skill_draft_service,
                   TokenUsageService, model_response_validation, verify_skill_script_cases,
                   get_build_checkpoints)


@lru_cache(maxsize=1)
def _creator_code_version():
    """A restarted release never trusts artifacts checked by different code."""
    directory = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*.py")):
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(path.read_bytes())
    digest.update(design_instructions().encode())
    return digest.hexdigest()


def get_build_checkpoints(*, context, policy, overrides, owner_id, session_id):
    from app.domain.services.model_route import resolve_model_route
    from app.infrastructure.external.cache import get_cache
    route = resolve_model_route(policy.settings, overrides)
    # Output roles invalidate authored artifacts, not the stable source identity
    # used to retain earlier adverse scientific decisions across upgrades.
    scope = BuildScope.create(owner_id=owner_id, session_id=session_id,
        source={"evidence": context.evidence, "sources": context.sources, "programs": context.programs,
                "privacy": content_digest([(entry.category, entry.value) for entry in context.privacy.entries])},
        configuration={"provider": route.provider, "model": policy.model_name, "api_base": route.api_base,
                       "generated_outputs": content_digest(sorted(context.privacy.generated_output_filenames)),
                       "capacity": policy.capacity, "overrides": overrides,
                       "model_settings": {key: value for key, value in policy.settings.model_dump(mode="json").items()
                                          if key.startswith("model_")}},
        code_version=_creator_code_version())
    return BuildCheckpoints(get_cache(), scope, privacy=context.privacy)


def design_instructions():
    guide = Path(__file__).resolve().parents[5] / "skills" / "skill-creator" / "SKILL.md"
    texts = [guide.read_text(encoding="utf-8")]
    texts.extend((guide.parent / "references" / name).read_text(encoding="utf-8")
                 for name in ("design-quality.md", "portable-scripts.md"))
    return SYSTEM_PROMPT + "\n\n内置设计规范：\n" + "\n\n".join(texts)


def render_result(result):
    content, references, scripts = build_skill_package(result.recipe, result.contract,
        program_reference_count=result.program_reference_count)
    validation = json.loads(references["references/validation.json"])
    validation["execution"] = result.execution
    # Detailed machine receipts can be large. Keep every field and value while
    # avoiding indentation consuming the portable package's byte allowance.
    references["references/validation.json"] = json.dumps(
        validation, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if result.synthetic_plan is not None:
        references["references/synthetic-validation.json"] = json.dumps(
            result.synthetic_plan, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if result.execution["status"] == "passed":
        workflow = json.loads(references["references/workflow.json"])
        workflow["implementation_provenance"]["generated_script_execution"] = "synthetic_cases_passed"
        references["references/workflow.json"] = json.dumps(workflow, ensure_ascii=False, indent=2)
    return content, references, scripts


async def create_skill_from_session(
    session_id: str, user_id: str, session_repository: SessionRepository, registry: SkillRegistry,
    *, llm_overrides: dict | None = None, usage_context: dict | None = None,
    dependencies: CreationDependencies | None = None, on_progress=None,
) -> SkillDraft:
    dependencies = dependencies or CreationDependencies.production()
    context = await prepare_analysis_context(session_id, user_id, session_repository, registry)
    overrides = dict(llm_overrides or {})
    overrides.pop("max_tokens", None)
    overrides["temperature"] = 0
    settings = dependencies.settings_factory()
    try:
        policy = resolve_skill_model_policy(settings, overrides)
        settings = policy.settings
        if policy.capacity is not None:
            # Pin the verified endpoint; SDK environment defaults must not
            # redirect an unset base to a compatible gateway with other limits.
            overrides["api_base"] = policy.api_base
        model = dependencies.model_factory(settings, overrides=overrides)
    except Exception as exc:
        raise SkillCreationError("method_requirements", "model_unavailable") from exc
    gateway = SynthesisGateway(model=model, system=design_instructions(), privacy=context.privacy,
        response_validation=dependencies.response_validation,
        usage_recorder=dependencies.usage_recorder_factory(), user_id=user_id, session_id=session_id,
        settings=settings, overrides=overrides, usage_context=usage_context, on_progress=on_progress,
        model_policy=policy)
    checkpoints = None
    if dependencies.checkpoint_factory is not None:
        try:
            checkpoints = dependencies.checkpoint_factory(context=context, policy=policy,
                overrides=overrides, owner_id=user_id, session_id=session_id)
        except Exception as exc:
            # Resume availability does not replace the ordinary validation path.
            logger.warning("Skill build checkpoint initialization unavailable error_type=%s", type(exc).__name__)
    try:
        verifier = dependencies.script_verifier
        if checkpoints is not None and verifier is verify_skill_script_cases:
            verifier = partial(verifier, checkpoints=checkpoints)
        result = await SkillCreationPipeline(context, gateway, script_verifier=verifier,
                                             checkpoints=checkpoints).run()
    except SkillCreationError as exc:
        logger.warning("Skill synthesis rejected stage=%s code=%s", exc.stage, exc.code)
        raise
    content, references, scripts = render_result(result)
    recipe = result.recipe
    try:
        return await dependencies.draft_service_factory().create(
            name=recipe.name, description=recipe.description, triggers=recipe.triggers, content=content,
            owner_id=user_id, session_id=session_id, applicability=recipe.applicability,
            capabilities=[step.action for step in recipe.steps], references=references, scripts=scripts,
            synthetic_execution=result.execution)
    except SkillDraftCapacityError as exc:
        raise SkillCreationError("package_storage", "package_too_large") from exc
    except (OSError, ConnectionError, TimeoutError) as exc:
        logger.warning("Skill creation storage unavailable error_type=%s", type(exc).__name__)
        raise SkillCreationError("package_storage", "storage_unavailable") from exc
