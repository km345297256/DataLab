"""Selected methods remain usable in a single run without replaying old work."""
import json

import pytest

from app.domain.models.event import MessageEvent
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.skills import SkillRegistry, SkillRenderer
from app.domain.services.tools.skill import SkillToolkit


def _package(tmp_path, *, script=True, validation=None):
    root = tmp_path / "skills"
    package = root / "grouped-analysis"
    (package / "references").mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "Use references/method.md and preserve full precision. "
        "Apply scripts/analyze.py to the selected inputs.", encoding="utf-8")
    (package / "references" / "method.md").write_text(
        "Group observations by experimental unit; record each exclusion separately.", encoding="utf-8")
    if validation is not None:
        (package / "references" / "validation.json").write_text(
            validation if isinstance(validation, str) else json.dumps(validation), encoding="utf-8")
    if script:
        (package / "scripts").mkdir()
        (package / "scripts" / "analyze.py").write_text(
            "import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument('--input')\n", encoding="utf-8")
    registry = SkillRegistry(str(root))
    registry.restrict_to(["grouped-analysis"])
    registry.pin()
    return registry, package


@pytest.mark.asyncio
async def test_full_method_references_and_script_are_from_pinned_selected_package(tmp_path):
    registry, package = _package(tmp_path)
    toolkit = SkillToolkit(registry)
    (package / "references" / "method.md").write_text("changed method", encoding="utf-8")
    (package / "scripts" / "analyze.py").write_text("changed script", encoding="utf-8")
    result = (await toolkit.get_tool("skill_read").ainvoke(
        {"id": "read", "args": {"name": "grouped-analysis"}})).artifact
    script = (await toolkit.get_tool("skill_read_script").ainvoke(
        {"id": "script", "args": {"skill_name": "grouped-analysis", "script_filename": "analyze.py"}})).artifact

    assert result.success and script.success
    assert "preserve full precision" in result.data["content"]
    assert result.data["reference_contents"] == {
        "method.md": "Group observations by experimental unit; record each exclusion separately."}
    assert result.data["implementation"] == "scripts_available"
    assert result.data["deferred_references"] == []
    assert "parser.add_argument('--input')" in script.data["content"]
    assert "changed" not in script.data["content"]
    assert result.data["validation"]["state"] == "unknown"
    assert script.data["validation"] == result.data["validation"]


