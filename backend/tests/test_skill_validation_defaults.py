"""Schema defaults must not change a frozen plan's bytes on worker entry."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.domain.services.skills.skill_validation import ExpectedValue, SyntheticValidationPlan
from app.domain.services.skills.skill_validation_extension import VerificationCoverageAmendment, apply_coverage_amendment
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_validation_worker import case, plan, runtime


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.25, True, False, None, "zero"])
@pytest.mark.parametrize("explicit", [False, True])
def test_expectation_default_roundtrip_is_byte_stable_without_coercing_scientific_values(value, explicit):
    arguments = {"value": value, **({"atol": 0, "rtol": 0} if explicit else {})}
    original = ExpectedValue.model_validate(arguments)
    restored = ExpectedValue.model_validate(original.model_dump())
    assert type(original.value) is type(restored.value) is type(value)
    assert original.value == restored.value == value
    assert type(original.atol) is type(original.rtol) is float
    assert original.atol == original.rtol == 0.0
    assert canonical(original.model_dump()) == canonical(restored.model_dump())
    assert canonical(original.model_dump(mode="json")) == canonical(restored.model_dump(mode="json"))


def extended_plan():
    original = plan()
    probe = case("boundary", output_type="json", expected=4)
    probe["id"] = "extra_boundary"
    # A substantively distinct, entirely artificial successful input probe.
    probe["input_files"][0]["rows"] = [{"group": "A", "value": 2}, {"group": "A", "value": 2}]
    amendment = VerificationCoverageAmendment.model_validate({"new_cases": [{
        "script_path": "scripts/summary.py", "case": probe}]})
    value = amendment.new_cases[0].case.assertions[0].expected
    assert type(value.atol) is type(value.rtol) is float and value.atol == value.rtol == 0.0
    return apply_coverage_amendment(original, amendment)


def test_new_successful_case_default_tolerances_preserve_exact_plan_json_on_roundtrip():
    original = extended_plan()
    for mode in ("python", "json"):
        document = original.model_dump(mode=mode)
        restored = SyntheticValidationPlan.model_validate(document)
        assert canonical(document) == canonical(restored.model_dump(mode=mode))
        assert worker._json(document) == worker._json(restored.model_dump(mode=mode))


@pytest.mark.asyncio
async def test_new_successful_case_worker_receipt_is_bound_to_exact_input_plan_bytes(runtime):
    candidate = extended_plan()
    document = candidate.model_dump()
    expected_hash = hashlib.sha256(worker._json(document)).hexdigest()
    result = await worker.validate_skill_scripts({"scripts/summary.py": "# artificial fixture"}, candidate,
        SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == "passed" and len(result["cases"]) == 4
    assert result["plan_sha256"] == expected_hash
    assert canonical(candidate.model_dump()) == canonical(document)


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1", reason="Opt-in disposable sandbox integration")
@pytest.mark.asyncio
async def test_real_worker_binds_new_case_default_tolerances_to_unchanged_plan_hash(monkeypatch):
    monkeypatch.setattr(worker, "helper_security_options", lambda: ["no-new-privileges:true"])
    candidate = extended_plan()
    before = candidate.model_dump()
    expected_hash = hashlib.sha256(worker._json(before)).hexdigest()
    source = '''import argparse,csv,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
rows=list(csv.DictReader(open(a.input)))
total=sum(float(row['value']) for row in rows)
Path(a.output,'summary.json').write_text(json.dumps({'value':total}))
with Path(a.output,'summary.csv').open('w',newline='') as handle:
    writer=csv.writer(handle);writer.writerow(['group','value']);writer.writerow(['A',total])
'''
    settings = SimpleNamespace(sandbox_image=os.environ.get("DATALAB_SYNTHETIC_TEST_IMAGE", "datalab-sandbox:analysis-repair-20261023"))
    receipt = await worker.validate_skill_scripts({"scripts/summary.py": source}, candidate, settings)
    assert receipt["status"] == "passed", receipt
    assert len(receipt["cases"]) == 4 and all(item["input_unchanged"] for item in receipt["cases"])
    assert receipt["plan_sha256"] == expected_hash
    assert canonical(candidate.model_dump()) == canonical(before)
