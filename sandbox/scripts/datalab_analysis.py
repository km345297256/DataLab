"""Small analysis checks and resumable *derived* computation stages.

These checks are structural evidence, never proof of scientific validity.
Capture row identities before filtering. Use explicit columns (frame['T']),
not attribute access. StageCache stores only producer-created intermediate
results in writable computation space, never mounts/copies source data.

Stage identity includes complete producer/validator module bytes, explicitly
listed helper source files, caller-provided immutable input versions, parameters,
a separate validation contract and installed Python distribution versions.
Declare every external dependency in input_versions/parameters/source_files;
undeclared globals, randomness and external services are not cacheable. Put
independent stages in separate modules to avoid invalidating them together.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import fcntl
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import Mapping


class AnalysisInvariantError(ValueError):
    """A structural acceptance condition failed; do not weaken it to pass."""


def _unique(values, label):
    values = tuple(values)
    try:
        unique = frozenset(values)
    except TypeError as exc:
        raise AnalysisInvariantError(f"{label} must contain hashable row identities") from exc
    def missing(value):
        if isinstance(value, tuple):
            return any(missing(part) for part in value)
        try:
            return value is None or bool(value != value)
        except (TypeError, ValueError):
            return True  # pandas.NA and ambiguous nullable identity values
    if len(unique) != len(values) or any(missing(value) for value in values):
        raise AnalysisInvariantError(f"{label} must contain unique, nonmissing row identities")
    return values, unique


class RowPartition:
    """Freeze original IDs and account for mutually exclusive exclusions.

    Keep the audit object in its own variable. ``exclude`` changes that object
    in place and returns None; it is not a chainable constructor. ``verify``
    returns an evidence dict, not retained IDs. Keep retained IDs separately:

    audit = RowPartition(original.index)
    audit.exclude('missing_response', missing.index)
    audit.exclude('nonpositive_response', nonpositive_after_missing.index)
    retained_ids = accepted.index
    evidence = audit.verify(retained_ids)

    Do not assign ``audit = RowPartition(ids).exclude(...)`` or pass the
    verification evidence as ``row_ids`` to another helper.
    """
    def __init__(self, original_ids):
        _, self._original = _unique(original_ids, "original_ids")
        self._excluded = {}

    def exclude(self, reason: str, row_ids) -> None:
        """Record a disjoint set of excluded original IDs in this audit.

        Returns
        -------
        None
            Mutates the existing audit in place; no audit object or IDs are
            returned. Use ``audit = RowPartition(original_ids)`` followed by
            ``audit.exclude('missing', missing_ids)`` as a separate statement.
        """
        if not isinstance(reason, str) or not reason.strip() or reason in self._excluded:
            raise AnalysisInvariantError("Each exclusion requires a new, nonempty reason")
        _, ids = _unique(row_ids, "excluded_ids")
        previous = frozenset().union(*self._excluded.values())
        if not ids <= self._original or ids & previous:
            raise AnalysisInvariantError("Exclusions must be original rows and must not overlap")
        self._excluded[reason] = ids

    def verify(self, accepted_ids) -> dict:
        """Check that accepted rows plus disjoint exclusions cover originals.

        Returns
        -------
        dict
            An evidence dict, not a list of accepted/retained IDs. Fields are
            ``passed`` (bool), ``original_rows`` and ``accepted_rows`` (int),
            and ``excluded_rows`` (dict mapping reason to count). Retain your
            ``accepted_ids`` separately: ``evidence = audit.verify(retained_ids)``.
            Read evidence with dictionary indexing, e.g. ``evidence['passed']``.
            A failed partition raises AnalysisInvariantError, not passed=False.
        """
        _, accepted = _unique(accepted_ids, "accepted_ids")
        excluded = frozenset().union(*self._excluded.values())
        if accepted & excluded or accepted | excluded != self._original:
            raise AnalysisInvariantError("Original rows must equal accepted rows plus disjoint exclusions")
        return {"passed": True, "original_rows": len(self._original),
                "accepted_rows": len(accepted),
                "excluded_rows": {name: len(ids) for name, ids in self._excluded.items()}}


def aligned_groups(values, *, row_ids, labels) -> dict:
    """Group positional residuals using the exact filtered row IDs.

    Series inputs must already have the identical ordered index; silently
    aligning a fresh RangeIndex to a filtered index is rejected. Missing group
    labels must be explicitly accounted for before this check, never dropped.

    If a ``row_id`` column defines identity, set that index before fitting or
    constructing indexed inputs: ``indexed = retained.set_index('row_id')``;
    then use ``indexed.index`` as row_ids and Series from that indexed frame.
    Alternatively, explicitly pass ``values.to_numpy()`` and
    ``labels.to_numpy()`` in the same original retained order with those row IDs.
    Unlabeled NumPy arrays are positional; row_ids must still be the actual
    original IDs. Do not invent a new range or pass RowPartition.verify() output.

    Returns
    -------
    dict
        A plain dictionary mapping each group label to a NumPy ndarray of its
        values, preserving observation order within each group. Use
        ``groups['control']`` to select a group. The result is neither a
        DataFrame nor an array of retained IDs. Series values and Series labels
        must both have exactly the same ordered index as row_ids.
    """
    import numpy as np
    import pandas as pd
    ids, _ = _unique(row_ids, "row_ids")
    for name, item in (("values", values), ("labels", labels)):
        if isinstance(item, pd.Series) and tuple(item.index) != ids:
            raise AnalysisInvariantError(f"{name} index does not match the retained row identities")
    data = np.asarray(values)
    group_labels = list(labels)
    if data.ndim != 1 or len(data) != len(ids) or len(group_labels) != len(ids):
        raise AnalysisInvariantError("Values, row identities and group labels must have the same length")
    groups = {}
    for value, label in zip(data, group_labels):
        components = label if isinstance(label, tuple) else (label,)
        if any(bool(pd.isna(part)) for part in components):
            raise AnalysisInvariantError("Missing group labels require an explicit exclusion policy")
        try:
            groups.setdefault(label, []).append(value)
        except TypeError as exc:
            raise AnalysisInvariantError("Group labels must be hashable") from exc
    if sum(map(len, groups.values())) != len(ids):
        raise AnalysisInvariantError("Grouping lost observations")
    return {label: np.asarray(items) for label, items in groups.items()}


def _json(value):
    # Scientific special values need explicit representation before entering an
    # identity/contract; NaN and generic str() would create ambiguous cache keys.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Stage files must be regular, non-symlink files")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _safe_directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ValueError("Stage directory must be an absolute computation path")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Stage directories cannot use symlinks")
    for forbidden in (Path("/home/ubuntu/inputs"), Path("/home/ubuntu/output")):
        if path == forbidden or path.is_relative_to(forbidden):
            raise ValueError("Stage cache must be separate from mounted inputs and delivered outputs")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class StageResult:
    identity: str
    outputs: dict[str, Path]
    evidence: dict
    reused: bool
    output_sha256: dict[str, str] = field(default_factory=dict, repr=False)

    def dependency(self) -> dict:
        """Bind downstream work to these exact derived bytes, never source copies.

        An old result handle is not usable after its files changed. This is a
        cache dependency receipt, not independent scientific certification.
        """
        observed = {name: _digest(path) for name, path in self.outputs.items()}
        if not observed or observed != self.output_sha256:
            raise AnalysisInvariantError("Upstream stage bytes changed or have no version receipt")
        return {"stage_identity": self.identity, "outputs": dict(observed)}


class StageCache:
    """Opt-in resumable stages, with validation before commit and on every reuse.

    produce(work_dir, parameters, input_versions) -> {name: local_file}
    validate(outputs, contract) -> {'passed': True, ...checked evidence...}

    Keep validation separate from implementation and supply expectations derived
    from the method/data, not copied from the producer's result. A failed stage
    never replaces a successful version. No files are automatically published.
    Use only deterministic stages with all dependencies explicitly declared.
    """
    def __init__(self, root="/home/ubuntu/.datalab/stages"):
        self.root = _safe_directory(root)

    def run(self, name, *, input_versions, parameters, contract, produce, validate,
            source_files=(), upstream=None) -> StageResult:
        """Compute or reuse a validated stage with explicitly bound inputs.

        Returns
        -------
        StageResult
            A result object, not a dict. Read ``result.outputs`` (named Paths),
            ``result.evidence`` (validation dict), ``result.reused`` (bool),
            and ``result.identity`` (str). Use ``result.dependency()`` for the
            checked upstream dependency dict; this does not publish files.
        """
        upstream_checks = ()
        if upstream is not None:
            if not isinstance(upstream, Mapping) or any(
                    not isinstance(key, str) or not isinstance(value, StageResult)
                    for key, value in upstream.items()):
                raise ValueError("Upstream stages must be named StageResult values")
            if not isinstance(input_versions, dict) or "__upstream_stages" in input_versions:
                raise ValueError("Reserved upstream dependency namespace")
            # Keep the original handles and receipts independently of a caller's
            # mutable mapping/StageResult dictionaries. Later validation must
            # compare against the dependency bytes used to compute this key.
            declared = [(key, value, value.dependency()) for key, value in upstream.items()]
            upstream_checks = tuple((value, receipt) for _, value, receipt in declared)
            if declared:
                input_versions = {**input_versions, "__upstream_stages": {
                    key: receipt for key, _, receipt in declared}}
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise ValueError("Stage name must use letters, numbers, underscores or hyphens")
        if not isinstance(input_versions, dict) or not input_versions:
            raise ValueError("Declare immutable selected-input or preceding-stage versions")
        if not isinstance(contract, dict) or not contract:
            raise ValueError("A stage requires a separate, nonempty validation contract")
        sources = {Path(__file__).absolute(), *(Path(path).absolute() for path in source_files)}
        for function in (produce, validate):
            source = inspect.getsourcefile(function)
            if not source or not Path(source).is_file():
                raise ValueError("Stage producer and validator must be saved Python functions")
            sources.add(Path(source).absolute())
        if any(path.suffix != ".py" or path.is_relative_to("/home/ubuntu/inputs") for path in sources):
            raise ValueError("Declare Python implementation modules, not mounted input files")
        source_hashes = {str(path): _digest(path) for path in sorted(sources)}
        environment = sorted((dist.metadata.get("Name", ""), dist.version)
                             for dist in importlib.metadata.distributions())
        identity_payload = {"version": 1, "stage": name, "inputs": input_versions,
                            "parameters": parameters, "contract": contract,
                            "sources": source_hashes, "python": sys.version,
                            "producer": produce.__qualname__, "validator": validate.__qualname__,
                            "environment": environment}
        encoded_identity = _json(identity_payload)
        key = hashlib.sha256(encoded_identity).hexdigest()
        directory = self.root / f"{name}-{key}"
        lock = self.root / f".{name}-{key}.lock"
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            return self._run(key, directory, name, parameters, input_versions, contract,
                             produce, validate, sources, source_hashes, upstream_checks)

    def _run(self, key, directory, name, parameters, input_versions, contract,
             produce, validate, sources, source_hashes, upstream_checks=()):
        frozen_contract = _json(contract)
        def check_upstream():
            for value, expected in upstream_checks:
                if value.dependency() != expected:
                    raise AnalysisInvariantError("Upstream stage version changed during execution")
        check_upstream()
        # Copies protect the caller's contract/identity from producer mutation.
        def check(outputs):
            evidence = validate(dict(outputs), json.loads(frozen_contract))
            check_upstream()
            if not isinstance(evidence, dict) or evidence.get("passed") is not True:
                raise AnalysisInvariantError("Stage validation did not explicitly pass")
            _json(evidence)
            return evidence

        if directory.is_dir() and not directory.is_symlink():
            try:
                manifest_path = directory / "stage.json"
                if manifest_path.is_symlink() or manifest_path.stat().st_size > 1024 * 1024:
                    raise ValueError("Invalid stage manifest")
                manifest = json.loads(manifest_path.read_text())
                if manifest["identity"] != key:
                    raise ValueError("Stage identity changed")
                outputs = self._outputs(directory, manifest["outputs"])
                if {label: _digest(path) for label, path in outputs.items()} != manifest["hashes"]:
                    raise ValueError("Stage bytes changed")
                evidence = check(outputs)
                if {label: _digest(path) for label, path in outputs.items()} != manifest["hashes"]:
                    raise ValueError("Validator mutated stage outputs")
                return StageResult(key, outputs, evidence, True, dict(manifest["hashes"]))
            except (OSError, ValueError, TypeError, KeyError):
                # A stale upstream dependency cannot be repaired by rerunning
                # this downstream producer against the old identity.
                check_upstream()
                # Invalid cache is not evidence and is never used downstream.
                # Preserve it for diagnosis; compute into a fresh directory.
                pass
        work = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=self.root))
        try:
            outputs = self._outputs(work, produce(work, json.loads(_json(parameters)),
                                                  json.loads(_json(input_versions))))
            hashes = {label: _digest(path) for label, path in outputs.items()}
            evidence = check(outputs)
            if {label: _digest(path) for label, path in outputs.items()} != hashes:
                raise AnalysisInvariantError("Validator must not modify stage outputs")
            if {str(path): _digest(path) for path in sorted(sources)} != source_hashes:
                raise AnalysisInvariantError("Stage source changed during execution")
            declared = set(outputs.values())
            for item in work.rglob("*"):
                if item.is_symlink() or not item.is_dir() and item not in declared:
                    raise AnalysisInvariantError("Undeclared files cannot enter the derived-stage cache")
            manifest = {"identity": key, "outputs": {label: str(path.relative_to(work))
                         for label, path in outputs.items()}, "hashes": hashes,
                        "evidence": evidence, "contract": json.loads(frozen_contract)}
            encoded_manifest = _json(manifest)
            if len(encoded_manifest) > 1024 * 1024:
                raise ValueError("Stage manifest exceeds metadata limit")
            (work / "stage.json").write_bytes(encoded_manifest)
            # The stage lock makes this replacement serial. Retain a rejected
            # version for diagnosis, but make the newly validated version reusable.
            if directory.exists() or directory.is_symlink():
                os.rename(directory, self.root / f".rejected-{name}-{uuid.uuid4().hex}")
            os.rename(work, directory)
            return StageResult(key, {label: directory / path.relative_to(work)
                                     for label, path in outputs.items()}, evidence, False, dict(hashes))
        except BaseException:
            shutil.rmtree(work)
            raise

    @staticmethod
    def _outputs(directory, items):
        if not isinstance(items, Mapping) or not items or any(not isinstance(key, str) for key in items):
            raise ValueError("A stage must declare named, derived file outputs")
        outputs = {}
        for name, item in items.items():
            path = Path(item)
            path = path if path.is_absolute() else directory / path
            if (".." in path.parts or not path.is_relative_to(directory)
                    or path == directory / "stage.json"
                    or any(parent.is_symlink() for parent in (path, *path.parents))):
                raise ValueError("Stage output must be inside its computation directory")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Stage outputs must be newly created regular files, not input links")
            outputs[name] = path
        return outputs
