"""A generated reference disagreement cannot authorize scientific rewrites."""
import copy
import json
from unittest.mock import AsyncMock

import pytest

from skill_review_fixtures import review_for_payload

from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_script_verification import (
    _reference_corrections_confirmed, verify_skill_script_cases,
)
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_session_skill_creator import helper_script
from test_skill_creation_checkpoints import Artifact, MemoryCache, builds, generated
from test_skill_reference_verification import host_receipt, method_review, output_files
from test_skill_script_verification import recipe, revalidate
from test_skill_validation_plan import method_contract, sources
from test_skill_validation_review import accepted_fixture_review, typed


def scenario(monkeypatch, *, initial, reference, candidate, unrelated_failure=False):
    plan = typed()
    normal = plan.scripts[0].cases[0]
    for row in normal.input_files[0].rows:
        row["value"] = 0.25
    normal.assertions[0].fields["mean"].value = initial
    normal.oracle_explanation = "The arithmetic mean of two values 0.25 and 0.25 is 0.25."
    original, calls, runs = recipe(), [], []
    original_dump = original.model_dump(mode="json")

    async def generate(schema, stage, instruction, payload, check):
        calls.append((stage, copy.deepcopy(payload)))
        if stage == "synthetic_validation_plan":
            document = plan.model_dump()
        elif stage == "synthetic_reference_implementation":
            document = {**payload["script_request"], "content": helper_script()["content"]}
        elif stage == "synthetic_reference_review":
            document = method_review(payload)
        elif stage == "synthetic_plan_review":
            document = review_for_payload(accepted_fixture_review(payload["validation_plan"]), payload)
        elif stage == "synthetic_method_audit":
            # A wrong reference does not establish a defect in the original
            # method. Preserve the original numeric fixture and source.
            document = {"requirements": [{"requirement_id": requirement_id,
                "status": "preserved", "source_id": "", "source_quote": "",
                "script_path": item["script_path"], "code_quote": "",
                "explanation": "No original-source method violation is established by this implementation."}
                for item in payload["review_scope"] for requirement_id in item["requirement_ids"]]}
        else:
            assert stage == "synthetic_script_repair"
            document = {**payload["script"], "content": payload["script"]["content"] + "\nrepaired_status_handling = True\n"}
        value = schema.model_validate(document)
        check(value)
        return value

    async def execute(programs, frozen):
        tested = "IMPLEMENTATION_MARKER_NOT_FOR_ORACLE" in "\n".join(programs.values())
        candidate_number = sum(item[0] for item in runs)
        runs.append((tested, dict(programs), frozen.model_dump(mode="json")))
        def files(case):
            values = output_files(case)
            if case.kind == "normal":
                mean = candidate if tested else reference
                values["summary.csv"] = f"group,mean,valid_count,missing_count,missing_rate\nsynthetic-a,{mean},2,0,0\n".encode()
                if tested and unrelated_failure and candidate_number == 0:
                    values["status.json"] = json.dumps({"status": "wrong"}).encode()
            return values
        return host_receipt(programs, frozen, files=files)
    monkeypatch.setattr(worker, "validate_skill_scripts", execute)
    return original, original_dump, generate, calls, runs


