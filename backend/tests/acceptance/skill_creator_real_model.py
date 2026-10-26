"""Explicit opt-in real-model acceptance; never reads a user session or DB.

Run via the accompanying overlay runner against the configured DataLab backend.
The synthetic reference is executed first. Production source, model adapter and
isolated helper worker are exercised unchanged; only persistence and usage
recording are replaced with in-memory sinks. Every artificial model exchange is
captured for diagnosis. This is deliberately outside automatic pytest discovery.
"""
from __future__ import annotations

import asyncio
import argparse
import contextlib
import dataclasses
import hashlib
import inspect
import json
import logging
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skill_creator_scenarios import SCENARIOS, PAIRED_MIXED_SIGN_INPUT, PAIRED_MIXED_SIGN_EXPECTED

from app.domain.models.event import DoneEvent, MessageEvent, PlanEvent, PlanStatus, StepEvent, StepStatus, ToolEvent, ToolStatus
from app.domain.models.file import FileInfo
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.tool_result import ToolResult
from app.domain.services.skills.skill_creation.service import CreationDependencies, create_skill_from_session
from app.domain.utils.model_response import model_answer_text, model_finish_reason


def report(kind, **values):
    print(json.dumps({"kind": kind, **values}, ensure_ascii=False), flush=True)


def validated_replay_message(content, validators, finish_reason="stop"):
    """Exercise the same response callback that the real model driver uses."""
    from langchain_core.messages import AIMessage
    assert validators, "Replay requires the production response-validation boundary"
    message = AIMessage(content=content, response_metadata={"finish_reason": finish_reason})
    validators[-1](message)
    return message


def creator_wire_metadata(request):
    """Inspect only explicit SDK request options, never model reasoning text."""
    body = {**request, **(request.get("extra_body") or {})}
    thinking = body.get("thinking") or {}
    metadata = {"wire_max_output_tokens": body.get("max_tokens"),
                "wire_response_format": body.get("response_format"),
                "wire_thinking_type": thinking.get("type"),
                "wire_reasoning_effort": body.get("reasoning_effort")}
    assert metadata["wire_thinking_type"] == "enabled", "Creator thinking must be enabled at the SDK boundary"
    assert metadata["wire_reasoning_effort"] == "high", "Creator reasoning effort must be high at the SDK boundary"
    assert metadata["wire_max_output_tokens"] == 393216, "Creator output capacity changed at the SDK boundary"
    assert metadata["wire_response_format"] in (None, {"type": "json_object"})
    return metadata


class WireAuditedAsyncClient:
    """Transparent SDK create proxy; records whitelisted request flags only."""
    def __init__(self, client, capture):
        self.client, self.capture = client, capture

    def __getattr__(self, name):
        if name in {"with_raw_response", "with_streaming_response"}:
            return WireAuditedAsyncClient(getattr(self.client, name), self.capture)
        return getattr(self.client, name)

    async def create(self, *args, **kwargs):
        self.capture(creator_wire_metadata(kwargs))
        return await self.client.create(*args, **kwargs)


def artifact(kind, stage, content):
    print("ARTIFACT:" + json.dumps({"kind": kind, "stage": stage, "content": content}, ensure_ascii=False), flush=True)


def traced_reference_worker(execute, capture=None):
    """Keep actual artificial execution evidence even when a later gate rejects."""
    async def traced(*args, **kwargs):
        receipt = await execute(*args, **kwargs)
        if capture is not None:
            capture(args, kwargs, receipt)
        artifact("reference_worker_receipt", "synthetic_reference_execution", receipt)
        return receipt
    return traced


def stabilize_artificial_event_identity(events, scenario):
    """Repeat the same authored session across explicit acceptance invocations.

    Real events already persist their IDs. The in-memory artificial repository
    must not create new UUIDs on each retry: those IDs are correctly included
    in production's private-source scope. No scientific payload is changed.
    """
    from datetime import datetime
    from uuid import NAMESPACE_URL, uuid5
    assert scenario in SCENARIOS
    assert len({event.seq for event in events}) == len(events)
    identities = []
    for event in events:
        event.id = str(uuid5(NAMESPACE_URL, f"datalab-artificial-acceptance-v1/{scenario}/event/{event.seq}"))
        event.timestamp = datetime(2020, 1, 1, 0, 0, 0)
        identity = {"seq": event.seq, "id": event.id, "timestamp": event.timestamp.isoformat()}
        if isinstance(event, PlanEvent):
            event.plan.id = str(uuid5(NAMESPACE_URL, f"datalab-artificial-acceptance-v1/{scenario}/plan/{event.seq}"))
            identity["plan_id"] = event.plan.id
        identities.append(identity)
    return {"version": 1, "scenario": scenario, "events": identities}


def assert_draft_package_limits(values):
    """Apply the actual draft storage byte/count limits before test capture.

    The artificial draft is intentionally not persisted, but it must not claim
    success for a package the real storage service would reject.
    """
    from app.application.services.skill_draft_service import MAX_DRAFT_BYTES, SkillDraftService
    markdown = ("---\nname: " + values["name"] + "\ndescription: " + json.dumps(values["description"], ensure_ascii=False)
        + "\ntriggers: " + json.dumps(values["triggers"], ensure_ascii=False)
        + "\n---\n\n" + values["content"].strip() + "\n")
    files = {"SKILL.md": markdown, **values["references"], **values["scripts"]}
    for path in files:
        SkillDraftService._validate_resource(path)
    total = sum(len(value.encode("utf-8")) for value in files.values())
    assert len(files) <= 64 and total <= MAX_DRAFT_BYTES, "Real SkillDraftService would reject this package's size"
    result = {"resource_count": len(files), "total_utf8_bytes": total, "max_utf8_bytes": MAX_DRAFT_BYTES,
        "resource_sha256": {path: hashlib.sha256(value.encode()).hexdigest() for path, value in files.items()}}
    report("draft_storage_limits_checked", resource_count=len(files), total_utf8_bytes=total, max_utf8_bytes=MAX_DRAFT_BYTES)
    return result


def assert_independent_values(actual, expected):
    if isinstance(expected, dict):
        assert isinstance(actual, dict) and actual.keys() == expected.keys()
        for key in expected:
            assert_independent_values(actual[key], expected[key])
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected)
        for left, right in zip(actual, expected):
            assert_independent_values(left, right)
    elif isinstance(expected, float):
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool)
        assert math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12), (actual, expected)
    else:
        assert actual == expected, (actual, expected)


