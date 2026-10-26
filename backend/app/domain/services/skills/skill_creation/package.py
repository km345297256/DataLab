"""Validate and render a portable package; no model or storage calls."""
import json
import re
from typing import Any
from app.domain.services.skills.method_contract import CoverageError, MethodContract
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from .models import SkillRecipe
from .script_policy import SkillScriptValidationError, invocation_arguments, validate_script
from .privacy import ensure_context


def document_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from document_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from document_strings(item)


def validate_portable_document(document, privacy=None):
    """Apply to every generated document, including reviews and test fixtures."""
    issues = ensure_context(privacy).scan_document(document)
    if issues:
        raise SkillPackageValidationError("private_package_content", details={
            "privacy_issues": [issue.as_dict() for issue in issues[:20]]})


def validate_recipe(recipe: SkillRecipe, private_values=None):
    document = recipe.model_dump()
    validate_portable_document(document, private_values)
    if "\n" in recipe.description or any("\n" in value or not 2 <= len(value) <= 100 for value in recipe.triggers):
        raise SkillPackageValidationError("invalid_skill_metadata")
    # Python artifacts are stored as source files, never rendered as Markdown.
    # Index/call syntax such as handlers[key](value), including adjacent lines,
    # must not inherit document-link rules. Privacy still scanned the complete
    # recipe above, and every source file passes the script policy below.
    prose = recipe.model_dump(exclude={"scripts": {"__all__": {"content"}}})
    if any(re.search(r"\]\s*\(", text) for text in document_strings(prose)):
        raise SkillPackageValidationError("unverified_document_link")
    if len({parameter.name for parameter in recipe.parameters}) != len(recipe.parameters):
        raise SkillPackageValidationError("duplicate_skill_parameters")
    if {case.kind for case in recipe.validation_cases} != {"normal", "boundary", "invalid"}:
        raise SkillPackageValidationError("missing_validation_categories")
    if len({script.path for script in recipe.scripts}) != len(recipe.scripts):
        raise SkillPackageValidationError("duplicate_skill_scripts")
    for values in (recipe.applicability, recipe.data_requirements, recipe.dependencies, recipe.limitations):
        if any(not 5 <= len(value) <= 1200 for value in values):
            raise SkillPackageValidationError("incomplete_skill_conditions")
    for script in recipe.scripts:
        validate_script(script)


def validate_portable_contract(contract, privacy):
    issues = ensure_context(privacy).scan_document(contract.model_dump())
    if issues:
        raise CoverageError("private_method_contract", details={
            "privacy_issues": [issue.as_dict() for issue in issues[:20]]})

def normalize_contract_exclusions(contract: MethodContract) -> MethodContract:
    """A cited source is retained even when its result text was also excluded.

    This only fixes redundant bookkeeping. All requirements, references and
    nonoverlapping exclusions remain intact; complete structural validation and
    independent scientific review must still accept the resulting contract.
    """
    retained = {source for requirement in contract.requirements for source in requirement.source_ids}
    return contract.model_copy(update={"excluded_sources": [item for item in contract.excluded_sources
                                                            if item.source_id not in retained]})


def validate_implementation(recipe: SkillRecipe, contract: MethodContract, programs: list[dict]) -> None:
    plan = recipe.implementation
    if plan is None:
        raise CoverageError("missing_implementation_plan")
    ids = {item.id for item in contract.requirements}
    sources = {item["id"] for item in programs}
    paths = [item.path for item in plan.scripts]
    if len(set(paths)) != len(paths):
        raise CoverageError("duplicate_script_plan")
    if (plan.kind == "parameterized_scripts") != bool(plan.scripts):
        raise CoverageError("inconsistent_implementation_plan")
    for index, item in enumerate(plan.scripts):
        if not set(item.requirement_ids) <= ids or not set(item.implementation_source_ids) <= sources:
            raise CoverageError("unknown_implementation_reference")
        # The source-authoring boundary freezes this metadata. Check its
        # existing invocation contract while workflow design can still repair
        # it; regenerating Python later cannot fix a wrong frozen command.
        try:
            invocation_arguments(item)
        except SkillScriptValidationError as error:
            raise SkillScriptValidationError(error.code, "脚本调用声明无效，请先修正工作流接口。", details={
                "invocation_issues": [{"location": ["implementation", "scripts", index, "invocation"],
                                       "code": error.code}],
            }) from error
    if recipe.scripts and [item.path for item in recipe.scripts] != paths:
        raise CoverageError("unexpected_generated_script")


