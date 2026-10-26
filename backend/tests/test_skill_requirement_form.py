"""Source selection cannot silently rewrite a scientific obligation."""
from copy import deepcopy

import pytest
from app.domain.services.skills.method_contract import CoverageError, MethodContract, validate_contract
from app.domain.services.skills.skill_creation.requirement_form import (
    RequirementFormError,
    requirement_form_adapter,
)


def adapter(texts=None):
    texts = texts or ["Input values must be nonempty strings.", "Use OLS. Report the coefficient and confidence interval."]
    sources = [{"id": f"S{index + 1:03d}", "kind": "request", "text": text} for index, text in enumerate(texts)]
    return requirement_form_adapter(MethodContract, "method_requirements", {"sources": sources}), sources


def document(*rows, excluded=()):
    return {"requirements": [{"kind": kind, "unit_ids": units} for kind, units in rows],
            "excluded_sources": list(excluded)}


@pytest.mark.parametrize("schema,stage,payload", [
    (dict, "method_requirements", {"sources": []}),
    (MethodContract, "workflow", {"sources": []}),
    (MethodContract, "method_review", {"sources": []}),
    (MethodContract, "method_requirements", {}),
    (MethodContract, "method_requirements", None),
])
def test_adapter_only_applies_to_source_bound_requirement_generation(schema, stage, payload):
    assert requirement_form_adapter(schema, stage, payload) is None


@pytest.mark.parametrize("sources", [None, {}, [], [None], [{"id": "S001"}],
    [{"id": "S001", "text": " "}], [{"id": "private-source", "text": "Input required."}],
    [{"id": 1, "text": "Input required."}],
    [{"id": "S001", "text": "Input required."}, {"id": "S001", "text": "Output required."}],
])
def test_malformed_host_sources_do_not_fall_back_to_freeform_requirements(sources):
    with pytest.raises(ValueError):
        requirement_form_adapter(MethodContract, "method_requirements", {"sources": sources})


def test_wire_schema_contains_only_classification_and_known_unit_selection():
    value, _ = adapter()
    schema = value.request_schema()
    assert set(schema["properties"]) == set(schema["required"]) == {"requirements", "excluded_sources"}
    assert schema["additionalProperties"] is False
    row = schema["properties"]["requirements"]["items"]
    assert set(row["properties"]) == set(row["required"]) == {"kind", "unit_ids"}
    assert row["additionalProperties"] is False
    assert row["properties"]["unit_ids"]["items"]["enum"] == ["U001", "U002", "U003"]
    assert row["properties"]["unit_ids"]["uniqueItems"] is True
    assert set(row["properties"]["kind"]["enum"]) == {"input", "method", "output", "validation", "constraint"}
    exclusion = schema["properties"]["excluded_sources"]["items"]
    assert exclusion["properties"]["source_id"]["enum"] == ["S001", "S002"]
    assert set(exclusion["properties"]["reason"]["enum"]) == {"observed_result", "one_time_instruction", "duplicate"}
    assert exclusion["additionalProperties"] is False


def test_units_are_exact_host_text_and_a_fresh_deterministic_projection():
    value, sources = adapter()
    expected = [
        {"id": "U001", "source_id": "S001", "text": "Input values must be nonempty strings."},
        {"id": "U002", "source_id": "S002", "text": "Use OLS."},
        {"id": "U003", "source_id": "S002", "text": "Report the coefficient and confidence interval."},
    ]
    assert value.source_units == expected
    assert all(unit["text"] in next(source["text"] for source in sources if source["id"] == unit["source_id"])
               for unit in value.source_units)
    view = value.source_units
    view[0]["text"] = "Invented narrowing condition."
    assert value.source_units == expected
    assert requirement_form_adapter(MethodContract, "method_requirements", {"sources": sources}).source_units == expected


def test_model_cannot_add_stripping_or_other_unstated_preconditions():
    value, sources = adapter()
    wire = document(("input", ["U001"]), ("method", ["U002"]), ("output", ["U003"]))
    projected = value.document(wire)
    typed = MethodContract.model_validate(projected)
    validate_contract(typed, sources)
    assert typed.requirements[0].statement == "Input values must be nonempty strings."
    assert "strip" not in typed.requirements[0].statement
    assert typed.requirements[0].id == "R001"
    assert typed.requirements[0].source_ids == ["S001"]
    wire["requirements"][0]["statement"] = "Input values must remain nonempty after stripping whitespace."
    with pytest.raises(RequirementFormError):
        value.document(wire)


