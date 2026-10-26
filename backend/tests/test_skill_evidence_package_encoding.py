"""Large machine evidence remains byte-bounded without dropping any values."""
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest

from app.application.services.skill_draft_service import MAX_DRAFT_BYTES, SkillDraftService
from app.domain.services.skills.skill_creation.models import SkillRecipe
from app.domain.services.skills.skill_creation.service import render_result
from test_session_skill_creator import recipe_payload
from test_skill_validation_plan import method_contract


@pytest.mark.asyncio
async def test_large_machine_evidence_is_lossless_through_real_package_builder():
    # This is a serialization fixture, not a claim of executed scientific tests.
    checks = [{"name": "assertion_" + str(index), "passed": True,
               "expected": {"value": 0.0, "null": None, "label": "零值\n只读"},
               "actual": {"value": 0.0, "null": None, "label": "零值\n只读"}}
              for index in range(4500)]
    execution = {"status": "passed", "cases": [{"id": "encoding_fixture", "status": "passed",
                 "input_unchanged": True, "checks": checks}],
                 "provenance": {"negative_history": [{"verdict": "rejected", "explanation": "保留原问题"}],
                                "number_types": [0, 0.0, False, None]}}
    frozen = copy.deepcopy(execution)
    recipe = SkillRecipe.model_validate(recipe_payload())
    result = SimpleNamespace(recipe=recipe, contract=method_contract(), execution=execution,
                             synthetic_plan={"fixture": "encoding", "values": [0, 0.0, False, None]},
                             program_reference_count=0)
    content, references, scripts = render_result(result)
    assert json.loads(references["references/validation.json"])["execution"] == frozen
    assert json.loads(references["references/synthetic-validation.json"]) == result.synthetic_plan
    assert execution == frozen
    assert len(json.dumps(execution, ensure_ascii=False, indent=2).encode()) > MAX_DRAFT_BYTES
    assert sum(len(item.encode()) for item in (content, *references.values(), *scripts.values())) < MAX_DRAFT_BYTES

    captured = []
    async def upload(stream, *args, **kwargs):
        captured.append(stream.read())
        return SimpleNamespace(file_id="encoding-fixture")
    repository = SimpleNamespace(create=AsyncMock(side_effect=lambda draft: draft))
    storage = SimpleNamespace(upload_file=AsyncMock(side_effect=upload))
    retention = SimpleNamespace(retain_file=AsyncMock(return_value=True))
    draft = await SkillDraftService(repository, storage, retention).create(
        owner_id="encoding-owner", session_id="encoding-session", name=recipe.name,
        description=recipe.description, triggers=recipe.triggers, applicability=recipe.applicability,
        capabilities=[step.action for step in recipe.steps], content=content,
        references=references, scripts=scripts, synthetic_execution=execution)
    assert not draft.installed_skill_id
    with zipfile.ZipFile(io.BytesIO(captured[0])) as archive:
        assert sum(item.file_size for item in archive.infolist()) < MAX_DRAFT_BYTES
        for path, text in references.items():
            assert archive.read(path).decode() == text
        recovered = json.loads(archive.read("references/validation.json"))["execution"]
    assert recovered == frozen
    assert [type(item) for item in recovered["provenance"]["number_types"]] == [int, float, bool, type(None)]
