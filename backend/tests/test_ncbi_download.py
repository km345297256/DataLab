import hashlib
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.ncbi_download import download_package, validate_package
from app.application.services.ncbi_discovery_service import NcbiDiscoveryService
from app.domain.models.file import FileInfo
from app.application.errors.exceptions import BadRequestError
from app.domain.models.data_connector import ConnectorResult, ConnectorFile

ACCESSION = 'GCF_000009045.1'


def package(accession=ACCESSION, fasta=b'>sequence\nACGT\n', extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        z.writestr('ncbi_dataset/data/assembly_data_report.jsonl', json.dumps({'accession': accession}))
        z.writestr(f'ncbi_dataset/data/{accession}/{accession}_genomic.fna', fasta)
        if extra:
            z.writestr(*extra)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_actual_download_contract_uses_fixed_origin_exact_version_and_fasta():
    data = package()
    def reply(request):
        assert str(request.url).startswith(f'https://api.ncbi.nlm.nih.gov/datasets/v2/genome/accession/{ACCESSION}/download?')
        assert request.url.params['include_annotation_type'] == 'GENOME_FASTA'
        return httpx.Response(200, content=data)
    content, digest, url = await download_package(ACCESSION, transport=httpx.MockTransport(reply))
    assert content == data and digest == hashlib.sha256(data).hexdigest()
    assert url.endswith(f'/{ACCESSION}/download')


@pytest.mark.asyncio
@pytest.mark.parametrize('status,headers,content,reason', [
    (302, {'Location': 'http://localhost/private'}, b'', 'upstream_unavailable'),
    (429, {}, b'', 'rate_limited'),
    (404, {}, b'', 'query_not_found_or_invalid'),
    (200, {'Content-Length': str(33*1024*1024)}, b'', 'download_too_large'),
    (200, {}, b'<html>error</html>', 'invalid_download_package'),
])
async def test_failures_never_become_successful_downloads(status, headers, content, reason):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, headers=headers, content=content))
    with pytest.raises(SourceConnectorError) as error:
        await download_package(ACCESSION, transport=transport)
    assert error.value.reason == reason


@pytest.mark.parametrize('data', [
    package('GCF_000009045.2'), package(fasta=b'not a fasta'),
    package(extra=('../escape', b'unsafe')), package()[:-25],
])
def test_invalid_packages_are_rejected_before_persistence(data):
    with pytest.raises(SourceConnectorError):
        validate_package(data, ACCESSION)


@pytest.mark.asyncio
async def test_ownership_download_persistence_and_reuse():
    data = package()
    digest = hashlib.sha256(data).hexdigest()
    info = FileInfo(file_id='owned-file', filename=f'{ACCESSION}_genomic.zip',
                    size=len(data), user_id='owner', metadata={'sha256': digest})
    repo = SimpleNamespace(get=AsyncMock(return_value=None), save_acquisition=AsyncMock())
    files = SimpleNamespace(upload_file=AsyncMock(return_value=info), get_file_info=AsyncMock(return_value=info))
    sessions = SimpleNamespace(find_by_id_and_user_id=AsyncMock(return_value=object()), upsert_connector_file=AsyncMock())
    downloader = AsyncMock(return_value=(data, digest, 'https://api.ncbi.nlm.nih.gov/datasets/v2/download'))
    service = NcbiDiscoveryService(repo, None, files=files, sessions=sessions, downloader=downloader)
    service.describe = AsyncMock(return_value=SimpleNamespace(candidate_id='candidate', title='Verified organism · '+ACCESSION))
    receipt = await service.acquire('owner', 'session', ACCESSION)
    assert receipt.file_id == 'owned-file'
    sessions.upsert_connector_file.assert_awaited_once_with('session', 'owner', info)
    assert files.upload_file.await_args.args[0].getvalue() == data
    repo.get.return_value = receipt.model_dump()
    assert await service.acquire('owner', 'session', ACCESSION) == receipt
    downloader.assert_awaited_once()
    sessions.find_by_id_and_user_id.return_value = None
    with pytest.raises(BadRequestError):
        await service.acquire('other-owner', 'session', ACCESSION)
    downloader.assert_awaited_once()