@pytest.mark.asyncio
async def test_large_references_are_deferred_whole_and_method_only_is_explicit(tmp_path, monkeypatch):
    registry, _ = _package(tmp_path, script=False)
    toolkit = SkillToolkit(registry)
    monkeypatch.setattr(SkillToolkit, "MAX_INLINE_REFERENCE_BYTES", 10)
    result = (await toolkit.get_tool("skill_read").ainvoke(
        {"id": "read", "args": {"name": "grouped-analysis"}})).artifact
    reference = (await toolkit.get_tool("skill_read_reference").ainvoke(
        {"id": "ref", "args": {"skill_name": "grouped-analysis", "ref_filename": "method.md"}})).artifact

    assert result.data["reference_contents"] == {}
    assert result.data["deferred_references"] == ["method.md"]
    assert result.data["implementation"] == "method_only"
    assert reference.data["content"].endswith("each exclusion separately.")


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [
    None,
    {"status": "passed", "checks": {"structure": "passed", "script_static": "passed"}},
    {"status": "not_run", "checks": {"script_static": "passed"}},
    "not valid json",
    [],
    {"execution": {"status": "passed", "scope": "current_user_data"}},
    {"execution": {"status": ["passed"], "scope": "synthetic"}},
])
async def test_missing_legacy_or_invalid_validation_never_becomes_an_execution_pass(tmp_path, record):
    registry, _ = _package(tmp_path, validation=record)
    result = (await SkillToolkit(registry).get_tool("skill_read").ainvoke(
        {"id": "read", "args": {"name": "grouped-analysis"}})).artifact
    validation = result.data["validation"]
    assert result.success
    assert validation["state"] == "unknown"
    assert validation["recorded_only"] is True
    assert validation["current_data_validated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["passed", "failed", "not_run", "unavailable", "unsupported"])
async def test_recorded_synthetic_state_is_pinned_explicit_and_not_current_data_evidence(tmp_path, state):
    record = {"execution": {"status": state, "scope": "synthetic", "cases": [
        {"id": kind, "kind": kind, "status": state, "reason": "expected scientific assertions"}
        for kind in ("normal", "boundary", "invalid")
    ]}}
    registry, package = _package(tmp_path, validation=record)
    # The script and validation must both retain the version selected for this
    # turn, even when another process replaces files in the installed package.
    (package / "references" / "validation.json").write_text('{"execution":{"status":"other"}}')
    toolkit = SkillToolkit(registry)
    result = (await toolkit.get_tool("skill_read").ainvoke(
        {"id": "read", "args": {"name": "grouped-analysis"}})).artifact
    script = (await toolkit.get_tool("skill_read_script").ainvoke(
        {"id": "script", "args": {"skill_name": "grouped-analysis", "script_filename": "analyze.py"}})).artifact
    expected = {"state": state, "source": "references/validation.json", "scope": "synthetic",
                "recorded_only": True, "current_data_validated": False,
                "case_counts": {"normal": 1, "boundary": 1, "invalid": 1}}
    assert result.data["validation"] == expected
    assert script.data["validation"] == expected


@pytest.mark.asyncio
async def test_failed_scientific_checks_remain_visible_when_references_are_deferred(tmp_path, monkeypatch):
    record = {"status": "passed", "checks": {"script_static": "passed"},
              "execution": {"status": "failed", "scope": "synthetic", "returncode": 0,
                            "cases": [{"id": "missing", "kind": "boundary", "status": "failed",
                                       "reason": "All-missing group was filled with zero"}]}}
    registry, _ = _package(tmp_path, validation=record)
    monkeypatch.setattr(SkillToolkit, "MAX_INLINE_REFERENCE_BYTES", 10)
    result = (await SkillToolkit(registry).get_tool("skill_read").ainvoke(
        {"id": "read", "args": {"name": "grouped-analysis"}})).artifact
    assert "validation.json" in result.data["deferred_references"]
    assert result.data["reference_contents"] == {}
    assert result.data["validation"]["state"] == "failed"
    assert result.data["validation"]["case_counts"] == {"boundary": 1}


@pytest.mark.asyncio
async def test_method_only_not_applicable_state_does_not_invent_script_execution(tmp_path):
    registry, _ = _package(tmp_path, script=False, validation={
        "execution": {"status": "not_applicable", "scope": "synthetic", "cases": []}})
    result = (await SkillToolkit(registry).get_tool("skill_read").ainvoke(
        {"id": "read", "args": {"name": "grouped-analysis"}})).artifact
    assert result.data["implementation"] == "method_only"
    assert result.data["resources"]["scripts"] == []
    assert result.data["validation"]["state"] == "not_applicable"
    assert result.data["validation"]["case_counts"] == {}
    assert result.data["validation"]["current_data_validated"] is False


def test_skill_validation_policy_distinguishes_tests_scientific_evidence_and_read_only_intent(tmp_path):
    from app.domain.services.prompts.execution import EXECUTION_PROMPT
    from app.domain.services.prompts.planner import PLANNER_SYSTEM_PROMPT

    registry, _ = _package(tmp_path)
    rendered = SkillRenderer.render(registry.list_skills())
    assert "a zero exit code or files existing does not establish a pass" in rendered
    assert "do not substitute a large real-data run for validation" in rendered
    assert "without launching tests or analysis" in rendered
    assert "repair only this run's implementation" in EXECUTION_PROMPT
    assert "Inspection-only and explanation-only requests do not authorize synthetic tests" in EXECUTION_PROMPT
    assert "must not gain test-running or analysis steps" in PLANNER_SYSTEM_PROMPT


def test_fast_path_exposes_selected_method_tools_without_skill_creation(tmp_path):
    registry, _ = _package(tmp_path)
    toolkit = SkillToolkit(registry)
    agent = object.__new__(ExecutionAgent)
    agent.toolkits = [toolkit]
    agent._dataset_fast_path_mode = True

    names = {agent._tool_name(tool) for tool in agent.get_tools()}
    assert {"skill_read", "skill_read_reference", "skill_read_script"} <= names
    assert agent.get_tool("skill_read") is not None
    assert "skill_create_from_session" not in names
    assert agent.get_tool("skill_create_from_session") is None
    rendered = SkillRenderer.render(registry.list_skills())
    assert "Reuse it instead of independently reconstructing the algorithm" in rendered


def _plan_payload(plan, current):
    rendered = ExecutionAgent._render_plan_context(plan, current)
    return json.loads(next(line for line in rendered.splitlines() if line.startswith("{")))


def test_multistep_context_assigns_current_duty_and_consumes_prior_results_only():
    qc = Step(id="prepare", description="Validate and aggregate observations", success=True,
              status=ExecutionStatus.COMPLETED, result="Prepared 119 observations",
              attachments=["/home/ubuntu/output/aggregated.csv"])
    failed = Step(id="failed", description="Earlier failed attempt", success=False,
                  status=ExecutionStatus.FAILED, attachments=["/home/ubuntu/output/unverified.csv"])
    model = Step(id="model", description="Fit requested model using prepared observations",
                 inputs={"source_step": "prepare", "input": "/home/ubuntu/output/aggregated.csv"})
    report = Step(id="report", description="Write final report from the fitted model")
    payload = _plan_payload(Plan(goal="Prepare, model and report", steps=[qc, failed, model, report]), model)

    assert payload["evidence_scope"] == "current_plan_only"
    assert payload["current_step"]["description"] == model.description
    assert payload["current_step"]["inputs"]["source_step"] == "prepare"
    assert payload["later_step_responsibilities"] == [{"id": "report", "description": report.description}]
    assert payload["existing_artifacts"] == ["/home/ubuntu/output/aggregated.csv"]
    assert payload["completed_steps"][1]["success"] is False


def test_history_can_resolve_references_but_is_not_current_execution_evidence():
    flow = object.__new__(PlanActFlow)
    payload = json.loads(flow._render_session_context([
        MessageEvent(role="assistant", message="Earlier model result and report completed.")]))

    assert payload["evidence_scope"] == "historical_context_only"
    assert payload["current_execution_evidence"] is False
    assert "Earlier model result" in payload["messages"][0]["content"]
