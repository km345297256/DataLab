"""Synthetic fixture and frozen-string roles never weaken source privacy."""
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints
from app.domain.services.skills.skill_creation.evidence import AnalysisContext
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.pipeline import SkillCreationPipeline
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, generation_privacy
from app.domain.services.skills.skill_creation.repair_progress import privacy_failure_snapshot
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_reference_verification import reference_generation_payload
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_review import ValidationPlanReview
from test_skill_creation_checkpoints import MemoryCache, scope
from test_skill_creation_gateway import make_gateway
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources


UUID = "12345678-1234-5678-9abc-123456789abc"
POINTER = "/groups/0/group"


def document(data=None, expected="A"):
    result = plan_payload()
    for case in result["scripts"][0]["cases"]:
        case["argv"][1] = "{input:fixture.json}"
        case["input_files"] = [{"path": "fixture.json", "format": "json", "data": deepcopy(data or {
            "user_id": "artificial-user", "session_id": UUID, "mapping": {UUID: "sample"},
            "description": "owner_id=synthetic-owner"})}]
        for assertion in case["assertions"]:
            if assertion["type"] == "json_value":
                assertion["pointer"] = POINTER
                assertion["expected"]["value"] = expected
    return result


def plan_context(base=None):
    return generation_privacy(base or PrivacyContext(), SyntheticValidationPlan, "synthetic_validation_plan", {})


def test_complete_synthetic_json_roles_preserve_artificial_identity_values_and_keys():
    raw = document()
    plan = SyntheticValidationPlan.model_validate(raw)
    scoped = plan_context()
    assert PrivacyContext().scan_document(raw)
    assert scoped.scan_document(plan) == []
    assert scoped.scan_document(raw) == []
    assert scoped.redact_document(raw) == raw
    assert privacy_failure_snapshot(plan, scoped) is None
    assert scoped.synthetic_identity_roots == frozenset(), "Document roles must never mutate the request context"


@pytest.mark.parametrize("defect", ["ordinary", "wrong_stage", "wrong_schema", "wrapped", "fake_data", "invalid_parent", "csv"])
def test_fixture_roles_require_request_scope_and_complete_typed_plan(defect):
    raw, scoped = document(), plan_context()
    if defect == "ordinary": scoped = PrivacyContext()
    elif defect == "wrong_stage": scoped = generation_privacy(scoped, SyntheticValidationPlan, "workflow", {})
    elif defect == "wrong_schema": scoped = generation_privacy(scoped, ValidationPlanReview, "synthetic_validation_plan", {})
    elif defect == "wrapped": raw = {"previous_candidate": raw}
    elif defect == "fake_data": raw = {"input_files": [{"data": {"user_id": UUID}}]}
    elif defect == "invalid_parent": raw["scripts"][0]["cases"][0]["unexpected"] = True
    elif defect == "csv":
        raw = plan_payload()
        for case in raw["scripts"][0]["cases"]:
            case["input_files"][0] = {"path": "table.csv", "format": "csv", "columns": ["user_id"], "rows": [{"user_id": UUID}]}
    assert scoped.scan_document(raw)
    assert privacy_failure_snapshot(raw, scoped) is not None


@pytest.mark.parametrize("category,value", [
    ("identity", "artificial-user"), ("identity", UUID), ("credential", "artificial-user"),
    ("source_filename", "source-private.csv"), ("host_path", "/workspace/private/input.csv"),
])
def test_exact_source_values_always_win_inside_synthetic_data(category, value):
    base = PrivacyContext((PrivateValue(value, category),), generated_output_filenames=frozenset({"previous-output.csv"}))
    raw, scoped = document({"user_id": value}), plan_context(base)
    issues = scoped.scan_document(raw)
    assert any(issue.category == category for issue in issues)
    assert privacy_failure_snapshot(raw, scoped) is not None
    assert value not in json.dumps(scoped.redact_document(raw))
    assert scoped.generated_output_filenames == base.generated_output_filenames


@pytest.mark.parametrize("data,category", [
    ({"user_id": "sk-abcdefgh12345678"}, "credential"),
    ({"user_id": "Bearer abcdefgh12345678"}, "credential"),
    ({"user_id": {"password": "literal-password"}}, "credential"),
    ({"token": "literal-token"}, "credential"),
    ({"message": "password=literal-password"}, "credential"),
    ({"user_id": "/Users/private-person/data.csv"}, "host_path"),
    ({"user_id": "https://private.invalid/source"}, "url"),
])
def test_synthetic_roles_do_not_disable_credentials_paths_or_urls(data, category):
    raw, scoped = document(data), plan_context()
    assert any(issue.category == category for issue in scoped.scan_document(raw))
    assert privacy_failure_snapshot(raw, scoped) is not None


@pytest.mark.parametrize("field", ["description", "oracle_explanation"])
def test_fixture_identity_permission_does_not_cover_case_prose(field):
    raw = document()
    raw["scripts"][0]["cases"][0][field] = "Use user_id=" + UUID
    assert plan_context().scan_document(raw)


def test_fixture_identity_permission_does_not_cover_expected_answers_or_tiny_name_checks():
    assert plan_context().scan_document(document(expected=UUID))
    assert plan_context().scan_document({"path": UUID})


