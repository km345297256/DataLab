"""The first generation sees exactly the static API policy used for checking."""
import ast
import copy

import pytest

from app.domain.services.skills.skill_creation import script_policy
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.script_authoring import (
    ScriptFunctionUnit, ScriptModulePlan, ScriptRepairSelection,
)
from app.domain.services.skills.skill_creation.source_response import source_authoring_capabilities
from test_skill_creation_gateway import make_gateway, requests, SmallRecipe, VALID
from test_skill_creation_script_authoring import REQUEST, PLAN, COUNT, MAIN, author
from test_skill_creation_source_response import SOURCE


@pytest.mark.parametrize("schema,form", [
    (HelperScript, "complete_script"), (ScriptModulePlan, "module_manifest"),
    (ScriptFunctionUnit, "function"), (ScriptRepairSelection, "repair_selection"),
])
@pytest.mark.parametrize("stage", ["implementation", "synthetic_script_repair", "synthetic_build_repair", "synthetic_method_repair", "synthetic_reference_implementation"])
@pytest.mark.asyncio
async def test_every_source_protocol_declares_capabilities_before_first_response(schema, form, stage):
    if schema is HelperScript:
        response = {key: REQUEST[key] for key in ("path", "purpose", "invocation")}
        response["content"] = "# Complete synthetic module used only to check the request boundary.\ndef main():\n    return 1\n"
        payload = {"script_request": REQUEST, "script": response}
    elif schema is ScriptModulePlan:
        response, payload = PLAN, {}
    elif schema is ScriptFunctionUnit:
        response, payload = COUNT, {"function_request": PLAN["functions"][0]}
    else:
        response, payload = {"unit_ids": ["U001"]}, {}
    payload["script_authoring_capabilities"] = {"allowed_import_roots": ["importlib"]}
    gateway, calls, _ = make_gateway([response])
    await gateway.generate(schema, stage, "Honor the frozen scientific method.", payload)
    sent, = requests(calls)
    capability = sent["script_authoring_capabilities"]
    assert capability == script_policy.script_authoring_capabilities(form)
    assert capability["dependency_policy"]["admission"] == "open"
    assert "allowed_import_roots" not in capability and "allowed_module_exports" not in capability
    assert capability["execution_validation_required"] is True
    assert capability["dependency_policy"]["api_admission"] == "open"
    assert "blocked_operations" not in capability
    assert "script_unsafe_operation" not in capability["static_rules"]
    assert capability["future_imports"]["mode"] == ("python_syntax" if schema is HelperScript else "excluded_from_modular_assembly")
    assert "response_format" in sent if schema in {HelperScript, ScriptFunctionUnit} else "schema" in sent


def test_declared_policy_is_the_same_source_used_for_import_validation():
    capability = script_policy.script_authoring_capabilities("complete_script")
    assert capability["dependency_policy"]["admission"] == "open"
    for statement in ["import importlib", "import synthetic_missing_library", "from statsmodels.api import GLM",
                      "import os", "from __future__ import annotations"]:
        script_policy.validate_supported_imports(ast.parse(statement))
    assert source_authoring_capabilities(HelperScript, "workflow") is None
    assert source_authoring_capabilities(SmallRecipe, "implementation") is None
    assert "blocked_operations" not in capability
    assert capability["dependency_policy"]["api_admission"] == "open"


@pytest.mark.asyncio
async def test_modular_manifest_and_every_complete_function_declare_open_dependency_policy():
    plan = copy.deepcopy(PLAN)
    plan["imports"].append("import importlib")
    _, calls = await author([plan, COUNT, MAIN])
    assert [item["script_authoring_capabilities"]["source_form"] for item in calls] == [
        "module_manifest", "function", "function"]
    assert all(item["script_authoring_capabilities"]["future_imports"]["mode"] == "excluded_from_modular_assembly"
               for item in calls)
    assert all(item["script_authoring_capabilities"]["dependency_policy"]["admission"] == "open" for item in calls)
    assert all("repair_feedback" not in item for item in calls)


@pytest.mark.asyncio
async def test_unrelated_json_stages_do_not_gain_source_capabilities():
    gateway, calls, _ = make_gateway([VALID])
    await gateway.generate(SmallRecipe, "workflow", "Return a valid workflow.", {})
    assert "script_authoring_capabilities" not in requests(calls)[0]


@pytest.mark.parametrize("stage", ["implementation", "synthetic_script_repair", "synthetic_build_repair", "synthetic_method_repair", "synthetic_reference_implementation"])
@pytest.mark.asyncio
async def test_library_introspection_is_accepted_on_first_source_response(stage):
    from langchain.messages import AIMessage

    source = "import numpy as np\nLIBRARY_VERSION = np.__version__\n" + SOURCE
    metadata = {key: REQUEST[key] for key in ("path", "purpose", "invocation")}
    script_policy.validate_script(HelperScript(**metadata, content=source))
    gateway, calls, _ = make_gateway([AIMessage(content=source)])
    payload = {"script_request": REQUEST, "script": {**metadata, "content": SOURCE}}
    result = await gateway.generate(HelperScript, stage, "Preserve all computations and frozen interfaces.",
        payload, script_policy.validate_script)
    first, = requests(calls)
    assert first["script_authoring_capabilities"]["dependency_policy"]["api_admission"] == "open"
    assert "repair_feedback" not in first
    assert result.content == source.strip()
    assert "__version__" in result.content
    script_policy.validate_script(result)
