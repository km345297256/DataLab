"""The review form owns target identity; canonical validators own judgments."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_creation.review_form import (
    ReviewFormAdapter,
    ReviewFormError,
    review_form_adapter,
)


def payload():
    return {
        "method_contract": {
            "requirements": [{"id": "R017"}, {"id": "R003"}],
            "excluded_sources": [{"source_id": "S019"}, {"source_id": "P007"}],
        },
        "sources": [{"id": "S019"}, {"id": "S004"}],
    }


def adapter():
    return review_form_adapter(MethodReview, "method_review", payload())


def form():
    return {
        "sources": {
            "S004": {"status": "preserved", "explanation": "The complete source is faithfully represented."},
            "S019": {"status": "valid_exclusion", "explanation": "The host explicitly excluded this source."},
        },
        "requirements": {
            "R003": {"status": "preserved", "explanation": "The declared output obligation is implemented."},
            "R017": {"status": "preserved", "explanation": "The declared method obligation is implemented."},
        },
        "implementation": {"status": "accepted", "explanation": "The implementation suits the original method and public interface."},
    }


@pytest.mark.parametrize("stage", ["method_review", "synthetic_reference_review"])
def test_factory_binds_exact_host_order_and_does_not_invent_withheld_targets(stage):
    value = review_form_adapter(MethodReview, stage, payload())
    assert value.source_ids == ("S019", "S004")
    assert value.requirement_ids == ("R017", "R003")
    assert value.excluded_source_ids == frozenset({"S019"})
    assert "P007" not in value.request_schema()["properties"]["sources"]["properties"]


@pytest.mark.parametrize("schema,stage,context", [
    (dict, "method_review", payload()),
    (MethodReview, "implementation", payload()),
    (MethodReview, "synthetic_validation_review", payload()),
    (MethodReview, "method_review", {}),
    (MethodReview, "method_review", {"sources": []}),
    (MethodReview, "method_review", {"method_contract": {}}),
    (MethodReview, "method_review", None),
])
def test_unrelated_or_legacy_request_has_no_form(schema, stage, context):
    assert review_form_adapter(schema, stage, context) is None


@pytest.mark.parametrize("section,value", [
    ("method_contract", None),
    ("method_contract", {}),
    ("method_contract", {"requirements": []}),
    ("method_contract", {"requirements": {"R001": {}}}),
    ("method_contract", {"requirements": [{"id": "R017"}, {"id": "R017"}]}),
    ("method_contract", {"requirements": [{"id": "S001"}]}),
    ("method_contract", {"requirements": [{"id": "R1"}]}),
    ("method_contract", {"requirements": [{"id": "R017\n"}]}),
    ("method_contract", {"requirements": [{"id": "R017"}], "excluded_sources": [{"source_id": "S019"}, {"source_id": "S019"}]}),
    ("sources", [{"id": "S004"}, {"id": "S004"}]),
    ("sources", [{"id": "source-path-secret"}]),
    ("sources", [{"id": 1}]),
    ("sources", {}),
])
def test_present_but_invalid_host_context_never_falls_back_to_unscoped_review(section, value):
    context = payload()
    context[section] = value
    with pytest.raises(ValueError):
        review_form_adapter(MethodReview, "method_review", context)


def test_schema_owns_all_target_keys_and_is_a_fresh_projection():
    value = adapter()
    schema = value.request_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == {"sources", "requirements", "implementation"}
    for section, identifiers in (("sources", value.source_ids), ("requirements", value.requirement_ids)):
        node = schema["properties"][section]
        assert list(node["properties"]) == node["required"] == list(identifiers)
        assert node["additionalProperties"] is False
        for target in node["properties"].values():
            assert set(target["required"]) == set(target["properties"]) == {"status", "explanation"}
            assert target["additionalProperties"] is False
    implementation = schema["properties"]["implementation"]
    assert implementation["properties"]["status"]["enum"] == ["accepted", "rejected"]
    assert "frozen task" in implementation["description"]
    assert "review response IDs" in implementation["description"]
    schema["properties"]["sources"]["properties"].clear()
    assert list(value.request_schema()["properties"]["sources"]["properties"]) == list(value.source_ids)


@pytest.mark.parametrize("stage", ["method_review", "synthetic_reference_review"])
def test_source_criterion_explains_host_role_without_changing_targets_or_statuses(stage):
    context = payload()
    context["method_contract"]["excluded_sources"][0]["explanation"] = "PRIVATE_RESULT_DO_NOT_COPY"
    value = review_form_adapter(MethodReview, stage, context)
    schema = value.request_schema()
    targets = schema["properties"]["sources"]["properties"]
    excluded = targets["S019"]["description"]
    included = targets["S004"]["description"]
    assert "declared excluded" in excluded and "whether that exclusion is justified" in excluded
    assert "preserved means you disagree" in excluded
    assert "Do not mark preserved merely because duplicated method text is already implemented" in excluded
    assert "not proof" in excluded
    assert "not declared excluded" in included and "preserved means they are included and preserved" in included
    assert "valid_exclusion means" in included and "adverse judgment" in included
    assert list(targets) == ["S019", "S004"]
    assert "P007" not in targets
    assert all(item["properties"]["status"]["enum"] == ["preserved", "valid_exclusion", "missing", "changed"]
               for item in targets.values())
    assert "PRIVATE_RESULT_DO_NOT_COPY" not in str(schema)
    assert "不能仅因重复方法已被实现" in value.instruction


@pytest.mark.parametrize("excluded_status,included_status,adverse", [
    ("valid_exclusion", "preserved", False),
    ("preserved", "preserved", True),
    ("missing", "preserved", True),
    ("changed", "preserved", True),
    ("valid_exclusion", "valid_exclusion", True),
])
def test_clear_source_role_never_overrides_an_unjustified_exclusion_verdict(excluded_status, included_status, adverse):
    from app.domain.services.skills.method_contract import CoverageError, MethodContract, validate_review
    value = adapter()
    document = form()
    document["sources"]["S019"].update(status=excluded_status,
        explanation="This source contains a distinct required method, so excluding it is not justified.")
    document["sources"]["S004"]["status"] = included_status
    original = deepcopy(document)
    contract = MethodContract(requirements=[
        {"id": "R017", "kind": "method", "statement": "Preserve the complete original method.", "source_ids": ["S004"]},
        {"id": "R003", "kind": "output", "statement": "Export the declared method results.", "source_ids": ["S004"]},
    ], excluded_sources=[{"source_id": "S019", "reason": "duplicate", "explanation": "Declared duplicate source evidence."}])
    review = MethodReview.model_validate(value.document(document))
    if adverse:
        with pytest.raises(CoverageError) as error:
            validate_review(review, contract, [{"id": "S019"}, {"id": "S004"}])
        assert error.value.code == "method_review_rejected"
    else:
        validate_review(review, contract, [{"id": "S019"}, {"id": "S004"}])
    assert value.observe(("sources", "S019", "status"), excluded_status) == (excluded_status != "valid_exclusion")
    assert value.observe(("sources", "S004", "status"), included_status) == (included_status != "preserved")
    assert review.sources[0].status == excluded_status and review.sources[1].status == included_status
    assert document == original


def test_positive_form_projects_mechanically_without_mutation_or_new_review():
    original = form()
    before = deepcopy(original)
    projected = adapter().document(original)
    assert original == before
    assert [item["source_id"] for item in projected["sources"]] == ["S019", "S004"]
    assert [item["requirement_id"] for item in projected["requirements"]] == ["R017", "R003"]
    assert projected["sources"][0] == {"source_id": "S019", **original["sources"]["S019"]}
    assert projected["implementation_appropriate"] is True
    assert projected["implementation_reason"] == original["implementation"]["explanation"]
    assert MethodReview.model_validate(projected).implementation_appropriate is True


def test_all_genuine_negative_values_and_explanations_survive_projection():
    original = form()
    original["sources"]["S004"]["status"] = "changed"
    original["requirements"]["R017"]["status"] = "missing"
    original["implementation"]["status"] = "rejected"
    projected = adapter().document(original)
    typed = MethodReview.model_validate(projected)
    assert typed.sources[1].status == "changed"
    assert typed.requirements[0].status == "missing"
    assert typed.implementation_appropriate is False
    assert typed.implementation_reason == original["implementation"]["explanation"]


@pytest.mark.parametrize("section,key", [("sources", "S004"), ("requirements", "R003"), ("implementation", None)])
@pytest.mark.parametrize("problem", ["missing", "empty", "short", "non_string"])
def test_explanation_defects_reach_canonical_validation_and_targeted_repair(section, key, problem):
    original = form()
    target = original[section][key] if key else original[section]
    if problem == "missing":
        target.pop("explanation")
    else:
        target["explanation"] = {"empty": "", "short": "short", "non_string": 42}[problem]
    projected = adapter().document(original)
    with pytest.raises(ValidationError) as error:
        MethodReview.model_validate(projected)
    expected_field = "implementation_reason" if section == "implementation" else "explanation"
    assert any(item["loc"][-1] == expected_field for item in error.value.errors())


@pytest.mark.parametrize("status", [None, 0, {}, "rejected", "uncertain", "accepted"])
@pytest.mark.parametrize("section,key", [("sources", "S004"), ("requirements", "R003")])
def test_illegal_target_status_is_form_error_not_canonical_array_repair(status, section, key):
    original = form()
    original[section][key]["status"] = status
    with pytest.raises(ReviewFormError) as error:
        adapter().document(original)
    assert error.value.fields == [section, "status"]


@pytest.mark.parametrize("section,key", [("sources", "S004"), ("requirements", "R003")])
def test_missing_status_is_form_error_and_missing_explanation_remains_field_repair(section, key):
    original = form()
    original[section][key].pop("status")
    with pytest.raises(ReviewFormError) as error:
        adapter().document(original)
    assert error.value.fields == [section, "status"]


@pytest.mark.parametrize("status", [None, False, "preserved", "uncertain", "inappropriate", "", {}])
def test_illegal_implementation_status_is_not_coerced_into_positive_boolean(status):
    original = form()
    original["implementation"]["status"] = status
    with pytest.raises(ReviewFormError) as error:
        adapter().document(original)
    assert error.value.fields == ["implementation", "status"]


@pytest.mark.parametrize("section,key", [("sources", "S004"), ("requirements", "R003")])
@pytest.mark.parametrize("change", ["unknown", "missing", "renamed", "legacy_list", "row_not_object", "extra_field"])
def test_invalid_scope_is_rejected_whole_without_filtering_or_inventing_judgments(section, key, change):
    original = form()
    if change == "unknown":
        original[section]["untrusted-secret-id"] = {"status": "rejected", "explanation": "Invented target."}
    elif change == "missing":
        del original[section][key]
    elif change == "renamed":
        original[section]["untrusted-secret-id"] = original[section].pop(key)
    elif change == "legacy_list":
        original[section] = list(original[section].values())
    elif change == "row_not_object":
        original[section][key] = "preserved"
    else:
        original[section][key]["unknown-secret-field"] = "secret-value"
    before = deepcopy(original)
    with pytest.raises(ReviewFormError) as error:
        adapter().document(original)
    assert original == before
    assert error.value.reason == str(error.value) == "invalid_review_scope"
    assert error.value.fields == [section]
    assert "secret" not in str(error.value.fields)


@pytest.mark.parametrize("change", ["legacy_global", "extra", "missing", "not_object"])
def test_whole_document_shape_must_be_exact(change):
    original = form()
    if change == "legacy_global":
        original["implementation_appropriate"] = False
    elif change == "extra":
        original["untrusted-secret-key"] = "secret-value"
    elif change == "missing":
        original.pop("implementation")
    else:
        original = list(original.values())
    with pytest.raises(ReviewFormError) as error:
        adapter().document(original)
    assert error.value.fields == ["sources", "requirements", "implementation"]


@pytest.mark.parametrize("path,status", [
    (("requirements", "R017", "status"), "missing"),
    (("requirements", "R017", "status"), "changed"),
    (("requirements", "R017", "status"), "unsupported"),
    (("requirements", "R017", "status"), "rejected"),
    (("requirements", "R017", "status"), "uncertain"),
    (("sources", "S004", "status"), "missing"),
    (("sources", "S004", "status"), "changed"),
    (("sources", "S004", "status"), "rejected"),
    (("sources", "S004", "status"), "uncertain"),
    (("sources", "S004", "status"), "unsupported"),
    (("sources", "S004", "status"), "valid_exclusion"),
    (("sources", "S019", "status"), "preserved"),
    (("implementation", "status"), "rejected"),
    (("implementation", "status"), "uncertain"),
    (("implementation", "status"), "inappropriate"),
])
def test_known_explicit_negative_remains_sticky_even_in_an_incomplete_form(path, status):
    assert adapter().observe(path, status) is True


@pytest.mark.parametrize("path,value", [
    (("requirements", "R999", "status"), "rejected"),
    (("sources", "S999", "status"), "missing"),
    (("sources", "P007", "status"), "changed"),
    (("requirements", "R017", "explanation"), "rejected"),
    (("implementation", "explanation"), "rejected"),
    (("implementation",), "rejected"),
    (("implementation_appropriate",), False),
    (("requirements", 0, "status"), "missing"),
    (("requirements", "R017", "nested", "status"), "missing"),
    (("requirements", "R017", "status"), {"status": "missing"}),
    (("requirements", "R017", "status"), "preserved"),
    (("sources", "S004", "status"), "preserved"),
    (("sources", "S019", "status"), "valid_exclusion"),
    (("implementation", "status"), "accepted"),
])
def test_unknown_targets_prose_and_legacy_fields_do_not_create_scientific_votes(path, value):
    assert adapter().observe(path, value) is False


def test_instruction_separates_scope_errors_from_real_implementation_defects():
    value = adapter()
    assert "不得添加、删除、重命名 ID" in value.instruction
    assert "真实缺陷或无法确认时必须 rejected" in value.instruction
    assert "不得因自己生成未知编号而否决 implementation" in value.instruction
    assert "不得为通过创建而改变真实负评" in value.instruction
    assert isinstance(value, ReviewFormAdapter)
