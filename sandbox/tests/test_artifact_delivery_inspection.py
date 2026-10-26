"""Delivery proves readable output bytes, never content or preview correctness."""
import hashlib
import json
import os
import threading

import pytest

from app.services import artifact_manifest as manifest
from app.services import artifact_validation as validation


def inspect(path, root, kind="data", **kwargs):
    return validation.validate_artifacts(
        [{"path": str(path), "kind": kind}], root=root, **kwargs,
    )["files"][0]


@pytest.mark.parametrize("name,kind,content", [
    ("plot.svg", "image", b'<svg><foreignObject><p>private label</p></foreignObject></svg>'),
    ("plot.png", "image", b"not an image"),
    ("table.csv", "table", b'a,b\n"unclosed,1\n'),
    ("report.json", "report", b'{"value":NaN'),
    ("report.html", "report", b'<script>fetch("https://example.com/")</script>'),
    ("code.py", "code", b"def function(:\n"),
    ("data.zip", "data", b"not a zip"),
    ("result.any-format", "data", b"\x00\xff\x01"),
    ("script.py", "image", b"raise RuntimeError('must never execute')"),
])
def test_default_delivery_never_calls_content_parsers_or_classifies_suffix(
    tmp_path, monkeypatch, name, kind, content,
):
    path = tmp_path / name
    path.write_bytes(content)
    monkeypatch.setattr(validation, "_classify", lambda *args: pytest.fail("suffix classification"))
    monkeypatch.setattr(validation, "_validate_content", lambda *args: pytest.fail("content parser"))
    result = inspect(path, tmp_path, kind)
    assert result["valid"] is True and result["reason"] == "delivery_ready"
    assert result["kind"] == result["expected_kind"] == kind
    assert result["purpose"] == "delivery"
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert result["size"] == len(content)
    assert result["metadata"] == {"validation_scope": "file_integrity", "content_validation": "not_performed"}
    assert result["proof"] == {
        "scope": "file_integrity", "content_validation": "not_performed",
        "checks": ["output_path", "regular_file", "readable", "size_match", "stable_snapshot", "sha256"],
    }
    assert result["diagnostics"] == {}
    assert "private label" not in json.dumps(result)


@pytest.mark.parametrize("kind", sorted(validation.KINDS))
def test_saved_empty_files_have_an_exact_delivery_receipt(tmp_path, kind):
    path = tmp_path / "empty.json"
    path.touch()
    result = inspect(path, tmp_path, kind)
    assert result["valid"] and result["size"] == 0
    assert result["sha256"] == hashlib.sha256(b"").hexdigest()
    assert result["proof"]["content_validation"] == "not_performed"


def test_content_inspection_requires_explicit_opt_in_and_does_not_change_file(tmp_path):
    path = tmp_path / "invalid.py"
    original = b"def private_function(:\n"
    path.write_bytes(original)
    delivery = inspect(path, tmp_path, "code")
    content = inspect(path, tmp_path, "code", purpose="content")
    assert delivery["valid"] is True and delivery["purpose"] == "delivery"
    assert content["valid"] is False and content["reason"] == "invalid_code_syntax"
    assert content["purpose"] == "content"
    assert delivery["sha256"] == content["sha256"]
    assert path.read_bytes() == original


def test_large_delivery_streams_without_content_inspection_size_limits(tmp_path, monkeypatch):
    path = tmp_path / "large.bin"
    chunk = b"0123456789abcdef" * (64 * 1024)
    with path.open("wb") as stream:
        for _ in range(3):
            stream.write(chunk)
    monkeypatch.setattr(validation, "MAX_FILE_BYTES", 1)
    monkeypatch.setattr(validation, "MAX_BATCH_BYTES", 1)
    original = validation._open_output
    requests = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            assert 0 < size <= validation.READ_CHUNK_BYTES
            requests.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(validation, "_open_output", lambda *args: BoundedReader(original(*args)))
    result = inspect(path, tmp_path)
    assert result["valid"] and result["size"] == 3 * len(chunk)
    assert result["sha256"] == hashlib.sha256(chunk * 3).hexdigest()
    assert len(requests) >= 4


