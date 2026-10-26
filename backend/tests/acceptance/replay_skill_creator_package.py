"""Recheck an accepted artificial package against a deployed worker, no model.

The artifact must be the unmodified final-package record from this acceptance
suite. Scripts, fixtures, expectations and tolerances are replayed byte-for-byte;
the original host-recorded script/plan digests must match the deployed receipt.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys


BOOTSTRAP = '''import asyncio,json,sys
from app.infrastructure.external.sandbox.skill_validation_worker import validate_skill_scripts
request=json.load(sys.stdin)
receipt=asyncio.run(validate_skill_scripts(request["scripts"],request["plan"]))
print("RECEIPT:"+json.dumps(receipt,ensure_ascii=False),flush=True)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--container", default="datalab-backend-1")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    artifact = json.loads(args.artifact.read_text())
    if artifact.get("kind") != "package" or artifact.get("stage") != "final":
        raise SystemExit("Expected an original final-package acceptance artifact")
    package = artifact["content"]
    previous = package["synthetic_execution"]
    if previous.get("status") != "passed":
        raise SystemExit("Only already accepted artificial packages can be replayed")
    request = {"scripts": package["scripts"],
               "plan": json.loads(package["references"]["references/synthetic-validation.json"])}
    result = subprocess.run(["docker", "exec", "-i", args.container, "python", "-B", "-c", BOOTSTRAP],
        input=json.dumps(request, ensure_ascii=False), text=True, capture_output=True)
    receipts = [line[len("RECEIPT:"):] for line in result.stdout.splitlines() if line.startswith("RECEIPT:")]
    if result.returncode or len(receipts) != 1:
        raise SystemExit("Deployed replay did not return a valid receipt; exit=" + str(result.returncode))
    current = json.loads(receipts[0])
    args.receipt.write_text(json.dumps(current, ensure_ascii=False, indent=2))
    args.receipt.chmod(0o600)
    assert current["source_sha256"] == previous["source_sha256"], "Helper source changed"
    assert current["plan_sha256"] == previous["plan_sha256"], "Frozen independent plan changed"
    previous_ids = {(case["script_path"], case["id"]) for case in previous["cases"]}
    current_ids = {(case["script_path"], case["id"]) for case in current["cases"]}
    assert current_ids == previous_ids, "The selected test cases changed"
    assert current["status"] == "passed"
    assert all(case["status"] == "passed" and case["input_unchanged"] for case in current["cases"])
    print(json.dumps({"kind": "deployed_replay", "success": True, "cases": len(current["cases"]),
                      "assertions": sum(len(case.get("checks", [])) for case in current["cases"]),
                      "input_unchanged": True, "original_sources_and_oracle_unchanged": True,
                      "model_called": False, "receipt": str(args.receipt)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