def test_metadata_cannot_claim_file_completion():
    with pytest.raises(ValueError):
        ConnectorResult(connector_id='x', connector_version='1.0.0', operation='describe',
                        resources=[], files_downloaded=True)
    with pytest.raises(ValueError):
        ConnectorResult(connector_id='x', connector_version='1.0.0', operation='acquire',
                        kind='files', resources=[], files_downloaded=True)
    with pytest.raises(ValueError):
        ConnectorFile(file_id='x', filename='../escape.zip', size=4, sha256='a'*64, source_url='https://example.org')


def test_download_progress_and_failures_are_explained_without_upstream_content():
    from app.domain.models.event import ToolEvent, ToolStatus
    from app.interfaces.schemas.event import connector_status_text
    event = ToolEvent(tool_call_id='call', tool_name='data_discovery',
        function_name='data_connector_call', function_args={'operation': 'acquire'}, status=ToolStatus.CALLING)
    assert '正在下载' in connector_status_text(event)
    event.status = ToolStatus.CALLED
    event.function_result = {'success': False, 'message': 'private upstream body',
                             'data': {'reason': 'download_too_large'}}
    notice = connector_status_text(event)
    assert '32 MiB' in notice and 'private' not in notice
    event.function_name = 'ncbi_plan_acquisition'
    assert '尚未开始下载' in connector_status_text(event)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['valid', 'without-sandbox', 'missing', 'wrong-owner', 'wrong-id', 'wrong-size'])
async def test_agent_acquisition_returns_only_verified_owned_object_references(monkeypatch, case):
    from app.domain.services.tools.data_discovery import DataDiscoveryToolkit
    from app.application.services.file_service import FileService
    from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
    import app.infrastructure.external.file.factory as factory
    content = package()
    digest = hashlib.sha256(content).hexdigest()
    receipt = ConnectorFile(title='Verified organism', file_id='owned-file', filename=f'{ACCESSION}_genomic.zip',
        size=len(content), sha256=digest, source_url='https://api.ncbi.nlm.nih.gov/datasets/v2/download')
    upstream = SimpleNamespace(acquire=AsyncMock(return_value=receipt))
    info = FileInfo(file_id=receipt.file_id, filename=receipt.filename, size=len(content), user_id='owner')
    if case == 'wrong-owner':
        info.user_id = 'another-owner'
    elif case == 'wrong-id':
        info.file_id = 'another-object'
    elif case == 'wrong-size':
        info.size += 1
    monkeypatch.setattr(factory, 'get_file_storage', lambda: object())
    lookup = AsyncMock(return_value=None if case == 'missing' else info)
    download = AsyncMock(side_effect=AssertionError('Acquisition must not download a stored object'))
    monkeypatch.setattr(FileService, 'get_file_info', lookup)
    monkeypatch.setattr(FileService, 'download_file', download)
    saved = AsyncMock()
    monkeypatch.setattr(MongoSessionRepository, 'upsert_connector_file', saved)
    sandbox = SimpleNamespace(file_upload=AsyncMock(side_effect=AssertionError('Acquisition must not copy files into a sandbox')))
    toolkit = DataDiscoveryToolkit(user_id='owner', session_id='session', service=upstream,
        sandbox=None if case == 'without-sandbox' else sandbox)
    result = await toolkit.get_tool('data_connector_call')._arun(
        connector_id='ncbi-datasets-v2', operation='acquire', arguments={'accession': ACCESSION})
    lookup.assert_awaited_once_with('owned-file', 'owner')
    download.assert_not_awaited()
    sandbox.file_upload.assert_not_awaited()
    assert 'attachments' not in result.data
    if case in {'valid', 'without-sandbox'}:
        assert result.success and result.data['files_downloaded']
        assert result.data['files'][0]['file_id'] == 'owned-file'
        saved.assert_awaited_once()
        assert info.file_path is None
        assert 'artifact_sha256' not in (info.metadata or {})
    else:
        assert not result.success and result.data['reason'] == 'download_integrity_failed'
        saved.assert_not_awaited()
