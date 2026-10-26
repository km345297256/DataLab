"""Owner-scoped public analysis history, with explicit historical precedence.

Historical text is context to be retained or explicitly superseded by later
evidence, never an assertion that every earlier experiment is the final method.
Raw tools, original paths and private execution receipts stay out of this view.
"""
from app.domain.models.event import ErrorEvent, MessageEvent, PlanEvent, StepEvent, ToolEvent
from app.domain.models.plan import ExecutionStatus
from app.domain.services.skills.method_contract import evidence_sources
from .intent import is_explicit_skill_create_command


def analysis_turns(events):
    starts = [index for index, event in enumerate(events)
              if isinstance(event, MessageEvent) and event.role == "user"]
    turns = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(events)
        turn = list(events[start:end])
        if not any(isinstance(event, (PlanEvent, StepEvent)) for event in turn) and (
                is_explicit_skill_create_command(events[start].message)
                or any(isinstance(event, ToolEvent) and event.function_name == "skill_create_from_session"
                       for event in turn)):
            continue
        turns.append(turn)
    return turns


def execution_status(events):
    from .evidence import _verified_turn
    try:
        _verified_turn(events)
        return "completed"
    except ValueError:
        if any(isinstance(event, ErrorEvent)
               or isinstance(event, StepEvent) and event.step.status == ExecutionStatus.FAILED
               or isinstance(event, PlanEvent) and event.plan.status == ExecutionStatus.FAILED for event in events):
            return "failed"
        return "incomplete"


def _turn_revisions(turn, privacy, *, offset, status):
    """Archive replaced public method versions, never raw event/tool payloads."""
    from .evidence import _successful_step
    revisions = []
    steps, step_positions = {}, {}
    previous_goal, goal_position, previous_goal_status = None, None, "incomplete"
    seen_steps, seen_goals = set(), set()

    def archive_step(identifier, replacement):
        old = steps[identifier]
        signature = (old.description, old.result or "")
        if (not _successful_step(old) or replacement is not None and _successful_step(replacement)
                and signature == (replacement.description, replacement.result or "")):
            return
        identity = (identifier, *signature)
        if identity in seen_steps:
            return
        seen_steps.add(identity)
        revisions.append({"chronology": offset + step_positions[identifier] + 1,
            "revision_kind": "replaced_step", "evidence": {"verified_steps": [{
                "method": privacy.redact_text(old.description),
                "evidence_summary": privacy.redact_text(old.result or "")}]}})

    messages = [event for event in turn if isinstance(event, MessageEvent)
                and event.role == "assistant" and event.message.strip()]
    final_message = messages[-1] if messages else None
    for index, event in enumerate(turn):
        if isinstance(event, MessageEvent) and event.role == "assistant" and event is not final_message and event.message.strip():
            revisions.append({"chronology": offset + index + 1, "revision_kind": "public_discussion",
                              "evidence": {"delivery_summary": privacy.redact_text(event.message)}})
        if status != "completed":
            continue
        if isinstance(event, PlanEvent):
            goal_identity = (previous_goal, previous_goal_status)
            if previous_goal and previous_goal != event.plan.goal and goal_identity not in seen_goals:
                seen_goals.add(goal_identity)
                revisions.append({"chronology": offset + goal_position + 1, "revision_kind": "replaced_goal",
                                  "execution_status": previous_goal_status,
                                  "evidence": {"goal": privacy.redact_text(previous_goal)}})
            previous_goal, goal_position = event.plan.goal, index
            previous_goal_status = ("completed" if event.plan.status == ExecutionStatus.COMPLETED
                                    else "failed" if event.plan.status == ExecutionStatus.FAILED else "incomplete")
            replacement = {step.id: step for step in event.plan.steps}
            for identifier in steps:
                archive_step(identifier, replacement.get(identifier))
            steps = replacement
            step_positions = {identifier: index for identifier in steps}
        elif isinstance(event, StepEvent):
            if event.step.id in steps:
                archive_step(event.step.id, event.step)
            steps[event.step.id] = event.step
            step_positions[event.step.id] = index
    return sorted(revisions, key=lambda item: item["chronology"])