def build_skill_package(recipe: SkillRecipe, contract: MethodContract | None = None, *,
                        program_reference_count: int = 0) -> tuple[str, dict[str, str], dict[str, str]]:
    """An index points to complete contracts; no method text is sliced away."""
    lines = [f"# {recipe.name}", "", recipe.goal, "", "## 适用范围",
             *[f"- {item}" for item in recipe.applicability], "", "## 输入与参数",
             "执行前读取 [输入契约](references/input-contract.json)，绑定本次用户提供的数据和参数。",
             "不得复用历史会话的数据、身份或路径；输入只读，产出写入本次独立输出目录。", "", "## 方法与决策",
             "以下为流程摘要。执行前读取 [完整方法与决策](references/workflow.json)，遵循其中的条件分支、验证和限制。"]
    lines += ["完整步骤、条件分支与检查逐项保存在 workflow.json；不要用本入口替代完整方法。"]
    if contract is not None:
        lines += ["执行前读取 [方法要求与覆盖关系](references/method-contract.json)，逐项满足其中的要求，不可因篇幅省略方法或验收。"]
    lines += ["", "## 结果与质量", "按 [输出契约与验证用例](references/validation.json) 检查结果。",
              "脚本合成用例的实际运行状态见验证记录 execution；不得据此声称新数据或完整泛化工作流已经验证通过。",
              "报告本次实际完成的检查、证据和未解决限制；关键条件不满足时停止相关结论并说明缺口。", "", "## 依赖摘要",
              "完整依赖及可用性检查见 input-contract.json。", "", "## 限制",
              "完整适用边界及未运行条件见 workflow.json；不得把静态检查解释为运行验证。"]
    for script in recipe.scripts:
        lines += ["", f"[{script.path}]({script.path})：{script.purpose}",
                  "调用参数见完整方法中的 scripts；查看静态检查和合成运行记录，未验证时首次使用前运行合成用例。"]
    references = {
        "references/input-contract.json": json.dumps({"parameters": [item.model_dump() for item in recipe.parameters],
                                                     "data_requirements": recipe.data_requirements,
                                                     "dependencies": recipe.dependencies}, ensure_ascii=False, indent=2),
        "references/workflow.json": json.dumps({"goal": recipe.goal, "applicability": recipe.applicability,
                                               "steps": [item.model_dump() for item in recipe.steps], "limitations": recipe.limitations,
                                               "scripts": [item.model_dump(exclude={"content"}) for item in recipe.scripts],
                                               "implementation": recipe.implementation.model_dump() if recipe.implementation else None,
                                               "implementation_provenance": {
                                                   "successful_source_references": program_reference_count,
                                                   "source_evidence": "host_verified_successful_source" if program_reference_count else "unavailable",
                                                   "generated_scripts": "new_parameterized_implementation" if recipe.scripts else "none",
                                                   "generated_script_execution": "not_run",
                                               }}, ensure_ascii=False, indent=2),
        "references/validation.json": json.dumps({"status": "not_run", "outputs": [item.model_dump() for item in recipe.outputs],
                                                 "cases": [item.model_dump() for item in recipe.validation_cases],
                                                 "checks": {"structure": "passed", "script_static": "passed" if recipe.scripts else "not_applicable",
                                                            "workflow_execution": "not_run"}}, ensure_ascii=False, indent=2),
    }
    if contract is not None:
        references["references/method-contract.json"] = json.dumps({
            "version": 1, "requirements": [item.model_dump() for item in contract.requirements],
            "coverage": [item.model_dump() for item in recipe.coverage],
            "review": "method_coverage_checked", "execution_validation": "not_run",
        }, ensure_ascii=False, indent=2)
    return "\n".join(lines), references, {script.path: script.content for script in recipe.scripts}
