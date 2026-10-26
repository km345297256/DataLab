"""Run frozen artificial TEXT/XML examples after real creation has succeeded.

This test-only executable never calls a model or stores/installs a skill. Public
CLI and output filenames are explicit bindings; all input/expected values come
from the previously frozen private fixture manifest.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from acceptance.check_skill_creator_hand_oracle import json_assertions
from app.domain.services.skills.skill_validation import SyntheticValidationPlan


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    path.chmod(0o600)


def decode_receipt(stdout):
    # JSON may contain literal U+0085/U+2028/U+2029. Those belong to a text
    # result, not this transport's physical LF-delimited record framing.
    lines = [line[len("RECEIPT:"):] for line in stdout.decode().split("\n") if line.startswith("RECEIPT:")]
    assert len(lines) == 1, "Worker did not return one complete receipt"
    return json.loads(lines[0])


def finish(receipt, request, package, fixture, fixed, args, overlay_sha, *, recovered_transport=False):
    dump(args.output_dir / "worker-receipt.json", receipt)
    expected_sources = {path: sha(source.encode()) for path, source in request["scripts"].items()}
    assert receipt["source_sha256"] == expected_sources == package["synthetic_execution"]["source_sha256"]
    assert receipt["plan_sha256"] == sha(json.dumps(request["plan"], ensure_ascii=False, sort_keys=True,
                                                     allow_nan=False).encode())
    assert [case["id"] for case in receipt["cases"]] == [case["id"] for case in fixture["cases"]]
    summary = {"scenario": args.scenario, "status": receipt["status"], "cases": len(receipt["cases"]),
        "passed": sum(case["status"] == "passed" for case in receipt["cases"]),
        "checks": sum(len(case.get("checks", [])) for case in receipt["cases"]),
        "passed_checks": sum(check.get("passed") is True for case in receipt["cases"] for check in case.get("checks", [])),
        "all_inputs_unchanged": all(case["input_unchanged"] is True for case in receipt["cases"]),
        "source_sha256": expected_sources, "plan_sha256": receipt["plan_sha256"],
        "artifact_sha256": sha(args.artifact.read_bytes()), "frozen_fixture_sha256": fixed["sha256"],
        "receipt_sha256": sha((args.output_dir / "worker-receipt.json").read_bytes()),
        "overlay_archive_sha256": overlay_sha, "container": args.container,
        "transport_recovered_from_original_stdout": recovered_transport,
        "model_called": False, "skill_saved": False, "skill_installed": False}
    dump(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    assert summary["status"] == "passed" and summary["passed"] == summary["cases"] and summary["all_inputs_unchanged"]


def build_request(package, fixture, output_path, output_argument):
    production_plan = json.loads(package["references"]["references/synthetic-validation.json"])
    assert len(package["scripts"]) == len(production_plan["scripts"]) == 1
    source_plan = production_plan["scripts"][0]
    script_path, = package["scripts"]
    assert source_plan["script_path"] == script_path
    # Binding is to a real successful public invocation and declared JSON sink,
    # never inferred from a desired output value or candidate runtime result.
    normal = [case for case in source_plan["cases"] if case["kind"] == "normal"]
    assert normal
    binding = next(case for case in normal
                   if case["argv"][case["argv"].index("--output") + 1] == output_argument
                   and any(item.get("path") == output_path and item["type"].startswith("json_")
                           for item in case["assertions"]))
    assert len(binding["argv"]) == 4 and binding["argv"][::2] == ["--input", "--output"], (
        "Bind any additional public CLI requirements explicitly; never compensate for JSON options")
    contract = json.loads(package["references"]["references/method-contract.json"])
    requirements = contract["requirements"]
    requirement_ids = [item["id"] for item in requirements]
    source_ids = sorted({source for item in requirements for source in item["source_ids"]})
    cases = []
    for case in fixture["cases"]:
        assertions = [] if case["reject"] else json_assertions(output_path, case["expected"])
        exact_keys = fixture["binding_policy"]["text_result_keys_exact"]
        if not case["reject"] and exact_keys:
            assertions.append({"type": "json_object_keys", "path": output_path, "pointer": "", "keys": exact_keys})
        cases.append({"id": case["id"], "kind": case["kind"],
            "description": "Independent pre-authored contract example, kept separate from creator validation.",
            "requirement_ids": requirement_ids, "source_ids": source_ids,
            "oracle_explanation": "Literal expected values and rejection outcomes were frozen independently before inspecting the generated candidate. No candidate outputs or model answers determine them.",
            "argv": ["--input", "{input:independent.json}", "--output", output_argument],
            "input_files": [{"path": "independent.json", "format": "json", "data": case["input"]}],
            "expected_exit_code": "nonzero" if case["reject"] else 0, "input_unchanged": True,
            "assertions": assertions})
    plan = SyntheticValidationPlan.model_validate({"version": 1, "status": "not_run", "scripts": [{
        "script_path": script_path, "support": "supported",
        "reason": "Independent text or XML contract inputs require only isolated JSON file execution.", "cases": cases}]})
    return {"scripts": package["scripts"], "plan": plan.model_dump(mode="json")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=("text", "document"))
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--completion-log", type=Path, required=True)
    parser.add_argument("--frozen-manifest", type=Path, required=True)
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--output-argument", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    events = [json.loads(line) for line in args.completion_log.read_text().splitlines() if line.startswith("{")]
    complete = [event for event in events if event.get("kind") == "complete" and event.get("scenario") == args.scenario]
    assert len(complete) == 1 and complete[0]["success"] is True
    document = json.loads(args.artifact.read_text())
    assert document["kind"] == "package" and document["stage"] == "final"
    package = document["content"]
    assert package["synthetic_execution"]["status"] == "passed"
    review = package["synthetic_execution"]["oracle_review"]
    assert review["status"] == "reviewed" and review["verdict"] == "accepted"
    manifest = json.loads(args.frozen_manifest.read_text())
    fixed = manifest["files"][args.scenario]
    raw = Path(fixed["path"]).read_bytes()
    assert sha(raw) == fixed["sha256"]
    fixture = json.loads(raw)
    assert fixture["scenario"] == args.scenario and fixture["model_oracle_used"] is False
    request = build_request(package, fixture, args.json_output, args.output_argument)
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    dump(args.output_dir / "request.json", request)
    root = Path(__file__).resolve().parents[2]
    archive = io.BytesIO()
    source_hashes = {}
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for path in sorted((root / "app").rglob("*")):
            if (path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                    and path.suffix not in (".pyc", ".pyo") and not path.name.startswith(".env")):
                source_hashes[str(path.relative_to(root))] = sha(path.read_bytes())
                bundle.add(path, arcname=str(path.relative_to(root)), recursive=False)
        data = json.dumps(request, ensure_ascii=False).encode()
        info = tarfile.TarInfo("artificial-request.json")
        info.size, info.mode = len(data), 0o600
        bundle.addfile(info, io.BytesIO(data))
    snapshot = args.output_dir / "source-overlay.tar.gz"
    snapshot.write_bytes(archive.getvalue()); snapshot.chmod(0o600)
    dump(args.output_dir / "overlay-source-sha256.json", source_hashes)
    bootstrap = '''import asyncio,io,json,sys,tarfile,tempfile
with tempfile.TemporaryDirectory(prefix="datalab-independent-generic-") as root:
 with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode="r:gz") as bundle: bundle.extractall(root,filter="data")
 sys.path.insert(0,root)
 from app.infrastructure.external.sandbox.skill_validation_worker import validate_skill_scripts
 request=json.load(open(root+"/artificial-request.json"))
 receipt=asyncio.run(validate_skill_scripts(request["scripts"],request["plan"]))
 print("RECEIPT:"+json.dumps(receipt,ensure_ascii=False),flush=True)
'''
    process = subprocess.run(["docker", "exec", "-i", args.container, "python", "-B", "-c", bootstrap],
                             input=archive.getvalue(), capture_output=True)
    for name, value in (("worker-stdout.txt", process.stdout), ("worker-stderr.txt", process.stderr)):
        path = args.output_dir / name; path.write_bytes(value); path.chmod(0o600)
    assert process.returncode == 0, "Worker process did not complete successfully"
    finish(decode_receipt(process.stdout), request, package, fixture, fixed, args, sha(archive.getvalue()))


if __name__ == "__main__":
    main()
