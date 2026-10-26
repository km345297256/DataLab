"""Explicit design stages, independent review and isolated synthetic execution.

The pipeline has no session repository or draft storage access. A negative
scientific verdict requires a changed implementation, never a reviewer retry.
"""
import ast
from dataclasses import dataclass
import json
import re
from pydantic import ValidationError

from app.domain.services.skills.method_contract import (
    CoverageError, MethodContract, MethodReview, validate_contract, validate_coverage, validate_review,
)
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError, SkillRepairReviewRejected
from app.domain.services.skills.skill_validation_review import oracle_decision_identity, oracle_scope_identity
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from .models import HelperScript, SkillRecipe
from .package import (
    validate_implementation, validate_portable_contract, validate_portable_document, validate_recipe,
)
from .prompts import (
    METHOD_REQUIREMENTS_INSTRUCTION, WORKFLOW_INSTRUCTION, IMPLEMENTATION_INSTRUCTION,
    REVISION_INSTRUCTION, REVIEW_INSTRUCTION, IMPLEMENTATION_REVISION_INSTRUCTION,
)
from .script_policy import SkillScriptValidationError
from .workflow_authoring import WorkflowDraft, compile_workflow, check_workflow_draft
from .checkpoints import content_digest
from .privacy import generation_privacy


@dataclass(frozen=True)
class CreationResult:
    recipe: SkillRecipe
    contract: MethodContract
    synthetic_plan: dict | None
    execution: dict
    program_reference_count: int


def method_content(recipe):
    document = recipe.model_dump(exclude={"name", "description", "triggers", "coverage"})
    for script in document["scripts"]:
        script["content"] = ast.dump(ast.parse(script["content"]), include_attributes=False)
        script.pop("purpose", None)
        script.pop("invocation", None)
    document["implementation"].pop("reason", None)
    for script in document["implementation"]["scripts"]:
        for key in ("purpose", "invocation", "requirement_ids", "implementation_source_ids"):
            script.pop(key, None)
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


