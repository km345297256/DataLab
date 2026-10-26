"""Add one independent, pre-authored numerical oracle to an accepted package.

CLI and output filenames come from the accepted public interface; scientific
data/expected values come solely from skill_creator_scenarios, authored before
any model request. Existing frozen cases are retained unchanged.
"""
import argparse
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skill_creator_scenarios import SCENARIOS, PAIRED_MIXED_SIGN_INPUT, PAIRED_MIXED_SIGN_EXPECTED


def json_assertions(path, expected, pointer=""):
    if isinstance(expected, dict):
        return [item for key, value in expected.items() for item in json_assertions(path, value, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))]
    if isinstance(expected, list):
        return [{"type": "json_array_length", "path": path, "pointer": pointer, "length": len(expected)}] + [
            item for index, value in enumerate(expected)
            for item in json_assertions(path, value, pointer + "/" + str(index))]
    tolerance = {"atol": 1e-9, "rtol": 1e-9} if isinstance(expected, float) else {}
    return [{"type": "json_value", "path": path, "pointer": pointer, "expected": {"value": expected, **tolerance}}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=tuple(SCENARIOS))
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--container", default="datalab-backend-1")
    parser.add_argument("--extra-argv", help="JSON array binding public field/output/scale flags to the pre-authored example")
    args = parser.parse_args()
    scenario = SCENARIOS[args.scenario]
    original = json.loads(args.artifact.read_text())
    package = copy.deepcopy(original["content"])
    assert original["kind"] == "package" and package["synthetic_execution"]["status"] == "passed"
    reviewed = package["synthetic_execution"]["oracle_review"]
    assert reviewed["status"] == "reviewed" and reviewed["verdict"] == "accepted"
    assert reviewed["plan_sha256"] == package["synthetic_execution"]["plan_sha256"]
    plan = json.loads(package["references"]["references/synthetic-validation.json"])
    assert len(plan["scripts"]) == 1, "Bind each public interface explicitly if a future case has multiple helpers"
    script = plan["scripts"][0]
    contract = json.loads(package["references"]["references/method-contract.json"])
    # The production package wraps the complete method contract alongside its
    # coverage. Its scientific statements and sources remain unchanged.
    requirements = contract.get("requirements") or contract["contract"]["requirements"]
    normal = next(case for case in script["cases"] if case["kind"] == "normal")
    paths = {kind: sorted({assertion["path"] for assertion in normal["assertions"] if assertion["type"] == kind})
             for kind in ("json_value", "csv_rows")}
    assert len(paths["json_value"]) == 1, "Expected one declared JSON output; do not guess an output mapping"
    argv = ["--input", "{input:host-hand.json}", "--output", normal["argv"][normal["argv"].index("--output") + 1]]
    extra_argv = json.loads(args.extra_argv) if args.extra_argv else []
    assert isinstance(extra_argv, list) and all(isinstance(value, str) for value in extra_argv)
    assert not any(value in {"--input", "--output"} for value in extra_argv)
    argv.extend(extra_argv)
    if args.scenario == "conversion":
        assert len(paths["csv_rows"]) == 1
        # Bind names from the actual public CLI rather than assume one spelling
        # for generated helpers. Scientific inputs and expected values stay fixed.
        assert args.extra_argv, "Explicitly bind the conversion helper's field/output/scale flags"
        assertions = [{"type": "csv_rows", "path": paths["csv_rows"][0], "match": {"sample_id": row["sample_id"]},
                       "count": 1, "fields": {key: {"value": value} for key, value in row.items()}}
                      for row in scenario.observed["rows"]]
        assertions += json_assertions(paths["json_value"][0], scenario.observed["quality"])
    else:
        assertions = json_assertions(paths["json_value"][0], scenario.observed)
        if args.scenario == "complex":
            assert len(paths["csv_rows"]) == 1, "The complete study must validate its CSV as well as its JSON report"
            csv_path = paths["csv_rows"][0]
            assertions.append({"type": "csv_rows", "path": csv_path, "match": {},
                               "count": len(scenario.observed["pivot"]["rows"]), "fields": {}})
            assertions.extend({"type": "csv_rows", "path": csv_path, "match": {"id": row["id"]}, "count": 1,
                               "fields": {key: {"value": value} for key, value in row.items()}}
                              for row in scenario.observed["pivot"]["rows"])
    added = {"id": "host-independent-hand-calculation", "kind": "normal",
             "description": "Independent authored small example created before any model response.",
             "requirement_ids": [item["id"] for item in requirements],
             "source_ids": sorted({source for item in requirements for source in item["source_ids"]}),
             "oracle_explanation": "Use the pre-authored scenario.normal and scenario.observed verbatim; compare all scalar results, preserving nulls, zeros, signs and sample denominators. Float tolerance only accommodates serialization precision.",
             "argv": argv, "input_files": [{"path": "host-hand.json", "format": "json", "data": scenario.normal}],
             "expected_exit_code": 0, "input_unchanged": True, "assertions": assertions}
    script["cases"].append(added)
    extra_ids = []
    if args.scenario == "paired":
        mixed = copy.deepcopy(added)
        mixed.update(id="host-independent-mixed-sign-enumeration",
                     description="Independent mixed-sign exact randomization regression.",
                     oracle_explanation="For differences [2,3,-1,1], signed sum frequencies are -7:1,-5:2,-3:2,-1:3,1:3,3:2,5:2,7:1. Six of sixteen have absolute sum >=5, yielding p=0.375. Mean=5/4, sample variance=35/12, SE=sqrt(35/12)/2. Expected values were authored before this model run.",
                     input_files=[{"path": "host-hand.json", "format": "json", "data": PAIRED_MIXED_SIGN_INPUT}],
                     assertions=json_assertions(paths["json_value"][0], PAIRED_MIXED_SIGN_EXPECTED))
        script["cases"].append(mixed)
        extra_ids.append(mixed["id"])
    request = {"scripts": package["scripts"], "plan": plan}
    root = Path(__file__).resolve().parents[2]
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for path in sorted((root / "app").rglob("*")):
            if (path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                    and path.suffix not in (".pyc", ".pyo") and not path.name.startswith(".env")):
                bundle.add(path, arcname=str(path.relative_to(root)), recursive=False)
        data = json.dumps(request, ensure_ascii=False).encode()
        info = tarfile.TarInfo("artificial-request.json")
        info.size, info.mode = len(data), 0o600
        bundle.addfile(info, io.BytesIO(data))
    bootstrap = '''import asyncio,io,json,sys,tarfile,tempfile
with tempfile.TemporaryDirectory(prefix="datalab-hand-oracle-") as root:
 with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode="r:gz") as bundle: bundle.extractall(root,filter="data")
 sys.path.insert(0,root)
 from app.infrastructure.external.sandbox.skill_validation_worker import validate_skill_scripts
 request=json.load(open(root+"/artificial-request.json"))
 receipt=asyncio.run(validate_skill_scripts(request["scripts"],request["plan"]))
 print("RECEIPT:"+json.dumps(receipt,ensure_ascii=False),flush=True)
'''
    result = subprocess.run(["docker", "exec", "-i", args.container, "python", "-B", "-c", bootstrap], input=archive.getvalue(), capture_output=True)
    # JSON permits literal Unicode line separators inside string values. Only
    # the wire's ASCII newline separates records; splitlines corrupts that data.
    lines = [line[len("RECEIPT:"):] for line in result.stdout.decode().split("\n") if line.startswith("RECEIPT:")]
    if result.returncode or len(lines) != 1:
        raise SystemExit("Independent worker did not return a receipt; exit=" + str(result.returncode))
    receipt = json.loads(lines[0])
    package["references"]["references/synthetic-validation.json"] = json.dumps(plan, ensure_ascii=False, indent=2)
    package["synthetic_execution"] = receipt
    validation = json.loads(package["references"]["references/validation.json"])
    validation["execution"] = receipt
    package["references"]["references/validation.json"] = json.dumps(validation, ensure_ascii=False, indent=2)
    artifact = {"kind": "package", "stage": "final", "content": package,
                "acceptance": {"scenario": args.scenario, "original_artifact": str(args.artifact),
                               "original_plan_sha256": original["content"]["synthetic_execution"]["plan_sha256"],
                               "production_oracle_review": {key: reviewed[key] for key in ("status", "verdict", "plan_sha256", "revision_count")},
                               "added_oracle": "pre-authored independent hand calculation",
                               "added_oracle_reviewed_by_model": False}}
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    assert receipt["source_sha256"] == original["content"]["synthetic_execution"]["source_sha256"]
    hand = next(case for case in receipt["cases"] if case["id"] == added["id"])
    extra_receipts = [case for case in receipt["cases"] if case["id"] in extra_ids]
    print(json.dumps({"scenario": args.scenario, "status": receipt["status"], "cases": len(receipt["cases"]),
                      "hand_oracle": hand, "extra_hand_oracles": extra_receipts,
                      "model_called": False, "artifact": str(args.output)}, ensure_ascii=False))
    assert receipt["status"] == "passed" and hand["status"] == "passed" and hand["input_unchanged"]
    assert all(case["status"] == "passed" and case["input_unchanged"] for case in extra_receipts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
