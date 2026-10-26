"""The read-only check resolves modules; it does not execute import code."""
import json
from pathlib import Path
import sys
import uuid

import pytest

from app.services.program import prepare_program, probe_program_prerequisites, ProgramPreparationError
from app.services.program_dependencies import inspect_program_dependencies


def test_runtime_catalog_is_available_before_script_creation(monkeypatch):
    import app.services.program_dependencies as dependencies
    monkeypatch.setattr(dependencies.importlib.metadata, "version", lambda distribution: "1.2.3")
    monkeypatch.setattr(dependencies, "_module_available", lambda module, paths: module != "statsmodels")
    report = dependencies.runtime_capabilities()
    assert report["check"] == "runtime_catalog" and report["state"] == "observed"
    packages = {item["module"]: item for item in report["packages"]}
    assert packages["statsmodels"]["available"] is False
    assert packages["patsy"]["version"] == "1.2.3"
    assert "path" not in json.dumps(report)


def test_runtime_endpoint_never_requires_a_user_script():
    import requests
    from conftest import BASE_URL
    response = requests.get(f"{BASE_URL}/api/v1/shell/runtime-capabilities", timeout=10)
    response.raise_for_status()
    data = response.json()["data"]
    assert data["check"] == "runtime_catalog"
    assert isinstance(data["python_version"], str)
    assert "statsmodels" in {package["module"] for package in data["packages"]}
    assert "script_path" not in data and "source_snapshot" not in data
from app.services.shell import ShellService


def check(source, tmp_path):
    return inspect_program_dependencies(source.encode(), str(tmp_path / "analysis.py"))


def test_required_missing_module_is_blocked_before_any_source_runs(tmp_path):
    marker = tmp_path / "effect"
    path = tmp_path / "analysis.py"
    path.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nimport datalab_nonexistent_required\n")
    with pytest.raises(ProgramPreparationError) as failure:
        prepare_program(str(path.resolve()), [])
    feedback = failure.value.data["program_preparation"]
    assert feedback["reason"] == "missing_dependencies"
    assert feedback["diagnostic"] == {"missing_modules": ["datalab_nonexistent_required"]}
    assert feedback["dependencies"]["state"] == "missing"
    assert not marker.exists()


def test_preflight_dependency_state_changes_when_local_module_becomes_available(tmp_path):
    root = tmp_path.resolve()
    path = root / "analysis.py"
    path.write_text("import local_dependency\n")
    first = probe_program_prerequisites(str(root), str(path))
    assert first["status"] == "blocked"
    assert first["dependencies"]["missing_modules"] == ["local_dependency"]
    (root / "local_dependency.py").write_text("raise RuntimeError('preflight must not execute this')\n")
    second = probe_program_prerequisites(str(root), str(path))
    assert second["status"] == "ready"
    assert second["source"] == first["source"]
    assert second["prerequisite_digest"] != first["prerequisite_digest"]
    assert str(root) not in json.dumps(second)


def test_dotted_local_import_does_not_execute_package_initializer(tmp_path):
    package = tmp_path / "local_package"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('must not import package')")
    (package / "child.py").write_text("raise RuntimeError('must not import child')")
    result = check("import local_package.child", tmp_path)
    assert result["state"] == "ready"
    assert result["required_modules"] == ["local_package.child"]


@pytest.mark.parametrize("source", [
    "try:\n import absent_optional\nexcept ImportError:\n import math\n",
    "try:\n import absent_optional\nexcept (ValueError, ModuleNotFoundError):\n pass\n",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n import absent_optional\n",
    "def lazy():\n import absent_optional\n",
    "if object():\n import absent_optional\n",
])
def test_optional_missing_imports_are_reported_without_blocking(tmp_path, source):
    result = check(source, tmp_path)
    assert result["state"] == "ready"
    assert result["missing_modules"] == []
    assert result["optional_missing_modules"] == ["absent_optional"]


