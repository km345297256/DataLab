"""Host-built delivery sets. Byte identity is not causal/scientific proof.

Only the runner calls this after owner-scoped upload reconciliation. No paths,
arguments, raw source, storage keys or model-authored provenance are published.
Unchanged historical objects retain their identity instead of being re-uploaded.
"""
from __future__ import annotations

import re
from app.domain.models.analysis_outcome import safe_artifact_name
from app.domain.services.execution_identity import private_identity_hmac


def _sha(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None


def build_result_set(*, owner_id, session_id, step_id, input_identity, files, records,
                     executions=()):
    current = {item.get("path"): item for item in records if isinstance(item, dict)}
    members = []
    for info in files:
        if not info.file_id:
            continue
        digest = _sha((info.metadata or {}).get("artifact_sha256"))
        receipt = current.get(info.file_path, {})
        observed = _sha(receipt.get("sha256"))
        state = "unconfirmed"
        if digest and observed == digest and receipt.get("valid") is True and receipt.get("size") == info.size:
            state = "current"
        elif digest and observed and observed != digest:
            state = "retained_prior_version"
        members.append({"file_id": info.file_id, "name": safe_artifact_name(info.filename or info.file_path),
                        "sha256": digest, "size": info.size, "state": state,
                        **({"content_assessment": "user_evaluated", "file_check_scope": "file_integrity"}
                           if (info.metadata or {}).get("artifact_validation_scope") == "file_integrity"
                           and (info.metadata or {}).get("content_assessment") == "user_evaluated" else {})})
    members.sort(key=lambda item: item["file_id"])
    versions = sorted({(item["operation_id"], item["source_digest"])
                       for item in executions if isinstance(item, dict)
                       and _sha(item.get("source_digest"))
                       and isinstance(item.get("operation_id"), str)
                       and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", item["operation_id"])})
    source_scope = private_identity_hmac({"purpose": "analysis-result-inputs/v1", "value": input_identity})
    # Launch identities and source fingerprints are private evidence. Publish
    # only a scoped, opaque reference; neither raw field belongs in step/SSE
    # output, even when the execution is host-confirmed.
    execution_versions = sorted(private_identity_hmac({
        "purpose": "analysis-result-execution/v1", "owner": owner_id,
        "session": session_id, "step": step_id, "operation_id": op,
        "source_digest": digest,
    }) for op, digest in versions)
    content = {"version": 1, "input_version": source_scope, "files": members,
               "executions": [{"execution_version": value} for value in execution_versions],
               "execution_linkage": "observed_step_only" if versions else "unknown",
               "scientific_review": "not_performed"}
    identity = private_identity_hmac({"purpose": "analysis-result-set/v1", "owner": owner_id,
                                     "session": session_id, "step": step_id, "content": content})
    return {"id": identity, **content}
