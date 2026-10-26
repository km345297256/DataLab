"""Public creation failures and fixed repair diagnostics, without model data."""
from __future__ import annotations

STAGE_LABELS = {
    "source_analysis": "分析依据检查", "method_requirements": "方法提炼",
    "workflow": "技能工作流设计", "implementation": "脚本生成", "implementation_revision": "实现修订",
    "method_review": "方法完整性审查", "synthetic_validation_plan": "合成验证设计",
    "synthetic_plan_review": "合成预期独立审查",
    "synthetic_reference_implementation": "独立参考计算准备",
    "synthetic_reference_review": "独立参考方法审查",
    "synthetic_reference_execution": "独立参考计算核验",
    "synthetic_build_repair": "程序构建修复",
    "synthetic_method_audit": "方法实现聚焦审查", "synthetic_method_repair": "方法实现修订",
    "synthetic_script_repair": "脚本验证与修复", "synthetic_execution": "合成用例执行",
    "package_validation": "技能包检查", "package_storage": "技能包保存",
}

# These explanations are developer-authored. No exception message, model value,
# source filename or provider response is suitable for public error text.
RECIPE_ERRORS = {
    "implementation_revision_invalid": ("实现修订未能准确对应原脚本，尚未应用修改。", "Return edits for existing scripts only. Each nonempty before must match the unchanged original source exactly once; use sufficient literal context, preserve whitespace, and keep all selected ranges nonoverlapping. Do not edit metadata or reference the result of another edit. Keep every resulting script complete and valid under the original package schema. Provide a substantive code change rather than an unchanged replacement."),
    "private_package_content": ("生成内容仍包含会话专属信息，需要重新参数化。", "Remove source-specific identities, paths, URLs, filenames and credentials; replace them with current-input parameters."),
    "invalid_skill_metadata": ("技能说明或触发词格式不完整。", "Keep description and trigger phrases on one line; each trigger must contain 2 to 100 characters."),
    "unverified_document_link": ("生成内容包含未经校验的文档链接。", "Remove Markdown links from generated descriptions; preserve the method as plain text. The host creates validated package links."),
    "duplicate_skill_parameters": ("技能参数名称重复。", "Give each input parameter a unique name without deleting required parameters."),
    "missing_validation_categories": ("验证用例缺少正常、边界或无效输入场景。", "Include all three validation case kinds: normal, boundary and invalid."),
    "duplicate_skill_scripts": ("技能包含重复脚本路径。", "Use unique declared script paths and preserve each required computation."),
    "incomplete_skill_conditions": ("适用条件、依赖或限制说明不完整。", "Write a complete explanation for each applicability, data_requirements, dependencies and limitations item (5 to 1200 characters)."),
}

