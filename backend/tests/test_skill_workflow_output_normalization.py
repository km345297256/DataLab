"""Output naming is host authoring, before all existing workflow checks."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.domain.services.skills.method_contract import (
    CoverageError, MethodContract, validate_contract, validate_coverage,
)
from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints
from app.domain.services.skills.skill_creation.package import (
    normalize_contract_exclusions, validate_implementation, validate_recipe,
)
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, generation_privacy
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft, compile_workflow
from app.domain.services.skills.skill_creation.workflow_outputs import normalize_generated_output_names
from test_skill_creation_checkpoints import MemoryCache, scope
from test_skill_creation_gateway import make_gateway
from test_skill_workflow_authoring import contract, draft_payload


def example():
    payload = draft_payload()
    payload["outputs"][0]["name"] = "old_result.csv"
    payload["outputs"][0]["acceptance"] = "Check old_result.csv has all covariance coefficients."
    payload["steps"][0]["verification"] = "Verify old_result.csv includes the HC3 covariance."
    payload["validation_cases"][0]["expected"] = "Validate old_result.csv against the synthetic covariance."
    context = PrivacyContext((PrivateValue("old_result.csv", "source_filename"),),
                             generated_output_filenames=frozenset({"old_result.csv"}))
    return WorkflowDraft.model_validate(payload), context


def test_output_copy_preserves_contract_bindings_and_nonoutput_content():
    draft, privacy = example()
    before = draft.model_dump(mode="json")
    result = normalize_generated_output_names(draft, privacy)
    assert result.outputs[0].name == "output_001.csv"
    assert result is not draft and draft.model_dump(mode="json") == before
    assert privacy.scan_document(result) == []
    original = compile_workflow(draft, contract())
    compiled = compile_workflow(result, contract())
    assert compiled.coverage == original.coverage
    for key in ("parameters", "data_requirements", "implementation", "dependencies", "limitations"):
        assert result.model_dump()[key] == draft.model_dump()[key]
    validate_recipe(compiled, privacy)
    validate_implementation(compiled, contract(), [])
    validate_coverage(contract(), compiled.model_dump())
    assert normalize_generated_output_names(result, privacy) is result


@pytest.mark.asyncio
async def test_gateway_normalizes_once_before_checks_and_only_stores_checked_output():
    draft, privacy = example()
    gateway, calls, _ = make_gateway([draft.model_dump()], privacy=privacy)
    cache = MemoryCache()
    build = BuildCheckpoints(cache, scope(), privacy=privacy)
    checked = []
    def check(value):
        compiled = compile_workflow(value, contract())
        validate_recipe(compiled, privacy)
        validate_coverage(contract(), compiled.model_dump())
        checked.append(value)
    async def run():
        return await build.run(WorkflowDraft, "workflow", "Preserve the declared method.", {}, check,
            lambda: gateway.generate(WorkflowDraft, "workflow", "Preserve the declared method.", {}, check))
    result = await run()
    assert await run() == result
    assert len(calls) == 1 and checked
    assert all(value.outputs[0].name == "output_001.csv" for value in checked)
    assert "old_result.csv" not in json.dumps(cache.entries)
    assert generation_privacy(privacy, WorkflowDraft, "workflow", {}).generated_output_filenames
    implementation_privacy = generation_privacy(privacy, WorkflowDraft, "implementation", {})
    assert implementation_privacy.generated_output_filenames == privacy.generated_output_filenames
    # Provenance follows the source; it never exempts historical filenames.
    assert implementation_privacy.scan_document({"content": "result = 'old_result.csv'"})


@pytest.mark.parametrize("index,sha,coverage_error", [
    (13, "3a0caea747d60cd41d64aa10ad954460ecdc8010f1fb9f426e6e7981a889680e", "requirement_wrong_section"),
    (18, "76dd7eb2d401f57d816157a3817e7b3bbdba3c6c2096845cef988f86b3a83e6e", None),
    (23, "57430df17dce4057d5108ecfe8bf23f056a626f94c971041ce1e47ad0817cd49", None),
])
def test_private_original_workflow_responses_zero_provider(index, sha, coverage_error):
    root = os.environ.get("DATALAB_PRIVATE_WORKFLOW_REPLAY")
    if not root:
        pytest.skip("Opt-in owned private response replay; no provider or raw data access.")
    root = Path(root)
    snapshot_bytes = (root / "source-snapshot.json").read_bytes()
    assert hashlib.sha256(snapshot_bytes).hexdigest() == "242f61ad06d1a5f91db79efe730548cc8fc680614b244ab53c8985f4132bbe0b"
    snapshot = json.loads(snapshot_bytes)["snapshot"]
    artifacts = root / "run-tn55ydw6"
    collected = PrivacyContext.collect(snapshot["events"], session_id=snapshot["session_id"], user_id=snapshot["user_id"])
    privacy = PrivacyContext(tuple(PrivateValue(**entry) for entry in snapshot["privacy_entries"]),
                             generated_output_filenames=collected.generated_output_filenames)
    assert len(privacy.generated_output_filenames) == 16
    content = json.loads((artifacts / f"{index:03d}-workflow-response.json").read_text())["content"]
    assert hashlib.sha256(content.encode()).hexdigest() == sha
    draft = WorkflowDraft.model_validate_json(content)
    method = normalize_contract_exclusions(MethodContract.model_validate_json(json.loads(
        (artifacts / "008-method_requirements-response.json").read_text())["content"]))
    validate_contract(method, snapshot["context"]["sources"])
    result = normalize_generated_output_names(draft, privacy)
    assert privacy.scan_document(draft) and not privacy.scan_document(result)
    original_recipe, compiled = (compile_workflow(value, method) for value in (draft, result))
    assert original_recipe.coverage == compiled.coverage
    for key in ("parameters", "data_requirements", "implementation", "dependencies", "limitations"):
        assert result.model_dump()[key] == draft.model_dump()[key]
    validate_recipe(compiled, privacy)
    validate_implementation(compiled, method, snapshot["context"]["programs"])
    def coverage(value):
        validate_coverage(method, value.model_dump(), pending_scripts=[s.model_dump() for s in value.implementation.scripts])
    if coverage_error:
        # The first response already had an unrelated coverage defect. Output
        # naming must not launder it or attribute it to this transformation.
        for value in (original_recipe, compiled):
            with pytest.raises(CoverageError) as error:
                coverage(value)
            assert error.value.code == coverage_error
    else:
        coverage(original_recipe)
        coverage(compiled)
