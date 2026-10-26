"""Opt-in real ExecutionAgent acceptance with native tools and no user DB.

Invoked by run_analysis_code_real_model.py inside the configured backend. Only
agent memory and usage persistence are replaced with in-memory sinks. Model
governance, prompts, tools, saved-program preparation and receipts remain real.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from analysis_code_cases import CASES, INPUT_ROOT, OUTPUT_ROOT, evaluate, request_for


class InMemoryRepository:
    def __init__(self):
        self.memories = {}

    async def get_memory(self, agent_id, name):
        from app.domain.models.memory import Memory
        return self.memories.setdefault((agent_id, name), Memory())

    async def save_memory(self, agent_id, name, memory):
        self.memories[agent_id, name] = memory

    async def add_memory(self, agent_id, name, memory):
        await self.save_memory(agent_id, name, memory)

    async def save(self, agent):
        return None

    async def find_by_id(self, agent_id):
        from app.domain.models.agent import Agent
        return Agent(id=agent_id)


class InMemoryUsage:
    async def record_from_message(self, *args, **kwargs):
        return None


def program_metrics(records, feedback, *, total_count=None):
    """Repairs mean changed-source reruns of a previously failed saved path."""
    previous, repairs = {}, 0
    for item in feedback:
        path = item["script_path"]
        old = previous.get(path)
        if old and old["returncode"] != 0 and old["source_digest"] != item["source_digest"]:
            repairs += 1
        previous[path] = item
    complete = ((total_count is None or total_count == len(records))
                and all(item.get("sequence", index) == index for index, item in enumerate(records, 1)))
    return {"program_requests": total_count if total_count is not None else len(records),
            "program_failures": sum(item["state"] != "succeeded" for item in records),
            "first_program_success": records[0]["state"] == "succeeded" if records and complete else None,
            "execution_records_complete": complete,
            "repair_runs": repairs,
            "program_metrics_definition": "native program_run requests; probes excluded; changed-source reruns after failed same path"}


async def run_case(config):
    from app.core.config import get_settings
    from app.domain.models.message import Message
    from app.domain.models.plan import Plan, Step
    from app.domain.services.agents.execution import ExecutionAgent
    from app.domain.services.agents import base
    from app.domain.services.model_runtime import invoke_model_request
    from app.domain.services.runtime_capabilities import inspect_runtime_capabilities
    from app.domain.services.tools.file import FileToolkit
    from app.domain.services.tools.shell import ShellToolkit
    from app.infrastructure.external.llm.chat_model import create_chat_model
    from app.domain.services.model_route import resolve_model_route
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox

    settings = get_settings()
    route = resolve_model_route(settings, {})
    # Route fingerprints omit API keys, authentication headers and all secrets.
    model_config = {"provider": route.provider, "model": settings.model_name,
                    "temperature": settings.temperature,
                    "route_sha256": hashlib.sha256((route.api_base or "provider-default").encode()).hexdigest(),
                    "context_capacity": settings.model_context_capacity_tokens,
                    "context_safety": settings.model_context_safety_tokens}
    sandbox = DockerSandbox(ip=config["sandbox_name"], container_name=config["sandbox_name"],
        image_reference=config["sandbox_image"], image_digest=config["sandbox_image_id"])
    timings, actual_models, feedback, tool_calls, artifacts = [], [], {}, {}, {}

    async def middleware(**request):
        invoke = request["invoke"]
        async def measured(messages, effective_output):
            started = time.perf_counter()
            try:
                response = await invoke(messages, effective_output)
                meta = getattr(response, "response_metadata", {}) or {}
                actual = meta.get("model_name") or meta.get("model")
                if isinstance(actual, str) and len(actual) <= 200:
                    actual_models.append(actual)
                return response
            finally:
                timings.append(time.perf_counter()-started)
        return await invoke_model_request(**dict(request, invoke=measured))

    def factory(*args, **kwargs):
        return create_chat_model(*args, **kwargs, request_middleware=middleware)

    started = time.perf_counter()
    result = {"case": config["case"], "repeat": config["repeat"]}
    try:
        async with asyncio.timeout(config["timeout_seconds"]):
            ready = False
            for _ in range(30):
                try:
                    response = await sandbox.client.get(sandbox.base_url + "/api/v1/shell/runtime-capabilities", timeout=2)
                    if response.status_code == 200 and response.json().get("success") is True:
                        ready = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(.2)
            if not ready:
                raise RuntimeError("artificial_sandbox_not_ready")
            facts = await inspect_runtime_capabilities(sandbox)
            with patch.object(base, "create_chat_model", factory):
                agent = ExecutionAgent(agent_id="artificial-analysis", agent_repository=InMemoryRepository(),
                    tools=[FileToolkit(sandbox), ShellToolkit(sandbox)], runtime_facts_provider=lambda: facts)
            agent.token_usage_service = InMemoryUsage()
            original = agent._record_confirmed_program_feedback
            def observed(item):
                # The actual production boundary calls this only after ledger
                # authentication. Copy the identity, not mutable tool prose.
                feedback.setdefault(item["operation_id"], {key: item[key] for key in (
                    "script_path", "returncode", "source_digest")})
                snapshot = item.get("source_snapshot")
                if (item["script_path"].startswith(OUTPUT_ROOT+"/") and isinstance(snapshot, dict)
                        and isinstance(snapshot.get("content"), str)):
                    artifacts["program-"+item["source_digest"]+".py"] = snapshot["content"].encode()
                return original(item)
            agent._record_confirmed_program_feedback = observed
            model_config["max_output_tokens"] = agent._model.max_output_tokens
            step = Step(description=request_for(config["case"]), inputs={
                "execution_mode": "dataset_fast_path", "dataset_intent": "analysis",
                "target_files": [INPUT_ROOT+"/observations.csv"]})
            plan = Plan(language="en", steps=[step])
            async for event in agent.execute_step(plan, step, Message(message=request_for(config["case"]))):
                if getattr(event, "type", None) == "tool":
                    tool_calls[event.tool_call_id] = event.function_name
            records = agent.last_execution_outcome.get("execution_records", [])
            result.update(program_metrics(records, list(feedback.values()),
                total_count=agent.last_execution_outcome.get("execution_record_count", len(records))))
            result["agent_reported_success"] = step.success
            result["execution_record_states"] = [item["state"] for item in records]
            files = {}
            for filename in CASES[config["case"]]["outputs"]:
                try:
                    content = await sandbox.file_download(OUTPUT_ROOT+"/"+filename, max_bytes=2*1024*1024)
                    files[filename] = content.read()
                except Exception:
                    pass
            result["oracle"] = evaluate(config["case"], files)
            artifacts.update(files)
            result["artifact_sha256"] = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
            result["executed_source_sha256"] = [item["source_digest"] for item in feedback.values()]
    except TimeoutError:
        result["harness_error"] = "explicit_acceptance_timeout"
    except Exception as error:
        result["harness_error"] = type(error).__name__
        # Fixed harness frame locations help diagnose plumbing without printing
        # provider error bodies, URLs, user paths or configured credentials.
        frames, tb = [], error.__traceback__
        while tb:
            frames.append({"file": Path(tb.tb_frame.f_code.co_filename).name, "line": tb.tb_lineno})
            tb = tb.tb_next
        result["error_frames"] = frames[-5:]
    finally:
        await sandbox.client.aclose()  # outer runner owns only its test container
    result.update(model_calls=len(timings), model_seconds=sum(timings),
        elapsed_seconds=time.perf_counter()-started, model_configuration=model_config,
        observed_model_names=sorted(set(actual_models)), tool_calls=len(tool_calls))
    for name, content in artifacts.items():
        print("ARTIFACT:"+json.dumps({"name": name, "base64": base64.b64encode(content).decode()}), flush=True)
    return result


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    config = json.loads(Path(sys.argv[1]).read_text())
    print("ACCEPTANCE:"+json.dumps(asyncio.run(run_case(config)), sort_keys=True), flush=True)