COVERAGE_CODES = frozenset({
    "duplicate_requirements", "duplicate_source_references", "unknown_requirement_source", "duplicate_exclusions",
    "unknown_source_exclusion", "source_exclusion_overlap", "missing_source_coverage", "method_identifiers_lost",
    "invalid_coverage_location", "unknown_coverage_location", "invalid_pending_script_declaration", "duplicate_script_plan",
    "duplicate_requirement_coverage", "incomplete_requirement_coverage", "requirement_wrong_section", "undeclared_script_requirement",
    "duplicate_source_review", "duplicate_requirement_review", "incomplete_method_review", "method_review_rejected",
    "private_method_contract", "missing_implementation_plan", "inconsistent_implementation_plan",
    "unknown_implementation_reference", "unexpected_generated_script", "method_revision_unchanged",
})
SCRIPT_FAILURE_REASONS = {
    "script_syntax_invalid": "脚本存在语法错误，无法解析。需要修正语法或补全脚本后重试。",
    # Retain the historical code without claiming that an import rejection
    # proves a missing package, a dangerous operation, or an input write.
    "script_dependency_unsupported": "脚本的依赖导入未通过检查。请确认验证环境中的依赖可用；旧版本的导入限制可在升级后重试。",
    "script_unsafe_operation": "脚本包含未通过安全检查的操作，需要调整实现后重试。",
    "script_arguments_missing": "脚本缺少明确的输入、输出命令行参数，需要补齐运行入口后重试。",
    "script_distinct_path_guard_missing": "尚未确认输入与输出路径不同，无法排除覆盖原始输入的风险。需要补齐路径检查后重试。",
    "script_output_destination_unknown": "无法确定脚本的文件写入位置，需要明确输出目标后重试。",
    "script_open_mode_unknown": "无法确定文件是只读打开还是用于写入，需要明确文件打开方式后重试。",
    "script_output_not_isolated": "尚未确认所有写入都限制在输出位置，无法保证输入只读。需要修正写入位置后重试。",
    "script_invocation_invalid": "脚本的运行示例与声明的脚本或输入、输出参数不一致，需要修正调用方式后重试。",
}
SCRIPT_CODES = frozenset(SCRIPT_FAILURE_REASONS)
VALIDATION_ERRORS = {
    "candidate_method_audit_invalid": "Return exactly one source-bound assessment per requested script and requirement. Preserve any adverse judgment. Cite only the unchanged requirement's original source and an exact executable portion of the same candidate; do not infer claims from tests or invent new requirements.",
    "reference_output_interface_unconfirmed": "Fix the proven conflict at the reported JSON output location. Preserve the original declared column-name prefix and structure; do not rename, reorder, or remove its fixed fields. No specific source-code form is required: dynamic structure is checked independently against real outputs by the host. Do not invent names or expose scientific values as schema.",
    "validation_oracle_review_incomplete": "Return exactly one script review, one review per case and one review per host-assigned assertion target. Include every input-validity, exit-behavior, method-coverage and tolerance assessment without changing a scientific verdict to satisfy structure.",
    "validation_oracle_revision_invalid": "Correct only independently justified expectation errors. Preserve original scripts, cases, kinds, inputs, argv, requirement coverage and assertion targets; never delete checks or increase tolerances. Add needed coverage without weakening existing tests.",
    "validation_oracle_revision_unchanged": "Return a substantively corrected validation plan based on the independent review; do not resubmit the unchanged rejected plan or ask the reviewer to reverse its verdict.",
    "validation_output_binding_invalid": "The script output_mode is file. Bind --output to {output}/a-filename using the script's declared output format, never to the directory {output}. Keep all scientific input and expected values unchanged.",
    "validation_output_field_unknown": "Use exact field names and nesting from script_interfaces.json_outputs. Correct assertion pointers to the declared interface; preserve independently derived scientific expectations and do not guess aliases.",
    "duplicate_validation_scripts": "Declare each script exactly once in the validation plan.",
    "incomplete_validation_scripts": "Provide exactly one validation plan for every declared helper script.",
    "invalid_validation_requirements": "Use unique requirement_ids from the immutable method contract.",
    "invalid_validation_sources": "Cite unique source_ids linked to every referenced requirement; do not infer an oracle from generated code.",
    "undeclared_validation_argument": "Use only CLI flags declared in script_interfaces; do not invent flags.",
    "nonfinite_validation_number": "Represent missing synthetic values with null, never NaN or Infinity.",
    "invalid_validation_fixture_reference": "Reference a different declared fixture with a complete {input:filename} placeholder.",
    "repaired_script_interface_changed": "Keep the declared script path and interface unchanged during repair.",
}
GENERAL_CODES = frozenset({
    "analysis_not_ready", "session_unavailable", "identity_mismatch", "source_evidence_invalid", "invalid_schema", "invalid_json",
    "output_truncated", "incomplete_response", "model_refused", "model_unavailable",
    "synthetic_execution_failed", "synthetic_execution_unavailable", "synthetic_repair_unchanged", "storage_unavailable", "package_too_large",
    "validation_oracle_uncertain", "validation_oracle_rejected", "validation_oracle_review_invalid",
    "reference_calculation_unconfirmed", "reference_execution_unavailable",
    "candidate_method_rejected", "candidate_method_uncertain", "dependency_unavailable", "invalid_review_scope", "invalid_requirement_evidence",
})
KNOWN_CODES = frozenset(RECIPE_ERRORS) | COVERAGE_CODES | SCRIPT_CODES | frozenset(VALIDATION_ERRORS) | GENERAL_CODES


class SkillPackageValidationError(ValueError):
    """A fixed validation reason and correction; never echo invalid content."""
    def __init__(self, code: str, *, details: dict | None = None):
        self.details = details or {}
        if code in RECIPE_ERRORS:
            message, self.repair_hint = RECIPE_ERRORS[code]
        elif code in VALIDATION_ERRORS:
            message = code
            self.repair_hint = VALIDATION_ERRORS[code]
        else:
            raise KeyError(code)
        self.code = code
        super().__init__(message)


