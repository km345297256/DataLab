"""Cross-turn evidence keeps chronology and does not promote failed experiments."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import DoneEvent, MessageEvent, PlanEvent, PlanStatus, StepEvent, StepStatus, ToolEvent, ToolStatus
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.skills.method_contract import CoverageError, MethodContract, evidence_sources, validate_contract
from app.domain.services.skills.program_method_evidence import method_program_references
from app.domain.services.skills.skill_creation.evidence import build_synthesis_evidence, prepare_analysis_context
from app.domain.services.skills.skill_creation.history import bind_program_history
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_program_method_projection import host_record
from test_session_skill_creator import Repository, analysis_events


def turn(request, *, failed=False, discussion=None):
    events = analysis_events(failed=failed)
    events[0].message = request
    if discussion:
        events.insert(1, MessageEvent(role="assistant", message=discussion))
    return events


def program(events, sequence, source):
    events.insert(2, ToolEvent(seq=sequence, tool_call_id=f"program-{sequence}", tool_name="shell",
        function_name="program_run", function_args={}, status=ToolStatus.CALLED,
        function_result={"success": True}))
    return {**host_record(source), "event_seq": sequence}


@pytest.mark.asyncio
async def test_earlier_requests_and_public_decisions_are_typed_history_not_silently_lost():
    earlier = turn("Use HC3 and alpha 0.05.", discussion="Explain why the covariance choice matters.")
    failed = turn("Try a different grouping.", failed=True, discussion="This candidate has unresolved grouping assumptions.")
    failed[3].step.description = "FAILED_METHOD_MUST_NOT_BECOME_SUCCESS_EVIDENCE"
    latest = turn("Use the revised grouping and preserve the initial robust covariance.",
                  discussion="The revised grouping supersedes the earlier grouping choice.")
    events = [*earlier, MessageEvent(role="user", message="/skill-creator"), *failed, *latest]
    repository = Repository(events)
    before = copy.deepcopy([event.model_dump() for event in events])
    context = await prepare_analysis_context("session-owned", "owner", repository, SimpleNamespace(user_id="owner"))
    current_sources = evidence_sources(build_synthesis_evidence(latest, private_values=context.privacy))
    assert context.sources[:len(current_sources)] == current_sources
    historical = [source for source in context.sources if source.get("historical")]
    assert {source["turn_id"] for source in historical} == {"T001", "T002", "T003"}
    assert max(source["chronology"] for source in historical if source["turn_id"] == "T001") < min(
        source["chronology"] for source in historical if source["turn_id"] == "T002")
    assert any("HC3" in source["text"] and source["kind"] == "request" for source in historical)
    assert any("covariance choice" in source["text"] for source in historical)
    assert all(source["kind"] in {"request", "observed_summary"}
               for source in historical if source["execution_status"] != "completed")
    assert "FAILED_METHOD_MUST_NOT_BECOME_SUCCESS_EVIDENCE" not in json.dumps(context.evidence)
    assert "FAILED_METHOD_MUST_NOT_BECOME_SUCCESS_EVIDENCE" not in json.dumps(context.sources)
    assert "rm -rf" not in json.dumps(context.evidence) and "private-row" not in json.dumps(context.evidence)
    assert context.history[-1]["turn_id"] == "T003" and not context.history[-1]["historical"]
    assert [event.model_dump() for event in events] == before


@pytest.mark.asyncio
async def test_historical_private_values_are_redacted_using_all_selected_turns():
    earlier = turn("Analyze /Users/alice/private/table.csv and retain the chosen method.",
                   discussion="The input at /Users/alice/private/table.csv was inspected.")
    latest = turn("Continue with the same method on the current input.")
    context = await prepare_analysis_context("session-owned", "owner", Repository([*earlier, *latest]),
                                             SimpleNamespace(user_id="owner"))
    assert "/Users/alice" not in json.dumps(context.evidence)
    assert "/Users/alice" not in json.dumps(context.sources)
    assert not context.privacy.scan_document(context.evidence)


@pytest.mark.asyncio
async def test_program_versions_are_explicit_context_without_replaying_or_adopting_failed_sources():
    earlier = turn("Use HC3 for the first analysis.")
    failed = turn("Try a changed method.", failed=True)
    latest = turn("Retain HC3 and use the final corrected calculation.")
    old = program(earlier, 10, 'value = compute(data, method="HC3")')
    program(failed, 20, 'value = failed_method(data)')
    new = program(latest, 30, 'value = corrected(data, method="HC3")')
    repository = Repository([*earlier, *failed, *latest])
    repository.get_successful_program_sources = AsyncMock(return_value=[old, new])
    context = await prepare_analysis_context("session-owned", "owner", repository, SimpleNamespace(user_id="owner"))
    repository.get_successful_program_sources.assert_awaited_once_with("session-owned", "owner", [10, 30])
    assert context.programs[0]["record_status"] == "historical_success"
    assert context.programs[1]["record_status"] == "current_turn"
    assert context.programs[1]["prior_versions"] == ["P001"]
    assert context.programs[0]["source_turns"] == ["T001"]
    assert context.programs[1]["source_turns"] == ["T003"]
    assert all(item["reference_only"] for item in context.programs)
    assert all("HC3" in item["source"] for item in context.programs)
    assert old["script_path"] not in json.dumps(context.programs)
    assert old["source_digest"] not in json.dumps(context.programs)
    assert "failed_method" not in json.dumps(context.programs)


@pytest.mark.asyncio
async def test_latest_failed_analysis_still_cannot_fall_back_to_older_success():
    repository = Repository([*turn("Earlier completed analysis."), *turn("Newest unresolved analysis.", failed=True)])
    repository.get_successful_program_sources = AsyncMock()
    with pytest.raises(SkillCreationError) as failure:
        await prepare_analysis_context("session-owned", "owner", repository, SimpleNamespace(user_id="owner"))
    assert failure.value.code == "analysis_not_ready"
    repository.get_successful_program_sources.assert_not_awaited()


def revised_turn(before="HC0", after="HC3", *, update_kind="plan", repeat=False):
    old = Step(id="old", description=f"Use {before} covariance for the comparison.",
               result="Completed the first calculation.", status=ExecutionStatus.COMPLETED, success=True)
    new = old.model_copy(update={"id": "new" if update_kind == "plan" else "old",
                                 "description": f"Use {after} covariance for the comparison.",
                                 "result": "Completed the corrected calculation."})
    old_plan = PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(title="Compare data", goal="Compare the selected data.",
        steps=[old], status=ExecutionStatus.COMPLETED))
    events = [MessageEvent(role="user", message="Compare the selected data using the final method."), old_plan]
    if repeat:
        events.extend([old_plan.model_copy(deep=True), StepEvent(step=old, status=StepStatus.COMPLETED)])
    events.append(MessageEvent(role="assistant", message=f"Initially used {before}; the choice is being reconsidered."))
    if update_kind == "plan":
        events.append(PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(title="Corrected comparison",
            goal="Compare the selected data.", steps=[new], status=ExecutionStatus.COMPLETED)))
    else:
        events.append(StepEvent(step=new, status=StepStatus.COMPLETED))
    events.extend([MessageEvent(role="assistant", message=f"Delivered the comparison using {after}."), DoneEvent()])
    return events


def replacement_contract(older, later):
    return MethodContract(requirements=[{"id": "R001", "kind": "method", "statement": later["text"],
                                        "source_ids": [later["id"]]}],
        excluded_sources=[{"source_id": older["id"], "reason": "superseded",
                           "explanation": "The later retained method replaces this earlier method.",
                           "replacement_source_ids": [later["id"]]}])


@pytest.mark.asyncio
@pytest.mark.parametrize("update_kind", ["plan", "step"])
async def test_same_turn_replaced_methods_and_intermediate_discussion_are_explicit_history(update_kind):
    events = revised_turn(update_kind=update_kind, repeat=True)
    context = await prepare_analysis_context("session-owned", "owner", Repository(events), SimpleNamespace(user_id="owner"))
    current = evidence_sources(build_synthesis_evidence(events, private_values=context.privacy))
    assert context.sources[:len(current)] == current
    assert all(set(source) == {"id", "kind", "text"} for source in current)
    assert all("HC0" not in source["text"] for source in current)
    old_methods = [source for source in context.sources if source["kind"] == "method" and "HC0" in source["text"]]
    assert len(old_methods) == 1  # unchanged Plan/Step snapshots are not extra methods
    old = old_methods[0]
    assert old["historical"] and old["turn_id"] == "T001" and old["revision_kind"] == "replaced_step"
    discussions = [source for source in context.sources if source.get("revision_kind") == "public_discussion"]
    assert len(discussions) == 1 and discussions[0]["historical"]
    retained = next(source for source in current if source["kind"] == "method" and "HC3" in source["text"])
    validate_contract(replacement_contract(old, retained), [retained, old])


@pytest.mark.asyncio
async def test_mixed_turn_and_revision_chronology_allows_only_forward_retained_replacements():
    first, second, latest = revised_turn("HC0", "HC1"), revised_turn("HC2", "HC3"), turn("Finalize the current comparison.")
    context = await prepare_analysis_context("session-owned", "owner", Repository([*first, *second, *latest]),
                                             SimpleNamespace(user_id="owner"))
    methods = {source["text"].split()[1]: source for source in context.sources
               if source["kind"] == "method" and source["text"].startswith("Use HC")}
    assert methods["HC0"]["chronology"] < methods["HC1"]["chronology"] < methods["HC2"]["chronology"] < methods["HC3"]["chronology"]
    assert methods["HC1"]["chronology"] == len(first)
    assert methods["HC3"]["chronology"] == len(first) + len(second)
    validate_contract(replacement_contract(methods["HC0"], methods["HC3"]), [methods["HC0"], methods["HC3"]])
    with pytest.raises(CoverageError, match="invalid_historical_exclusion"):
        validate_contract(replacement_contract(methods["HC2"], methods["HC1"]), [methods["HC2"], methods["HC1"]])


def test_reused_source_digest_does_not_move_its_future_path_back_in_history():
    records = [{"source_digest": "digest-a", "script_path": "/private/a.py", "event_seq": 100},
               {"source_digest": "digest-b", "script_path": "/private/b.py", "event_seq": 200},
               {"source_digest": "digest-a", "script_path": "/private/b.py", "event_seq": 300}]
    references = [{"id": "P001"}, {"id": "P002"}]
    bind_program_history(references, records, {100: "T001", 200: "T002", 300: "T003"}, "T003",
                         {100: 3, 200: 8, 300: 15})
    assert "prior_versions" not in references[1]
    assert references[0]["prior_versions"] == ["P002"]
    assert references[0]["version_transitions"] == [{"previous_reference_id": "P002", "previous_turn_id": "T002",
                                                     "turn_id": "T003", "previous_chronology": 8, "chronology": 15}]
    serialized = json.dumps(references)
    assert "/private" not in serialized and "digest-" not in serialized and "event_seq" not in serialized


def test_version_relationship_order_uses_receipt_sequence_without_changing_reference_identity():
    records = [{"source_digest": "digest-b", "script_path": "/private/a.py", "event_seq": 200},
               {"source_digest": "digest-a", "script_path": "/private/a.py", "event_seq": 100}]
    # Projection IDs follow input digest order, while temporal edges follow seq.
    references = [{"id": "P001"}, {"id": "P002"}]
    bind_program_history(references, records, {100: "T001", 200: "T002"}, "T002")
    assert references[0]["prior_versions"] == ["P002"]
    assert "prior_versions" not in references[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_status", list(ExecutionStatus))
async def test_replaced_goal_keeps_its_own_execution_status_when_the_turn_later_succeeds(previous_status):
    events = revised_turn()
    plans = [event.plan for event in events if isinstance(event, PlanEvent)]
    plans[0].goal = "Use prior-goal-selector."
    plans[0].status = previous_status
    plans[0].steps[0].status = previous_status
    plans[0].steps[0].success = previous_status == ExecutionStatus.COMPLETED
    plans[-1].goal = "Use final-goal-selector."
    context = await prepare_analysis_context("session-owned", "owner", Repository(events), SimpleNamespace(user_id="owner"))
    archived = next(source for source in context.sources if source.get("revision_kind") == "replaced_goal")
    expected_status = ("completed" if previous_status == ExecutionStatus.COMPLETED
                       else "failed" if previous_status == ExecutionStatus.FAILED else "incomplete")
    assert archived["historical"] and archived["execution_status"] == expected_status
    assert "prior-goal-selector" in archived["text"]  # retained context, never silently dropped
    reference = method_program_references([host_record('earlier = compute(mode="prior-goal-selector")\nfinal = compute(mode="final-goal-selector")')],
        privacy_context=context.privacy, method_sources=context.sources)[0]
    assert ("prior-goal-selector" in reference["source"]) == (previous_status == ExecutionStatus.COMPLETED)
    assert "final-goal-selector" in reference["source"]


@pytest.mark.asyncio
async def test_same_goal_failed_then_successful_history_keeps_distinct_status_evidence():
    events = revised_turn()
    first = events[1]
    first.plan.goal = "Use repeated-goal-selector."
    first.plan.status = ExecutionStatus.FAILED
    first.plan.steps[0].status = ExecutionStatus.FAILED
    first.plan.steps[0].success = False
    final_plan = next(event for event in reversed(events) if isinstance(event, PlanEvent))
    completed_repeat = final_plan.model_copy(deep=True)
    completed_repeat.plan.goal = first.plan.goal
    events.insert(events.index(final_plan) + 1, completed_repeat)
    events.insert(events.index(completed_repeat) + 1, final_plan.model_copy(deep=True))
    context = await prepare_analysis_context("session-owned", "owner", Repository(events), SimpleNamespace(user_id="owner"))
    archived = [source for source in context.sources if source.get("revision_kind") == "replaced_goal"
                and "repeated-goal-selector" in source["text"]]
    assert {source["execution_status"] for source in archived} == {"failed", "completed"}
    reference = method_program_references([host_record('value = compute(mode="repeated-goal-selector")')],
        privacy_context=context.privacy, method_sources=context.sources)[0]
    assert "repeated-goal-selector" in reference["source"]