@pytest.mark.parametrize("injected", ["id", "statement", "source_ids", "evidence", "explanation", "quote"])
def test_no_freeform_requirement_or_reference_override_is_accepted(injected):
    value, _ = adapter()
    wire = document(("input", ["U001"]))
    wire["requirements"][0][injected] = "UNTRUSTED_CONTENT"
    with pytest.raises(RequirementFormError) as error:
        value.document(wire)
    assert error.value.fields == ["requirements", 0]
    assert "UNTRUSTED" not in str(error.value)


def test_composition_uses_original_source_order_without_new_connective_semantics():
    value, _ = adapter()
    wire = document(("constraint", ["U003", "U001", "U002"]))
    before = deepcopy(wire)
    compiled = value.document(wire)
    assert wire == before
    assert compiled["requirements"] == [{"id": "R001", "kind": "constraint",
        "statement": "Input values must be nonempty strings.\nUse OLS.\nReport the coefficient and confidence interval.",
        "source_ids": ["S001", "S002"]}]


def test_same_complete_unit_can_support_distinct_requirement_kinds():
    value, _ = adapter()
    compiled = value.document(document(("input", ["U001"]), ("constraint", ["U001"])))
    assert [item["id"] for item in compiled["requirements"]] == ["R001", "R002"]
    assert [item["kind"] for item in compiled["requirements"]] == ["input", "constraint"]
    assert compiled["requirements"][0]["statement"] == compiled["requirements"][1]["statement"]


@pytest.mark.parametrize("ids", [[], "U001", [None], [1], [["U001"]], ["U999"],
    ["U001", "U001"], ["U001", "../../secret"], ["Input values must be nonempty strings."]])
def test_empty_unknown_or_duplicate_local_unit_reference_is_safe_structural_failure(ids):
    value, _ = adapter()
    with pytest.raises(RequirementFormError) as error:
        value.document(document(("input", ids)))
    assert error.value.reason == "invalid_requirement_evidence"
    assert error.value.fields == ["requirements", 0, "unit_ids"]
    assert "secret" not in str(error.value.fields)


@pytest.mark.parametrize("kind", [None, "input_modified", "result", {}, []])
def test_unknown_kind_does_not_create_a_new_obligation_type(kind):
    value, _ = adapter()
    with pytest.raises(RequirementFormError):
        value.document(document((kind, ["U001"])))


@pytest.mark.parametrize("wire", [None, [], {}, {"requirements": []},
    {"requirements": [], "excluded_sources": []},
    {"requirements": [{"kind": "input"}], "excluded_sources": []},
    {"requirements": ["U001"], "excluded_sources": []},
    {"requirements": [{"kind": "input", "unit_ids": ["U001"]}], "excluded_sources": [], "extra": "value"}])
def test_incomplete_or_extra_document_shape_is_never_silently_normalized(wire):
    value, _ = adapter()
    with pytest.raises(RequirementFormError):
        value.document(wire)


@pytest.mark.parametrize("text,expected", [
    ("输入必须非空。保留原有空白。", ["输入必须非空。", "保留原有空白。"]),
    ("Use OLS. Report estimates.", ["Use OLS.", "Report estimates."]),
    ("Use α = 0.05. Report p < α.", ["Use α = 0.05.", "Report p < α."]),
    ("Use e.g. OLS as specified. Report estimates.", ["Use e.g. OLS as specified.", "Report estimates."]),
    ("Use the method in Eq. 3. Report estimates.", ["Use the method in Eq. 3.", "Report estimates."]),
    ("Do not trim strings. Preserve whitespace.", ["Do not trim strings.", "Preserve whitespace."]),
    ("Use Unicode ‘空.白’ unchanged. Report counts.", ["Use Unicode ‘空.白’ unchanged.", "Report counts."]),
    ("Keep `x.y` unchanged. Export JSON.", ["Keep `x.y` unchanged.", "Export JSON."]),
    ("Use (the specified method. Do not replace it). Report counts.", ["Use (the specified method. Do not replace it).", "Report counts."]),
    ("Run OLS\nReport counts", ["Run OLS", "Report counts"]),
    ("OLS", ["OLS"]),
    ("n≥2", ["n≥2"]),
    ("x", ["x"]),
    ("  λ = Σᵢ xᵢ / n  ", ["λ = Σᵢ xᵢ / n"]),
])
def test_complete_sentences_short_phrases_and_math_are_retained_exactly(text, expected):
    value, _ = adapter([text])
    assert [unit["text"] for unit in value.source_units] == expected
    compiled = value.document(document(("method", [unit["id"] for unit in value.source_units])))
    assert MethodContract.model_validate(compiled).requirements[0].statement == "\n".join(expected)


