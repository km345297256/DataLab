"""Overlay current application source in an ephemeral directory, never deploy it.

Explicitly invokes the configured model using authored artificial evidence only.
Keeps complete synthetic exchanges in a private temporary directory; prints
compact stage receipts. Use after deterministic checks and owner coordination.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

from skill_creator_checkpoint_state import load_private_state, save_private_state
from skill_creator_replay_binding import load_bound_prefix


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=("join", "paired", "conversion", "complex", "text", "document"))
    parser.add_argument("--container", default="datalab-backend-1")
    parser.add_argument("--resume-prefix", type=Path, help="Explicit manifest of unchanged artificial response artifacts to revalidate")
    parser.add_argument("--replay-only", action="store_true", help="Never call the provider after the saved response prefix")
    parser.add_argument("--resume-prefix-until-change", action="store_true", help="For bound exchanges only, stop reusing at the first complete request mismatch")
    parser.add_argument("--checkpoint-state", type=Path, help="Private artificial-only state file for a separately invoked continuation")
    args = parser.parse_args()
    if args.replay_only and not (args.resume_prefix or args.checkpoint_state):
        parser.error("--replay-only requires --resume-prefix or --checkpoint-state")
    if args.resume_prefix_until_change and (args.replay_only or not args.resume_prefix):
        parser.error("--resume-prefix-until-change requires a bound prefix and fresh-call mode")
    root = Path(__file__).resolve().parents[2]
    archive = io.BytesIO()
    checkpoint_state = load_private_state(args.checkpoint_state, args.scenario) if args.checkpoint_state else None
    replay = None
    if args.resume_prefix:
        manifest = json.loads(args.resume_prefix.read_text())
        assert manifest["scenario"] == args.scenario
        if manifest.get("binding") == "complete_request_v1":
            replay = load_bound_prefix(manifest, args.scenario)
        else:
            assert not args.resume_prefix_until_change, "Unbound legacy prefixes cannot authorize changed requests"
            replay = []
            for source in manifest["response_artifacts"]:
                origin = Path(source)
                saved = json.loads(origin.read_text())
                assert saved["kind"] == "response" and isinstance(saved["content"], str)
                replay.append({"stage": saved["stage"], "content": saved["content"],
                               "response_sha256": hashlib.sha256(saved["content"].encode()).hexdigest(),
                               "origin_artifact": str(origin)})
        assert replay
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for folder in ("app", "skills"):
            for path in sorted((root / folder).rglob("*")):
                if (path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                        and path.suffix not in (".pyc", ".pyo") and not path.name.startswith(".env")):
                    bundle.add(path, arcname=str(path.relative_to(root)), recursive=False)
        for relative in ("tests/skill_creator_scenarios.py", "tests/skill_creator_complex_scenario.py",
                         "tests/skill_creator_generic_scenarios.py",
                         "tests/acceptance/skill_creator_real_model.py", "tests/acceptance/check_skill_creator_hand_oracle.py",
                         "tests/acceptance/skill_creator_checkpoint_state.py", "tests/acceptance/skill_creator_oracle_audit.py"):
            bundle.add(root / relative, arcname=relative, recursive=False)
        bundle.add(root / "tests/acceptance/skill_creator_replay_binding.py",
                   arcname="tests/acceptance/skill_creator_replay_binding.py", recursive=False)
        if checkpoint_state is not None:
            data = json.dumps(checkpoint_state, ensure_ascii=False).encode()
            info = tarfile.TarInfo("checkpoint-state.json")
            info.size, info.mode = len(data), 0o600
            bundle.addfile(info, io.BytesIO(data))
        if replay is not None:
            data = json.dumps(replay, ensure_ascii=False).encode()
            info = tarfile.TarInfo("resume-prefix.json")
            info.size, info.mode = len(data), 0o600
            bundle.addfile(info, io.BytesIO(data))
    bootstrap = '''import tempfile,tarfile,io,sys,runpy,os
with tempfile.TemporaryDirectory(prefix="datalab-creator-matrix-") as root:
 with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode="r:gz") as bundle: bundle.extractall(root,filter="data")
 sys.path.insert(0,root)
 os.environ["PYTHONDONTWRITEBYTECODE"]="1"
 sys.argv=[root+"/tests/acceptance/skill_creator_real_model.py",SCENARIO]
 if REPLAY: sys.argv += ["--resume-prefix",root+"/resume-prefix.json"]
 if SAVED_ONLY: sys.argv += ["--replay-only"]
 if CHECKPOINTS: sys.argv += ["--checkpoint-state",root+"/checkpoint-state.json"]
 if UNTIL_CHANGE: sys.argv += ["--resume-prefix-until-change"]
 runpy.run_path(sys.argv[0],run_name="__main__")'''.replace("SCENARIO", repr(args.scenario))
    bootstrap = bootstrap.replace("REPLAY", repr(replay is not None))
    bootstrap = bootstrap.replace("SAVED_ONLY", repr(args.replay_only))
    bootstrap = bootstrap.replace("CHECKPOINTS", repr(checkpoint_state is not None))
    bootstrap = bootstrap.replace("UNTIL_CHANGE", repr(args.resume_prefix_until_change))
    artifact_root = Path(tempfile.mkdtemp(prefix="datalab-creator-matrix-" + args.scenario + "-"))
    snapshot = artifact_root / "source-overlay.tar.gz"
    snapshot.write_bytes(archive.getvalue())
    snapshot.chmod(0o600)
    print(json.dumps({"kind": "diagnostic_directory", "scenario": args.scenario, "path": str(artifact_root)}), flush=True)
    print(json.dumps({"kind": "overlay_snapshot", "scenario": args.scenario,
                      "path": str(snapshot),
                      "archive_sha256": hashlib.sha256(archive.getvalue()).hexdigest(),
                      "bytes": len(archive.getvalue())}), flush=True)
    process = subprocess.Popen(["docker", "exec", "-i", args.container, "python", "-B", "-c", bootstrap],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    process.stdin.write(archive.getvalue())
    process.stdin.close()
    count = 0
    for line in process.stdout:
        value = line.decode("utf-8", "replace")
        if value.startswith("ARTIFACT:"):
            artifact = json.loads(value[len("ARTIFACT:"):])
            count += 1
            # Stage/kind originate from application enums and this harness.
            # Restrict filenames even if a future adapter changes that contract.
            stage = "".join(ch for ch in str(artifact["stage"]) if ch.isalnum() or ch in "_-")
            kind = "".join(ch for ch in str(artifact["kind"]) if ch.isalnum() or ch in "_-")
            target = artifact_root / f"{count:03d}-{stage}-{kind}.json"
            target.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
            target.chmod(0o600)
            if artifact["kind"] == "checkpoint_state":
                assert args.checkpoint_state is not None
                save_private_state(args.checkpoint_state, artifact["content"], args.scenario)
        else:
            print(value, end="", flush=True)
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