def test_plan_and_blind_reference_scopes_revalidate_synthetic_fixture_roles_without_leaking_them():
    raw = document()
    scoped = PrivacyContext().for_validation_plan(raw)
    assert not scoped.synthetic_plan_inputs and not scoped.synthetic_identity_roots
    payload = reference_generation_payload(SyntheticValidationPlan.model_validate(raw), method_contract(), sources(), interfaces())
    blind = PrivacyContext().for_reference_targets(payload["blind_cases"])
    assert not blind.synthetic_identity_roots and not blind.review_json_string_values
    assert blind.scan_document({"content": "user_id=" + UUID})
    bad = deepcopy(payload["blind_cases"])
    bad[0]["cases"][0]["input_files"][0]["data"]["token"] = "literal-token"
    with pytest.raises(SkillPackageValidationError):
        PrivacyContext().for_reference_targets(bad)


@pytest.mark.parametrize("expected,citation", [
    ("A", "/groups/0/group=A"), ("A", '/groups/0/group="A"'),
    ("A", r'/groups/0/group="\u0041"'), ("组A", '/groups/0/group="组A"'),
    ("", '/groups/0/group=""'), ('A"B', r'/groups/0/group="A\"B"'),
    ("A B", '/groups/0/group="A B"'), ("G-1", "/groups/0/group!=G-1"),
])
def test_only_exact_declared_string_values_gain_comparison_syntax(expected, citation):
    raw = document({"group": "sample"}, expected=expected)
    scoped = PrivacyContext().for_validation_plan(raw)
    prose = {"correctness": {"explanation": f"Compare `{citation}` with the declared expectation."}}
    assert scoped.scan_document(prose) == []
    assert scoped.redact_document(prose) == prose
    raw["scripts"][0]["cases"][0]["oracle_explanation"] = prose["correctness"]["explanation"]
    assert plan_context().scan_document(raw) == []


@pytest.mark.parametrize("citation", [
    "/groups/0/group=B", "/groups/0/group=A/suffix", "/groups/0/group=A\\suffix", "/groups/0/group=A@host",
    "/groups/0/group=A%2fsuffix", "/groups/0/group=A.csv", '/groups/0/group="A"/suffix',
    "/groups/0/group=/Users/private/file", "/groups/0/group=sk-abcdefgh12345678", "/groups/0/group===A",
    "/elsewhere/groups/0/group=A", "/groups/0/groups=A", "/groups/1/group=A",
])
def test_string_comparison_scope_does_not_admit_unfrozen_values_or_path_tails(citation):
    scoped = PrivacyContext().for_validation_plan(document({"group": "sample"}))
    assert scoped.scan_document({"explanation": f"Compare `{citation}`."})


@pytest.mark.parametrize("field", ["content", "code", "default", "description", "implementation_reason", "path", "expected"])
def test_string_comparison_permission_does_not_apply_outside_oracle_explanations(field):
    scoped = PrivacyContext().for_validation_plan(document({"group": "sample"}))
    assert scoped.scan_document({field: "/groups/0/group=A"})


def test_string_comparison_permission_cannot_survive_a_different_plan_or_stage():
    scoped = PrivacyContext().for_validation_plan(document({"group": "sample"}))
    prose = {"explanation": "/groups/0/group=A"}
    other = scoped.for_validation_plan(document({"group": "sample"}, expected="B"))
    assert other.scan_document(prose)
    assert generation_privacy(scoped, ValidationPlanReview, "workflow", {}).scan_document(prose)
    assert plan_context(scoped).scan_document(prose)


@pytest.mark.parametrize("category", ["identity", "credential", "host_path", "source_filename"])
def test_exact_private_markers_cannot_be_granted_permission_by_frozen_comparisons(category):
    base = PrivacyContext((PrivateValue("A", category),))
    with pytest.raises(SkillPackageValidationError):
        base.for_validation_plan(document({"group": "sample"}))
    scoped = PrivacyContext(base.entries, frozenset({POINTER}), review_json_string_values=frozenset({(POINTER, "A")}))
    assert any(issue.category == category for issue in scoped.scan_document({"explanation": POINTER + "=A"}))


@pytest.mark.asyncio
async def test_artificial_identity_plan_crosses_generation_and_checkpoint_without_repair():
    raw, cache = document(), MemoryCache()
    gateway, calls, _ = make_gateway([raw])
    analysis = AnalysisContext(privacy=gateway.privacy, evidence={}, sources=sources(), programs=[])
    for _ in range(2):
        pipeline = SkillCreationPipeline(analysis, gateway, script_verifier=AsyncMock(),
            checkpoints=BuildCheckpoints(cache, scope(), privacy=gateway.privacy))
        pipeline.contract = method_contract()
        value = await pipeline.generate(SyntheticValidationPlan, "synthetic_validation_plan", "Design synthetic inputs", {},
            lambda result: validate_portable_document(result, plan_context(gateway.privacy)))
        assert value.model_dump() == SyntheticValidationPlan.model_validate(raw).model_dump()
    assert len(calls) == 1