def assert_oracle_isolation(stage, request, *, reuse_baseline=None):
    """Review and oracle repair receive a public contract, never tested code."""
    if stage not in {"synthetic_plan_review", "synthetic_validation_plan"}:
        return
    common = {"stage", "schema", "method_contract", "sources", "script_interfaces", "repair_feedback", "previous_candidate", "reference_evidence"}
    extra = {"validation_plan", "review_targets", "review_scope", "oracle_reuse"} if stage == "synthetic_plan_review" else {
        "previous_validation_plan", "validation_review", "eligible_cases", "required_cases"}
    assert set(request) <= common | extra, "An oracle stage received non-contract data"
    if "eligible_cases" in request:
        from app.domain.services.skills.skill_validation import SyntheticValidationPlan
        from app.domain.services.skills.skill_validation_review import (
            ValidationPlanReview, coverage_revision_cases, eligible_postcondition_cases,
        )
        assert stage == "synthetic_validation_plan"
        eligible = request["eligible_cases"]
        assert all(set(item) == {"script_path", "case_id"} for item in eligible)
        identities = [(item["script_path"], item["case_id"]) for item in eligible]
        assert len(identities) == len(set(identities))
        previous = SyntheticValidationPlan.model_validate(request["previous_validation_plan"])
        assert set(identities) == eligible_postcondition_cases(previous)
        required = request["required_cases"]
        assert all(set(item) == {"script_path", "case_id"} for item in required)
        required_ids = [(item["script_path"], item["case_id"]) for item in required]
        assert len(required_ids) == len(set(required_ids))
        assert set(required_ids) == coverage_revision_cases(previous,
            ValidationPlanReview.model_validate(request["validation_review"]))
        # Required successes may need exact output structure. Eligible cases
        # identify additional ordinary rejections that can gain output_empty.
        assert request["schema"]["title"] in {"VerificationCoverageAmendment", "SchemaFieldRepairs"}, (
            "Coverage amendment must not request a replacement scientific-plan schema")
    for source in request["sources"]:
        assert set(source) <= {"id", "kind", "text"}, "Original code/results reached the independent reviewer"
    for interface in request["script_interfaces"]:
        assert set(interface) <= {"script_path", "arguments", "invocation", "cli_options", "output_mode", "json_outputs",
                                 "input_contract", "output_contract", "validation_cases", "exit_contract"}, "Non-public implementation fields reached the reviewer"
    if "reference_evidence" in request:
        evidence = request["reference_evidence"]
        assert set(evidence) == {"cases", "corrections"}
        for case in evidence["cases"]:
            assert set(case) <= {"script_path", "id", "kind", "status", "reason", "expected_exit_code",
                "actual_exit_code", "checks", "input_unchanged"}
            for check in case.get("checks", []):
                assert set(check) <= {"id", "passed", "expected", "actual", "reason"}
        for change in evidence["corrections"]:
            assert set(change) == {"script_path", "case_id", "assertion_index", "target_type", "field", "before", "after"}
    if stage == "synthetic_plan_review":
        from app.domain.services.skills.skill_validation import SyntheticValidationPlan
        from app.domain.services.skills.skill_validation_review import review_targets, _review_parts
        assert {"validation_plan", "review_targets"} <= set(request)
        plan = SyntheticValidationPlan.model_validate(request["validation_plan"])
        full = review_targets(plan)
        if "oracle_reuse" in request:
            from acceptance.skill_creator_oracle_audit import reuse_targets
            full = reuse_targets(request, reuse_baseline)
        scope = request.get("review_scope")
        if scope is not None:
            assert isinstance(scope, dict) and set(scope) == {"script_path", "case_ids", "target_ids"}
            assert isinstance(scope["script_path"], str)
            assert isinstance(scope["case_ids"], list) and len(scope["case_ids"]) <= 1
            assert isinstance(scope["target_ids"], list)
            assert all(isinstance(item, str) for item in [*scope["case_ids"], *scope["target_ids"]])
            assert len(scope["target_ids"]) == len(set(scope["target_ids"]))
            allowed = _review_parts(plan, full)
            assert allowed is not None and (request["review_targets"], scope) in allowed, "Unknown or incomplete oracle work partition"
            matching = [script for script in full if script["script_path"] == scope["script_path"]]
            assert len(matching) == 1
            script = matching[0]
            selected = []
            for case_id in scope["case_ids"]:
                cases = [case for case in script["cases"] if case["case_id"] == case_id]
                assert len(cases) == 1
                targets = {target["target_id"]: target for target in cases[0]["targets"]}
                assert set(scope["target_ids"]) <= set(targets)
                selected.append({"case_id": case_id, "targets": [targets[key] for key in scope["target_ids"]]})
            if not scope["case_ids"]:
                assert not script["cases"] and not scope["target_ids"]
            assert request["review_targets"] == [{"script_path": scope["script_path"], "cases": selected}]
        else:
            assert _review_parts(plan, full) is None and request["review_targets"] == full, "Unscoped review omitted required targets or cases"
    field_repairs = request.get("repair_feedback", {}).get("field_repairs")
    if field_repairs is not None:
        assert request["schema"]["title"] == "SchemaFieldRepairs"
        assert isinstance(request.get("previous_candidate"), dict)
        document_title = ("VerificationCoverageAmendment" if "eligible_cases" in request else
                          "SyntheticValidationPlan" if stage == "synthetic_validation_plan" else "ValidationPlanReview")
        assert request["repair_feedback"]["document_schema"]["title"] == document_title
        assert isinstance(field_repairs, list) and field_repairs
        for item in field_repairs:
            assert set(item) == {"field_id", "location", "type", "action", "hint"}
        report("oracle_schema_field_repair_checked", stage=stage,
               permitted_field_count=len(field_repairs), implementation_visible=False,
               observed_answers_visible=False)
    report("oracle_isolation_checked", stage=stage, implementation_visible=False, observed_answers_visible=False)


def assert_reference_isolation(stage, request, *, candidate_sources=(), original_sources=(), reference_sources=None,
                               reference_revision_context=None, runtime_repair_context=None):
    """The reference computes from inputs, without candidate answers or code."""
    if stage not in {"synthetic_reference_implementation", "synthetic_reference_review"}:
        return
    common = {"stage", "schema", "response_format", "method_contract", "sources", "script_interfaces",
              "blind_cases", "repair_feedback", "previous_candidate"}
    extra = {"script_request", "reference_revision", "runtime_repair", "script_authoring_capabilities"} if stage == "synthetic_reference_implementation" else {"reference_scripts"}
    assert set(request) <= common | extra, "The blind reference received unexpected material"
    assert_script_capabilities(stage, request)
    assert {"method_contract", "sources", "script_interfaces", "blind_cases"} <= set(request)
    for source in request["sources"]:
        assert set(source) == {"id", "kind", "text"}
        assert source["kind"] in {"request", "method", "goal"}
    for interface in request["script_interfaces"]:
        assert set(interface) <= {"script_path", "arguments", "invocation", "cli_options", "output_mode",
            "json_outputs", "input_contract", "output_contract", "exit_contract"}
        for output in interface.get("json_outputs", []):
            assert set(output) <= {"path", "shape", "filename_argument"}
            assert_public_output_shape(output["shape"])
    for script in request["blind_cases"]:
        assert set(script) == {"script_path", "cases"}
        for case in script["cases"]:
            assert set(case) == {"case_id", "input_files", "argv", "requirement_ids", "output_targets"}
            for target in case["output_targets"]:
                if target["type"] in {"output_empty", "stderr_contains"}:
                    assert set(target) == {"type"}
                elif target["type"] == "csv_rows":
                    assert set(target) == {"type", "path", "match_fields", "field_names"}
                elif target["type"] == "csv_header":
                    assert set(target) == {"type", "path"}
                else:
                    assert target["type"] in {"json_value", "json_array_length", "json_object_keys"}
                    assert set(target) == {"type", "path", "pointer"}
    safe_copy = dict(request)
    if stage == "synthetic_reference_implementation":
        assert set(request["script_request"]) == {"path", "purpose", "invocation"}
        if "runtime_repair" in request:
            assert "reference_revision" not in request
            assert runtime_repair_context is not None, "Runtime repair requires exact preceding host execution provenance"
            assert request["runtime_repair"] == runtime_repair_context
            repair = request["runtime_repair"]
            assert set(repair) == {"reference_scripts", "failures"} and repair["failures"]
            for failure in repair["failures"]:
                assert set(failure) == {"script_path", "case_id", "exception_type"}
                assert failure["exception_type"] in {"TypeError", "NameError", "UnboundLocalError", "AttributeError"}
        if "reference_revision" in request:
            from app.domain.services.skills.method_contract import CoverageError, MethodContract, MethodReview, validate_review
            revision = request["reference_revision"]
            assert set(revision) == {"reference_scripts", "review"}
            assert reference_revision_context is not None, "Reference revision requires prior generated-review provenance"
            assert revision == reference_revision_context, "Revision must preserve the exact prior reference and its review"
            review = MethodReview.model_validate(revision["review"])
            try:
                validate_review(review, MethodContract.model_validate(request["method_contract"]), request["sources"])
            except CoverageError as error:
                assert error.code == "method_review_rejected", "Reference review scope must remain complete and valid"
            else:
                raise AssertionError("A reference revision needs a concrete adverse review")
    else:
        assert reference_sources is not None, "The reference reviewer needs generated-source provenance"
        reviewed = {}
        for script in request["reference_scripts"]:
            assert set(script) == {"path", "purpose", "invocation", "content"}
            assert script["path"] not in reviewed
            reviewed[script["path"]] = script["content"]
        assert reviewed == reference_sources, "The reviewer received something other than its own reference sources"
        # Own reference code is allowed for method review; original/candidate
        # code remains forbidden in every other context field.
        safe_copy.pop("reference_scripts")
    wire = json.dumps(safe_copy, ensure_ascii=False)
    assert all(json.dumps(source, ensure_ascii=False)[1:-1] not in wire
               for source in [*candidate_sources, *original_sources] if source)
    report("reference_isolation_checked", stage=stage, candidate_implementation_visible=False,
           original_implementation_visible=False, candidate_expectations_visible=False,
           observed_candidate_answers_visible=False)


def assert_public_output_shape(shape):
    """Public shape may reveal proven column names, never candidate values."""
    kind = shape["type"]
    if kind == "unknown":
        assert set(shape) == {"type"}
    elif kind == "object":
        assert set(shape) <= {"type", "properties", "additional_keys"}
        assert isinstance(shape["properties"], dict)
        for child in shape["properties"].values():
            assert_public_output_shape(child)
    elif kind == "array":
        assert set(shape) <= {"type", "items", "column_name_prefix"}
        assert_public_output_shape(shape["items"])
        if "column_name_prefix" in shape:
            assert isinstance(shape["column_name_prefix"], list) and shape["column_name_prefix"]
            assert all(isinstance(name, str) and name for name in shape["column_name_prefix"])
    else:
        assert kind == "tuple" and set(shape) == {"type", "items"}
        assert isinstance(shape["items"], list)
        for child in shape["items"]:
            assert_public_output_shape(child)


