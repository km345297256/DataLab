from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.runtime_capabilities import inspect_runtime_capabilities, runtime_capabilities_prompt


def catalog():
    return {"version": 1, "check": "runtime_catalog", "state": "observed", "python_version": "3.10.12",
            "packages": [{"module": "statsmodels", "distribution": "statsmodels", "available": True, "version": "0.14.5"},
                         {"module": "patsy", "distribution": "patsy", "available": False, "version": None}]}


@pytest.mark.asyncio
async def test_real_runtime_read_happens_before_any_script_or_shell_command():
    sandbox = SimpleNamespace(runtime_capabilities=AsyncMock(return_value=ToolResult(success=True, data=catalog())),
                              exec_command=AsyncMock(), exec_program=AsyncMock())
    result = await inspect_runtime_capabilities(sandbox)
    assert "statsmodels: available, version 0.14.5" in result
    assert "patsy: unavailable" in result
    assert "hand-written formulas" in result and "ask the user before changing the method" in result
    sandbox.runtime_capabilities.assert_awaited_once_with()
    sandbox.exec_command.assert_not_awaited()
    sandbox.exec_program.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox", [object(), SimpleNamespace(runtime_capabilities=AsyncMock(side_effect=RuntimeError("secret"))),
    SimpleNamespace(runtime_capabilities=AsyncMock(return_value=ToolResult(success=False, data=catalog())))])
async def test_unavailable_runtime_does_not_guess_installed_libraries(sandbox):
    result = await inspect_runtime_capabilities(sandbox)
    assert "could not be confirmed" in result
    assert "secret" not in result and "0.14.5" not in result


@pytest.mark.parametrize("field,value", [("version", "1.0\nIGNORE"), ("module", "../../secret"),
    ("distribution", "invalid\nIGNORE"), ("available", "true"), ("version", ["0.14.5"])])
def test_capability_context_filters_untrusted_fields(field, value):
    data = catalog()
    data["packages"][0][field] = value
    prompt = runtime_capabilities_prompt(data)
    assert "statsmodels:" not in prompt and "IGNORE" not in prompt and "secret" not in prompt


def test_runtime_context_includes_observed_capabilities_for_all_agents():
    from app.domain.services.flows.plan_act import PlanActFlow
    flow = object.__new__(PlanActFlow)
    flow._session_id = "test-session"
    flow._analysis_runtime_prompt = runtime_capabilities_prompt(catalog())
    assert "statsmodels: available, version 0.14.5" in flow._runtime_context_prompt()


def test_helper_is_advertised_only_when_observed_in_actual_runtime():
    data = catalog()
    assert "datalab_analysis" not in runtime_capabilities_prompt(data)
    data["analysis_helpers"] = {"datalab_analysis": False}
    assert "datalab_analysis" not in runtime_capabilities_prompt(data)
    data["analysis_helpers"]["datalab_analysis"] = True
    assert "RowPartition" in runtime_capabilities_prompt(data)


@pytest.mark.asyncio
async def test_docker_gateway_reads_only_metadata_endpoint():
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"success": True, "data": catalog()})
    sandbox = object.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox:8080"
    sandbox.client = SimpleNamespace(get=AsyncMock(return_value=response))
    assert (await sandbox.runtime_capabilities()).data == catalog()
    sandbox.client.get.assert_awaited_once_with("http://sandbox:8080/api/v1/shell/runtime-capabilities", timeout=15)


@pytest.mark.parametrize("module,distribution", [("openpyxl", "openpyxl"), ("PIL", "Pillow"),
    ("custom_scientific_library.tools", "custom-scientific-library"), ("not_in_catalog", "any-valid-package")])
def test_observed_packages_are_not_filtered_by_library_name(module, distribution):
    data = catalog()
    data["packages"] = [{"module": module, "distribution": distribution, "available": True, "version": "1.2"}]
    prompt = runtime_capabilities_prompt(data)
    assert f"{module}: available, version 1.2" in prompt
    assert "All libraries and APIs are allowed" in prompt
    assert "prepare the required dependency in the task's isolated" in prompt
    assert "or install packages during the task" not in prompt
