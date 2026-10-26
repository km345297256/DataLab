"""Backward-compatible imports for the skill creation service.

Implementation lives in skill_creation: evidence, privacy, typed model gateway,
explicit design pipeline, static policy and draft persistence are independent.
New callers should import their required boundary directly.
"""
import re
from .skill_creation.service import create_skill_from_session
from .skill_creation.models import (
    SkillParameter, WorkflowStep, OutputContract, ValidationCase, HelperScript,
    ScriptRequest, ImplementationPlan, SkillRecipe,
)
from .skill_creation.intent import is_explicit_skill_create_command, is_skill_create_request
from .skill_creation.evidence import select_analysis_turn, build_synthesis_evidence, _verified_turn
from .skill_creation.package import validate_recipe, build_skill_package, normalize_contract_exclusions
from .skill_creation.schema_diagnostics import _schema_diagnostics, _schema_error_fields
from .skill_creation.script_policy import SkillScriptValidationError, validate_script as _validate_script

# Legacy callers of program_method_evidence may still supply this matcher.
# The creation pipeline exclusively uses typed, per-field PrivacyContext.
_PRIVATE = re.compile(
    r"(?:https?://[^\s<>\]\[\"'`]+|(?:/Users|/home|/root|/private|/tmp|/var|/mnt|/workspace|/data|/app|/opt)/[^\s<>\]\[\"'`]+|"
    r"[A-Za-z]:[\\/][^\s\"'`]+|(?:file|dataset|session|user|workspace)[_-]?id\s*[:=]\s*[^\s,;]+|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{24,64}\b|"
    r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+|(?:api[_-]?key|password|secret|token|authorization)\s*[:=]\s*[^\s,;]+)",
    re.IGNORECASE,
)
