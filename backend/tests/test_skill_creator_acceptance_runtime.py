"""Offline regressions for wire audit and the unchanged artificial hand oracles."""
from copy import deepcopy
import hashlib
import json

import pytest


def test_artificial_draft_capture_obeys_real_storage_limits_and_unicode_bytes():
    from acceptance.skill_creator_real_model import assert_draft_package_limits
    from app.application.services.skill_draft_service import MAX_DRAFT_BYTES
    values = {"name": "test-skill", "description": "中文方法", "triggers": ["分析"],
        "content": "\n执行步骤\n", "references": {"references/evidence.json": '{"result":"中文"}'},
        "scripts": {"scripts/run.py": "print('ok')\n"}}
    receipt = assert_draft_package_limits(values)
    assert receipt["resource_count"] == 3 and receipt["total_utf8_bytes"] < MAX_DRAFT_BYTES
    assert set(receipt["resource_sha256"]) == {"SKILL.md", "references/evidence.json", "scripts/run.py"}
    values["content"] = "中" * (MAX_DRAFT_BYTES // 2)
    with pytest.raises(AssertionError, match="Real SkillDraftService"):
        assert_draft_package_limits(values)
    values["content"] = "short"
    values["references"] = {f"references/file_{index}.json": "{}" for index in range(64)}
    with pytest.raises(AssertionError, match="Real SkillDraftService"):
        assert_draft_package_limits(values)

from acceptance.skill_creator_real_model import (
    WireAuditedAsyncClient, complex_hand_cases, creator_wire_metadata, independently_verify_complex,
)


@pytest.mark.parametrize("response_format", [None, {"type": "json_object"}])
def test_creator_wire_audit_checks_effective_json_and_source_options(response_format):
    request = {"max_tokens": 393216, "reasoning_effort": "high",
               "thinking": {"type": "disabled"},
               "extra_body": {"thinking": {"type": "enabled"}, "response_format": response_format},
               "messages": [{"role": "system", "content": "PROMPT_PRIVATE_CANARY"}]}
    metadata = creator_wire_metadata(request)
    assert metadata == {"wire_max_output_tokens": 393216, "wire_reasoning_effort": "high",
                        "wire_thinking_type": "enabled", "wire_response_format": response_format}
    assert "PROMPT_PRIVATE_CANARY" not in json.dumps(metadata)


@pytest.mark.parametrize("change", [{"max_tokens": 8192}, {"reasoning_effort": "low"},
                                    {"extra_body": {"thinking": {"type": "disabled"}}}])
def test_creator_wire_audit_fails_closed_on_missing_creator_override(change):
    request = {"max_tokens": 393216, "reasoning_effort": "high",
               "extra_body": {"thinking": {"type": "enabled"}}}
    with pytest.raises(AssertionError):
        creator_wire_metadata({**request, **change})


@pytest.mark.asyncio
async def test_wire_proxy_forwards_unchanged_without_reading_hidden_reasoning():
    class OpaqueResponse:
        @property
        def reasoning_content(self):
            raise AssertionError("Hidden reasoning must not be inspected")

    response = OpaqueResponse()
    received = []

    class Client:
        async def create(self, *args, **kwargs):
            received.append((args, kwargs))
            return response

    captured = []
    request = {"max_tokens": 393216, "reasoning_effort": "high",
               "extra_body": {"thinking": {"type": "enabled"}}, "messages": ["PRIVATE_CANARY"]}
    original = deepcopy(request)
    returned = await WireAuditedAsyncClient(Client(), captured.append).create(**request)
    assert returned is response and received == [((), original)] and request == original
    assert len(captured) == 1 and "PRIVATE_CANARY" not in json.dumps(captured)


@pytest.mark.asyncio
async def test_sdk_audit_matches_mocked_http_wire_for_deepseek_json_and_python():
    """Real driver and installed SDK, with an entirely local HTTP transport."""
    import httpx
    from langchain_core.messages import HumanMessage
    from app.core.config import Settings
    from app.infrastructure.external.llm.chat_model import create_chat_model
    from test_model_driver import RecordingMiddleware

    actual_wire, audited = [], []

    def transport(request):
        actual_wire.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "offline-wire-audit", "object": "chat.completion",
            "created": 1, "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "{}"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}})

    middleware = RecordingMiddleware()
    middleware.output = 393216
    model = create_chat_model(Settings(_env_file=None, model_provider="deepseek", model_name="deepseek-flash",
        api_key="offline-key", api_base="https://api.deepseek.com", model_request_capabilities={"deepseek": {
            "deepseek-flash": {"context_tokens": 1048576, "max_output_tokens": 393216}}}), request_middleware=middleware)
    model._client.async_client = WireAuditedAsyncClient(model._client.async_client, audited.append)
    await model._client.root_async_client._client.aclose()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http_client:
            model._client.root_async_client._client = http_client
            for response_format in ({"type": "json_object"}, None):
                await model.ainvoke([HumanMessage(content="Offline artificial request")],
                    response_format=response_format, extra_body={"thinking": {"type": "enabled"}},
                    reasoning_effort="high")
    finally:
        model._client.root_client.close()
        await model._client.root_async_client.close()
    assert len(actual_wire) == len(audited) == 2
    assert [creator_wire_metadata(body) for body in actual_wire] == audited
    assert all("extra_body" not in body for body in actual_wire)


def test_complex_original_and_zero_hand_inputs_and_expectations_remain_exact():
    script = {"cases": [{"id": "original", "kind": "normal",
        "argv": ["--input", "{input:original.json}", "--output", "{output}"],
        "assertions": [{"type": "json_value", "path": "report.json"},
                       {"type": "csv_rows", "path": "wide.csv"}]}]}
    before = deepcopy(script)
    cases = complex_hand_cases(script)
    assert script == before
    # Snapshots of the two already executed fixed cases, captured before this
    # integration. These include every input, expectation and tolerance.
    frozen_hashes = {
        "host-original-fixed-hand": "4ce7cf27d118a5780a6ce56f9877ef7b3fc446f54b2a73e260fcc44b28adfacb",
        "host-new-zero-variance-formula-check": "a39389c2e982003802241a8ca501b776f26849fe760f4b070a6f16293053be21",
    }
    expected_checks = [184, 30]
    for case, count in zip(cases, expected_checks):
        material = {key: case[key] for key in ("input_files", "argv", "assertions")}
        assert hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest() == frozen_hashes[case["id"]]
        actual = sum(1 + assertion["count"] * len(assertion["fields"])
                     if assertion["type"] == "csv_rows" else 1 for assertion in case["assertions"])
        assert actual == count
    rejection = cases[2]
    assert rejection["id"] == "host-explicit-null-units-rejected"
    assert rejection["expected_exit_code"] == "nonzero" and rejection["input_unchanged"] is True
    assert rejection["input_files"][0]["data"]["units"] is None
    assert rejection["assertions"] == [] and "--units-json" not in rejection["argv"]
    # A generated plan's optional filename override cannot change these fixed
    # acceptance targets while its original CLI overrides are absent.
    script["cases"][0]["assertions"][0]["path"] = "custom.json"
    script["cases"][0]["assertions"][1]["path"] = "custom.csv"
    assert complex_hand_cases(script) == cases


@pytest.mark.asyncio
async def test_complex_final_acceptance_cannot_hide_a_failed_reference_behind_correct_candidate(monkeypatch):
    from acceptance import skill_creator_real_model as harness
    from app.infrastructure.external.sandbox import skill_validation_worker as worker
    from test_skill_validation_plan import plan_payload

    plan = plan_payload()
    path = plan["scripts"][0]["script_path"]
    values = {"scripts": {path: "CANDIDATE_BYTES"}, "references": {
        "references/synthetic-validation.json": json.dumps(plan)}}
    observed, artifacts = [], []

    async def execute(programs, typed_plan):
        observed.append((deepcopy(programs), typed_plan.model_dump()))
        is_reference = programs[path] == "REFERENCE_BYTES"
        cases = []
        for case in typed_plan.scripts[0].cases:
            count = {"host-original-fixed-hand": 184, "host-new-zero-variance-formula-check": 30}.get(case.id, 0)
            failed = is_reference and case.id == "host-original-fixed-hand"
            cases.append({"id": case.id, "status": "failed" if failed else "passed", "input_unchanged": True,
                "checks": [{"passed": not failed or index > 0} for index in range(count)]})
        return {"status": "failed" if is_reference else "passed", "cases": cases}

    monkeypatch.setattr(worker, "validate_skill_scripts", execute)
    monkeypatch.setattr(harness, "artifact", lambda kind, stage, value: artifacts.append(kind))
    await independently_verify_complex(values)
    with pytest.raises(AssertionError):
        await independently_verify_complex(values, reference_sources={path: "REFERENCE_BYTES"})
    assert observed[0][0] == values["scripts"]
    assert observed[1][0] == {path: "REFERENCE_BYTES"}
    assert observed[0][1] == observed[1][1]
    assert artifacts == ["candidate_hand_validation", "reference_hand_validation"]


@pytest.mark.asyncio
async def test_reference_worker_trace_preserves_failure_and_forwards_contract_unchanged(monkeypatch):
    from acceptance import skill_creator_real_model as harness

    programs, plan, contract = {"scripts/helper.py": "SOURCE"}, object(), object()
    receipt = {"status": "failed", "cases": [{"id": "normal", "reason": "output_missing:report.json"}]}
    artifacts = []

    async def execute(given_programs, given_plan, **kwargs):
        assert given_programs is programs and given_plan is plan
        assert kwargs == {"public_output_contract": contract}
        return receipt

    monkeypatch.setattr(harness, "artifact", lambda *value: artifacts.append(value))
    observed = await harness.traced_reference_worker(execute)(programs, plan, public_output_contract=contract)
    assert observed is receipt
    assert artifacts == [("reference_worker_receipt", "synthetic_reference_execution", receipt)]
