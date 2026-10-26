"""Explicit interfaces retain workflow privacy repair and output ownership."""
import json

import pytest

from app.domain.services.skills.skill_creation.privacy import generation_privacy
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraftV2
from app.domain.services.skills.skill_creation.workflow_privacy_repair import build_workflow_privacy_repair_spec
from test_skill_creation_gateway import check_privacy, make_gateway, requests
from test_skill_workflow_output_normalization import example
from test_skill_workflow_output_repair_gateway import multistage_draft


def explicit_draft():
    document, privacy = multistage_draft()
    document["implementation"]["interface_version"] = 2
    document["implementation"]["scripts"][0]["interface_contract"] = {
        "cli_options": [{"flags": [flag], "required": True, "value_type": "str",
                         "meaning": "Bind selected input and isolated output paths."}
                        for flag in ("--input", "--output")],
        "inputs": [{"argument": "--input", "format": "csv", "dynamic_columns": True,
                    "structure": "The caller provides a table with configured columns.",
                    "missing_semantics": ["Missing observations remain explicitly missing."]}],
        "output_mode": "directory",
        "outputs": [{"path": "@output/old_result.csv", "format": "csv", "dynamic_columns": True,
                     "structure": "The output table contains all declared covariance terms.",
                     "missing_semantics": ["Unavailable covariance values remain explicitly missing."]}],
    }
    return WorkflowDraftV2.model_validate(document), privacy


@pytest.mark.asyncio
async def test_v2_owned_output_is_repaired_by_exact_leaves_without_interface_downgrade():
    draft, privacy = explicit_draft()
    original = draft.model_dump(mode="json")
    revised = json.loads(json.dumps(original).replace("old_result.csv", "output_001.csv"))
    scope = build_workflow_privacy_repair_spec(draft, privacy, stage="workflow")
    assert scope is not None
    changes = []
    for field in scope.fields:
        value = revised
        for part in field.path:
            value = value[part]
        changes.append({"field_id": field.field_id, "action": "set", "value": value})
    gateway, calls, _ = make_gateway([original, {"repairs": changes}], privacy=privacy)
    result = await gateway.generate(WorkflowDraftV2, "workflow", "Preserve all scientific obligations.", {},
                                    check_privacy(privacy))
    assert type(result) is WorkflowDraftV2
    assert result.model_dump(mode="json") == revised
    assert original == draft.model_dump(mode="json")
    second = requests(calls)[1]
    assert second["schema"]["title"] == "WorkflowPrivacyFieldRepairs"
    binding = second["repair_feedback"]["details"]["portable_output_bindings"][0]
    assert ["implementation", "scripts", 0, "interface_contract", "outputs", 0, "path"] in binding["locations"]
    assert "old_result.csv" not in json.dumps(second)
    assert generation_privacy(privacy, WorkflowDraftV2, "workflow", {}).generated_output_filenames


@pytest.mark.asyncio
async def test_v2_host_output_normalization_keeps_strict_version_type():
    old, privacy = example()
    document = old.model_dump(mode="json")
    document["implementation"]["interface_version"] = 2
    draft = WorkflowDraftV2.model_validate(document)
    gateway, calls, _ = make_gateway([draft.model_dump(mode="json")], privacy=privacy)
    result = await gateway.generate(WorkflowDraftV2, "workflow", "Preserve methods.", {}, check_privacy(privacy))
    assert type(result) is WorkflowDraftV2
    assert result.implementation.interface_version == 2
    assert result.outputs[0].name == "output_001.csv"
    assert len(calls) == 1


def test_v2_privacy_text_scope_never_authorizes_default_or_option_identity_changes():
    draft, privacy = explicit_draft()
    document = draft.model_dump(mode="json")
    document["implementation"]["scripts"][0]["interface_contract"]["cli_options"][0]["default"] = "old_result.csv"
    assert build_workflow_privacy_repair_spec(WorkflowDraftV2.model_validate(document), privacy, stage="workflow") is None
    assert draft.model_dump(mode="json") != document
