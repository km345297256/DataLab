"""Registered provider redirects share bounded probes and durable transfers."""
import hashlib

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources


ORIGIN = "https://ndownloader.figshare.com/files/123"
STORAGE = "https://s3-eu-west-1.amazonaws.com/pfigshare-u-files/123/data.csv"


@pytest.mark.parametrize("filename", ["00_ReadMe_MIT_Libraries_Coloring_Books.txt", "01-license.txt", "001_metadata.json"])
def test_numbered_repository_documentation_is_not_entity_data(filename):
    assert OpenDataSources._document_option({"source_filename": filename})
    assert not OpenDataSources._document_option({"source_filename": "00_iris_measurements.csv"})


def option(data):
    return dict(id="file123", source_id="figshare", external_id="42", filename="data.csv",
                source_filename="data.csv", content_type="text/csv", size=len(data), available=True,
                url=ORIGIN, checksum="md5:" + hashlib.md5(data).hexdigest(),
                record_url="https://figshare.com/articles/dataset/_/42")


@pytest.mark.asyncio
async def test_registered_redirect_is_probed_and_downloaded_with_safe_receipt(monkeypatch):
    data = b"x,y\n1,2\n"
    calls = []
    def handler(request):
        calls.append(request)
        if str(request.url) == ORIGIN:
            return httpx.Response(302, headers={"Location": STORAGE})
        assert str(request.url) == STORAGE
        return httpx.Response(200, content=data, headers={"Content-Type": "text/csv"})
    sources = OpenDataSources(transport=httpx.MockTransport(handler))
    async def resolve(_):
        return [option(data)]
    monkeypatch.setattr(sources, "resolve", resolve)
    candidate = dict(source_id="figshare", external_id="42", source_url="https://figshare.com/articles/dataset/_/42")
    checked = await sources.verify(candidate)
    assert len(checked) == 1 and checked[0]["verification"]["sample_bytes"] == len(data)
    stream, _, _, receipt = await sources.download(candidate, {"id": "file123", "url": "http://localhost/private"})
    try:
        assert stream.read() == data
        assert receipt["source_url"] == candidate["source_url"]
        assert receipt["provider_checksum_verified"]
    finally:
        stream.close()
    assert len(calls) == 4
    assert calls[1].headers["Range"] == "bytes=0-16383"


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "https://s3-eu-west-1.amazonaws.com/other-bucket/file",
                                     "https://ndownloader.figshare.com.evil.example/files/123"])
async def test_unregistered_redirect_is_rejected_before_contact(location):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": location})
    sources = OpenDataSources(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceConnectorError, match="unsafe_download_redirect"):
        await sources._probe_file(option(b"x,y\n1,2\n"))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": ORIGIN})
    sources = OpenDataSources(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceConnectorError, match="unsafe_download_redirect"):
        await sources._probe_file(option(b"x,y\n1,2\n"))
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_resume_retains_range_across_registered_redirects(monkeypatch, tmp_path):
    monkeypatch.setenv("OPEN_DATA_DOWNLOAD_ROOT", str(tmp_path))
    data = b"a" * 70000
    ranges = []
    def handler(request):
        if str(request.url) == ORIGIN:
            return httpx.Response(302, headers={"Location": STORAGE})
        start = int(request.headers.get("Range", "bytes=0-")[6:].split("-")[0])
        ranges.append(start)
        headers = {"ETag": '"stable-file"'}
        if start:
            headers["Content-Range"] = f"bytes {start}-{len(data)-1}/{len(data)}"
        return httpx.Response(206 if start else 200, content=data[start:], headers=headers)
    sources = OpenDataSources(transport=httpx.MockTransport(handler))
    candidate = dict(source_id="figshare", external_id="42")
    async def resolve(_):
        return [option(data)]
    monkeypatch.setattr(sources, "resolve", resolve)
    async def interrupted(*_):
        raise RuntimeError("simulated connection loss")
    with pytest.raises(RuntimeError, match="simulated"):
        await sources.download(candidate, option(data), interrupted, transfer_key="a" * 32)
    stream, *_ = await sources.download(candidate, option(data), transfer_key="a" * 32)
    try:
        assert stream.read() == data
    finally:
        stream.close()
    assert ranges == [0, 65536]
    await sources.cleanup_transfer("a" * 32)