@pytest.mark.parametrize("text", [
    "If n >= 2, compute the sample variance. Otherwise return null.",
    "仅当样本量足够时计算。否则返回空值。",
    "如果有效数为零：\n拒绝输入。\n不生成结果。",
    "Use OLS. This preserves the specified method.",
    "先检查样本量。其不足时不计算。",
    "Apply these constraints:\n- retain zero\n- reject missing",
    "Compute x =\n  a + b\nReport x.",
    "Use the expression (a +\nb) / n. Report it.",
    "Use $$s^2 =\nΣ(xᵢ − x̄)²/(n−1)$$. Report variance.",
    "```python\nif n > 1:\n    result = n / (n - 1)\n```\nRetain this method.",
    "Use an unfinished (definition. Do not infer its meaning.",
])
def test_dependent_or_ambiguous_context_is_not_cut_into_unqualified_rules(text):
    value, _ = adapter([text])
    assert [unit["text"] for unit in value.source_units] == [text]


def test_mixed_method_and_observed_result_is_a_pool_not_a_mandatory_result_contract():
    value, sources = adapter(["采用 OLS 拟合。此次系数估计为 12.34。"])
    sources[0]["kind"] = "observed_summary"
    compiled = value.document(document(("method", ["U001"])))
    typed = MethodContract.model_validate(compiled)
    validate_contract(typed, sources)
    assert typed.requirements[0].statement == "采用 OLS 拟合。"
    assert "12.34" not in typed.requirements[0].statement
    # Quoting proves origin, not semantic completeness: the full sources are
    # still supplied to independent review; this module invents no exclusion.
    assert typed.excluded_sources == []


def test_whole_result_source_can_be_excluded_without_importing_its_numbers():
    value, sources = adapter(["Use OLS.", "The observed estimate was 12.34."])
    sources[1]["kind"] = "observed_summary"
    exclusion = {"source_id": "S002", "reason": "observed_result", "explanation": "This source reports only the completed run's numeric estimate."}
    compiled = value.document(document(("method", ["U001"]), excluded=[exclusion]))
    assert compiled["excluded_sources"] == [exclusion]
    typed = MethodContract.model_validate(compiled)
    validate_contract(typed, sources)
    assert "12.34" not in typed.requirements[0].statement


def test_existing_source_coverage_and_overlap_gates_still_reject_invalid_selection():
    value, sources = adapter()
    missing = MethodContract.model_validate(value.document(document(("input", ["U001"]))))
    with pytest.raises(CoverageError, match="missing_source_coverage"):
        validate_contract(missing, sources)
    exclusion = {"source_id": "S001", "reason": "duplicate", "explanation": "A claimed duplication does not permit overlapping scope."}
    overlap = MethodContract.model_validate(value.document(document(("input", ["U001"]), ("method", ["U002", "U003"]), excluded=[exclusion])))
    with pytest.raises(CoverageError, match="source_exclusion_overlap"):
        validate_contract(overlap, sources)


@pytest.mark.parametrize("exclusions", [None, {}, [None], ["S002"],
    [{"source_id": "S002", "reason": "irrelevant", "explanation": "Unsupported exclusion must fail form validation."}],
    [{"source_id": "S999", "reason": "observed_result", "explanation": "An invented source cannot be excluded from this scope."}],
    [{"source_id": " S002 ", "reason": "observed_result", "explanation": "Source identity cannot be silently normalized."}],
    [{"source_id": 2, "reason": "observed_result", "explanation": "Source identity must remain a string."}],
    [{"source_id": "S002", "reason": "duplicate", "explanation": "short"}],
    [{"source_id": "S002", "reason": "duplicate", "explanation": 12345678}],
    [{"source_id": "S002", "reason": "duplicate"}],
    [{"source_id": "S002", "reason": "duplicate", "explanation": "This is a complete explanation.", "extra": "private-value"}],
])
def test_exclusion_schema_and_ids_fail_in_form_shape_not_projected_legacy_repair(exclusions):
    value, _ = adapter()
    wire = document(("input", ["U001"]))
    wire["excluded_sources"] = exclusions
    before = deepcopy(wire)
    with pytest.raises(RequirementFormError) as error:
        value.document(wire)
    assert error.value.reason == "invalid_requirement_evidence"
    assert error.value.fields[0] == "excluded_sources"
    assert "private" not in str(error.value.fields)
    assert wire == before
