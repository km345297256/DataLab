"""Explicit artificial real-model acceptance in disposable sandbox containers.

The backend's existing configured credentials stay inside its process. No model
keys, user data, DB state or production environment files enter source archives.
Application/helper source is the treatment; model/scientific runtime/tasks and
the harness must match before a comparison is accepted. Input fixtures are
created here and mounted read-only. Nothing is committed or deployed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import uuid

from analysis_code_cases import CASES, VERSION, canonical, case_identity, compare_reports, summarize


HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[2]
HARNESS_FILES = ("analysis_code_cases.py", "analysis_code_real_model.py", "run_analysis_code_real_model.py")
API_SERVER = '''from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from app.api.v1.file import router as file_router
from app.api.v1.shell import router as shell_router
from app.core.exceptions import AppException,app_exception_handler,general_exception_handler,http_exception_handler,validation_exception_handler
from app.services.analysis_workspace import analysis_workspace_lifespan
app=FastAPI(lifespan=analysis_workspace_lifespan)
app.add_exception_handler(AppException,app_exception_handler)
app.add_exception_handler(HTTPException,http_exception_handler)
app.add_exception_handler(RequestValidationError,validation_exception_handler)
app.add_exception_handler(Exception,general_exception_handler)
app.include_router(file_router,prefix="/api/v1/file")
app.include_router(shell_router,prefix="/api/v1/shell")
'''


def digest_files(files):
    return hashlib.sha256(canonical({name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())})).hexdigest()


def docker(*arguments, data=None):
    run = subprocess.run(["docker", *arguments], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        # Docker diagnostics may contain backend environment fragments. Do not
        # echo them into a shared transcript; retain a fixed safe failure.
        raise RuntimeError("Acceptance Docker operation failed: " + arguments[0])
    return run.stdout


def source_files(source):
    files = {}
    for folder in ("backend/app", "sandbox/app", "sandbox/scripts", "sandbox/scientific_operators"):
        for path in sorted((source/folder).rglob("*")):
            if (path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                    and path.suffix not in {".pyc", ".pyo"} and not path.name.startswith(".env")):
                files[str(path.relative_to(source))] = path.read_bytes()
    if not files or "backend/app/domain/services/agents/execution.py" not in files:
        raise ValueError("A complete application source snapshot is required")
    return files


def archive(entries):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as bundle:
        for name, content in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.mode, info.size, info.mtime = 0o644, len(content), 0
            bundle.addfile(info, io.BytesIO(content))
    return result.getvalue()


def save_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as out:
        os.chmod(path, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")


def scientific_environment(container):
    code = """import importlib.metadata,json,platform
print(json.dumps({'python':platform.python_version(),'packages':sorted((d.metadata.get('Name',''),d.version) for d in importlib.metadata.distributions())},sort_keys=True))"""
    return json.loads(docker("exec", container, "/app/.venv/bin/python", "-c", code))


def run_one(args, files, harness, case, repeat, base_image_id, network):
    name = "datalab-analysis-acceptance-" + uuid.uuid4().hex[:12]
    # Colima shares the user's workspace, but need not share macOS TMPDIR.
    fixture_parent = WORKSPACE/".local"/"analysis-acceptance-inputs"
    fixture_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fixture-", dir=fixture_parent) as directory:
        root = Path(directory).resolve()
        fixture = root/"observations.csv"
        fixture.write_text(CASES[case]["csv"], encoding="utf-8")
        fixture.chmod(0o444)
        config = {"case": case, "repeat": repeat, "sandbox_name": name,
                  "sandbox_image": args.sandbox_image, "sandbox_image_id": base_image_id,
                  "timeout_seconds": args.timeout_seconds}
        created = False
        try:
            docker("create", "--name", name, "--network", network,
                   "--label", "datalab.artificial-acceptance=true", "--memory", "2g", "--pids-limit", "256",
                   "--mount", "type=bind,src="+str(root)+",dst=/home/ubuntu/inputs/regression,readonly",
                   "-w", "/app", "--entrypoint", "/app/.venv/bin/python", args.sandbox_image,
                   "-m", "uvicorn", "acceptance_api:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "error")
            created = True
            # Only repository source is overlaid. Runtime packages are exactly
            # the same base image for both variants; no installation occurs.
            sandbox_entries = {"app/acceptance_api.py": API_SERVER.encode()}
            for path, content in files.items():
                if path.startswith("sandbox/"):
                    relative = path[len("sandbox/"):]
                    sandbox_entries["app/"+relative] = content
                    if relative.startswith("scripts/") and relative.endswith(".py"):
                        sandbox_entries["usr/local/lib/ai-dataseek/"+relative[len("scripts/"):]] = content
            docker("cp", "-", name+":/", data=archive(sandbox_entries))
            docker("start", name)
            runtime = scientific_environment(name)
            backend_entries = {path[len("backend/"):]: content for path, content in files.items() if path.startswith("backend/")}
            for filename in HARNESS_FILES[:2]:
                backend_entries["tests/acceptance/"+filename] = harness[filename]
            backend_entries["acceptance-config.json"] = canonical(config)
            bootstrap = """import tempfile,tarfile,io,sys,runpy,os
