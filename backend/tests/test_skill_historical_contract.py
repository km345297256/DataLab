"""Later decisions replace historical methods without erasing current obligations."""
import pytest

from app.domain.services.skills.method_contract import CoverageError, MethodContract, validate_contract
from app.domain.services.skills.skill_creation.requirement_form import requirement_form_adapter
from app.domain.services.skills.skill_creation_error import SkillCreationError


def sources():
    return [
        {"id": "S001", "kind": "request", "text": "Use Holm correction for the final comparison."},
        {"id": "S002", "kind": "request", "text": "Use Bonferroni correction for the comparison.",
         "historical": True, "chronology": 1, "turn_id": "T001", "execution_status": "completed"},
    ]


def contract(**change):
    exclusion = {"source_id": "S002", "reason": "superseded",
                 "explanation": "The later request explicitly changes the correction method.",
                 "replacement_source_ids": ["S001"], **change}
    return MethodContract(requirements=[{"id": "R001", "kind": "method",
        "statement": sources()[0]["text"], "source_ids": ["S001"]}], excluded_sources=[exclusion])


def test_later_retained_correction_does_not_force_old_algorithm_back():
    validate_contract(contract(), sources())


@pytest.mark.parametrize("replacement", [[], ["S999"], ["S002"], ["S001", "S001"]])
def test_replacement_must_be_known_unique_retained_and_later(replacement):
    with pytest.raises(CoverageError, match="invalid_historical_exclusion"):
        validate_contract(contract(replacement_source_ids=replacement), sources())


def test_historical_replacement_cannot_point_backwards():
    rows = sources()
    rows[0].update(historical=True, chronology=1)
    rows[1]["chronology"] = 2
    with pytest.raises(CoverageError, match="invalid_historical_exclusion"):
        validate_contract(contract(), rows)


@pytest.mark.parametrize("reason", ["superseded", "outside_scope"])
def test_current_obligation_cannot_be_dismissed_as_history(reason):
    rows = sources()
    rows[1].pop("historical")
    value = contract(reason=reason, replacement_source_ids=["S001"] if reason == "superseded" else [])
    with pytest.raises(CoverageError, match="invalid_historical_exclusion"):
        validate_contract(value, rows)


def test_unrelated_history_can_be_excluded_without_changing_current_scope():
    validate_contract(contract(reason="outside_scope", replacement_source_ids=[]), sources())
    with pytest.raises(CoverageError, match="invalid_historical_exclusion"):
        validate_contract(contract(reason="outside_scope"), sources())


def test_legacy_exclusions_cannot_smuggle_replacement_references():
    with pytest.raises(CoverageError, match="invalid_historical_exclusion"):
        validate_contract(contract(reason="duplicate"), sources())
    # Legacy serialized contracts retain their shape without empty new fields.
    assert "replacement_source_ids" not in contract(reason="duplicate", replacement_source_ids=[]).model_dump()["excluded_sources"][0]


def test_host_bound_form_retains_forward_reference_and_exact_source_text():
    adapter = requirement_form_adapter(MethodContract, "method_requirements", {"sources": sources()})
    wire = {"requirements": [{"kind": "method", "unit_ids": ["U001"]}],
            "excluded_sources": [contract().model_dump()["excluded_sources"][0]]}
    result = MethodContract.model_validate(adapter.document(wire))
    validate_contract(result, sources())
    assert result.requirements[0].statement == sources()[0]["text"]
    assert adapter.request_schema()["properties"]["excluded_sources"]["items"]["properties"]["replacement_source_ids"]["items"]["enum"] == ["S001", "S002"]


def test_deterministic_extraction_failure_does_not_suggest_repeating_unchanged_request():
    error = SkillCreationError("source_analysis", "source_evidence_invalid")
    assert error.retryable is False
    assert "稍后重试" not in str(error)