class SkillCreationError(ValueError):
    """Expected failure that can be rendered without failing the source analysis."""
    def __init__(self, stage: str, code: str, *, retryable: bool | None = None):
        if stage not in STAGE_LABELS or code not in KNOWN_CODES:
            raise KeyError("Unknown skill creation diagnostic")
        self.stage = stage
        self.code = code
        self.retryable = (code not in {"analysis_not_ready", "session_unavailable", "identity_mismatch", "model_refused", "package_too_large"}
                          if retryable is None else retryable)
        reason = RECIPE_ERRORS.get(code, (None,))[0]
        if code in {"analysis_not_ready", "session_unavailable", "identity_mismatch"}:
            reason = "请先在当前账户中完成一次有实际执行和交付结果的分析，再创建技能。"
        elif code == "source_evidence_invalid":
            reason = "已完成分析中的程序依据暂时无法安全提炼为技能，已停止创建。原分析结果保持不变，可稍后重试创建。"
        elif code in {"method_review_rejected", "method_revision_unchanged"}:
            reason = "方法审查仍发现遗漏或改变，自动修复后未达到原分析要求。"
        elif code == "candidate_method_rejected":
            reason = "方法实现与原分析要求存在已确认的矛盾，尚未通过修订及验证。"
        elif code == "candidate_method_uncertain":
            reason = "聚焦审查尚不能可靠确认方法实现，已保留未解决的问题。"
        elif code in SCRIPT_CODES:
            reason = SCRIPT_FAILURE_REASONS[code]
        elif code in {"synthetic_execution_failed", "synthetic_repair_unchanged"}:
            reason = "脚本在合成用例中运行或结果校验失败，自动修复后仍未通过。"
        elif code == "synthetic_execution_unavailable":
            reason = "合成验证环境暂时不可用，请稍后重试。"
        elif code == "dependency_unavailable":
            reason = "验证环境缺少所需依赖，或现有依赖无法正常导入。请先检查依赖安装和版本兼容性，再重试创建技能。"
        elif code == "reference_calculation_unconfirmed":
            reason = "独立参考计算尚未可靠确认验收预期，未依据这些预期修改分析脚本。"
        elif code == "invalid_review_scope":
            reason = "审查结果未完整对应系统指定的要求，自动修正后仍未完成审查。"
        elif code == "invalid_requirement_evidence":
            reason = "方法要求尚未正确关联原始分析依据，自动修正后仍未完成提炼。"
        elif code == "reference_execution_unavailable":
            reason = "独立参考计算环境暂时不可用，请稍后重试。"
        elif code == "validation_oracle_uncertain":
            reason = "独立审查无法可靠确认合成预期，已停止创建，尚未据此运行或修改分析脚本。"
        elif code in {"validation_oracle_rejected", "validation_oracle_review_invalid", "validation_oracle_revision_invalid", "validation_oracle_revision_unchanged", "validation_oracle_review_incomplete"}:
            reason = "合成验收计划的预期或审查仍未满足原方法要求，已停止创建，未据此修改分析脚本。"
        elif code == "model_unavailable":
            reason = "模型服务暂时不可用，请稍后重试。"
        elif code == "model_refused":
            reason = "模型未能接受本次技能生成请求，请调整分析范围后再试。"
        elif code == "storage_unavailable":
            reason = "技能包暂时无法保存，请稍后重试。"
        elif code == "package_too_large":
            reason = "技能包超过容量限制，请缩小技能范围后重新创建。"
        elif code in {"output_truncated", "incomplete_response"}:
            reason = "模型未返回完整内容，自动重试后仍未完成。"
        elif code in {"invalid_schema", "invalid_json"}:
            reason = "模型返回的结构不完整，自动修复后仍未通过。"
        self.public_message = f"{STAGE_LABELS[stage]}未完成：{reason or '生成内容仍未满足原分析的方法与验证要求。'}未创建技能，也未添加到技能库。"
        super().__init__(self.public_message)


class SkillRepairReviewRejected(SkillCreationError):
    """A real adverse repair review retained privately for bounded continuation."""

    def __init__(self, review):
        from app.domain.services.skills.method_contract import MethodReview
        if (not isinstance(review, MethodReview) or not (
                not review.implementation_appropriate
                or any(item.status != "preserved" for item in review.requirements)
                or any(item.status not in {"preserved", "valid_exclusion"} for item in review.sources))):
            raise ValueError("Repair continuation requires an adverse typed method review")
        self.review = review
        super().__init__("method_review", "method_review_rejected")