def test_input_aliases_nonfiles_and_outside_paths_are_never_delivery_receipts(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    source = tmp_path / "inputs" / "source.bin"
    source.parent.mkdir()
    source.write_bytes(b"private input")
    (root / "symlink.bin").symlink_to(source)
    (root / "linked-parent").symlink_to(source.parent, target_is_directory=True)
    os.link(source, root / "hardlink.bin")
    os.mkfifo(root / "pipe.bin")
    (root / "directory.bin").mkdir()
    for path in [source, root / "../inputs/source.bin", root / "symlink.bin",
                 root / "linked-parent/source.bin", root / "hardlink.bin"]:
        result = inspect(path, root)
        assert not result["valid"] and result["reason"] == "unavailable_or_unsafe_path"
        assert result["sha256"] is None and result["size"] is None
        assert result["proof"]["checks"] == []
        assert "private input" not in json.dumps(result)
    for path in [root / "pipe.bin", root / "directory.bin"]:
        result = inspect(path, root)
        assert not result["valid"] and result["reason"] == "not_regular_file"
        assert result["sha256"] is None and result["size"] is None


def test_missing_and_unreadable_outputs_have_fixed_codes_without_os_details(tmp_path, monkeypatch):
    assert inspect(tmp_path / "missing", tmp_path)["reason"] == "missing_artifact"
    original = manifest.os.open

    def denied(path, *args, **kwargs):
        if path == "denied":
            raise PermissionError("private permission diagnostic")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(manifest.os, "open", denied)
    result = inspect(tmp_path / "denied", tmp_path)
    assert not result["valid"] and result["reason"] == "unavailable_or_unsafe_path"
    assert "private permission" not in json.dumps(result)


@pytest.mark.parametrize("change", ["rewrite", "replace_parent", "remove", "append"])
def test_changed_outputs_never_receive_a_trusted_digest(tmp_path, monkeypatch, change):
    root = tmp_path / "output"
    root.mkdir()
    path = root / "result.bin"
    path.write_bytes(b"original")
    # Some container filesystems update mtime/ctime at a coarser clock tick.
    # Make this injected same-size rewrite observably change the snapshot's
    # metadata instead of depending on elapsed time between Python calls.
    earlier = path.stat().st_mtime_ns - 1_000_000_000
    os.utime(path, ns=(earlier, earlier))
    original = validation._open_output
    changed = False

    class RacingReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            nonlocal changed
            data = self.stream.read(size)
            if not changed:
                changed = True
                if change == "rewrite":
                    path.write_bytes(b"modified")
                elif change == "replace_parent":
                    root.rename(tmp_path / "old-output")
                    root.mkdir()
                    path.write_bytes(b"original")
                elif change == "remove":
                    path.unlink()
                else:
                    with path.open("ab") as target:
                        target.write(b"appended")
            return data

    monkeypatch.setattr(validation, "_open_output", lambda *args: RacingReader(original(*args)))
    result = inspect(path, root)
    assert not result["valid"] and result["reason"] == "changed_during_read"
    assert result["sha256"] is None and result["size"] is None
    assert result["proof"]["checks"] == []


def test_cancelled_delivery_never_opens_or_certifies_a_file(tmp_path, monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(validation, "_open_output", lambda *args: pytest.fail("cancelled open"))
    result = inspect(tmp_path / "result", tmp_path, cancelled=cancelled)
    assert not result["valid"] and result["reason"] == "validation_cancelled"
    assert result["sha256"] is None and result["proof"]["checks"] == []


def test_cancelled_large_read_stops_between_bounded_chunks(tmp_path, monkeypatch):
    path = tmp_path / "result"
    path.write_bytes(b"x" * (2 * validation.READ_CHUNK_BYTES))
    cancelled = threading.Event()
    original = validation._open_output
    reads = []

    class CancellingReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            reads.append(size)
            data = self.stream.read(size)
            cancelled.set()
            return data

    monkeypatch.setattr(validation, "_open_output", lambda *args: CancellingReader(original(*args)))
    result = inspect(path, tmp_path, cancelled=cancelled)
    assert result["reason"] == "validation_cancelled" and not result["valid"]
    assert result["sha256"] is None and len(reads) == 1


@pytest.mark.parametrize("endpoint", ["inspect-deliveries", "validate-artifacts"])
def test_http_default_is_delivery_with_machine_readable_scope(client, tmp_path, monkeypatch, endpoint):
    from conftest import BASE_URL
    from app.api.v1 import file as file_api
    path = tmp_path / "saved.png"
    path.write_bytes(b"not previewable")
    monkeypatch.setattr(file_api, "validate_artifacts", lambda items, **kwargs:
        validation.validate_artifacts(items, root=tmp_path, **kwargs))
    response = client.post(f"{BASE_URL}/api/v1/file/{endpoint}",
                           json={"items": [{"path": str(path), "kind": "image"}]})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["purpose"] == "delivery"
    assert data["files"][0]["valid"] and data["files"][0]["reason"] == "delivery_ready"
    assert data["files"][0]["proof"]["content_validation"] == "not_performed"
    assert "not previewable" not in response.text


def test_http_delivery_endpoint_cannot_be_switched_to_content_inspection(client, tmp_path):
    from conftest import BASE_URL
    for purpose in ("content", "other"):
        response = client.post(f"{BASE_URL}/api/v1/file/inspect-deliveries", json={
            "purpose": purpose, "items": [{"path": str(tmp_path / "anything"), "kind": "data"}],
        })
        assert response.status_code == 422


def test_invalid_service_purpose_never_falls_back_to_a_content_parser(tmp_path):
    with pytest.raises(ValueError, match="invalid_validation_purpose"):
        inspect(tmp_path / "anything", tmp_path, purpose="other")