def build_analysis_history(turns, privacy):
    from .evidence import build_synthesis_evidence
    history = []
    offset = 0
    for index, turn in enumerate(turns):
        current = index == len(turns) - 1
        status = execution_status(turn)
        evidence = build_synthesis_evidence(turn, private_values=privacy)
        messages = [event for event in turn if isinstance(event, MessageEvent)
                    and event.role == "assistant" and event.message.strip()]
        # Public intermediate explanations are the available decision record;
        # they are observations, not hidden reasoning or verified obligations.
        discussion = [privacy.redact_text(event.message) for event in messages[:-1]]
        if status != "completed":
            evidence = {"research_request": evidence["research_request"],
                        "delivery_summary": evidence["delivery_summary"]}
        revisions = _turn_revisions(turn, privacy, offset=offset, status=status)
        offset += len(turn)
        history.append({"turn_id": f"T{index + 1:03d}", "chronology": offset,
                        "historical": not current, "execution_status": status,
                        "evidence": evidence, "public_discussion": discussion, "revisions": revisions})
    return history


def build_creation_sources(evidence, history):
    """Keep v1 current-source order/shape, then append explicitly typed history."""
    sources = evidence_sources(evidence)
    for turn in history:
        metadata = {key: turn[key] for key in ("turn_id", "chronology", "execution_status")}
        for revision in turn["revisions"]:
            for source in evidence_sources(revision["evidence"]):
                sources.append({**source, "id": f"S{len(sources) + 1:03d}", **metadata,
                                "historical": True, "chronology": revision["chronology"],
                                "execution_status": revision.get("execution_status", turn["execution_status"]),
                                "revision_kind": revision["revision_kind"]})
        if turn["historical"]:
            for source in evidence_sources(turn["evidence"]):
                sources.append({**source, "id": f"S{len(sources) + 1:03d}", **metadata, "historical": True})
    return sources


def history_index(history, sources):
    """Refer to source text once rather than repeating all prior conversations."""
    return [{**{key: turn[key] for key in ("turn_id", "chronology", "historical", "execution_status")},
             "source_ids": [source["id"] for source in sources
                            if source.get("turn_id") == turn["turn_id"]
                            or not turn["historical"] and not source.get("historical")]}
            for turn in history]


def bind_program_history(programs, records, sequence_turns, current_turn_id, sequence_chronology=None):
    """Expose source-version relationships without treating old versions as adopted.

    Exact source digests and original paths are only used locally to connect
    receipts. The model receives portable reference IDs and chronological turns.
    """
    by_digest = {}
    for record in records or []:
        by_digest.setdefault(record["source_digest"], []).append(record)
    references = {digest: reference for digest, reference in zip(by_digest, programs)}
    for reference, occurrences in zip(programs, by_digest.values()):
        turns = list(dict.fromkeys(sequence_turns[record["event_seq"]]
                    for record in sorted((item for item in occurrences if item.get("event_seq") in sequence_turns),
                                         key=lambda item: item["event_seq"])))
        if turns:
            reference["source_turns"] = turns
            reference["record_status"] = "current_turn" if current_turn_id in turns else "historical_success"
    # Follow receipts in their actual order. Grouping all future occurrences of
    # a digest first would incorrectly move a later path association backward.
    previous_paths = {}
    ordered_records = sorted((record for record in records or [] if record.get("event_seq") in sequence_turns),
                             key=lambda record: record["event_seq"])
    for record in ordered_records:
        path = record.get("script_path")
        if not isinstance(path, str):
            continue
        reference = references[record["source_digest"]]
        previous = previous_paths.get(path)
        if previous is not None and previous["reference_id"] != reference["id"]:
            prior = reference.setdefault("prior_versions", [])
            if previous["reference_id"] not in prior:
                prior.append(previous["reference_id"])
            reference["version_relation"] = "same_source_path_not_scope_or_scientific_acceptance"
            if sequence_chronology and previous["event_seq"] in sequence_chronology and record["event_seq"] in sequence_chronology:
                reference.setdefault("version_transitions", []).append({
                    "previous_reference_id": previous["reference_id"],
                    "previous_turn_id": sequence_turns[previous["event_seq"]],
                    "turn_id": sequence_turns[record["event_seq"]],
                    "previous_chronology": sequence_chronology[previous["event_seq"]],
                    "chronology": sequence_chronology[record["event_seq"]]})
        previous_paths[path] = {"reference_id": reference["id"], "event_seq": record["event_seq"]}
