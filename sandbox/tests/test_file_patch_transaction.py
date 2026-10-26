"""Real file/HTTP transactions: no partial writes, stale bases, or input edits."""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.schemas.file import FilePatchRequest
from app.services.file import FileService
from app.services import file_patch
from conftest import BASE_URL


def request(path, replacements=None, **kwargs):
    return FilePatchRequest(file=str(path), base_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        replacements=replacements or [{"old_str": "old", "new_str": "new", "expected_count": 1}], **kwargs)


def test_batch_matches_original_not_previous_replacement_output(tmp_path):
    path = tmp_path / "report.md"
    path.write_bytes("old\r\n第二节\r\n".encode())
    original = path.read_bytes()
    patch = request(path, [{"old_str": "old", "new_str": "第二节", "expected_count": 1},
                           {"old_str": "第二节", "new_str": "修订", "expected_count": 1}])
    path.chmod(0o640)
    result = file_patch.apply_patch(patch)
    assert path.read_bytes() == "第二节\r\n修订\r\n".encode()
    assert result.before_sha256 == hashlib.sha256(original).hexdigest()
    assert result.after_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.replacement_counts == [1, 1] and result.changed
    assert path.stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.md"]


@pytest.mark.parametrize("replacements,code", [
    ([{"old_str": "old", "new_str": "new", "expected_count": 1},
      {"old_str": "missing", "new_str": "x", "expected_count": 1}], "match_count_mismatch"),
    ([{"old_str": "old", "new_str": "new", "expected_count": 2}], "match_count_mismatch"),
    ([{"old_str": "old", "new_str": "new", "expected_count": 1},
      {"old_str": "ld", "new_str": "x", "expected_count": 1}], "overlapping_replacements"),
])
def test_all_preconditions_checked_before_any_save(tmp_path, replacements, code):
    path = tmp_path / "source.py"
    path.write_text("old")
    before = path.stat()
    with pytest.raises(file_patch.FilePatchError) as caught:
        file_patch.apply_patch(request(path, replacements))
    assert caught.value.data["error_code"] == code
    assert caught.value.data["side_effect_state"] == "not_started"
    assert path.read_text() == "old"
    assert path.stat().st_ino == before.st_ino and path.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("syntax,new", [("python", "return 1"), ("python", "def missing(:"),
    ("json", '{"x": NaN}'), ("json", '{"x": 1, "x": 2}'), ("json", '{"x":')])
def test_optional_syntax_validation_never_executes_or_saves_invalid_code(tmp_path, syntax, new):
    path = tmp_path / "source.txt"
    path.write_text("old")
    with pytest.raises(file_patch.FilePatchError) as caught:
        file_patch.apply_patch(request(path, [{"old_str": "old", "new_str": new, "expected_count": 1}],
                                      syntax_check=syntax))
    assert caught.value.data["error_code"] == "syntax_invalid"
    assert path.read_text() == "old"