def test_unhandled_import_and_class_body_are_required(tmp_path):
    result = check("try:\n import missing_required\nexcept ValueError:\n pass\nclass E:\n import missing_class\n", tmp_path)
    assert result["missing_modules"] == ["missing_class", "missing_required"]


def test_dynamic_imports_are_explicitly_unchecked(tmp_path):
    result = check("import importlib\nimportlib.import_module('not_executed')", tmp_path)
    assert result["state"] == "ready"
    assert result["dynamic_imports"] is True


def test_loaded_standard_library_alias_and_qualified_exception_are_supported(tmp_path):
    result = check("import os.path\nimport builtins\ntry:\n import absent_optional\nexcept builtins.ImportError:\n pass\n", tmp_path)
    assert result["state"] == "ready"
    assert result["optional_missing_modules"] == ["absent_optional"]


@pytest.mark.parametrize("source", [
    "\n".join(f"import missing_{index}" for index in range(257)),
    "import " + "x" * 257,
])
def test_import_limit_yields_unavailable_instead_of_large_feedback(tmp_path, source):
    path = tmp_path.resolve() / "analysis.py"
    path.write_text(source)
    result = probe_program_prerequisites(str(tmp_path.resolve()), str(path))
    assert result["status"] == "unavailable" and result["prerequisite_digest"] is None


@pytest.mark.asyncio
async def test_missing_dependency_does_not_spawn_and_receipt_is_not_started(tmp_path, monkeypatch):
    service = ShellService()
    script = tmp_path.resolve() / "analysis.py"
    script.write_text("import missing_before_launch\n")
    async def forbidden(*args, **kwargs):
        pytest.fail("missing required dependency must not start a process")
    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    operation = uuid.uuid4().hex
    with pytest.raises(ProgramPreparationError):
        await service.exec_program("deps", str(tmp_path.resolve()), str(script), [], operation_id=operation)
    receipt = await service.operation_status("deps", operation)
    assert receipt.state == "not_started"


@pytest.mark.asyncio
async def test_real_program_executor_fits_sklearn_and_writes_validated_json(tmp_path, monkeypatch):
    # This is intentionally a real child in the deployed Python interpreter.
    from app.services.artifact_validation import validate_artifacts
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.setattr(sys, "path", [sys.path[0], str(scripts), *sys.path[1:]])
    monkeypatch.setenv("PYTHONPATH", str(scripts))
    service = ShellService()
    root = tmp_path.resolve()
    script = root / "model.py"
    artifact = root / "model.json"
    script.write_text(f"""
import sklearn
from sklearn.linear_model import LinearRegression
from datalab_json import write_json, not_estimable
assert sklearn.__version__ == '1.7.2'
model = LinearRegression().fit([[0.0], [1.0], [2.0]], [1.0, 3.0, 5.0])
assert abs(model.predict([[3.0]])[0] - 7.0) < 1e-10
write_json({str(artifact)!r}, {{'coefficient': model.coef_, 'uncertainty': not_estimable('No independent validation observations')}}, output_root={str(root)!r})
print('sklearn-fit-and-json-ok')
""")
    operation = uuid.uuid4().hex
    result = await service.exec_program("sklearn", str(root), str(script), [], operation_id=operation)
    if result.status == "running":
        result = await service.wait_for_process("sklearn", 30, operation_id=operation)
    try:
        assert result.returncode == 0, result.output
        assert "sklearn-fit-and-json-ok" in result.output
        dependencies = result.program_execution["dependencies"]
        assert dependencies["state"] == "ready"
        assert next(p for p in dependencies["runtime"]["packages"] if p["module"] == "sklearn")["version"] == "1.7.2"
        receipt = validate_artifacts([{"path": str(artifact), "kind": "report"}], root=root)["files"][0]
        assert receipt["valid"] is True
        data = json.loads(artifact.read_text())
        assert data["coefficient"] == pytest.approx([2.0])
        assert data["uncertainty"]["status"] == "not_estimable"
    finally:
        await service.release_shell("sklearn", operation_id=operation)
