"""Author content with local requirement labels; compile locations on the host.

The model cannot reliably count references into a document it is still writing.
It assigns the supplied requirement IDs to the actual content instead. Numeric
locations exist only in the compiled, backward-compatible package and are never
accepted as author input. This is bookkeeping, not semantic approval: the
existing coverage, privacy, implementation and independent method checks remain
mandatory after compilation.
"""
from copy import deepcopy
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.domain.services.skills.method_contract import (
    COVERAGE_SECTIONS_BY_KIND, CoverageError, MethodContract,
)
from .models import (
    ImplementationPlan, OutputContract, SkillParameter, SkillRecipe,
    ValidationCase, WorkflowStep,
)


RequirementId = Annotated[str, Field(pattern=r"^R[0-9]{3,}$")]


class _LinkedContent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    requirement_ids: list[RequirementId] = Field(
        default_factory=list,
        description="Existing method_contract requirement IDs implemented by this content item. "
        "Use [] for supporting content; these labels do not replace actual implementation.",
    )


class AuthoredParameter(SkillParameter, _LinkedContent):
    pass


class AuthoredStep(WorkflowStep, _LinkedContent):
    pass


class AuthoredOutput(OutputContract, _LinkedContent):
    pass


class AuthoredValidationCase(ValidationCase, _LinkedContent):
    pass


class AuthoredCondition(_LinkedContent):
    content: str = Field(min_length=5, max_length=1200)


def _recipe_field(name):
    # Keep every product-content constraint identical to the stored recipe;
    # only the authoring form of links and string sections differs.
    result = deepcopy(SkillRecipe.model_fields[name])
    if name in {"parameters", "steps", "outputs", "validation_cases", "limitations", "data_requirements", "dependencies"}:
        kinds = [kind for kind, sections in COVERAGE_SECTIONS_BY_KIND.items() if name in sections]
        result.description = (
            "Content here can satisfy requirement kinds: " + ", ".join(kinds) +
            ". Associate requirement_ids with the item that actually implements the requirement."
            if kinds else "Supporting dependency information; requirements still need their own method, input, output or validation content."
        )
    return result


class WorkflowDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = _recipe_field("name")
    description: str = _recipe_field("description")
    triggers: list[str] = _recipe_field("triggers")
    goal: str = _recipe_field("goal")
    applicability: list[str] = _recipe_field("applicability")
    parameters: list[AuthoredParameter] = _recipe_field("parameters")
    data_requirements: list[AuthoredCondition] = _recipe_field("data_requirements")
    dependencies: list[AuthoredCondition] = _recipe_field("dependencies")
    steps: list[AuthoredStep] = _recipe_field("steps")
    outputs: list[AuthoredOutput] = _recipe_field("outputs")
    validation_cases: list[AuthoredValidationCase] = _recipe_field("validation_cases")
    limitations: list[AuthoredCondition] = _recipe_field("limitations")
    implementation: ImplementationPlan | None = _recipe_field("implementation")


_CONTENT_ROOTS = ("parameters", "data_requirements", "dependencies", "steps",
                  "outputs", "validation_cases", "limitations")
_STRING_ROOTS = frozenset(("data_requirements", "dependencies", "limitations"))


def check_workflow_draft(draft: WorkflowDraft, contract: MethodContract, validate) -> SkillRecipe:
    """Keep all recipe checks, translating failure locations to author input.

    Host-only coverage array positions are not fields the author can fix. Link
    diagnostics instead identify the exact explicit requirement labels and the
    content sections that must implement the requirement. This never adds links
    or infers whether existing content satisfies a scientific requirement.
    """
    recipe = compile_workflow(draft, contract)
    try:
        validate(recipe)
    except CoverageError as error:
        if error.code not in {"requirement_wrong_section", "incomplete_requirement_coverage",
                              "invalid_coverage_location", "unknown_coverage_location", "method_identifiers_lost"}:
            raise
        identifiers = set(error.identifiers)
        identifiers.update(item.get("requirement_id") for item in error.details.get("coverage_issues", []))
        document = draft.model_dump()
        authored = []
        for requirement in contract.requirements:
            if requirement.id not in identifiers:
                continue
            locations = []
            for root in _CONTENT_ROOTS:
                for index, item in enumerate(document[root]):
                    if requirement.id in item["requirement_ids"]:
                        locations.append([root, index, "requirement_ids"])
            if document.get("implementation"):
                for index, declaration in enumerate(document["implementation"]["scripts"]):
                    if requirement.id in declaration["requirement_ids"]:
                        locations.append(["implementation", "scripts", index, "requirement_ids"])
            sections = [["implementation", "scripts"] if section == "scripts" else [section]
                        for section in COVERAGE_SECTIONS_BY_KIND[requirement.kind]]
            authored.append({"code": error.code, "requirement_id": requirement.id,
                "requirement_kind": requirement.kind, "locations": locations,
                "allowed_sections": sections, "repair_action": "need_" + requirement.kind})
        # Keep scientific anchor diagnostics, but remove the host-generated
        # coverage-array coordinates and compiled root inventory from feedback.
        details = {key: value for key, value in error.details.items()
                   if key not in {"coverage_issues", "allowed_roots", "available_locations"}}
        details["authored_issues"] = authored
        raise CoverageError(error.code, error.identifiers, details=details) from error
    return recipe


def compile_workflow(draft: WorkflowDraft, contract: MethodContract) -> SkillRecipe:
    """Resolve only explicit item labels, without guessing or modifying content.

    Missing coverage is left missing for validate_coverage to reject. A pending
    script has a real ordered declaration, but its body is not invented here;
    validate_coverage's existing pending-script rules govern that boundary.
    """
    document = draft.model_dump()
    # Workflow authoring owns declarations only. Source files are generated and
    # validated in the implementation stage; no empty or placeholder bodies
    # are accepted as workflow content. The stored recipe format is unchanged.
    document["scripts"] = []
    locations = {requirement.id: [] for requirement in contract.requirements}

    def bind(identifiers, pointer, label_location):
        seen = set()
        issues = []
        for index, identifier in enumerate(identifiers):
            code = ("unknown_implementation_reference" if identifier not in locations else
                    "duplicate_requirement_coverage" if identifier in seen else None)
            seen.add(identifier)
            if code:
                # Return only schema-owned coordinates and trusted contract IDs.
                # Arbitrary model text never enters diagnostics.
                issues.append({"code": code, "location": [*label_location, index]})
            elif pointer not in locations[identifier]:
                locations[identifier].append(pointer)
        if issues:
            raise CoverageError(issues[0]["code"], details={
                "binding_issues": issues, "available_requirement_ids": list(locations),
            })

    for root in _CONTENT_ROOTS:
        for index, item in enumerate(document[root]):
            bind(item.pop("requirement_ids"), f"/{root}/{index}", [root, index, "requirement_ids"])
            if root in _STRING_ROOTS:
                document[root][index] = item["content"]

    plan = document.get("implementation")
    if plan:
        declarations = plan["scripts"]
        for index, declaration in enumerate(declarations):
            bind(declaration["requirement_ids"], f"/scripts/{index}",
                 ["implementation", "scripts", index, "requirement_ids"])

    document["coverage"] = [
        {"requirement_id": identifier, "locations": pointers}
        for identifier, pointers in locations.items() if pointers
    ]
    return SkillRecipe.model_validate(document)