def test_valid_python_is_compiled_without_running_it(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("old")
    marker = tmp_path / "must-not-exist"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    file_patch.apply_patch(request(path, [{"old_str": "old", "new_str": source, "expected_count": 1}],
                                  syntax_check="python"))
    assert not marker.exists() and path.read_text() == source


def test_identity_patch_reuses_bytes_without_touching_mtime(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("old")
    before = path.stat()
    result = file_patch.apply_patch(request(path, [{"old_str": "old", "new_str": "old", "expected_count": 1}]))
    assert not result.changed and result.write_action == "reuse"
    assert result.before_sha256 == result.after_sha256
    assert path.stat().st_mtime_ns == before.st_mtime_ns and path.stat().st_ino == before.st_ino


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="Linux extended attributes")
def test_atomic_replacement_preserves_extended_attributes(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("old")
    os.setxattr(path, "user.datalab-test", b"retained metadata")
    file_patch.apply_patch(request(path))
    assert os.getxattr(path, "user.datalab-test") == b"retained metadata"


def test_atomic_replace_does_not_bypass_readonly_parent(tmp_path):
    directory = tmp_path / "readonly"
    directory.mkdir()
    path = directory / "report.md"
    path.write_text("old")
    patch = request(path)
    directory.chmod(0o555)
    try:
        with pytest.raises(file_patch.FilePatchError):
            file_patch.apply_patch(patch)
        assert path.read_text() == "old"
    finally:
        directory.chmod(0o755)


def test_concurrent_patches_using_same_base_cannot_both_commit(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("old")
    first = request(path)
    second = request(path, [{"old_str": "old", "new_str": "different", "expected_count": 1}])
    def run(patch):
        try:
            return file_patch.apply_patch(patch).changed
        except file_patch.FilePatchError as error:
            return error.data["error_code"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [first, second]))
    assert sorted(map(str, results)) == ["True", "version_conflict"]
    assert path.read_text() in {"new", "different"}


@pytest.mark.parametrize("fault", ["fsync", "replace"])
def test_failed_commit_preserves_destination_and_cleans_staging(tmp_path, monkeypatch, fault):
    path = tmp_path / "report.md"
    path.write_text("old")
    def fail(*args, **kwargs):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(file_patch.os, fault, fail)
    with pytest.raises(file_patch.FilePatchError) as caught:
        file_patch.apply_patch(request(path))
    assert caught.value.data["error_code"] == "io_error"
    assert path.read_text() == "old" and list(tmp_path.iterdir()) == [path]


def test_external_change_while_staging_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "report.md"
    path.write_text("old")
    real_fsync = file_patch.os.fsync
    def concurrent_write(fd):
        real_fsync(fd)
        path.write_text("external revision")
    monkeypatch.setattr(file_patch.os, "fsync", concurrent_write)
    with pytest.raises(file_patch.FilePatchError) as caught:
        file_patch.apply_patch(request(path))
    assert caught.value.data["error_code"] == "version_conflict"
    assert path.read_text() == "external revision" and list(tmp_path.iterdir()) == [path]


def test_parent_replaced_by_symlink_while_staging_cannot_redirect_patch(tmp_path, monkeypatch):
    directory = tmp_path / "workspace"
    directory.mkdir()
    path = directory / "report.md"
    path.write_text("old")
    other = tmp_path / "input"
    other.mkdir()
    (other / "report.md").write_text("private input")
    moved = tmp_path / "moved-workspace"
    real_fsync = file_patch.os.fsync
    def change_parent(fd):
        real_fsync(fd)
        directory.rename(moved)
        directory.symlink_to(other, target_is_directory=True)
    monkeypatch.setattr(file_patch.os, "fsync", change_parent)
    with pytest.raises(file_patch.FilePatchError) as caught:
        file_patch.apply_patch(request(path))
    assert caught.value.data["error_code"] == "version_conflict"
    assert (other / "report.md").read_text() == "private input"
    assert (moved / "report.md").read_text() == "old"
    assert list(moved.iterdir()) == [moved / "report.md"]


def test_expanding_patch_is_rejected_before_materializing_or_writing_result(tmp_path, monkeypatch):
    path = tmp_path / "source.txt"
    path.write_text("old")
    monkeypatch.setattr(file_patch, "MAX_PATCH_BYTES", 16)
    with pytest.raises(file_patch.FilePatchError) as caught:
        file_patch.apply_patch(request(path, [{"old_str": "old", "new_str": "x" * 17, "expected_count": 1}]))
    assert caught.value.data["error_code"] == "file_too_large"
    assert path.read_text() == "old" and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("variant", ["symlink", "parent_symlink", "hardlink", "readonly", "input"])
def test_patch_cannot_bypass_readonly_or_link_boundary(tmp_path, monkeypatch, variant):
    original = tmp_path / "original.txt"
    original.write_text("old")
    path = original
    if variant == "symlink":
        path = tmp_path / "link.txt"
        path.symlink_to(original)
    elif variant == "parent_symlink":
        directory = tmp_path / "linked"
        directory.symlink_to(tmp_path, target_is_directory=True)
        path = directory / original.name
    elif variant == "hardlink":
        os.link(original, tmp_path / "second.txt")
    elif variant == "readonly":
        original.chmod(0o444)
    else:
        monkeypatch.setattr(file_patch, "READONLY_ROOTS", (str(tmp_path),))
    with pytest.raises(file_patch.FilePatchError):
        file_patch.apply_patch(request(path))
    assert original.read_text() == "old"


@pytest.mark.asyncio
async def test_full_digest_is_explicit_even_when_read_output_is_an_excerpt(tmp_path):
    path = tmp_path / "source.py"
    content = "first\r\nsecond 中文\r\nthird\r\n".encode()
    path.write_bytes(content)
    read = await FileService().read_file(str(path), start_line=1, end_line=2,
                                         max_length=6, include_version=True)
    assert read.content == "second(truncated)"
    assert read.content_scope == "line_range_prefix" and read.complete_content is False
    assert read.file_sha256 == hashlib.sha256(content).hexdigest()
    assert read.sha256_scope == "full_file" and read.version_available
    ordinary = await FileService().read_file(str(path), max_length=5)
    assert ordinary.file_sha256 is None and ordinary.version_available is False


@pytest.mark.asyncio
async def test_oversized_read_never_claims_a_preview_hash_is_a_full_version(tmp_path, monkeypatch):
    from app.services import file as file_module
    monkeypatch.setattr(file_module, "MAX_PATCH_BYTES", 8)
    path = tmp_path / "large.txt"
    path.write_text("old" * 30)
    result = await FileService().read_file(str(path), include_version=True, max_length=3)
    assert result.content == "old(truncated)"
    assert not result.version_available and result.file_sha256 is None


@pytest.mark.file_api
def test_real_http_read_patch_stale_base_and_invalid_request(client, tmp_path):
    path = tmp_path / "source.py"
    path.write_text("value = 1\n")
    read = client.post(f"{BASE_URL}/api/v1/file/read", json={"file": str(path), "include_version": True}).json()
    payload = {"file": str(path), "base_sha256": read["data"]["file_sha256"], "syntax_check": "python",
               "replacements": [{"old_str": "value = 1", "new_str": "value = 2", "expected_count": 1}]}
    result = client.post(f"{BASE_URL}/api/v1/file/apply-patch", json=payload)
    assert result.status_code == 200 and result.json()["data"]["changed"] is True
    stale = client.post(f"{BASE_URL}/api/v1/file/apply-patch", json=payload)
    assert stale.status_code == 409 and stale.json()["data"]["error_code"] == "version_conflict"
    invalid = client.post(f"{BASE_URL}/api/v1/file/apply-patch", json={**payload, "sudo": True})
    assert invalid.status_code == 422 and path.read_text() == "value = 2\n"
