"""Host oracles and disposable Docker boundaries for synthetic Skill checks."""
import ast
import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from types import SimpleNamespace

import docker
import pytest

from app.domain.services.skills.skill_validation import ExpectedValue, SyntheticValidationCase, SyntheticValidationPlan
from app.infrastructure.external.sandbox import skill_validation_worker as module


def case(kind="normal", *, expected=4, output_type="csv", exit_code=0):
    assertion = ({"type": "csv_rows", "path": "summary.csv", "match": {"group": "A"}, "count": 1,
                  "fields": {"value": {"value": expected}}} if output_type == "csv" else
                 {"type": "json_value", "path": "summary.json", "pointer": "/value", "expected": {"value": expected}})
    return {"id": kind + "_case", "kind": kind, "description": "Independent synthetic numerical check",
            "requirement_ids": ["R001"], "source_ids": ["S001"],
            "oracle_explanation": "Two values sum to four; an empty aggregate remains missing.",
            "argv": ["--input", "{input:table.csv}", "--output", "{output}"],
            "input_files": [{"path": "table.csv", "format": "csv", "columns": ["group", "value"],
                             "rows": [{"group": "A", "value": 1}, {"group": "A", "value": 3}]}],
            "expected_exit_code": exit_code, "input_unchanged": True,
            "assertions": [assertion] if exit_code == 0 else []}


def plan(**kwargs):
    return SyntheticValidationPlan.model_validate({"version": 1, "status": "not_run", "scripts": [{
        "script_path": "scripts/summary.py", "support": "supported", "reason": "Small deterministic tabular oracle",
        "cases": [case(kind, **kwargs) for kind in ("normal", "boundary", "invalid")]}]})


class FakeContainer:
    def __init__(self, client, config):
        self.client, self.config = client, config
        self.attrs = {"Config": {"Labels": config["labels"]}}
        self.removed = self.started = False

    def put_archive(self, path, payload):
        assert path == "/validation"
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            self.client.inputs.update({member.name: archive.extractfile(member).read()
                                       for member in archive if member.isfile()})
        return True

    def exec_run(self, command, **kwargs):
        assert command[:3] == ["/app/.venv/bin/python", "-I", "-c"]
        assert command[3] == module._READ_FILE
        assert kwargs["user"] == module.RUNTIME_USER and kwargs["demux"] is True
        path = command[4] + "/" + command[5]
        if path.startswith("/validation/"):
            payload = self.client.inputs[path.removeprefix("/validation/")]
            if self.client.changed_input:
                payload += b"changed"
        elif path in self.client.outputs:
            payload = self.client.outputs[path]
        else:
            return SimpleNamespace(exit_code=2, output=(None, b"output_missing\n"))
        return SimpleNamespace(exit_code=0, output=(payload, None))

    def remove(self, **kwargs):
        self.removed = True


class FakeVolume:
    def __init__(self, labels):
        self.attrs = {"Labels": labels}
        self.removed = False

    def remove(self):
        self.removed = True


class FakeDocker:
    def __init__(self):
        self.inputs, self.created, self.created_volumes, self.requests = {}, [], [], []
        self.outputs = {module.OUTPUT_ROOT + "/summary.csv": b"group,value\nA,4\n", module.OUTPUT_ROOT + "/summary.json": b'{"value":4}'}
        self.changed_input, self.closed, self.exit_code = False, False, 0
        self.images = SimpleNamespace(get=lambda _: SimpleNamespace(attrs={"Config": {"Env": ["TOKEN=do-not-inherit", "HTTPS_PROXY=private"]}}))
        self.containers = SimpleNamespace(create=self.create, get=self.get)
        self.volumes = SimpleNamespace(create=self.volume, get=self.get_volume)

    def create(self, **config):
        result = FakeContainer(self, config)
        self.created.append(result)
        return result

    def get(self, name):
        return next(item for item in self.created if item.config["name"] == name)

    def volume(self, name, labels):
        result = FakeVolume(labels)
        self.created_volumes.append(result)
        return result

    def get_volume(self, name):
        return self.created_volumes[-1]

    def receipt(self, container, request, cancelled, deadline):
        container.started = True
        self.requests.append(request)
        return {"nonce": request["nonce"], "exit_code": self.exit_code, "reason": None, "stderr": ""}

    def close(self):
        self.closed = True


@pytest.fixture
def runtime(monkeypatch):
    client = FakeDocker()
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    monkeypatch.setattr(module, "_capture_receipt", client.receipt)
    monkeypatch.setattr(module, "helper_security_options", lambda: ["no-new-privileges:true"])
    return client