class SkillCreationPipeline:
    def __init__(self, context, gateway, *, script_verifier, checkpoints=None):
        self.context, self.gateway = context, gateway
        self.script_verifier = script_verifier
        self.contract = None
        self.checkpoints = checkpoints
        self._method_revision_kinds = set()

    async def generate(self, schema, stage, instruction, payload, check=None):
        privacy = generation_privacy(self.context.privacy, schema, stage, payload)
        def check_generated(value):
            # Reviews and frozen synthetic fixtures are generated artifacts too.
            # They share the same privacy boundary as the recipe and scripts.
            if isinstance(value, MethodContract):
                validate_portable_contract(value, privacy)
            else:
                validate_portable_document(value, privacy)
            if check is not None:
                check(value)
        self.gateway.stage = stage
        produced = False
        async def produce():
            nonlocal produced
            produced = True
            return await self.gateway.generate(schema, stage, instruction, payload, check_generated, contract=self.contract)
        if self.checkpoints is None:
            return await produce()
        decision_identity = None
        if stage == "method_review":
            decision_identity = {"contract": self.contract, "sources": self.context.sources,
                                 "method": method_content(SkillRecipe.model_validate(payload["skill_package"]))}
        elif stage == "synthetic_reference_review":
            from app.domain.services.skills.skill_reference_verification import reference_review_identity
            decision_identity = reference_review_identity(payload)
        elif stage == "synthetic_plan_review":
            plan = SyntheticValidationPlan.model_validate(payload["validation_plan"])
            decision_identity = {"contract": self.contract, "sources": self.context.sources,
                                 "interfaces": payload.get("script_interfaces"),
                                 "plan": oracle_decision_identity(plan),
                                 "scope": oracle_scope_identity(plan, payload.get("review_scope"))}
        elif stage == "synthetic_method_audit":
            from app.domain.services.skills.skill_candidate_method_audit import candidate_method_audit_identity
            decision_identity = candidate_method_audit_identity(payload)
        value = await self.checkpoints.run(schema, stage, instruction, payload,
            check_generated, produce, decision_identity=decision_identity)
        if not produced:
            await self.gateway._progress(stage, 0, "passed")
        return value

    def check_contract(self, value):
        validate_contract(value, self.context.sources)
        validate_portable_contract(value, self.context.privacy)

    def check_recipe(self, value, *, pending=False):
        validate_recipe(value, self.context.privacy)
        validate_implementation(value, self.contract, self.context.programs)
        validate_coverage(self.contract, value.model_dump(),
            pending_scripts=[item.model_dump() for item in value.implementation.scripts] if pending else None)

    async def extract_requirements(self):
        self.contract = await self.generate(MethodContract, "method_requirements", METHOD_REQUIREMENTS_INSTRUCTION,
            {"sources": self.context.sources,
             "successful_tool_capabilities": self.context.evidence["successful_tool_capabilities"]}, self.check_contract)

    async def design_package(self, revision=None):
        mark = self.checkpoints.mark() if self.checkpoints is not None else None
        revision_payload = {"method_revision": revision} if revision is not None else {}
        # Author content and its requirement IDs together. Only the host knows
        # the final array positions; models never author stored JSON pointers.
        def check_draft(value):
            check_workflow_draft(value, self.contract, lambda recipe: self.check_recipe(recipe, pending=True))

        draft = await self.generate(WorkflowDraft, "workflow",
            WORKFLOW_INSTRUCTION + (REVISION_INSTRUCTION if revision is not None else ""),
            {"analysis_evidence": self.context.evidence, "sources": self.context.sources,
             "method_contract": self.contract.model_dump(), "implementation_sources": self.context.programs,
             **revision_payload}, check_draft)
        candidate = compile_workflow(draft, self.contract)
        generated = []
        for request in candidate.implementation.scripts:
            def check_script(value):
                if value.path != request.path:
                    raise CoverageError("unexpected_generated_script")
                partial = candidate.model_copy(update={"scripts": [*generated, value]})
                validate_recipe(partial, self.context.privacy)
                # Resolve coverage as soon as all its referenced content
                # exists, inside the generator's repair boundary. A helper
                # missing an assigned method must never become a checkpoint
                # that fails only when the final package is assembled.
                ready = []
                for binding in partial.coverage:
                    future = any((match := re.match(r"^/scripts/(\d+)(?:/|$)", pointer))
                                 and int(match[1]) >= len(partial.scripts)
                                 for pointer in binding.locations)
                    if not future:
                        ready.append(binding)
                identifiers = {binding.requirement_id for binding in ready}
                contract = self.contract.model_copy(update={"requirements": [
                    item for item in self.contract.requirements if item.id in identifiers]})
                document = partial.model_dump()
                document["coverage"] = [binding.model_dump() for binding in ready]
                validate_coverage(contract, document)
            generated.append(await self.generate(HelperScript, "implementation",
                IMPLEMENTATION_INSTRUCTION + (REVISION_INSTRUCTION if revision is not None else ""),
                {"script_request": request.model_dump(), "method_contract": self.contract.model_dump(),
                 "sources": self.context.sources, "workflow": candidate.model_dump(exclude={"scripts"}),
                 "implementation_sources": self.context.programs, **revision_payload}, check_script))
        if generated:
            candidate.scripts = generated
        self.gateway.stage = "package_validation"
        try:
            self.check_recipe(candidate)
        except (CoverageError, SkillScriptValidationError, SkillPackageValidationError):
            if self.checkpoints is not None:
                await self.checkpoints.discard_since(mark, stages={"workflow", "implementation"})
            raise
        return candidate

    def check_review_shape(self, value):
        try:
            validate_review(value, self.contract, self.context.sources)
        except CoverageError as exc:
            if exc.code != "method_review_rejected":
                raise

    async def review(self, recipe):
        return await self.generate(MethodReview, "method_review", REVIEW_INSTRUCTION,
            {"sources": self.context.sources, "method_contract": self.contract.model_dump(),
             "skill_package": recipe.model_dump(), "implementation_sources": self.context.programs},
            self.check_review_shape)

    async def review_and_revise(self, recipe):
        # A rejected method design and a rejected executable implementation
        # need different authoring transactions. Each kind has one opportunity
        # per run; neither can obtain a new vote on unchanged code or methods.
        for _ in range(3):
            review = await self.review(recipe)
            try:
                validate_review(review, self.contract, self.context.sources)
            except CoverageError as exc:
                if exc.code != "method_review_rejected":
                    raise
                if self.checkpoints is not None:
                    await self.checkpoints.save_revision({"recipe": recipe, "review": review,
                        "contract_digest": content_digest(self.contract)}, check=self.check_revision)
                recipe = await self.revise_rejected_candidate(recipe, review)
            else:
                return recipe
        raise AssertionError("Unreachable review boundary")

    def implementation_only_revision(self, recipe, review):
        excluded = {item.source_id for item in self.contract.excluded_sources}
        return bool(recipe.scripts) and not review.implementation_appropriate and (
            all(item.status == "preserved" for item in review.requirements)
            and all(item.status == ("valid_exclusion" if item.source_id in excluded else "preserved")
                    for item in review.sources))

    async def revise_rejected_candidate(self, recipe, review):
        kind = "implementation" if self.implementation_only_revision(recipe, review) else "workflow"
        if kind in self._method_revision_kinds:
            raise CoverageError("method_review_rejected")
        self._method_revision_kinds.add(kind)
        if kind == "implementation":
            return await self.revise_implementation(recipe, review)
        return await self.design_revision(recipe, review)

    async def revise_implementation(self, previous, review):
        from .implementation_revision import ImplementationEdits, ImplementationEditError, apply_implementation_edits

        self.check_recipe(previous)
        self.check_review_shape(review)
        if not self.implementation_only_revision(previous, review):
            raise CoverageError("method_review_rejected")
        original_method = method_content(previous)

        def check_edits(value):
            candidate = apply_implementation_edits(previous, value)
            try:
                # Applying literal edits preserves bytes; independently check
                # that the complete result still satisfies the stored schema.
                SkillRecipe.model_validate(candidate.model_dump())
            except ValidationError:
                raise ImplementationEditError("invalid_result") from None
            self.check_recipe(candidate)
            if method_content(candidate) == original_method:
                raise CoverageError("method_revision_unchanged")

        edits = await self.generate(ImplementationEdits, "implementation_revision",
            IMPLEMENTATION_REVISION_INSTRUCTION,
            {"review": review.model_dump(), "previous_skill_package": previous.model_dump(),
             "method_contract": self.contract.model_dump(), "sources": self.context.sources,
             "implementation_sources": self.context.programs}, check_edits)
        return apply_implementation_edits(previous, edits)

    def check_revision(self, revision):
        if revision.contract_digest != content_digest(self.contract):
            raise ValueError("The method contract changed")
        self.check_recipe(revision.recipe)
        self.check_review_shape(revision.review)

    async def design_revision(self, previous, review):
        mark = self.checkpoints.mark() if self.checkpoints is not None else None
        recipe = await self.design_package({"review": review.model_dump(),
                                           "previous_skill_package": previous.model_dump()})
        if method_content(recipe) == method_content(previous):
            if self.checkpoints is not None:
                await self.checkpoints.discard_since(mark, stages={"workflow", "implementation"})
            raise SkillCreationError("method_review", "method_revision_unchanged")
        return recipe

    async def initial_package(self):
        revision = (await self.checkpoints.get_revision(check=self.check_revision)
                    if self.checkpoints is not None else None)
        if revision is None:
            return await self.design_package()
        return await self.revise_rejected_candidate(revision.recipe, revision.review)

    async def review_repaired_script(self, recipe):
        review = await self.review(recipe)
        # The verifier retains this pending candidate with the exact frozen
        # plan before review. A rejected execution repair must not fall back
        # into whole-workflow authoring and silently replace its test basis.
        try:
            validate_review(review, self.contract, self.context.sources)
        except CoverageError as error:
            if error.code == "method_review_rejected":
                raise SkillRepairReviewRejected(review) from error
            raise

    async def run(self):
        self._method_revision_kinds.clear()
        try:
            await self.extract_requirements()
            recipe = await self.review_and_revise(await self.initial_package())
            self.gateway.stage = "synthetic_execution"
            recipe, plan, execution = await self.script_verifier(
                recipe, self.contract, self.context.sources, generate=self.generate,
                revalidate=self.check_recipe, review=self.review_repaired_script)
            return CreationResult(recipe, self.contract, plan, execution, len(self.context.programs))
        except (CoverageError, SkillScriptValidationError, SkillPackageValidationError) as exc:
            raise SkillCreationError(self.gateway.stage, exc.code) from exc