def assert_reference_provenance(plan, receipt, reference_sources, *, interfaces=None, execution_records=()):
    """Reconstruct the pre-correction plan; only declared expected values move."""
    import copy
    from app.domain.services.skills.skill_validation import SyntheticValidationPlan
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    digest = lambda value: hashlib.sha256(canonical(value).encode()).hexdigest()
    frozen = SyntheticValidationPlan.model_validate(plan).model_dump()
    assert receipt["status"] == "verified" and receipt["scope"] == "synthetic_reference"
    assert receipt["corrected_plan_sha256"] == digest(frozen)
    assert receipt["reference_source_sha256"] == {
        path: hashlib.sha256(source.encode()).hexdigest() for path, source in reference_sources.items()}
    execution = receipt["execution"]
    assert execution["status"] == "passed" and execution["scope"] == "synthetic"
    assert execution["plan_sha256"] == receipt["corrected_plan_sha256"]
    assert execution["source_sha256"] == receipt["reference_source_sha256"]
    expected_cases = {(script["script_path"], case["id"]) for script in frozen["scripts"] for case in script["cases"]}
    actual_cases = [(case["script_path"], case["id"]) for case in execution["cases"]]
    assert len(actual_cases) == len(set(actual_cases)) and set(actual_cases) == expected_cases
    assert all(case["status"] == "passed" and case["input_unchanged"] is True and
               all(check["passed"] for check in case["checks"]) for case in execution["cases"])
    if "postcondition_extension" in receipt:
        from app.domain.services.skills.skill_validation_extension import (
            validate_coverage_extension, validate_reference_coverage_confirmation,
        )
        extension = receipt["postcondition_extension"]
        transition_keys = {"previous_plan_sha256", "proposed_plan_sha256", "confirmed_plan_sha256",
                           "initial_execution", "execution", "corrections", "original_explanations"}
        assert set(extension) == {"base_plan_sha256", "base_reference_verification", "previous_confirmations", *transition_keys}
        base_receipt = extension["base_reference_verification"]
        assert "postcondition_extension" not in base_receipt
        assert extension["base_plan_sha256"] == base_receipt["corrected_plan_sha256"]

        def captured_plan(plan_sha, observed=None):
            matches = {canonical(record["plan"]): record["plan"] for record in execution_records
                if digest(record["plan"]) == plan_sha and record["scripts"] == reference_sources
                and (observed is None or canonical(record["execution"]) == canonical(observed))}
            assert len(matches) == 1, "Reference extension needs the captured original plan and actual same-source execution"
            value = next(iter(matches.values()))
            if observed is not None:
                assert observed["plan_sha256"] == plan_sha
                assert observed["source_sha256"] == receipt["reference_source_sha256"]
            return value

        base_plan = captured_plan(extension["base_plan_sha256"], base_receipt["execution"])
        assert_reference_provenance(base_plan, base_receipt, reference_sources,
                                   interfaces=interfaces, execution_records=execution_records)
        previous = base_plan
        cumulative_corrections = copy.deepcopy(base_receipt["corrections"])
        cumulative_explanations = copy.deepcopy(base_receipt["original_explanations"])
        transitions = [*extension["previous_confirmations"], {key: extension[key] for key in transition_keys}]
        for transition in transitions:
            assert set(transition) == transition_keys
            assert transition["previous_plan_sha256"] == digest(previous)
            proposed = captured_plan(transition["proposed_plan_sha256"], transition["initial_execution"])
            confirmed_plan = captured_plan(transition["confirmed_plan_sha256"], transition["execution"])
            validate_reference_coverage_confirmation(SyntheticValidationPlan.model_validate(previous),
                SyntheticValidationPlan.model_validate(proposed), SyntheticValidationPlan.model_validate(confirmed_plan))
            # Reuse the full inverse-correction audit for this transition. It
            # checks exact before/after values, changed-case explanations and
            # both real execution/public-output receipts without another model.
            per_transition = {"status": "verified", "scope": "synthetic_reference",
                "original_plan_sha256": transition["proposed_plan_sha256"],
                "corrected_plan_sha256": transition["confirmed_plan_sha256"],
                "reference_source_sha256": receipt["reference_source_sha256"],
                **{key: transition[key] for key in ("initial_execution", "execution", "corrections", "original_explanations")}}
            assert_reference_provenance(confirmed_plan, per_transition, reference_sources,
                                       interfaces=interfaces, execution_records=execution_records)
            cumulative_corrections.extend(transition["corrections"])
            cumulative_explanations.extend(transition["original_explanations"])
            previous = confirmed_plan
        assert canonical(previous) == canonical(frozen)
        assert canonical(receipt["corrections"]) == canonical(cumulative_corrections)
        assert canonical(receipt["original_explanations"]) == canonical(cumulative_explanations)
        validate_coverage_extension(SyntheticValidationPlan.model_validate(base_plan),
                                   SyntheticValidationPlan.model_validate(frozen))
        changed = {"corrected_plan_sha256", "initial_execution", "execution", "postcondition_extension",
                   "corrections", "original_explanations"}
        assert {key: value for key, value in receipt.items() if key not in changed} == {
            key: value for key, value in base_receipt.items() if key not in changed}
        assert receipt["initial_execution"] == extension["initial_execution"]
        captured_plan(receipt["corrected_plan_sha256"], execution)
        if interfaces is not None:
            from app.domain.services.skills.skill_public_output_contract import (
                compile_public_output_contract, validate_public_contract_receipt,
            )
            contract = compile_public_output_contract(interfaces)
            if contract.rules:
                validate_public_contract_receipt(execution, contract, reference_sources,
                                                 SyntheticValidationPlan.model_validate(frozen))
        report("reference_postcondition_extension_checked", same_reference_source=True,
               original_scientific_plan_unchanged=True, actual_reexecution=True,
               confirmations=len(transitions), new_numeric_corrections=len(cumulative_corrections) - len(base_receipt["corrections"]))
        return
    before = copy.deepcopy(frozen)
    seen = set()
    for change in receipt["corrections"]:
        assert set(change) == {"script_path", "case_id", "assertion_index", "target_type", "field", "before", "after"}
        identity = tuple(change[key] for key in ("script_path", "case_id", "assertion_index", "target_type", "field"))
        assert identity not in seen
        seen.add(identity)
        script = next(item for item in before["scripts"] if item["script_path"] == change["script_path"])
        case = next(item for item in script["cases"] if item["id"] == change["case_id"])
        assertion = case["assertions"][change["assertion_index"]]
        field = change["field"]
        if assertion["type"] == "json_value":
            assert field == "value" and change["target_type"] == "json_value"
            target, key = assertion["expected"], "value"
        elif assertion["type"] == "json_array_length":
            assert field == "length" and change["target_type"] == "json_array_length"
            target, key = assertion, field
        elif change["target_type"] == "csv_row_count":
            assert assertion["type"] == "csv_rows" and field == "count"
            target, key = assertion, field
        else:
            assert assertion["type"] == "csv_rows" and change["target_type"] == "csv_field"
            target, key = assertion["fields"][field], "value"
        assert canonical(target[key]) == canonical(change["after"])
        target[key] = change["before"]
    changed_cases = {(item["script_path"], item["case_id"]) for item in receipt["corrections"]}
    explanation_cases = set()
    for original in receipt["original_explanations"]:
        assert set(original) == {"script_path", "case_id", "oracle_explanation"}
        identity = (original["script_path"], original["case_id"])
        assert identity in changed_cases and identity not in explanation_cases
        explanation_cases.add(identity)
        script = next(item for item in before["scripts"] if item["script_path"] == identity[0])
        case = next(item for item in script["cases"] if item["id"] == identity[1])
        case["oracle_explanation"] = original["oracle_explanation"]
    assert explanation_cases == changed_cases
    assert digest(before) == receipt["original_plan_sha256"], "Reference correction changed inputs, targets or tolerances"
    assert receipt["initial_execution"]["plan_sha256"] == receipt["original_plan_sha256"]
    assert receipt["initial_execution"]["source_sha256"] == receipt["reference_source_sha256"]
    if "runtime_build_recovery" in receipt:
        recovery = receipt["runtime_build_recovery"]
        assert set(recovery) == {"failures", "failed_execution"}
        matched = [record for record in execution_records if record["execution"] == recovery["failed_execution"]]
        assert len(matched) == 1, "Recovery receipt needs a captured real execution"
        record = matched[0]
        assert record["plan"] == before, "Build repair cannot change the frozen original plan"
        assert runtime_context([], record)["failures"] == recovery["failures"]
        assert record["execution"]["source_sha256"] != receipt["reference_source_sha256"]
    if interfaces is not None:
        from app.domain.services.skills.skill_public_output_contract import (
            compile_public_output_contract, validate_public_contract_receipt,
        )
        # Recompile from the original host interface. A self-reported contract
        # or a reference's newly inferred schema cannot replace this binding.
        contract = compile_public_output_contract(interfaces)
        if contract.rules:
            validate_public_contract_receipt(receipt["initial_execution"], contract, reference_sources,
                                             SyntheticValidationPlan.model_validate(before))
            validate_public_contract_receipt(execution, contract, reference_sources,
                                             SyntheticValidationPlan.model_validate(frozen))
            report("reference_public_output_checked", rules=len(contract.rules),
                   contract_sha256=execution["public_output_verification"]["contract_sha256"],
                   original_plan_unchanged=True, same_execution_receipt=True)
    report("reference_provenance_checked", reference_scripts=len(reference_sources),
           reference_cases=len(execution["cases"]), corrections=len(receipt["corrections"]),
           inputs_targets_tolerances_unchanged=True, reference_source_frozen=True)