@pytest.mark.parametrize("actual,expected,csv_value,passed", [
    ("", None, True, True), ("0", None, True, False), ("NaN", None, True, False),
    (None, None, False, True), (0, None, False, False), ("", None, False, False),
    ("4.00001", 4, True, False), ("4.0", 4, True, True), ("NaN", 0, True, False),
    ("inf", 0, True, False), (float("nan"), 0, False, False), (True, 1, False, False),
    ("4", 4, False, False), (True, True, False, True), (1, True, False, False),
    ("9007199254740992", 9007199254740993, True, False), (9007199254740992, 9007199254740993, False, False),
])
def test_values_preserve_missing_and_number_semantics(actual, expected, csv_value, passed):
    assert module._same_value(actual, ExpectedValue(value=expected), csv_value=csv_value) is passed


def test_numeric_tolerance_is_host_computed():
    assert module._same_value("1.01", ExpectedValue(value=1, atol=.011), csv_value=True)
    assert not module._same_value("1.02", ExpectedValue(value=1, atol=.011), csv_value=True)


@pytest.mark.parametrize("payload", [b"group,value\nA,0\n", b"group,value\nA,NaN\n", b"group\nA\n",
                                      b"group,value\nA,\nA,\n", b"group,value,value\nA,,\n"])
def test_all_missing_regression_rejects_zero_nan_missing_columns_and_duplicate_rows(payload):
    item = SyntheticValidationCase.model_validate(case(expected=None))
    assert any(not check["passed"] for check in module._assertions(item, {"summary.csv": payload}))


def test_all_missing_empty_cell_passes_and_oracle_is_never_program_code():
    item = SyntheticValidationCase.model_validate(case(expected=None))
    assert all(check["passed"] for check in module._assertions(item, {"summary.csv": b"group,value\nA,\n"}))
    tree = ast.parse(module._SUPERVISOR)
    assert not any(isinstance(node, ast.Name) and node.id in {"eval", "exec"} for node in ast.walk(tree))
    assert "expected" not in module._SUPERVISOR


@pytest.mark.parametrize("payload", [b'{"value":0}', b'{}', b'{"value":NaN}', b'{"value":null,"value":null}'])
def test_json_missing_null_nonfinite_and_duplicate_keys_are_distinct(payload):
    item = SyntheticValidationCase.model_validate(case(expected=None, output_type="json"))
    assert any(not check["passed"] for check in module._assertions(item, {"summary.json": payload}))


def test_nonfinite_json_reports_fixed_actionable_code_for_script_repair():
    item = SyntheticValidationCase.model_validate(case(expected=None, output_type="json"))
    checks = module._assertions(item, {"summary.json": b'{"value":null,"nested":[NaN]}'})
    assert checks == [{"id": "assertion_1", "passed": False, "reason": "nonfinite_json"}]


def test_json_pointer_decodes_keys_and_rejects_expression():
    raw = case(output_type="json")
    raw["assertions"][0]["pointer"] = "/a~1b/~0value/0"
    item = SyntheticValidationCase.model_validate(raw)
    assert module._assertions(item, {"summary.json": b'{"a/b":{"~value":[4]}}'})[0]["passed"]


