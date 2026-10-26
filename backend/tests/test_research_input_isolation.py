"""Research inputs must be owned, and upload annotations cannot grant authority."""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile

from app.application.errors.exceptions import BadRequestError
from app.domain.models.file import FileInfo
from app.domain.services.agent_domain_service import AgentDomainService
from app.infrastructure.external.file.gridfsfile import GridFSFileStorage
from app.interfaces.api.file_routes import upload_file, init_large_upload
from app.interfaces.schemas.file import LargeUploadInitRequest


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', [None, 'other-researcher'])
async def test_unavailable_or_foreign_attachment_cannot_be_admitted(owner):
    service = AgentDomainService.__new__(AgentDomainService)
    info = FileInfo(file_id='file', filename='private.csv', user_id=owner) if owner else None
    service._file_storage = SimpleNamespace(get_file_info=AsyncMock(return_value=info))
    with pytest.raises(BadRequestError):
        await service._resolve_message_attachments([{'file_id': 'file', 'filename': 'claimed.csv'}], 'researcher')


@pytest.mark.asyncio
async def test_gridfs_upload_metadata_cannot_overwrite_owner_or_server_filename():
    storage = GridFSFileStorage.__new__(GridFSFileStorage)
    bucket = SimpleNamespace(upload_from_stream=AsyncMock(return_value='opaque'))
    storage._get_gridfs_bucket = lambda: bucket
    storage._get_files_collection = lambda: SimpleNamespace(find_one=AsyncMock(return_value={'length': 3}))
    await storage.upload_file(BytesIO(b'abc'), 'real.csv', 'researcher', metadata={
        'user_id': 'victim', 'filename': '/private/secret.csv', 'uploadDate': 'forged',
    })
    metadata = bucket.upload_from_stream.call_args.kwargs['metadata']
    assert metadata['user_id'] == 'researcher'
    assert metadata['filename'] == 'real.csv'
    assert metadata['uploadDate'] != 'forged'


@pytest.mark.asyncio
async def test_http_upload_annotations_are_separate_from_acquisition_receipts(monkeypatch):
    info = FileInfo(file_id='opaque', filename='real.csv', user_id='researcher', size=3)
    service = SimpleNamespace(upload_file=AsyncMock(return_value=info))
    async def response(file):
        return {'file_id': file.file_id, 'filename': file.filename, 'size': file.size}
    monkeypatch.setattr('app.interfaces.api.file_routes.FileInfoResponse.from_file_info', response)
    await upload_file(UploadFile(filename='real.csv', file=BytesIO(b'abc')),
                      '{"user_id":"victim","source":"data_connector","file_path":"/private/secret"}',
                      service, SimpleNamespace(id='researcher'))
    args = service.upload_file.call_args.kwargs
    assert args['user_id'] == 'researcher'
    assert 'source' not in args['metadata'] and 'file_path' not in args['metadata']
    assert args['metadata']['user_metadata']['source'] == 'data_connector'


@pytest.mark.asyncio
async def test_multipart_upload_annotations_are_separate_from_service_metadata():
    service = SimpleNamespace(init_large_upload=AsyncMock(side_effect=RuntimeError('stop after boundary')))
    request = LargeUploadInitRequest(filename='large.csv', size=10, metadata={'source': 'data_connector', 'user_id': 'victim'})
    with pytest.raises(RuntimeError, match='stop after boundary'):
        await init_large_upload(request, service, SimpleNamespace(id='researcher'))
    args = service.init_large_upload.call_args.kwargs
    assert args['user_id'] == 'researcher'
    assert args['metadata'] == {'user_metadata': request.metadata}
