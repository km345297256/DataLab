"""Exact artificial exchange provenance for optional bound response replay.

No generation, response editing, scope migration or normalization occurs here.
Request JSON includes every captured field, preserving scalar types. The
original artifact bytes and the provider's recorded finish reason are retained.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def request_digest(document):
    return hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def load_bound_prefix(manifest, scenario):
    assert manifest["scenario"] == scenario and manifest["binding"] == "complete_request_v1"
    result = []
    for entry in manifest["exchanges"]:
        documents = {}
        for role, kind in (("request", "request"), ("response", "response"), ("provider_metadata", "provider_metadata")):
            raw = Path(entry[role + "_artifact"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == entry[role + "_artifact_sha256"], "Original exchange artifact bytes changed"
            documents[role] = json.loads(raw)
            assert documents[role]["kind"] == kind
        request, response, metadata = (documents[key] for key in ("request", "response", "provider_metadata"))
        stage = request["stage"]
        assert stage == response["stage"] == metadata["stage"] == request["content"]["stage"] == metadata["content"]["stage"]
        assert isinstance(request["content"], dict) and isinstance(response["content"], str)
        finish = metadata["content"]["finish_reason"]
        assert finish is None or isinstance(finish, str)
        result.append({"binding": "complete_request_v1", "stage": stage,
            "content": response["content"], "response_sha256": hashlib.sha256(response["content"].encode()).hexdigest(),
            "original_request": request["content"], "request_sha256": request_digest(request["content"]),
            "original_provider_metadata": metadata["content"], "provider_metadata_sha256": request_digest(metadata["content"]),
            "finish_reason": finish, "origin_artifact": entry["response_artifact"],
            "origin_request_artifact": entry["request_artifact"], "origin_provider_metadata_artifact": entry["provider_metadata_artifact"],
            "artifact_sha256": {role: entry[role + "_artifact_sha256"] for role in documents}})
    assert result, "Bound replay needs at least one original exchange"
    return result


def bound_response_matches(request, saved):
    assert saved["binding"] == "complete_request_v1"
    assert hashlib.sha256(saved["content"].encode()).hexdigest() == saved["response_sha256"], "Saved response bytes changed"
    assert request_digest(saved["original_request"]) == saved["request_sha256"], "Saved complete request changed"
    assert saved["original_request"]["stage"] == saved["stage"]
    assert "finish_reason" in saved, "Replay cannot invent provider completion metadata"
    assert request_digest(saved["original_provider_metadata"]) == saved["provider_metadata_sha256"], "Provider metadata changed"
    assert saved["finish_reason"] == saved["original_provider_metadata"]["finish_reason"], "Provider completion reason changed"
    return request_digest(request) == saved["request_sha256"]