@pytest.mark.asyncio
async def test_runs_three_cases_without_user_data_network_credentials_or_expected_values(runtime):
    result = await module.validate_skill_scripts({"scripts/summary.py": "print('synthetic')"}, plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == "passed" and result["scope"] == "synthetic"
    assert len(result["cases"]) == 3 and all(item["input_unchanged"] for item in result["cases"])
    assert len(result["plan_sha256"]) == 64 and len(result["source_sha256"]["scripts/summary.py"]) == 64
    assert runtime.closed and all(item.removed for item in runtime.created + runtime.created_volumes)
    seed, *workers = runtime.created
    assert not seed.started
    assert all(worker.started for worker in workers)
    for container in runtime.created:
        config = container.config
        assert config["network_mode"] == "none" and config["read_only"]
        assert config["user"] == "1000:1000" and config["cap_drop"] == ["ALL"]
        assert config["security_opt"] == ["no-new-privileges:true"]
        assert config["environment"]["TOKEN"] == config["environment"]["HTTPS_PROXY"] == ""
        assert config["mounts"][0]["Type"] == "volume"
        assert config["mounts"][0]["ReadOnly"] is (container is not seed)
        assert not any(key in config for key in ("ports", "privileged", "volumes"))
    for request in runtime.requests:
        assert set(request) == {"script", "argv", "nonce"}
        assert "{input:" not in str(request["argv"])
        assert request["argv"][1].startswith("/validation/cases/")
        assert request["argv"][3] == module.OUTPUT_ROOT


def rejection_plan():
    value = plan().model_dump()
    value["scripts"][0]["cases"][2] = case("invalid", exit_code="nonzero")
    return SyntheticValidationPlan.model_validate(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("actual,passed", [(1, True), (2, True), (64, True), (0, False), (-9, False),
                                           (124, False), (125, False), (126, False), (127, False), (137, False)])
async def test_semantic_rejection_accepts_error_codes_but_not_success_signal_or_timeout(runtime, actual, passed):
    original = runtime.receipt
    def observed(container, request, cancelled, deadline):
        result = original(container, request, cancelled, deadline)
        if "s0-c2" in container.config["name"]:
            result["exit_code"] = actual
        return result
    # The fixture binds the receipt method before this test, so replace that
    # binding without altering the plan or the worker's verdict code.
    from unittest.mock import patch
    with patch.object(module, "_capture_receipt", side_effect=observed):
        result = await module.validate_skill_scripts({"scripts/summary.py": "print('synthetic')"},
                                                    rejection_plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert (result["status"] == "passed") is passed
    assert result["cases"][2]["expected_exit_code"] == "nonzero"
    assert result["cases"][2]["actual_exit_code"] == actual
    assert all(item["input_unchanged"] for item in result["cases"])


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["ModuleNotFoundError", "SyntaxError", "NameError", "MemoryError"])
async def test_runtime_breakage_cannot_pass_invalid_input_rejection(runtime, monkeypatch, error):
    original = runtime.receipt
    def broken(container, request, cancelled, deadline):
        result = original(container, request, cancelled, deadline)
        if "s0-c2" in container.config["name"]:
            result.update(exit_code=1, stderr=f"{error}: synthetic failure")
        return result
    monkeypatch.setattr(module, "_capture_receipt", broken)
    result = await module.validate_skill_scripts({"scripts/summary.py": "print('synthetic')"},
                                                rejection_plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == "failed"
    assert result["cases"][2]["reason"] == "program_runtime_failure"


@pytest.mark.parametrize("expected,actual,passed", [(2, 1, False), (2, 2, True), (0, 1, False), (0, 0, True)])
def test_explicit_exit_code_contract_is_never_broadened(expected, actual, passed):
    assert module._exit_matches(expected, actual) is passed


@pytest.mark.asyncio
async def test_zero_for_missing_fails_with_specific_host_expected_actual(runtime):
    runtime.outputs[module.OUTPUT_ROOT + "/summary.csv"] = b"group,value\nA,0\n"
    result = await module.validate_skill_scripts({"scripts/summary.py": "print(0)"}, plan(expected=None), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == "failed"
    failure = next(check for check in result["cases"][0]["checks"] if not check["passed"])
    assert failure["expected"] is None and failure["actual"] == "0"
    assert result["cases"][0]["script_path"] == "scripts/summary.py"


@pytest.mark.asyncio
async def test_modified_input_is_failure_even_when_outputs_match(runtime):
    runtime.changed_input = True
    result = await module.validate_skill_scripts({"scripts/summary.py": "print(4)"}, plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == "failed"
    assert all(item["reason"] == "synthetic_input_changed" for item in result["cases"])


@pytest.mark.asyncio
async def test_missing_output_cannot_pass_from_stdout_claim(runtime):
    runtime.outputs.clear()
    result = await module.validate_skill_scripts({"scripts/summary.py": "print('passed')"}, plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == "failed"
    assert result["cases"][0]["reason"] == "output_missing:summary.csv"


@pytest.mark.asyncio
async def test_transport_failure_is_unavailable_and_owned_resources_removed(runtime, monkeypatch):
    monkeypatch.setattr(module, "_capture_receipt", lambda *args: (_ for _ in ()).throw(ConnectionError("private docker address")))
    result = await module.validate_skill_scripts({"scripts/summary.py": "print(4)"}, plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == "unavailable"
    assert "private docker address" not in json.dumps(result)
    assert all(item.removed for item in runtime.created + runtime.created_volumes)


@pytest.mark.asyncio
async def test_image_unavailable_does_not_forge_pass_or_create_container(runtime):
    result = await module.validate_skill_scripts({"scripts/summary.py": "print(4)"}, plan(), SimpleNamespace(sandbox_image=None))
    assert result["status"] == "unavailable" and not runtime.created


@pytest.mark.asyncio
async def test_unsupported_does_not_touch_docker(runtime):
    raw = {"scripts": [{"script_path": "scripts/summary.py", "support": "unsupported",
                        "reason": "This imaging workflow needs a specialized fixture.", "cases": []}]}
    result = await module.validate_skill_scripts({"scripts/summary.py": "print(4)"}, raw, SimpleNamespace(sandbox_image=None))
    assert result["status"] == "unsupported" and not runtime.created


@pytest.mark.asyncio
async def test_wrong_script_binding_rejected_before_docker(runtime):
    with pytest.raises(ValueError):
        await module.validate_skill_scripts({"../outside.py": "print(4)"}, plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert not runtime.created


@pytest.mark.asyncio
async def test_cancellation_waits_for_worker_cleanup(runtime, monkeypatch):
    entered = threading.Event()
    def wait(container, request, cancelled, deadline):
        entered.set()
        while not cancelled.wait(.01):
            pass
        raise module._Cancelled()
    monkeypatch.setattr(module, "_capture_receipt", wait)
    running = asyncio.create_task(module.validate_skill_scripts({"scripts/summary.py": "print(4)"}, plan(), SimpleNamespace(sandbox_image="existing:sandbox")))
    await asyncio.to_thread(entered.wait, 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert all(item.removed for item in runtime.created + runtime.created_volumes)


@pytest.mark.parametrize("link_parent", [False, True])
def test_fixed_reader_rejects_leaf_and_ancestor_symlinks(tmp_path, link_parent):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.csv").write_text("private")
    output = tmp_path / "output"
    output.mkdir()
    if link_parent:
        (output / "alias").symlink_to(outside, target_is_directory=True)
        relative = "alias/result.csv"
    else:
        (output / "result.csv").symlink_to(outside / "result.csv")
        relative = "result.csv"
    result = subprocess.run([sys.executable, "-I", "-c", module._READ_FILE, str(output), relative], capture_output=True)
    assert result.returncode == 2 and result.stdout == b"" and b"output_unsafe" in result.stderr


def test_scientific_pythonpath_is_fixed_not_inherited():
    assert '"PYTHONPATH":"/usr/local/lib/ai-dataseek:/app/.venv/lib/python3.10/site-packages:/usr/lib/python3/dist-packages"' in module._SUPERVISOR


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_delayed_cleanup(runtime, monkeypatch):
    entered, cleaning, finish = threading.Event(), threading.Event(), threading.Event()
    def wait(container, request, cancelled, deadline):
        entered.set()
        cancelled.wait(2)
        cleaning.set()
        finish.wait(2)
        raise module._Cancelled()
    monkeypatch.setattr(module, "_capture_receipt", wait)
    running = asyncio.create_task(module.validate_skill_scripts({"scripts/summary.py": "print(4)"}, plan(), SimpleNamespace(sandbox_image="existing:sandbox")))
    await asyncio.to_thread(entered.wait, 2)
    running.cancel()
    await asyncio.to_thread(cleaning.wait, 2)
    running.cancel()
    await asyncio.sleep(.01)
    assert not running.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert all(item.removed for item in runtime.created + runtime.created_volumes)


def test_json_fixture_role_mapping_is_data_only():
    raw = case()
    raw["input_files"].append({"path": "mapping.json", "format": "json", "data": {"role": "{input:table.csv}"}})
    value = module._materialize(SyntheticValidationCase.model_validate(raw), "/validation/case")
    assert json.loads(value["mapping.json"])["role"] == "/validation/case/table.csv"


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1", reason="Opt-in disposable sandbox integration")
@pytest.mark.asyncio
async def test_real_existing_sandbox_runs_synthetic_helper_and_reads_actual_files(monkeypatch):
    monkeypatch.setattr(module, "helper_security_options", lambda: ["no-new-privileges:true"])
    source = '''import argparse,csv,json,pathlib
from datalab_json import write_json
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
rows=list(csv.DictReader(open(a.input)));value=sum(float(row['value']) for row in rows)
write_json(pathlib.Path(a.output,'summary.json'),{'value':value})
'''
    result = await module.validate_skill_scripts({"scripts/summary.py": source}, plan(output_type="json"),
        SimpleNamespace(sandbox_image=os.environ.get("DATALAB_SYNTHETIC_TEST_IMAGE", "datalab-sandbox:analysis-recovery-20261022")))
    assert result["status"] == "passed", json.dumps(result)


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1", reason="Opt-in disposable sandbox integration")
@pytest.mark.asyncio
async def test_real_readonly_inputs_and_wrong_missing_value_rejected(monkeypatch):
    monkeypatch.setattr(module, "helper_security_options", lambda: ["no-new-privileges:true"])
    source = '''import argparse,json,pathlib
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
try:
 pathlib.Path(a.input).write_text('tampered')
except OSError:
 pass
pathlib.Path(a.output,'summary.json').write_text(json.dumps({'value':0}))
print('passed')
'''
    result = await module.validate_skill_scripts({"scripts/summary.py": source}, plan(output_type="json", expected=None),
        SimpleNamespace(sandbox_image=os.environ.get("DATALAB_SYNTHETIC_TEST_IMAGE", "datalab-sandbox:analysis-recovery-20261022")))
    assert result["status"] == "failed", json.dumps(result)
    assert all(item["input_unchanged"] and item["reason"] == "assertion_mismatch" for item in result["cases"])
    assert all(item["checks"][0]["expected"] is None and item["checks"][0]["actual"] == 0 for item in result["cases"])