@pytest.mark.asyncio
async def test_correct_candidate_quarter_is_not_rewritten_to_wrong_reference_half_and_retry_preserves_source(monkeypatch):
    original, before, base, calls, runs = scenario(monkeypatch, initial=0.25, reference=0.5, candidate=0.25)
    cache = MemoryCache()
    upstream = AsyncMock(return_value=Artifact(content="Previously validated scientific method and original code."))
    method_review_after_repair = AsyncMock()
    for _ in range(2):
        build = builds(cache)
        for stage in ("method_requirements", "workflow", "implementation"):
            await generated(build, upstream, stage=stage)
        async def generate(schema, stage, instruction, payload, check):
            return await build.run(schema, stage, instruction, payload, check,
                lambda: base(schema, stage, instruction, payload, check))
        with pytest.raises(SkillCreationError) as failure:
            await verify_skill_script_cases(original, method_contract(), sources(), generate=generate,
                revalidate=revalidate, review=method_review_after_repair, checkpoints=build)
        assert (failure.value.stage, failure.value.code) == (
            "synthetic_reference_execution", "reference_calculation_unconfirmed")
        saved = await builds(cache).get_execution_revision()
        assert saved is not None and saved.recipe.model_dump(mode="json") == before
        assert saved.repair_used is False and saved.pending_recipe is None
        assert saved.method_audit.verdict() == "accepted"
    assert upstream.await_count == 3
    assert original.model_dump(mode="json") == before
    assert all(programs == {script.path: script.content for script in original.scripts}
               for tested, programs, _ in runs if tested)
    stages = [stage for stage, _ in calls]
    assert "synthetic_script_repair" not in stages
    # Explicit retry retains this failed evidence; it cannot silently replace
    # the frozen question or re-poll an unchanged method for another verdict.
    assert stages.count("synthetic_validation_plan") == stages.count("synthetic_reference_implementation") == 1
    assert stages.count("synthetic_method_audit") == 1
    assert "synthetic_method_repair" not in stages
    assert sum(tested for tested, _, _ in runs) == 2
    method_review_after_repair.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_rounding_correction_still_allows_unchanged_candidate_that_passes(monkeypatch):
    original, before, generate, calls, runs = scenario(monkeypatch, initial=0.2500000002, reference=0.25, candidate=0.25)
    result, _, receipt = await verify_skill_script_cases(original, method_contract(), sources(),
        generate=generate, revalidate=revalidate, review=AsyncMock())
    assert result.model_dump(mode="json") == before and receipt["status"] == "passed"
    assert len(receipt["reference_verification"]["corrections"]) == 1
    assert "synthetic_script_repair" not in [stage for stage, _ in calls]
    assert len(runs) == 3


@pytest.mark.asyncio
async def test_confirmed_corrected_targets_do_not_prevent_repairing_other_original_targets(monkeypatch):
    original, _, generate, calls, runs = scenario(monkeypatch, initial=0.2500000002, reference=0.25,
        candidate=0.25, unrelated_failure=True)
    review = AsyncMock()
    result, _, receipt = await verify_skill_script_cases(original, method_contract(), sources(),
        generate=generate, revalidate=revalidate, review=review)
    assert receipt["status"] == "passed" and len(runs) == 4
    assert [stage for stage, _ in calls].count("synthetic_script_repair") == 1
    assert "repaired_status_handling" in result.scripts[0].content
    review.assert_awaited_once_with(result)


@pytest.mark.parametrize("target_type,field,identifiers", [
    ("json_value", "value", ["assertion_3"]),
    ("json_array_length", "length", ["assertion_3"]),
    ("csv_row_count", "count", ["assertion_3.count"]),
    ("csv_field", "count", ["assertion_3.row_1.count", "assertion_3.row_2.count"]),
    ("csv_field", "mean.value", ["assertion_3.row_1.mean.value"]),
])
def test_each_corrected_target_requires_its_own_complete_passing_host_checks(target_type, field, identifiers):
    correction = {"script_path": "scripts/summary.py", "case_id": "normal", "assertion_index": 2,
                  "target_type": target_type, "field": field, "before": 0.25, "after": 0.5}
    case = {"script_path": "scripts/summary.py", "id": "normal",
            "checks": [{"id": name, "passed": True} for name in identifiers]}
    assert _reference_corrections_confirmed({"cases": [case]}, [correction])
    case["checks"][-1]["passed"] = False
    assert not _reference_corrections_confirmed({"cases": [case]}, [correction])
    case["checks"] = [{"id": "assertion_2", "passed": True}]
    assert not _reference_corrections_confirmed({"cases": [case]}, [correction])
    assert not _reference_corrections_confirmed({"cases": []}, [correction])
