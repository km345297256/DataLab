"""Actual API observations, without executing targets or leaking their values."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "python_inspect.py"
spec = importlib.util.spec_from_file_location("datalab_python_inspect", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def run_probe(module, names, *, extra_path=None):
    environment = os.environ.copy()
    if extra_path is not None:
        environment["PYTHONPATH"] = str(extra_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), json.dumps({"module": module, "names": names})],
        capture_output=True, env=environment, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stderr == b""
    assert len(result.stdout) <= probe.MAX_REPORT_BYTES + 1
    return json.loads(result.stdout)


def install_module(monkeypatch, name="probe_fixture"):
    module = ModuleType(name)
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(probe.importlib.metadata, "packages_distributions", lambda: {})
    return module


def test_stdlib_signatures_and_missing_attribute_are_observed():
    report = run_probe("json", ["dumps", "JSONDecoder.raw_decode", "missing_api"])
    assert report["state"] == "observed"
    members = {item["name"]: item for item in report["members"]}
    assert "obj" in members["dumps"]["signature"]["text"]
    assert "indent" in members["dumps"]["signature"]["text"]
    assert "idx=0" in members["JSONDecoder.raw_decode"]["signature"]["text"]
    assert members["missing_api"]["state"] == "missing"


def test_missing_module_has_safe_structured_error():
    report = run_probe("datalab_missing_api_probe_fixture", ["anything"])
    assert report["state"] == "import_failed"
    assert report["error_type"] == "ModuleNotFoundError"
    assert report["members"] == []
    assert "Traceback" not in json.dumps(report)


def test_version_metadata_survives_missing_top_level_module_map(monkeypatch):
    module = install_module(monkeypatch)
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda name: "1.2.3")
    report = probe.inspect_api(module.__name__, [])
    assert report["distributions"] == [{"name": module.__name__, "version": "1.2.3", "lookup": "same_name_distribution"}]


def test_actual_scipy_signature_and_documented_returns():
    pytest.importorskip("scipy.stats")
    report = run_probe("scipy.stats", ["ttest_ind", "levene"])
    members = {item["name"]: item for item in report["members"]}
    assert "equal_var" in members["ttest_ind"]["signature"]["text"]
    assert "Returns" in members["ttest_ind"]["documentation"]["text"]
    assert "pvalue" in members["ttest_ind"]["documentation"]["text"]
    assert "center" in members["levene"]["signature"]["text"]
    assert any(item["name"] == "scipy" for item in report["distributions"])


def test_actual_statsmodels_f_test_return_semantics_are_preserved():
    pytest.importorskip("statsmodels.regression.linear_model")
    report = run_probe("statsmodels.regression.linear_model", ["RegressionResults.compare_f_test", "RegressionResults.t_test"])
    members = {item["name"]: item for item in report["members"]}
    comparison = members["RegressionResults.compare_f_test"]
    assert "restricted" in comparison["signature"]["text"]
    assert "Returns" in comparison["documentation"]["text"]
    assert "df_diff" in comparison["documentation"]["text"]
    assert "r_matrix" in members["RegressionResults.t_test"]["signature"]["text"]
    assert any(item["name"] == "statsmodels" for item in report["distributions"])


def test_target_functions_constructors_and_descriptors_are_not_called(monkeypatch):
    module = install_module(monkeypatch)
    actions = []

    def function(required, *, option=3):
        actions.append("called")
        raise AssertionError("inspection must not call a target")

    class Example:
        def __init__(self):
            actions.append("constructed")

        @property
        def secret(self):
            actions.append("property")
            return "private input"

        @staticmethod
        def static(value):
            actions.append("static")

    class CallableValue:
        def __call__(self):
            actions.append("callable")

        @property
        def __signature__(self):
            actions.append("signature property")
            raise AssertionError("must not inspect a dynamic instance descriptor")

    module.function = function
    module.Example = Example
    module.instance = CallableValue()
    report = probe.inspect_api(module.__name__, ["function", "Example", "Example.secret", "Example.static", "instance"])
    assert all(item["state"] == "observed" for item in report["members"])
    assert "required" in report["members"][0]["signature"]["text"]
    assert "value" in report["members"][3]["signature"]["text"]
    assert report["members"][4]["signature"]["state"] == "unavailable"
    assert actions == []


def test_attribute_values_paths_and_exception_messages_are_not_returned(monkeypatch):
    module = install_module(monkeypatch)
    module.token = "private-token-do-not-echo"
    module.__file__ = "/private/secret/project.py"
    module.table = {"private-row": [1, 2, 3]}
    report = probe.inspect_api(module.__name__, ["token", "__file__", "table"])
    encoded = probe.serialize_report(report)
    assert b"private-token" not in encoded and b"/private/secret" not in encoded and b"private-row" not in encoded
    assert all(item["documentation"]["text"] == "" for item in report["members"])

    def failed_import(_name):
        raise RuntimeError("private-token /private/secret/project.py")

    monkeypatch.setattr(probe.importlib, "import_module", failed_import)
    report = probe.inspect_api("unavailable_fixture", [])
    assert report["error_type"] == "RuntimeError"
    assert b"private-token" not in probe.serialize_report(report)


@pytest.mark.parametrize("module,names", [
    ("os.system('touch forbidden')", []),
    ("valid_module", ["call()"]),
    ("valid_module", ["object['key']"]),
    ("valid_module", ["bad;exec"]),
    ("valid_module", ["good"] * 9),
    ("valid_module", [None]),
])
def test_expression_and_invalid_request_rejection_precedes_import(monkeypatch, module, names):
    imports = []
    monkeypatch.setattr(probe.importlib, "import_module", lambda name: imports.append(name))
    with pytest.raises(ValueError):
        probe.inspect_api(module, names)
    assert imports == []


def test_long_unicode_documentation_keeps_returns_within_byte_budget(monkeypatch):
    module = install_module(monkeypatch)

    def target():
        raise AssertionError("not called")

    target.__doc__ = "说明参数与语义。" * 1500 + "\n\nReturns\n-------\ndf_diff : float\n    Difference in degrees of freedom.\n"
    module.target = target
    report = probe.inspect_api(module.__name__, ["target"])
    doc = report["members"][0]["documentation"]
    assert doc["truncated"] is True
    assert "df_diff" in doc["text"]
    assert len(doc["text"].encode("utf-8")) <= probe.MAX_DOCUMENTATION_CHARS
    assert len(probe.serialize_report(report)) <= probe.MAX_REPORT_BYTES


def test_final_json_bound_accounts_for_control_character_escaping(monkeypatch):
    module = install_module(monkeypatch)
    for index in range(8):
        def target():
            pass
        target.__doc__ = "\x01" * 7000 + "\nReturns\n-------\nvalue : int\n"
        setattr(module, f"target{index}", target)
    report = probe.inspect_api(module.__name__, [f"target{index}" for index in range(8)])
    encoded = probe.serialize_report(report)
    assert len(encoded) <= probe.MAX_REPORT_BYTES
    assert len(json.loads(encoded)["members"]) == 8
    assert all(item["documentation"]["truncated"] for item in report["members"])


def test_python_native_and_exit_output_do_not_pollute_json(tmp_path):
    module = tmp_path / "noisy_api_fixture.py"
    module.write_text(
        "import atexit, os, sys\n"
        "print('IMPORT PRIVATE STDOUT')\n"
        "print('IMPORT PRIVATE STDERR', file=sys.stderr)\n"
        "os.write(1, b'NATIVE PRIVATE STDOUT\\n')\n"
        "os.write(2, b'NATIVE PRIVATE STDERR\\n')\n"
        "atexit.register(lambda: print('EXIT PRIVATE STDOUT'))\n"
        "def target(value):\n"
        "    raise RuntimeError('TARGET MUST NOT RUN')\n",
        encoding="utf-8",
    )
    report = run_probe("noisy_api_fixture", ["target"], extra_path=tmp_path)
    assert report["members"][0]["signature"]["state"] == "observed"
    assert "PRIVATE" not in json.dumps(report)


def test_module_documentation_is_available_without_members():
    report = run_probe("json", [])
    assert report["members"][0]["name"] == "json"
    assert "JSON" in report["members"][0]["documentation"]["text"]
