"""Coverage amendment locators retain their schema role at every privacy gate."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.method_contract import MethodContract
from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints
from app.domain.services.skills.skill_creation.evidence import AnalysisContext
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.pipeline import SkillCreationPipeline
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, generation_privacy
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan, validate_validation_plan
from app.domain.services.skills.skill_validation_extension import VerificationCoverageAmendment, apply_coverage_amendment
from test_skill_creation_checkpoints import MemoryCache, scope
from test_skill_creation_gateway import make_gateway, requests

FIXTURE = Path(__file__).parent / "fixtures" / "skill_creator_coverage_pointer"
POINTER = "/summary/0/result"


def context(base=None, schema=VerificationCoverageAmendment, stage="synthetic_validation_plan"):
    return generation_privacy(base or PrivacyContext(), schema, stage, {})


def amendment(pointer=POINTER):
    return {"assertion_additions": [{"script_path": "scripts/summary.py", "case_id": "normal",
        "assertion": {"type": "json_object_keys", "path": "report.json", "pointer": pointer, "keys": ["mean"]}}],
        "new_cases": []}


def actual():
    raw = (FIXTURE / "108-synthetic_validation_plan-response.json").read_bytes()
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == provenance["response_sha256"]
    document = json.loads(json.loads(raw)["content"])
    return document, json.loads((FIXTURE / "105-domain-context.json").read_text())


def test_actual_24_original_json_pointer_roles_pass_without_modifying_one_field():
    document, payload = actual()
    value = VerificationCoverageAmendment.model_validate(document)
    assert len(value.assertion_additions) == 25 and len(value.new_cases) == 12
    base_issues = PrivacyContext().scan_document(value)
    assert len(base_issues) == 8 and all(item.category == "host_path" for item in base_issues)
    scoped = context()
    validate_portable_document(value, scoped)
    assert scoped.redact_document(document) == document
    original = SyntheticValidationPlan.model_validate(payload["previous_validation_plan"])
    revised = apply_coverage_amendment(original, value)
    validate_validation_plan(revised, MethodContract.model_validate(payload["method_contract"]),
        payload["sources"], payload["script_interfaces"])
    assert sum(len(script.cases) for script in revised.scripts) == 20


@pytest.mark.asyncio
async def test_actual_response_crosses_pipeline_gateway_cache_write_and_restore_without_regeneration():
    document, payload = actual()
    gateway, calls, _ = make_gateway([document])
    cache = MemoryCache()
    analysis = AnalysisContext(privacy=gateway.privacy, evidence={}, sources=payload["sources"], programs=[])
    original = SyntheticValidationPlan.model_validate(payload["previous_validation_plan"])
    contract = MethodContract.model_validate(payload["method_contract"])
    def check(value):
        result = apply_coverage_amendment(original, value)
        validate_validation_plan(result, contract, payload["sources"], payload["script_interfaces"])
        old = {(script.script_path, case.id): case for script in original.scripts for case in script.cases}
        new = {(script.script_path, case.id): case for script in result.scripts for case in script.cases}
        for item in payload["required_cases"]:
            key = item["script_path"], item["case_id"]
            assert len(new[key].assertions) > len(old[key].assertions)
    for _ in range(2):
        pipeline = SkillCreationPipeline(analysis, gateway, script_verifier=AsyncMock(),
            checkpoints=BuildCheckpoints(cache, scope(), privacy=gateway.privacy))
        pipeline.contract = contract
        value = await pipeline.generate(VerificationCoverageAmendment, "synthetic_validation_plan", "Append observable coverage", payload, check)
        assert value.model_dump(mode="json") == VerificationCoverageAmendment.model_validate(document).model_dump(mode="json")
    assert len(calls) == 1 and not gateway.privacy.coverage_amendment_pointers


@pytest.mark.parametrize("pointer", ["", "/summary/count", "/profiles/0", "/paired/0", "/adjustments/0", "/pivot/quality", "/a~1b/~0metric", "/统计/0/均值"])
def test_all_declared_json_key_locators_use_rfc6901_not_a_task_path_allowlist(pointer):
    document = amendment(pointer)
    assert context().scan_document(document) == []
    assert context().redact_document(document) == document


@pytest.mark.parametrize("category", ["host_path", "source_filename", "identity", "credential"])
def test_known_private_values_win_even_inside_valid_declarative_pointer(category):
    scoped = context(PrivacyContext((PrivateValue(POINTER, category),)))
    assert scoped.scan_document(amendment())
    assert POINTER not in json.dumps(scoped.redact_document(amendment()))


@pytest.mark.parametrize("defect", ["unknown_schema", "wrong_stage", "general_document", "code", "explanation", "path",
    "keys", "other_pointer", "nested_copy", "invalid_escape", "unknown_type", "incomplete_parent", "extra_parent_field", "secret"])
def test_amendment_pointer_role_never_exempts_other_fields_or_malformed_assertions(defect):
    document, scoped = amendment(), context()
    assertion = document["assertion_additions"][0]["assertion"]
    if defect == "unknown_schema": scoped = context(schema=SyntheticValidationPlan)
    elif defect == "wrong_stage": scoped = context(stage="workflow")
    elif defect == "general_document": scoped = PrivacyContext()
    elif defect in {"code", "explanation"}: document[defect] = POINTER
    elif defect == "path": assertion["path"] = "/tmp/output.json"
    elif defect == "keys": assertion["keys"] = [POINTER]
    elif defect == "other_pointer": document["pointer"] = POINTER
    elif defect == "nested_copy": document = {"wrapper": document}
    elif defect == "invalid_escape": assertion["pointer"] = "/summary/~2result"
    elif defect == "unknown_type": assertion["type"] = "csv_header"
    elif defect == "incomplete_parent": assertion.pop("keys")
    elif defect == "extra_parent_field": assertion["description"] = "extra"
    elif defect == "secret": assertion["pointer"] = "/summary/sk-abcdefgh12345678"
    assert scoped.scan_document(document)


@pytest.mark.asyncio
async def test_real_privacy_repair_keeps_public_pointer_but_removes_only_private_other_field():
    document = amendment()
    document["assertion_additions"][0]["assertion"]["keys"] = ["private-marker"]
    corrected = amendment()
    gateway, calls, _ = make_gateway([document, corrected], privacy=PrivacyContext((PrivateValue("private-marker", "identity"),)))
    scoped = context(gateway.privacy)
    result = await gateway.generate(VerificationCoverageAmendment, "synthetic_validation_plan", "Append coverage", {},
        lambda value: validate_portable_document(value, scoped))
    assert result.model_dump(mode="json") == VerificationCoverageAmendment.model_validate(corrected).model_dump(mode="json")
    previous = requests(calls)[1]["previous_candidate"]
    assert previous["assertion_additions"][0]["assertion"]["pointer"] == POINTER
    assert "private-marker" not in json.dumps(previous)


def test_request_scope_never_leaks_to_other_generation_or_prose_review():
    scoped = context()
    reset = generation_privacy(scoped, SyntheticValidationPlan, "synthetic_validation_plan", {})
    assert reset.scan_document(amendment()) and not reset.coverage_amendment_pointers
    assert scoped.scan_document({"explanation": POINTER})