with tempfile.TemporaryDirectory(prefix='datalab-analysis-acceptance-') as root:
 with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode='r:gz') as bundle: bundle.extractall(root,filter='data')
 sys.path.insert(0,root)
 os.environ['PYTHONDONTWRITEBYTECODE']='1'
 sys.argv=[root+'/tests/acceptance/analysis_code_real_model.py',root+'/acceptance-config.json']
 runpy.run_path(sys.argv[0],run_name='__main__')"""
            output = docker("exec", "-i", args.container, "/opt/venv/bin/python", "-B", "-c", bootstrap,
                            data=archive(backend_entries)).decode("utf-8", "replace")
            lines = [line[len("ACCEPTANCE:"):] for line in output.splitlines() if line.startswith("ACCEPTANCE:")]
            if len(lines) != 1:
                raise RuntimeError("Acceptance worker did not return exactly one receipt")
            result = json.loads(lines[0])
            artifact_root = args.output.parent/(args.output.stem+"-artifacts")/(case+"-"+str(repeat))
            artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for line in output.splitlines():
                if not line.startswith("ARTIFACT:"):
                    continue
                item = json.loads(line[len("ARTIFACT:"):])
                filename = item["name"]
                if Path(filename).name != filename or filename.startswith("."):
                    raise RuntimeError("Invalid artificial artifact name")
                content = base64.b64decode(item["base64"], validate=True)
                if len(content) > 2*1024*1024:
                    raise RuntimeError("Artificial artifact exceeded the bounded capture size")
                path = artifact_root/filename
                path.write_bytes(content)
                path.chmod(0o600)
            result["input_unchanged"] = fixture.read_text() == CASES[case]["csv"]
            return result, runtime
        finally:
            if created:
                docker("rm", "-f", name)  # Only the uniquely named container created above.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=WORKSPACE)
    parser.add_argument("--container", default="datalab-backend-1")
    parser.add_argument("--sandbox-image", default="datalab-sandbox:code-flow-20261026")
    parser.add_argument("--variant", default="candidate")
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES), default=list(CASES))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=900,
                        help="Explicit test-run deadline only; production execution policies are unchanged")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BASELINE", "CANDIDATE"))
    args = parser.parse_args()
    if args.compare:
        result = compare_reports(*(json.loads(p.read_text()) for p in args.compare))
        if args.output:
            save_private(args.output, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.repeat < 1 or args.timeout_seconds < 1 or not args.output:
        parser.error("--output, positive --repeat and positive --timeout-seconds are required")
    if len(set(args.cases)) != len(args.cases):
        parser.error("Cases must be unique")
    files = source_files(args.source_root.resolve())
    harness = {name: (HERE/name).read_bytes() for name in HARNESS_FILES}
    identity = json.loads(docker("inspect", args.container, "--format", "{{json .NetworkSettings.Networks}}"))
    networks = sorted(key for key in identity if key.startswith("datalab"))
    if len(networks) != 1:
        raise RuntimeError("Expected exactly one existing DataLab application network")
    base_image_id = docker("image", "inspect", args.sandbox_image, "--format", "{{.Id}}").decode().strip()
    # Source treatment hashes are recorded separately from comparability.
    report = {"kind": "real_model_analysis_acceptance/v1", "variant": args.variant, "completed": False,
              "source_sha256": digest_files(files), "cases": [],
              "scope": "production ExecutionAgent and native tools; in-memory persistence; artificial read-only inputs"}
    common = {"case_version": VERSION, "cases": [(name, case_identity(name)) for name in args.cases],
              "repeat": args.repeat, "timeout_seconds": args.timeout_seconds,
              "harness_sha256": digest_files(harness),
              "model_adapter_sha256": digest_files({name: data for name, data in files.items()
                  if name.startswith("backend/app/infrastructure/external/llm/") or name in {
                      "backend/app/domain/services/model_route.py", "backend/app/domain/services/model_input_policy.py"}}),
              "sandbox_base_image_id": base_image_id}
    for repeat in range(1, args.repeat+1):
        for case in args.cases:
            print(json.dumps({"status": "case_started", "variant": args.variant, "case": case, "repeat": repeat}), flush=True)
            result, runtime = run_one(args, files, harness, case, repeat, base_image_id, networks[0])
            binding = {**common, "model_configuration": result["model_configuration"],
                       "scientific_runtime": runtime, "observed_model_names": result["observed_model_names"]}
            if report.get("comparison_identity") is not None and report["comparison_identity"] != binding:
                raise RuntimeError("Model or runtime identity changed within acceptance run")
            report["comparison_identity"] = binding
            report["cases"].append(result)
            report["summary"] = summarize(report["cases"])
            save_private(args.output, report)
            print(json.dumps({"status": "case_finished", "variant": args.variant, **result}), flush=True)
    report["completed"] = True
    save_private(args.output, report)
    return 1 if any(item.get("harness_error") for item in report["cases"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
