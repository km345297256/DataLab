import asyncio
import io
import threading

import httpx

import pytest

from app.domain.models.open_data_registry import connector_sources, source_catalog
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources


FOUNDATIONS={'oeis','miplib','gwosc','nasa-cdaweb','crystallography-open-database','nist-thermoml','nomad','cds-vizier','mast'}


def test_foundation_sources_are_selectable_and_automatically_discoverable():
    definitions={s.id:s for s in connector_sources()}
    assert FOUNDATIONS <= definitions.keys()
    assert FOUNDATIONS <= {s['id'] for s in source_catalog()}
    assert FOUNDATIONS <= OpenDataSources()._adapters.keys()
    for key in FOUNDATIONS:
        assert definitions[key].authentication=='none'
        assert definitions[key].subjects
        assert definitions[key].limitation
    assert 'lmfdb' not in definitions


@pytest.mark.asyncio
async def test_cancel_large_validation_stops_worker_before_closing_transfer(monkeypatch):
    from app.infrastructure.external.data_sources import open_data_sources as main
    from app.infrastructure.external.data_sources import open_data_chem_materials_foundation_adapters as chemistry
    stream=io.BytesIO(b'data_1000001\n_cell_length_a 1\nloop_\n_atom_site_fract_x\n_atom_site_fract_y\n_atom_site_fract_z\n0.1 0.2 0.3\n')
    started=threading.Event();stopped=threading.Event()
    async def transfer(*args,**kwargs):return stream,'test-digest'
    def validate(source, body, chosen, *, cancelled=None):
        started.set()
        assert cancelled.wait(3), 'Cancellation should reach validator promptly'
        assert not body.closed, 'Do not close while a worker is still reading'
        stopped.set()
        raise asyncio.CancelledError()
    monkeypatch.setattr(main,'stream_download',transfer)
    monkeypatch.setattr(chemistry,'validate_chem_materials_stream',validate)
    chosen={'source_id':'crystallography-open-database','external_id':'1000001',
            'url':'https://www.crystallography.net/cod/1000001.cif@1','filename':'structure.cif','content_type':'text/plain'}
    task=asyncio.create_task(OpenDataSources()._stream_file(chosen,chosen,None))
    async def wait_started():
        while not started.is_set():await asyncio.sleep(.005)
    await asyncio.wait_for(wait_started(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert stopped.is_set() and stream.closed


@pytest.mark.asyncio
async def test_thermoml_normal_get_probe_is_still_bounded():
    body=b'<DataReport xmlns="http://www.iupac.org/namespaces/ThermoML"><Citation>'+b'x'*40000
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,content=body,headers={'content-type':'application/xml'})
    s=OpenDataSources(transport=httpx.MockTransport(handle))
    option={'source_id':'nist-thermoml','external_id':'10.1021/je000001a',
            'url':'https://trc.nist.gov/ThermoML/10.1021/je000001a.xml','filename':'experiment.xml',
            'content_type':'application/xml','probe_without_range':True}
    result=await s._probe_file(option)
    assert result and result['verification']['sample_bytes']==16384
    assert result['size']==len(body) and 'range' not in requests[0].headers


@pytest.mark.asyncio
async def test_only_thermoml_requests_full_restart_instead_of_broken_source_ranges():
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,content=b'data')
    source=OpenDataSources(transport=httpx.MockTransport(handle))
    for key,external,url in [
        ('nist-thermoml','10.1021/je000001a','https://trc.nist.gov/ThermoML/10.1021/je000001a.xml'),
        ('oeis','A000045','https://oeis.org/A000045/b000045.txt'),
    ]:
        option={'source_id':key,'external_id':external,'url':url}
        async with source._client() as client:
            async with source._file_response(client,option,headers={'Range':'bytes=10-','If-Range':'"etag"','Accept-Encoding':'identity'}) as response:
                assert response.status_code==200
    assert 'range' not in requests[0].headers and 'if-range' not in requests[0].headers
    assert requests[1].headers['range']=='bytes=10-' and requests[1].headers['if-range']=='"etag"'
    assert all(r.headers['accept-encoding']=='identity' for r in requests)
