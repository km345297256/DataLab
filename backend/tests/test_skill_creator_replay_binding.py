"""Bound artificial replay never substitutes an answer for another request."""
from copy import deepcopy
import hashlib
import json

import pytest

from acceptance.skill_creator_real_model import validated_replay_message
from acceptance.skill_creator_replay_binding import bound_response_matches, load_bound_prefix


def manifest(tmp_path, *, finish="stop"):
    stage = "synthetic_reference_review"
    entry = {}
    documents = {
        "request": {"stage": stage, "schema": {"title": "HostBoundMethodReview"},
                    "method_contract": {"requirements": [{"id": "R017", "statement": "Keep the defined method."}]},
                    "reference_scripts": [{"path": "scripts/reference.py", "content": "def method(): return 1\n"}],
                    "blind_cases": [{"value": 0}]},
        "response": '{"requirements":{"R017":{"status":"changed","explanation":"The source method is changed."}}}',
        "provider_metadata": {"stage": stage, "finish_reason": finish},
    }
    for role, content in documents.items():
        path = tmp_path / (role + ".json")
        raw = json.dumps({"kind": role, "stage": stage, "content": content}, ensure_ascii=False).encode()
        path.write_bytes(raw)
        entry[role + "_artifact"] = str(path)
        entry[role + "_artifact_sha256"] = hashlib.sha256(raw).hexdigest()
    return {"scenario": "text", "binding": "complete_request_v1", "exchanges": [entry]}


def test_bound_prefix_keeps_original_negative_bytes_and_complete_request(tmp_path):
    saved = load_bound_prefix(manifest(tmp_path), "text")[0]
    assert bound_response_matches(deepcopy(saved["original_request"]), saved)
    assert '"status":"changed"' in saved["content"]
    assert saved["finish_reason"] == "stop"
    assert set(saved["artifact_sha256"]) == {"request", "response", "provider_metadata"}


@pytest.mark.parametrize("part", ["schema", "method_contract", "reference_scripts", "blind_cases", "extra", "stage"])
def test_bound_prefix_rejects_any_changed_request_field(tmp_path, part):
    saved = load_bound_prefix(manifest(tmp_path), "text")[0]
    request = deepcopy(saved["original_request"])
    request[part] = "changed"
    assert not bound_response_matches(request, saved)


@pytest.mark.parametrize("number", [False, 0.0])
def test_binding_preserves_scalar_types(tmp_path, number):
    saved = load_bound_prefix(manifest(tmp_path), "text")[0]
    request = deepcopy(saved["original_request"])
    request["blind_cases"][0]["value"] = number
    assert not bound_response_matches(request, saved)


@pytest.mark.parametrize("part", ["request", "response", "provider_metadata"])
def test_origin_envelope_bytes_must_match_manifest(tmp_path, part):
    original = manifest(tmp_path)
    path = tmp_path / (part + ".json")
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(AssertionError, match="artifact bytes changed"):
        load_bound_prefix(original, "text")


@pytest.mark.parametrize("part", ["original_request", "content", "finish_reason", "changed_finish_reason"])
def test_packed_replay_requires_unchanged_bound_material(tmp_path, part):
    saved = load_bound_prefix(manifest(tmp_path), "text")[0]
    request = deepcopy(saved["original_request"])
    if part == "original_request":
        saved[part]["schema"] = {}
    elif part == "content":
        saved[part] += " "
    elif part == "changed_finish_reason":
        saved["finish_reason"] = "length"
    else:
        del saved[part]
    with pytest.raises(AssertionError):
        bound_response_matches(request, saved)


def test_replay_preserves_truncation_metadata_in_real_validation_boundary(tmp_path):
    saved = load_bound_prefix(manifest(tmp_path, finish="length"), "text")[0]
    observed = []
    message = validated_replay_message(saved["content"], [observed.append], saved["finish_reason"])
    assert observed == [message]
    assert message.content == saved["content"]
    assert message.response_metadata["finish_reason"] == "length"