def runtime_context(reference_scripts, record):
    """Project only typed crash categories from a captured real host receipt."""
    from app.domain.services.skills.skill_validation import SyntheticValidationPlan
    plan = SyntheticValidationPlan.model_validate(record["plan"])
    execution = record["execution"]
    assert execution["scope"] == "synthetic" and execution["status"] == "failed"
    assert execution["plan_sha256"] == hashlib.sha256(json.dumps(
        plan.model_dump(), ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    assert execution["source_sha256"] == {path: hashlib.sha256(code.encode()).hexdigest()
                                           for path, code in record["scripts"].items()}
    if reference_scripts:
        assert {script["path"]: script["content"] for script in reference_scripts} == record["scripts"]
    expected = {(script.script_path, case.id): case for script in plan.scripts for case in script.cases
                if script.support == "supported"}
    cases = execution["cases"]
    assert len(cases) == len(expected) and {(item["script_path"], item["id"]) for item in cases} == set(expected)
    failures = []
    for item in cases:
        case = expected[(item["script_path"], item["id"])]
        assert item["input_unchanged"] is True and type(item["actual_exit_code"]) is int
        assert item["expected_exit_code"] == case.expected_exit_code
        if item["status"] == "passed":
            assert all(check["passed"] is True for check in item.get("checks", []))
            assert (0 < item["actual_exit_code"] < 124 if case.expected_exit_code == "nonzero"
                    else item["actual_exit_code"] == case.expected_exit_code)
            continue
        assert item["status"] == "failed" and case.expected_exit_code == 0
        assert item["reason"] == "program_runtime_failure" and item.get("checks", []) == []
        assert 0 < item["actual_exit_code"] < 124
        assert item["runtime_error_type"] in {"TypeError", "NameError", "UnboundLocalError", "AttributeError"}
        failures.append({"script_path": item["script_path"], "case_id": item["id"], "exception_type": item["runtime_error_type"]})
    assert failures
    failed_ids = {(item["script_path"], item["case_id"]) for item in failures}
    for case in execution.get("public_output_verification", {}).get("cases", []):
        for check in case["checks"]:
            if (case["script_path"], case["id"]) in failed_ids:
                assert check["status"] == "failed" and check["observations"] == 0
                assert check["reason"] in {"public_execution_failed", "public_execution_unconfirmed"}
            else:
                assert check["status"] in {"passed", "not_observed"}
    return {"reference_scripts": reference_scripts, "failures": failures}


def assert_workflow_authoring_boundary(stage, request):
    if stage != "workflow":
        return
    schema = request["schema"]
    assert schema["title"] == "WorkflowDraft"
    assert "coverage" not in schema["properties"], "The model must not author numeric coverage pointers"
    assert "scripts" not in schema["properties"], "Workflow authoring plans scripts without generating bodies"
    definitions = schema.get("$defs", {})
    assert "AuthoredScript" not in definitions and "HelperScript" not in definitions
    assert "content" not in definitions["ScriptRequest"]["properties"]
    assert "requirement_ids" in definitions["ScriptRequest"]["properties"]
    for section in ("parameters", "steps", "outputs", "validation_cases",
                    "data_requirements", "dependencies", "limitations"):
        item_schema = schema["properties"][section]["items"]
        if "$ref" in item_schema:
            item_schema = definitions[item_schema["$ref"].rsplit("/", 1)[1]]
        assert "requirement_ids" in item_schema["properties"], section
    report("workflow_authoring_boundary_checked", model_numeric_coverage_pointers=False,
           inline_requirement_ids=True, storage_coverage_compiled_by_host=True,
           script_bodies_in_workflow=False, script_plan_location="implementation.scripts")


def assert_script_capabilities(stage, request):
    from app.domain.services.skills.skill_creation.models import HelperScript
    from app.domain.services.skills.skill_creation.script_authoring import (
        ScriptFunctionUnit, ScriptModulePlan, ScriptRepairSelection,
    )
    from app.domain.services.skills.skill_creation.source_response import source_authoring_capabilities
    schema = None
    if request.get("response_format", {}).get("type") == "python_source":
        schema = {"script": HelperScript, "function": ScriptFunctionUnit}.get(request["response_format"].get("artifact"))
        assert schema is not None, "Unknown Python source artifact type"
    elif isinstance(request.get("schema"), dict):
        for candidate in (ScriptModulePlan, ScriptRepairSelection):
            if request["schema"].get("title") == candidate.model_json_schema()["title"]:
                assert request["schema"] == candidate.model_json_schema(), "Authoring schema differs from its host type"
                schema = candidate
    expected = source_authoring_capabilities(schema, stage)
    if expected is None:
        assert "script_authoring_capabilities" not in request, "Capabilities cannot open unrelated stages"
    else:
        assert request.get("script_authoring_capabilities") == expected, "Capabilities must equal the exact host public policy"


def assert_source_authoring_boundary(stage, request):
    assert_script_capabilities(stage, request)
    if request.get("response_format", {}).get("type") != "python_source":
        return
    assert stage in {"implementation", "synthetic_script_repair", "synthetic_reference_implementation"}
    assert "schema" not in request, "Source must not be embedded in a JSON schema response"
    artifact_format = request["response_format"]
    assert artifact_format["complete_source_required"] is True
    assert artifact_format["host_metadata"]
    report("source_authoring_boundary_checked", stage=stage,
           source_as_python=True, host_metadata_frozen=True)


def assert_compiled_coverage(values):
    references = values["references"]
    inputs = json.loads(references["references/input-contract.json"])
    workflow = json.loads(references["references/workflow.json"])
    validation = json.loads(references["references/validation.json"])
    contract = json.loads(references["references/method-contract.json"])
    document = {**inputs, **workflow, "outputs": validation["outputs"],
                "validation_cases": validation["cases"]}
    document["scripts"] = [{**script, "content": values["scripts"][script["path"]]}
                           for script in workflow["scripts"]]
    covered = {entry["requirement_id"] for entry in contract["coverage"]}
    assert covered == {entry["id"] for entry in contract["requirements"]}
    pointer_count = 0
    for entry in contract["coverage"]:
        assert entry["locations"]
        for pointer in entry["locations"]:
            current = document
            for token in pointer.lstrip("/").split("/"):
                current = current[int(token)] if isinstance(current, list) else current[token]
            assert current is not None
            pointer_count += 1
    report("compiled_coverage_checked", requirements=len(covered), resolved_locations=pointer_count,
           unresolved_locations=0)


async def independently_verify_paired(values):
    """Fixed hand values run only after every production gate has accepted.

    The accepted public CLI chooses the filenames, never the input/expected
    values. No generated implementation is inspected to derive these oracles.
    These fixtures and expected values are not sent to the authoring model.
    """
    from copy import deepcopy
    from app.domain.services.skills.skill_validation import SyntheticValidationPlan
    from app.infrastructure.external.sandbox.skill_validation_worker import validate_skill_scripts

    frozen = json.loads(values["references"]["references/synthetic-validation.json"])
    assert len(frozen["scripts"]) == 1, "Paired acceptance expects one reusable entry point"
    script = frozen["scripts"][0]
    original = next(case for case in script["cases"] if case["kind"] == "normal")
    assert len(original["input_files"]) == 1 and original["input_files"][0]["format"] == "json"
    output_paths = {item["path"] for item in original["assertions"] if item["type"] == "json_value"}
    assert len(output_paths) == 1
    output_path = next(iter(output_paths))
    scenario = SCENARIOS["paired"]
    zero_input = {"pairs": [{"pair_id": "z1", "before": 0, "after": 0},
                             {"pair_id": "z2", "before": 2, "after": 2}]}
    zero_expected = {"n": 2, "excluded": 0, "mean_change": 0, "sample_sd": 0,
                     "standard_error": 0, "p_value": 1, "permutations": 4,
                     "direction": "after-minus-before"}
    mapped_input = {"fields": {"id": "unit", "before": "pre", "after": "post"},
                    "pairs": [{"unit": "m1", "pre": 0, "post": 1},
                              {"unit": "m2", "pre": 0, "post": 3}]}
    mapped_expected = {"n": 2, "excluded": 0, "mean_change": 2, "sample_sd": math.sqrt(2),
                       "standard_error": 1, "p_value": .5, "permutations": 4,
                       "direction": "after-minus-before"}
    fixtures = [("fixed-normal", "normal", scenario.normal, scenario.observed),
                ("fixed-mixed", "normal", PAIRED_MIXED_SIGN_INPUT, PAIRED_MIXED_SIGN_EXPECTED),
                ("fixed-zero", "boundary", zero_input, zero_expected),
                ("fixed-mapped", "normal", mapped_input, mapped_expected),
                ("fixed-duplicate", "invalid", {"pairs": [zero_input["pairs"][0]] * 2}, None),
                ("fixed-too-few", "invalid", {"pairs": zero_input["pairs"][:1]}, None),
                ("fixed-too-many", "invalid", {"pairs": [{"pair_id": str(i), "before": 0, "after": 1}
                                                          for i in range(13)]}, None),
                ("fixed-bool", "invalid", {"pairs": [{"pair_id": "a", "before": False, "after": 1},
                                                     {"pair_id": "b", "before": 0, "after": 1}]}, None)]
    cases = []
    for identifier, kind, data, expected in fixtures:
        case = deepcopy(original)
        case.update(id=identifier, kind=kind, description="Fixed independently authored paired-method acceptance case.",
                    oracle_explanation="Exact hand-derived paired differences and exhaustive sign combinations, authored before model generation.",
                    expected_exit_code=0 if expected is not None else "nonzero")
        case["input_files"][0]["data"] = data
        case["assertions"] = [{"type": "json_value", "path": output_path, "pointer": "/" + key,
                               "expected": {"value": value, "atol": 1e-12 if isinstance(value, float) else 0,
                                            "rtol": 1e-12 if isinstance(value, float) else 0}}
                              for key, value in (expected or {}).items()]
        cases.append(case)
    plan = SyntheticValidationPlan.model_validate({"scripts": [{**script, "cases": cases}]})
    execution = await validate_skill_scripts(values["scripts"], plan)
    artifact("hand_validation", "final", {"plan": plan.model_dump(), "execution": execution})
    assert execution["status"] == "passed", execution
    report("independent_hand_verification", scenario="paired", cases=len(cases),
           assertions=sum(len(case["assertions"]) for case in cases),
           passed=sum(case["status"] == "passed" for case in execution["cases"]),
           shown_to_model=False, production_isolated_worker=True)


def complex_hand_cases(script):
    """Retain the original 184 checks and the separate 30 zero-variance checks."""
    from copy import deepcopy
    from acceptance.check_skill_creator_hand_oracle import json_assertions
    normal = next(case for case in script["cases"] if case["kind"] == "normal")
    # These are the original fixed task's declared defaults. A generated
    # production case may test filename overrides; it cannot alter this
    # independent fixture's paths or compensate its arguments.
    json_path, csv_path, output = "report.json", "wide.csv", "{output}"
    hand = deepcopy(normal)
    hand.update(id="host-original-fixed-hand", kind="normal",
        description="Original fixed hand-calculation fixture, never shown to the model.",
        oracle_explanation="Pre-authored independent scalar and CSV answers, unchanged from the original acceptance fixture.",
        argv=["--input", "{input:fixed-hand.json}", "--output", output], expected_exit_code=0,
        input_files=[{"path": "fixed-hand.json", "format": "json", "data": deepcopy(SCENARIOS["complex"].normal)}],
        assertions=json_assertions(json_path, SCENARIOS["complex"].observed))
    rows = SCENARIOS["complex"].observed["pivot"]["rows"]
    hand["assertions"].append({"type": "csv_rows", "path": csv_path, "match": {}, "count": len(rows), "fields": {}})
    hand["assertions"].extend({"type": "csv_rows", "path": csv_path, "match": {"id": row["id"]}, "count": 1,
        "fields": {key: {"value": value} for key, value in row.items()}} for row in rows)
    # Independent fixed diagnostic: two zero changes in each group. The sample
    # variances, SE and pooled SD are zero; J=1-3/(4*(2+2-2)-1)=4/7. Undefined
    # ratios stay null; all six label splits and four sign flips are extreme.
    zero_input = {"observations": [], "pairs": [
        {"id": "a1", "group": "A", "before": 5, "after": 5},
        {"id": "a2", "group": "A", "before": 7, "after": 7},
        {"id": "b1", "group": "B", "before": 10, "after": 10},
        {"id": "b2", "group": "B", "before": 12, "after": 12}],
        "associations": [{"id": "c1", "x": 0, "y": 0}, {"id": "c2", "x": 1, "y": 1}],
        "hypotheses": [], "long_rows": []}
    comparison = {"n_a": 2, "n_b": 2, "mean_a": 0, "mean_b": 0, "difference_b_minus_a": 0,
        "permutations": 6, "p_value": 1, "standard_error": 0, "welch_t": None, "welch_df": None,
        "pooled_sd": 0, "cohen_d": None, "hedges_correction": 4 / 7, "hedges_g": None}
    assertions = json_assertions(json_path, {"comparison": comparison})
    for index, group in enumerate(("A", "B")):
        assertions.extend(json_assertions(json_path, {"group": group, "n": 2, "sum": 0, "mean": 0,
            "sample_sd": 0, "standard_error": 0, "permutations": 4, "p_value": 1}, "/paired/" + str(index)))
    zero = deepcopy(hand)
    zero.update(id="host-new-zero-variance-formula-check", kind="boundary",
        description="Separate independent zero-variance diagnostic; original fixed fixture is unchanged.",
        oracle_explanation="For two samples of two zero changes, sample variances=0, SE=0, pooled_sd=0, J=1-3/7=4/7; ratios with zero denominator are null; all sign/label permutations are extreme and p=1.",
        argv=["--input", "{input:zero-variance.json}", "--output", output],
        input_files=[{"path": "zero-variance.json", "format": "json", "data": zero_input}], assertions=assertions)
    null_units = deepcopy(hand)
    null_units.update(id="host-explicit-null-units-rejected", kind="invalid",
        description="Independent input-contract rejection of an explicitly null unit mapping.",
        oracle_explanation="The original contract requires units, when present, to be a nonempty object; an explicit null must be rejected without changing input.",
        argv=["--input", "{input:null-units.json}", "--output", output],
        input_files=[{"path": "null-units.json", "format": "json",
                      "data": {**deepcopy(SCENARIOS["complex"].normal), "units": None}}],
        expected_exit_code="nonzero", input_unchanged=True, assertions=[])
    return [hand, zero, null_units]


async def independently_verify_complex(values, *, reference_sources=None):
    from app.domain.services.skills.skill_validation import SyntheticValidationPlan
    from app.infrastructure.external.sandbox.skill_validation_worker import validate_skill_scripts
    frozen = json.loads(values["references"]["references/synthetic-validation.json"])
    assert len(frozen["scripts"]) == 1
    script = frozen["scripts"][0]
    plan = SyntheticValidationPlan.model_validate({"scripts": [{**script,
        "cases": [*script["cases"], *complex_hand_cases(script)]}]})
    role = "reference" if reference_sources is not None else "candidate"
    programs = values["scripts"] if reference_sources is None else reference_sources
    assert set(programs) == set(values["scripts"]), "The independent reference must preserve declared script identities"
    execution = await validate_skill_scripts(programs, plan)
    artifact(role + "_hand_validation", "final", {"plan": plan.model_dump(), "execution": execution})
    expected_checks = {"host-original-fixed-hand": 184, "host-new-zero-variance-formula-check": 30,
                       "host-explicit-null-units-rejected": 0}
    hand_receipts = [case for case in execution["cases"] if case["id"] in expected_checks]
    assert {case["id"] for case in hand_receipts} == set(expected_checks)
    for case in hand_receipts:
        checks = case.get("checks", [])
        report("independent_hand_verification", scenario="complex", program_role=role, case=case["id"],
               checks=len(checks), passed_checks=sum(check["passed"] for check in checks),
               status=case["status"], shown_to_model=False, production_isolated_worker=True)
        assert len(checks) == expected_checks[case["id"]], "The fixed hand checks must not be dropped"
    assert execution["status"] == "passed", execution
    assert all(case["input_unchanged"] for case in execution["cases"])


def assert_complex_coverage_extensions(plan):
    """Known original output contracts must become actual production checks.

    This is a host acceptance condition, not a model prompt or a replacement
    expected result. It does not alter the independent 184 + 30 fixtures.
    """
    required_keys = set(SCENARIOS["complex"].observed)
    # These are the original two cases whose missing observable structure
    # checks produced the retained negative review. Later focused probes need
    # not repeat the full report contract: their additional targets are checked
    # by the real production plan and independent oracle review.
    required_cases = {"normal_full_methods", "boundary_edges_and_overrides"}
    observed_cases = set()
    empty_pivot_cases = 0
    for script in plan.scripts:
        for case in script.cases:
            if case.id not in required_cases:
                continue
            assert case.id not in observed_cases, "An original complex acceptance case was duplicated"
            observed_cases.add(case.id)
            assert case.expected_exit_code == 0, "An original successful case cannot become a rejection"
            json_paths = {item.path for item in case.assertions if item.type == "json_value"}
            assert json_paths, "Complex success cases must observe their scientific report"
            for path in json_paths:
                assert any(item.type == "json_object_keys" and item.path == path and item.pointer == "" and
                           set(item.keys) == required_keys for item in case.assertions), (
                               "The original exact report-key contract still has no executable assertion")
            empty_long = any(item.format == "json" and isinstance(item.data, dict) and item.data.get("long_rows") == []
                             for item in case.input_files)
            if case.id == "boundary_edges_and_overrides":
                assert empty_long, "The original empty-pivot boundary input disappeared"
                empty_pivot_cases += 1
                paths = {item.path for item in case.assertions if item.type == "csv_rows"}
                assert paths, "The empty exported table must remain observable"
                for path in paths:
                    assert any(item.type == "csv_header" and item.path == path and item.columns == ["id"]
                               for item in case.assertions), "The original empty-table header contract is still untested"
    assert observed_cases == required_cases, "The original complex structure cases disappeared"
    assert empty_pivot_cases == 1, "The original empty-pivot boundary case disappeared"


async def main(name, resume_prefix=None, replay_only=False, checkpoint_state=None, prefix_until_change=False):
    assert not replay_only or resume_prefix or checkpoint_state, "Replay-only acceptance requires saved responses or private checkpoint state"
    scenario = SCENARIOS[name]
    observed = scenario.reference(scenario.normal)
    assert_independent_values(observed, scenario.observed)
    source = inspect.getsource(scenario.reference)
    digest = hashlib.sha256(source.encode()).hexdigest()
    record = {"schema": "program-method-evidence/v1", "source_digest": digest,
              "source_snapshot": {"version": 1, "encoding": "utf-8", "size_bytes": len(source.encode()), "sha256": digest, "content": source},
              "operation_id": "synthetic-acceptance", "script_path": "/tmp/artificial-reference.py", "event_seq": 2}
    step = Step(id="artificial-step", description=scenario.request, status=ExecutionStatus.COMPLETED, success=True,
                result="已执行人工参考计算并与手算小样例独立核对；输入保持不变，方法及输出契约已完成。")
    private_markers = ["artificial-research-batch-37.json", "artificial-private-file-37", "artificial-secret-canary-37"]
    events = [MessageEvent(role="user", message="已选择附件 artificial-research-batch-37.json。" + scenario.request,
                          attachments=[FileInfo(file_id=private_markers[1], filename=private_markers[0],
                                                file_path="/workspace/artificial-private/" + private_markers[0])], seq=1),
              ToolEvent(tool_call_id="artificial-call", tool_name="program", function_name="program_run",
                        function_args={"file_id": private_markers[1], "token": private_markers[2]},
                        function_result=ToolResult(success=True, data={}), status=ToolStatus.CALLED, seq=2),
              StepEvent(step=step, status=StepStatus.COMPLETED, seq=3),
              PlanEvent(plan=Plan(title=scenario.title, goal=scenario.request, steps=[step], status=ExecutionStatus.COMPLETED),
                        status=PlanStatus.COMPLETED, seq=4),
              MessageEvent(role="assistant", message="人工参考方法已运行完成，独立手算小样例的结果与参考计算一致。完整方法和输出见已完成步骤。", seq=5),
              DoneEvent(seq=6)]
    artifact("artificial_event_identity", "source_analysis", stabilize_artificial_event_identity(events, name))

    class ArtificialRepository:
        async def find_by_id_and_user_id(self, session_id, user_id):
            return SimpleNamespace(user_id=user_id)

        async def get_events(self, session_id):
            return events

        async def get_successful_program_sources(self, session_id, user_id, sequences):
            assert sequences == [2]
            return [record]

    class CaptureDraft:
        async def create(self, **values):
            execution = values["synthetic_execution"]
            artifact("package", "final", values)
            artifact("package_size", "final", assert_draft_package_limits(values))
            report("draft_generated", scenario=name, scripts=len(values["scripts"]), status=execution["status"],
                   cases=len(execution.get("cases", [])), passed=sum(c.get("status") == "passed" for c in execution.get("cases", [])),
                   persisted=False, installed=False)
            assert values["scripts"], "Scenario has reusable computation; a method-only result is not acceptance"
            portable = json.dumps({key: values[key] for key in ("content", "references", "scripts")}, ensure_ascii=False)
            assert not any(marker in portable for marker in private_markers), "Original artificial identifiers escaped into portable skill"
            assert execution["status"] == "passed", "All selected scenarios support real CSV/JSON worker validation"
            assert len(execution["cases"]) >= 3
            reference = execution["reference_verification"]
            assert reference["status"] == "verified"
            assert reference["corrected_plan_sha256"] == execution["plan_sha256"]
            assert_reference_provenance(json.loads(values["references"]["references/synthetic-validation.json"]),
                                        reference, reference_programs, interfaces=reference_interfaces,
                                        execution_records=reference_execution_records)
            from app.domain.services.skills.skill_validation import SyntheticValidationPlan
            oracle_provenance = oracle_audit.assert_receipt(SyntheticValidationPlan.model_validate_json(
                values["references"]["references/synthetic-validation.json"]), execution["oracle_review"])
            if oracle_provenance is not None:
                artifact("oracle_reuse_provenance", "synthetic_plan_review", oracle_provenance)
                report("oracle_reuse_provenance_checked", old_targets=len(oracle_provenance["reused_targets"]),
                       new_targets=oracle_provenance["new_review_target_count"],
                       original_review_preserved=True, all_case_metadata_reviewed=True)
            assert_compiled_coverage(values)
            if name == "paired":
                await independently_verify_paired(values)
            elif name == "complex":
                from app.domain.services.skills.skill_validation import SyntheticValidationPlan
                assert_complex_coverage_extensions(SyntheticValidationPlan.model_validate_json(
                    values["references"]["references/synthetic-validation.json"]))
                await independently_verify_complex(values)
                await independently_verify_complex(values, reference_sources=reference_programs)
            return SimpleNamespace(**values)

    class NoDatabaseUsageRecorder:
        async def record_from_message(self, *args, **kwargs):
            pass

    dependencies = CreationDependencies.production()
    current = {"stage": "none", "calls": 0}
    physical_records = []
    wire_records = []
    replay = json.loads(Path(resume_prefix).read_text()) if resume_prefix else []
    replayed = []
    candidate_programs, reference_programs = {}, {}
    verified_references = []
    active_validators = []
    reference_scope = None
    reference_interfaces = None
    reference_review_scripts = None
    reference_assessment = None
    reference_execution_records = []
    oracle_context = None
    from acceptance.skill_creator_oracle_audit import OracleReviewAudit
    oracle_audit = OracleReviewAudit()
    from acceptance.skill_creator_checkpoint_state import ArtificialCheckpointCache
    cache = (ArtificialCheckpointCache(name, json.loads(Path(checkpoint_state).read_text()),
             persist=lambda value: artifact("checkpoint_state", "private_artificial_cache", value))
             if checkpoint_state else None)

    def observe_validated(value, stage):
        nonlocal reference_assessment
        from app.domain.services.skills.skill_creation.models import HelperScript
        from app.domain.services.skills.method_contract import MethodReview
        if isinstance(value, HelperScript):
            if stage == "synthetic_reference_implementation":
                reference_programs[value.path] = value.content
            elif stage in {"implementation", "synthetic_script_repair"}:
                candidate_programs[value.path] = value.content
        if stage == "synthetic_reference_review" and isinstance(value, MethodReview):
            reference_assessment = value.model_dump(mode="json")

    def checkpoint_factory(**kwargs):
        from unittest.mock import patch
        # Use the complete production scope and BuildCheckpoints. Only the
        # isolated cache transport is substituted; no user Redis is touched.
        with patch("app.infrastructure.external.cache.get_cache", lambda: cache):
            checkpoints = dependencies.checkpoint_factory(**kwargs)
        artifact("checkpoint_scope", "private_artificial_cache", {
            "source_identity_sha256": checkpoints.scope.identity,
            "source_configuration_code_sha256": checkpoints.scope.artifacts})
        original_run = checkpoints.run
        original_revision = checkpoints.get_reference_revision
        original_oracle_revision = checkpoints.get_oracle_revision

        async def run(schema, stage, instruction, payload, check, produce, **options):
            nonlocal reference_review_scripts
            if stage == "synthetic_reference_review":
                reference_review_scripts = json.loads(json.dumps(payload["reference_scripts"]))
            produced = False
            async def traced_produce():
                nonlocal produced
                produced = True
                return await produce()
            value = await original_run(schema, stage, instruction, payload, check, traced_produce, **options)
            observe_validated(value, stage)
            if not produced:
                report("production_checkpoint_revalidated", stage=stage, new_physical_model_call=False)
            return value

        async def revision(**options):
            nonlocal reference_review_scripts, reference_assessment
            value = await original_revision(**options)
            if value is not None:
                reference_review_scripts = [item.model_dump(mode="json") for item in value.reference_scripts]
                reference_assessment = value.review.model_dump(mode="json")
                for script in value.reference_scripts:
                    observe_validated(script, "synthetic_reference_implementation")
                report("production_reference_revision_resumed", scripts=len(value.reference_scripts),
                       review_unchanged=True, new_physical_model_call=False)
            return value
        async def oracle_revision(**options):
            value = await oracle_audit.load_validated_revision(original_oracle_revision, oracle_context, **options)
            if value is not None:
                artifact("oracle_revision_resumed", "synthetic_plan_review", {
                    "plan": value.plan.model_dump(mode="json"), "review": value.review.model_dump(mode="json"),
                    "context": oracle_context})
                report("production_oracle_revision_resumed", review_unchanged=True,
                       verdict=value.review.verdict(), new_physical_model_call=False)
            return value
        checkpoints.run, checkpoints.get_reference_revision = run, revision
        checkpoints.get_oracle_revision = oracle_revision
        return checkpoints

    @contextlib.contextmanager
    def trace_reference_verification():
        from unittest.mock import patch
        from app.domain.services.skills import skill_script_verification as verification
        from app.domain.services.skills import skill_validation_review as oracle_review
        production_reference = verification.verify_reference_plan
        production_review = oracle_review._review_candidate

        async def traced_review(plan, context, generate, *, target_list=None):
            result = await production_review(plan, context, generate, target_list=target_list)
            oracle_audit.record(plan, context, result, target_list)
            artifact("oracle_review_completed", "synthetic_plan_review", {
                "plan": plan.model_dump(mode="json"), "review": result.model_dump(mode="json"),
                "partial_targets": target_list is not None})
            return result

        async def traced(*args, **kwargs):
            nonlocal reference_scope, reference_review_scripts, reference_assessment, reference_interfaces, oracle_context
            reference_scope = reference_review_scripts = reference_assessment = None
            reference_execution_records.clear()
            import copy
            reference_interfaces = copy.deepcopy(args[3] if len(args) > 3 else kwargs["interfaces"])
            from app.domain.services.skills.skill_validation import build_validation_generation_payload
            oracle_context = build_validation_generation_payload(
                args[1] if len(args) > 1 else kwargs["contract"],
                args[2] if len(args) > 2 else kwargs["sources"], reference_interfaces)
            execute = kwargs.get("execute")
            if execute is None:
                from app.infrastructure.external.sandbox.skill_validation_worker import validate_skill_scripts
                execute = validate_skill_scripts
            def capture(worker_args, worker_kwargs, execution):
                reference_execution_records.append(copy.deepcopy({"scripts": worker_args[0],
                    "plan": worker_args[1].model_dump(mode="json"), "execution": execution}))
            kwargs["execute"] = traced_reference_worker(execute, capture)
            original_verified = kwargs.get("on_verified")
            if original_verified is not None:
                def on_verified(reconfirm):
                    async def traced_reconfirm(extended_plan):
                        from app.domain.services.skills.skill_validation_extension import CoverageReferenceConfirmation
                        confirmation = await reconfirm(extended_plan)
                        assert isinstance(confirmation, CoverageReferenceConfirmation)
                        receipt = confirmation.evidence
                        assert_reference_provenance(confirmation.plan.model_dump(), receipt, reference_programs,
                            interfaces=reference_interfaces, execution_records=reference_execution_records)
                        artifact("reference_receipt", "synthetic_reference_execution", receipt)
                        verified_references.append(receipt)
                        return confirmation
                    original_verified(traced_reconfirm)
                kwargs["on_verified"] = on_verified
            corrected, receipt = await production_reference(*args, **kwargs)
            assert_reference_provenance(corrected.model_dump(), receipt, reference_programs,
                                        interfaces=reference_interfaces, execution_records=reference_execution_records)
            artifact("reference_receipt", "synthetic_reference_execution", receipt)
            verified_references.append(receipt)
            return corrected, receipt

        # Delegates to the complete production boundary and actual sandbox;
        # no receipt, reference source or corrected plan is fabricated here.
        with patch.object(verification, "verify_reference_plan", traced), \
                patch.object(oracle_review, "_review_candidate", traced_review):
            yield

    def model_factory(*args, **kwargs):
        from app.domain.services.model_runtime import invoke_model_request
        from app.domain.services.skills.skill_creation.model_policy import resolve_skill_model_policy
        policy = resolve_skill_model_policy(args[0], kwargs.get("overrides") or {})
        assert policy.model_name == "deepseek-flash", "Artificial acceptance is authorized for deepseek-flash only"
        assert (policy.api_base or "").rstrip("/") in {
            "https://api.deepseek.com", "https://api.deepseek.com/v1"
        }, "Artificial acceptance must use the explicitly authorized DeepSeek endpoint"
        governed = kwargs.pop("request_middleware", None) or invoke_model_request

        async def traced_middleware(**request):
            original_invoke = request["invoke"]

            async def traced_invoke(messages, effective_output):
                # This effective value enters the driver's native max_tokens
                # field immediately after all normal middleware checks.
                stage, call = current["stage"], current["calls"]
                report("provider_request", scenario=name, stage=stage, call=call,
                       wire_max_output_tokens=effective_output,
                       wire_response_format=request.get("response_format"), governance_preserved=True)
                response = await original_invoke(messages, effective_output)
                observed_wire = [item for item in wire_records if item["stage"] == stage and item["call"] == call]
                assert observed_wire, "The real SDK request must be audited before accepting its response"
                options = {key: observed_wire[-1][key] for key in (
                    "wire_max_output_tokens", "wire_response_format", "wire_thinking_type", "wire_reasoning_effort")}
                assert options["wire_max_output_tokens"] == effective_output
                assert options["wire_response_format"] == request.get("response_format")
                usage = getattr(response, "usage_metadata", None) or {}
                output_tokens = usage.get("output_tokens")
                if not isinstance(output_tokens, int):
                    metadata = getattr(response, "response_metadata", None) or {}
                    output_tokens = (metadata.get("token_usage") or {}).get("completion_tokens")
                finish = model_finish_reason(response)
                if finish not in {None, "stop", "end_turn", "completed", "length", "max_tokens", "max_output_tokens", "incomplete"}:
                    finish = "other"
                record = {"stage": stage, "call": call, **options,
                          "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
                          "finish_reason": finish, "answer_characters": len(model_answer_text(response.content))}
                physical_records.append(record)
                artifact("provider_metadata", stage, record)
                report("provider_response", scenario=name, **record)
                return response

            return await governed(**{**request, "invoke": traced_invoke})

        wrapped = dependencies.model_factory(*args, request_middleware=traced_middleware, **kwargs)
        def capture_wire(options):
            record = {"stage": current["stage"], "call": current["calls"], **options}
            wire_records.append(record)
            artifact("wire_request", current["stage"], record)
            report("provider_wire_request", scenario=name, **record)

        wrapped._client.async_client = WireAuditedAsyncClient(wrapped._client.async_client, capture_wire)
        return InstrumentedModel(wrapped)

    class InstrumentedModel:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        @property
        def capabilities(self):
            return self.wrapped.capabilities

        async def ainvoke(self, messages, *args, **kwargs):
            nonlocal reference_scope, reference_review_scripts, reference_assessment
            request = json.loads(messages[-1].content)
            current.update(stage=request.get("stage", "unknown"), calls=current["calls"] + 1)
            assert current["stage"] != "unknown", "Every generation/review/revision needs a named stage"
            wire = json.dumps(request, ensure_ascii=False)
            assert not any(marker in wire for marker in private_markers), "Original artificial identity/credential reached a model payload"
            baseline = oracle_audit.baseline_for(current["stage"], request)
            assert_oracle_isolation(current["stage"], request, reuse_baseline=baseline)
            if "reference_evidence" in request:
                import copy
                assert verified_references, "Reference evidence must follow real verified execution"
                receipt = verified_references[-1]
                cases = copy.deepcopy(receipt["execution"]["cases"])
                for case in cases:
                    case.pop("output_sha256", None)
                expected = {"cases": cases, "corrections": receipt["corrections"]}
                canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
                assert canonical(request["reference_evidence"]) == canonical(expected)
            revision_context = None if reference_assessment is None else {
                "reference_scripts": reference_review_scripts, "review": reference_assessment}
            if current["stage"] in {"synthetic_reference_implementation", "synthetic_reference_review"}:
                scope = {key: request[key] for key in ("method_contract", "sources", "script_interfaces", "blind_cases")}
                if reference_scope is None:
                    reference_scope = json.loads(json.dumps(scope))
                else:
                    assert scope == reference_scope, "Reference revision cannot change blind inputs, methods, interfaces or targets"
            assert_reference_isolation(current["stage"], request,
                candidate_sources=candidate_programs.values(), original_sources=[source],
                reference_sources=reference_programs, reference_revision_context=revision_context,
                runtime_repair_context=(runtime_context(reference_review_scripts, reference_execution_records[-1])
                    if "runtime_repair" in request and reference_execution_records else None))
            if current["stage"] == "synthetic_reference_review":
                reference_review_scripts = json.loads(json.dumps(request["reference_scripts"]))
                reference_assessment = None
            assert_workflow_authoring_boundary(current["stage"], request)
            assert_source_authoring_boundary(current["stage"], request)
            from acceptance.skill_creator_replay_binding import request_digest
            artifact("message_binding", current["stage"], {
                "request_sha256": request_digest(request),
                "messages_sha256": request_digest([message.model_dump(mode="json") for message in messages]),
                "message_count": len(messages),
                "binding_scope": "all_serialized_chat_messages",
            })
            artifact("request", current["stage"], request)
            report("model_started", scenario=name, stage=current["stage"], call=current["calls"], repair=bool(request.get("repair_feedback")))
            if len(replayed) < len(replay):
                saved = replay[len(replayed)]
                if saved.get("binding") == "complete_request_v1":
                    from acceptance.skill_creator_replay_binding import bound_response_matches, request_digest
                    if not bound_response_matches(request, saved):
                        assert prefix_until_change and not replay_only, "Bound replay request changed; no response may be reused"
                        artifact("unused_original_prefix", current["stage"], replay[len(replayed):])
                        report("replay_request_changed", stage=current["stage"], retained_original_records=len(replay) - len(replayed),
                               current_request_sha256=request_digest(request))
                        del replay[len(replayed):]
            if len(replayed) < len(replay):
                saved = replay[len(replayed)]
                assert saved["stage"] == current["stage"], "Resume stage changed; do not silently skip cached responses"
                assert hashlib.sha256(saved["content"].encode()).hexdigest() == saved["response_sha256"]
                replayed.append({key: saved[key] for key in ("stage", "response_sha256", "origin_artifact")})
                artifact("replayed_response", current["stage"], saved)
                report("cached_response", scenario=name, stage=current["stage"], call=current["calls"],
                       response_sha256=saved["response_sha256"], original_bytes_unchanged=True,
                       new_physical_model_call=False, request_sha256=saved.get("request_sha256"),
                       finish_reason=saved.get("finish_reason", "stop"))
                # Mirror the governed driver's response-validation callback so
                # replayed source receives identical validation and provenance
                # tracking before a later blind review. The gateway validates
                # the response again; no provider usage is invented.
                return validated_replay_message(saved["content"], active_validators, saved.get("finish_reason", "stop"))
            assert not replay_only, "Replay-only acceptance exhausted its exact saved responses; external calls are disabled"
            response = await self.wrapped.ainvoke(messages, *args, **kwargs)
            report("model_returned", scenario=name, stage=current["stage"], call=current["calls"])
            return response

    @contextlib.contextmanager
    def diagnostic_validation(validate):
        def capture_then_validate(message):
            nonlocal reference_assessment
            artifact("response", current["stage"], model_answer_text(message.content))
            try:
                value = validate(message)
                observe_validated(value, current["stage"])
                return value
            except Exception as error:
                report("candidate_rejected", scenario=name, stage=current["stage"],
                       type=type(error).__name__, code=getattr(error, "code", None), feedback=getattr(error, "feedback", None))
                raise
        active_validators.append(capture_then_validate)
        try:
            with dependencies.response_validation(capture_then_validate):
                yield
        finally:
            assert active_validators.pop() is capture_then_validate

    injected = dataclasses.replace(dependencies,
        model_factory=model_factory,
        draft_service_factory=CaptureDraft, usage_recorder_factory=NoDatabaseUsageRecorder,
        response_validation=diagnostic_validation, checkpoint_factory=checkpoint_factory if cache is not None else None)
    started = time.monotonic()
    report("fixture", scenario=name, artificial=True, source_executed=True, private_session_read=False)
    try:
        with trace_reference_verification():
            await create_skill_from_session("artificial-session-" + name, "artificial-owner-" + name,
                ArtificialRepository(), SimpleNamespace(user_id="artificial-owner-" + name), dependencies=injected)
    except Exception as error:
        if cache is not None:
            artifact("checkpoint_state", "private_artificial_cache", cache.snapshot())
            report("production_checkpoint_summary", **cache.stats)
        chain, item = [], error
        while item is not None and len(chain) < 8:
            # Provider/transport exceptions may contain configured endpoints;
            # only our known fixed or wholly artificial validation diagnostics
            # belong in this capture, never arbitrary upstream exception text.
            safe_message = str(item) if type(item).__name__ in {
                "SkillCreationError", "ValidationError", "RepairRequired", "SkillPackageValidationError"
            } else None
            chain.append({"type": type(item).__name__, "code": getattr(item, "code", None),
                          "stage": getattr(item, "stage", None), "message": safe_message,
                          "feedback": getattr(item, "feedback", None)})
            item = item.__cause__
        artifact("error", "final", chain)
        report("complete", scenario=name, success=False, stage=current["stage"], calls=current["calls"],
               seconds=round(time.monotonic() - started, 1), physical_calls=len(physical_records),
               cached_responses=len(replayed), replay_only=replay_only, chain=chain)
        return False
    if cache is not None:
        artifact("checkpoint_state", "private_artificial_cache", cache.snapshot())
        report("production_checkpoint_summary", **cache.stats)
    assert len(replayed) == len(replay), "Unconsumed prefix candidates must not be ignored"
    if physical_records:
        formats = {json.dumps(item["wire_response_format"], sort_keys=True) for item in wire_records}
        assert formats and formats <= {"null", '{"type": "json_object"}'}
        report("creator_wire_verified", native_json='{"type": "json_object"}' in formats,
               raw_python="null" in formats, thinking_type="enabled",
               reasoning_effort="high", max_output_tokens=393216, hidden_reasoning_recorded=False)
    artifact("replay_summary", "final", replayed)
    artifact("provider_summary", "final", physical_records)
    artifact("wire_summary", "final", wire_records)
    report("complete", scenario=name, success=True, calls=current["calls"], seconds=round(time.monotonic() - started, 1),
           persisted=False, installed=False,
           maximum_physical_output_tokens=max((item["output_tokens"] or 0 for item in physical_records), default=0),
           physical_calls=len(physical_records), cached_responses=len(replayed))
    return True


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=tuple(SCENARIOS), default="join", nargs="?")
    parser.add_argument("--resume-prefix")
    parser.add_argument("--replay-only", action="store_true", help="Fail when saved responses are exhausted; never call the provider")
    parser.add_argument("--checkpoint-state")
    parser.add_argument("--resume-prefix-until-change", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.scenario, args.resume_prefix, args.replay_only, args.checkpoint_state,
                                        args.resume_prefix_until_change)) else 1)
