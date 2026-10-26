"""The trusted zero-match response must never follow a destination write."""
from unittest.mock import AsyncMock

import pytest

from app.services.file import FileService


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "中文报告\n", "x" * 180_000])
async def test_identical_write_preserves_inode_and_mtime(tmp_path, content):
    path = tmp_path / "report.md"
    path.write_text(content, encoding="utf-8")
    before = path.stat()
    result = await FileService().write_file(str(path), content)
    after = path.stat()
    assert result.changed is False and result.bytes_written == 0
    assert result.write_action == "reuse" and result.side_effect_state == "not_started"
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_noop_append_and_identity_patch_never_open_destination_for_write(tmp_path):
    path = tmp_path / "analysis.py"
    path.write_text("saved source\n", encoding="utf-8")
    service = FileService()
    before = path.stat().st_mtime_ns
    assert (await service.write_file(str(path), "", append=True)).changed is False
    service.write_file = AsyncMock(side_effect=AssertionError("identity patch cannot write"))
    result = await service.str_replace(str(path), "source", "source")
    assert result.changed is False and result.replaced_count == 0
    service.write_file.assert_not_awaited()
    assert path.stat().st_mtime_ns == before


@pytest.mark.asyncio
async def test_real_edit_and_append_still_write_exact_bytes(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("旧报告", encoding="utf-8")
    service = FileService()
    result = await service.write_file(str(path), "新报告", trailing_newline=True)
    assert result.bytes_written == len("新报告\n".encode())
    await service.write_file(str(path), "新增章节", append=True)
    assert path.read_text(encoding="utf-8") == "新报告\n新增章节"


@pytest.mark.asyncio
async def test_identity_replace_http_is_successful_reuse_not_missing_text(tmp_path, monkeypatch):
    from app.api.v1 import file as file_api
    from app.schemas.file import FileReplaceRequest
    path = tmp_path / "script.py"
    path.write_text("saved", encoding="utf-8")
    monkeypatch.setattr(file_api, "file_service", FileService())
    result = await file_api.replace_in_file(FileReplaceRequest(
        file=str(path), old_str="saved", new_str="saved"))
    assert result.success is True
    assert result.data["changed"] is False and result.data["write_action"] == "reuse"


@pytest.mark.asyncio
async def test_zero_match_returns_before_any_write_and_preserves_original_file(tmp_path):
    path = tmp_path / "analysis.py"
    original = "print('原始内容')\n"
    path.write_text(original, encoding="utf-8")
    before = path.stat()
    service = FileService()
    service.write_file = AsyncMock(side_effect=AssertionError("zero matches must not write"))

    result = await service.str_replace(str(path), "missing text", "replacement")

    assert result.file == str(path) and result.replaced_count == 0
    service.write_file.assert_not_awaited()
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.asyncio
async def test_zero_match_http_contract_keeps_failure_and_exact_file_identity(tmp_path, monkeypatch):
    from app.api.v1 import file as file_api
    from app.schemas.file import FileReplaceRequest

    path = tmp_path / "script.py"
    path.write_text("original", encoding="utf-8")
    service = FileService()
    service.write_file = AsyncMock(side_effect=AssertionError("no mutation may be attempted"))
    monkeypatch.setattr(file_api, "file_service", service)

    result = await file_api.replace_in_file(FileReplaceRequest(
        file=str(path), old_str="not-present", new_str="replacement"))

    assert result.success is False
    assert result.data == {"file": str(path), "replaced_count": 0}
    service.write_file.assert_not_awaited()
