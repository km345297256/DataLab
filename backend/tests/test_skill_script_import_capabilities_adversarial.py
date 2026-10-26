"""Static admission never claims to prove runtime input/output safety."""
import ast
import sys

import pytest

from app.domain.services.skills.skill_creation.script_policy import (
    SkillScriptValidationError, script_authoring_capabilities, validate_script,
)
from test_skill_creation_script_policy import script


@pytest.mark.parametrize("body", [
    "import PIL, openpyxl, cairosvg, zipfile, xml.etree.ElementTree",
    "import requests, os, subprocess, importlib, io",
    "from os import system as execute_command",
    "from builtins import eval as evaluate_expression",
    "import synthetic_uninstalled_library",
    "import statsmodels.api as sm\nmodel = sm.GLM([1, 2], [[1], [2]])",
    "from statsmodels.api import GLM, families",
    "import statsmodels.formula.api as smf\nmodel = smf.ols('y ~ x', data={})",
    "from statsmodels.api import OLS\nmodel = OLS.from_formula('y ~ x', {})",
    "import statsmodels.api as sm\nother = {'library': sm}",
    "import statsmodels.api as sm\nmodel = sm.OLS([1, 2], [[1], [2]]).fit()\nvalue = model.load(args.input)",
    "import json as codec\nwith Path(args.input).open('r') as f:\n    value = codec.load(f)",
    "from json import load as read_json\nwith Path(args.input).open('r') as f:\n    value = read_json(f)",
    "import hashlib\nvalue = hashlib.sha256(b'example').hexdigest()",
    "import importlib.util\nvalue = importlib.util.find_spec('PIL')",
    "import importlib\nlibrary = importlib.import_module('scipy')",
    "import os\nvalue = os.path.splitext(args.input)",
    "import subprocess\nvalue = subprocess.run(['renderer', '--version'], capture_output=True)",
    "from subprocess import run as render\nvalue = render(['renderer', '--version'], capture_output=True)",
    "from .another_skill_module import calculation",
])
def test_module_names_and_export_names_do_not_decide_admission(body):
    value = script(body + "\nPath(args.output).write_text('result')")
    original = value.content
    modules = set(sys.modules)
    validate_script(value)
    assert value.content == original
    assert set(sys.modules) == modules


def test_module_wildcard_import_is_python_syntax_not_export_policy():
    value = script("Path(args.output).write_text('result')")
    value.content = "from statsmodels.api import *\n" + value.content
    validate_script(value)


@pytest.mark.parametrize("body", [
    "import os\nos.system('unsafe command')",
    "from os import system as launch\nlaunch('unsafe command')",
    "from os import remove as delete\ndelete(args.input)",
    "from builtins import eval as evaluate\nevaluate('unsafe expression')",
    "import os\nfirst = os.system\nsecond = first\nsecond('unsafe command')",
    "import shutil\nerase = shutil.rmtree\nerase(args.input)",
    "import pickle\nvalue = getattr(pickle, 'loads')",
    "value = __import__('a_module')",
    "Path(args.input).unlink()",
])
def test_dynamic_calls_and_aliases_do_not_have_an_operation_name_policy(body):
    # Validation examines source only; none of these calls are executed.
    validate_script(script(body))


@pytest.mark.parametrize("directive", [
    "from __future__ import annotations",
    "from __future__ import annotations as future_annotations",
    "from __future__ import division",
    "from __future__ import annotations, division",
    "import __future__",
])
def test_whole_module_directives_use_python_language_rules(directive):
    value = script("Path(args.output).write_text('result')")
    value.content = directive + "\n" + value.content
    validate_script(value)


@pytest.mark.parametrize("body", [
    "from __future__ import annotations\nPath(args.output).write_text('result')",
    "def helper():\n    from __future__ import annotations\n    return 1",
    "def helper():\n    from statsmodels.api import *\n    return 1",
])
def test_invalid_python_directive_placement_is_not_a_dependency_failure(body):
    with pytest.raises(SkillScriptValidationError) as caught:
        validate_script(script(body))
    assert caught.value.code == "script_syntax_invalid"


@pytest.mark.parametrize("source_form", ["complete_script", "module_manifest", "function", "repair_selection"])
def test_open_capabilities_do_not_claim_every_library_is_installed(source_form):
    policy = script_authoring_capabilities(source_form)
    assert policy["version"] == 4
    assert policy["dependency_policy"]["admission"] == "open"
    assert policy["dependency_policy"]["api_admission"] == "open"
    assert "blocked_operations" not in policy
    assert "script_unsafe_operation" not in policy["static_rules"]
    assert policy["dependency_policy"]["availability"] == "not_established_by_static_validation"
    assert not {"allowed_import_roots", "allowed_module_exports", "restricted_module_usage"} & policy.keys()
    assert "script_dependency_unsupported" not in policy["static_rules"]
    assert set(policy["static_rules"]) == {"script_syntax_invalid", "script_invocation_invalid"}
    assert policy["execution_validation_required"] is True
    assert policy["runtime_contract"]
    assert policy["runtime_contract"] == script_authoring_capabilities("complete_script")["runtime_contract"]


@pytest.mark.parametrize("body", [
    "import subprocess\nsubprocess.run(['rm', '-rf', 'data'])",
    "import subprocess as processes\nprocesses.check_call(('/bin/rm', 'file'))",
    "from subprocess import run as launch\nlaunch(args=['unlink', 'file'])",
    "import subprocess\nlaunch = subprocess.run\nlaunch(['chmod', '777', 'file'])",
    "from subprocess import Popen\nPopen(['mv', 'input', 'output'])",
])
def test_tool_names_do_not_form_an_executable_denylist(body):
    # Runtime mount isolation enforces filesystem access, not command spelling.
    validate_script(script(body))


@pytest.mark.parametrize("body", [
    "import subprocess\nsubprocess.run(['renderer', '--label', 'rm'])",
    "from subprocess import run as convert\nconvert(['converter', '--output', args.output])",
    "import subprocess\ncommand = ['renderer', args.input]\nsubprocess.run(command)",
    "import subprocess\ndef ordinary(subprocess):\n    return subprocess.run(['rm'])",
])
def test_normal_tool_calls_and_noncommand_text_are_not_destructive_operations(body):
    validate_script(script(body))


def test_static_admission_never_executes_dynamic_code_or_commands(tmp_path):
    target = tmp_path / "must-not-be-created"
    source = script(f"exec({('open(' + repr(str(target)) + ', chr(119)).close()')!r})")
    original = source.content
    validate_script(source)
    assert source.content == original
    assert not target.exists()
