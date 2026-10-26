"""Scientific API contracts use harmless synthetic entity data and MockTransport."""
import hashlib
import json
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_scientific_batch_definitions import DEFINITIONS, SOURCE_INFO
from app.infrastructure.external.data_sources.open_data_scientific_batch_adapters import ScientificBatchAdapters, SOURCES, safe_file_url
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources

NASA = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync'
URLS = {
    'ngdc-gwh': 'https://download.cncb.ac.cn/gwh/Plants/Rice/GWHAAAA00000001.gff.gz',
    'uniprot': 'https://rest.uniprot.org/uniprotkb/P69905.fasta',
    'rcsb-pdb': 'https://files.rcsb.org/download/1CRN.cif',
    'ena': 'https://www.ebi.ac.uk/ena/browser/api/fasta/AB123456.1?download=true',
    'chembl': 'https://www.ebi.ac.uk/chembl/api/data/molecule/CHEMBL113.sdf',
    'pubchem': 'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/2519/SDF?record_type=2d',
    'pride': 'https://ftp.pride.ebi.ac.uk/pride/data/archive/2020/01/PXD000001/plant_measurements.tsv',
    'metabolights': 'https://www.ebi.ac.uk/metabolights/ws/studies/MTBLS1/download?file=plant_measurements.tsv',
    'metabolomics-workbench': 'https://www.metabolomicsworkbench.org/rest/study/study_id/ST000001/mwtab',
    'nasa-exoplanet': NASA + '?' + urlencode({'query': "select * from pscomppars where pl_name = 'Kepler-22 b'", 'format': 'csv'}),
}


def pride_files(start, end):
    return [{'accession': f'file-{i}', 'projectAccessions': ['PXD000001'],
             'fileName': f'part-{i}.tsv', 'updatedDate': '2026-01-01',
             'publicFileLocations': [{'value': f'ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2020/01/PXD000001/part-{i}.tsv'}]}
            for i in range(start, end)]


@pytest.mark.asyncio
async def test_pride_enumerates_all_project_file_pages_and_keeps_last_file():
    fetch = AsyncMock(side_effect=[{'accession': 'PXD000001', 'publicationDate': '2026-01-01'},
                                  pride_files(0, 100), pride_files(100, 125)])
    options = await ScientificBatchAdapters(fetch).resolve({'source_id': 'pride', 'external_id': 'PXD000001'})
    assert len(options) == 125 and options[-1]['source_filename'] == 'part-124.tsv'
    assert [call.args[1]['page'] for call in fetch.call_args_list[1:]] == [0, 1]
    assert options[-1]['url'].startswith('https://ftp.pride.ebi.ac.uk/')


@pytest.mark.asyncio
async def test_pride_exact_page_boundary_reads_empty_page_before_claiming_completion():
    fetch = AsyncMock(side_effect=[{'accession': 'PXD000001', 'publicationDate': '2026-01-01'}, pride_files(0, 100), []])
    assert len(await ScientificBatchAdapters(fetch).resolve({'source_id': 'pride', 'external_id': 'PXD000001'})) == 100
    assert fetch.await_count == 3


@pytest.mark.asyncio
async def test_pride_provider_ignoring_page_cannot_loop_or_duplicate_files():
    fetch = AsyncMock(side_effect=[{'accession': 'PXD000001', 'publicationDate': '2026-01-01'},
                                  pride_files(0, 100), pride_files(0, 100)])
    with pytest.raises(SourceConnectorError, match='inconsistent_source_snapshot'):
        await ScientificBatchAdapters(fetch).resolve({'source_id': 'pride', 'external_id': 'PXD000001'})
    assert fetch.await_count == 3


@pytest.mark.asyncio
async def test_pride_later_page_retains_source_membership_validation():
    foreign = pride_files(100, 101)
    foreign[0]['projectAccessions'] = ['PXD999999']
    fetch = AsyncMock(side_effect=[{'accession': 'PXD000001', 'publicationDate': '2026-01-01'}, pride_files(0, 100), foreign])
    with pytest.raises(SourceConnectorError, match='invalid_candidate'):
        await ScientificBatchAdapters(fetch).resolve({'source_id': 'pride', 'external_id': 'PXD000001'})


@pytest.mark.parametrize('source', SOURCES)
def test_file_whitelist_is_exact_and_source_specific(source):
    url = URLS[source]
    assert safe_file_url(source, url)
    for other in SOURCES:
        if source != other:
            assert not safe_file_url(other, url)
    for invalid in (url.replace('https://', 'http://'), url.replace('https://', 'https://evil.example/'),
                    url.replace('https://', 'https://user:password@'), url + '#fragment',
                    url.replace('https://', 'https://127.0.0.1/')):
        assert not safe_file_url(source, invalid)


@pytest.mark.parametrize('url', [
    'https://www.ebi.ac.uk:444/chembl/api/data/molecule/CHEMBL113.sdf',
    'https://@www.ebi.ac.uk/chembl/api/data/molecule/CHEMBL113.sdf',
    'https://www.ebi.ac.uk.evil.test/chembl/api/data/molecule/CHEMBL113.sdf',
    'https://www.ebi.ac.uk/chembl/api/data/molecule/%2e%2e/CHEMBL113.sdf',
    'https://www.ebi.ac.uk/chembl/api/data/molecule/CHEMBL113%252e.sdf',
    'https://www.ebi.ac.uk/chembl/api/data/molecule/CHEMBL113.sdf?redirect=x',
])
def test_encoded_paths_and_unregistered_parameters_are_rejected(url):
    assert not safe_file_url('chembl', url)


@pytest.mark.parametrize('suffix', ['file=%252e%252e%252fsecret', 'file=../secret', 'file=a&file=b', 'file=x%00y'])
def test_metabolights_download_query_cannot_escape_study(suffix):
    assert not safe_file_url('metabolights', 'https://www.ebi.ac.uk/metabolights/ws/studies/MTBLS1/download?' + suffix)


@pytest.mark.parametrize('query', ["select * from users", "select * from pscomppars where pl_name = 'x' or 1=1", "select top 1 * from pscomppars"])
def test_nasa_export_cannot_change_query_scope(query):
    assert not safe_file_url('nasa-exoplanet', NASA + '?' + urlencode({'query': query, 'format': 'csv'}))


@pytest.mark.parametrize('page', [0, -1, True, 101, '2'])
@pytest.mark.asyncio
async def test_invalid_paging_is_rejected_before_network(page):
    async def fetch(*args, **kwargs):
        pytest.fail('invalid page must not contact a provider')
    with pytest.raises(SourceConnectorError, match='invalid_cursor'):
        await ScientificBatchAdapters(fetch).search('chembl', 'caffeine', page)


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['caffeine', '"caffeine"', '  "caffeine"  '])
async def test_pubchem_literal_name_uses_provider_value_without_query_quotes(query):
    def handle(request):
        if request.url.path.endswith('/cids/JSON'):
            assert request.url.path == '/rest/pug/compound/name/caffeine/cids/JSON'
            return httpx.Response(200, json={'IdentifierList': {'CID': [2519]}})
        return httpx.Response(200, json={'PropertyTable': {'Properties': [{'CID': 2519, 'Title': 'Caffeine'}]}})
    result = await OpenDataSources(transport=httpx.MockTransport(handle)).search('pubchem', query)
    assert [r['external_id'] for r in result['results']] == ['2519']


@pytest.mark.asyncio
async def test_pubchem_paging_uses_resolved_cids_and_ignores_foreign_properties():
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if request.url.path.endswith('/cids/JSON'):
            return httpx.Response(200, json={'IdentifierList': {'CID': list(range(1, 24))}})
        assert '/cid/11,12,13,14,15,16,17,18,19,20/property/' in request.url.path
        return httpx.Response(200, json={'PropertyTable': {'Properties': [{'CID': 11, 'Title': 'Example'}, {'CID': 999, 'Title': 'Foreign'}]}})
    sources = OpenDataSources(transport=httpx.MockTransport(handle))
    result = await ScientificBatchAdapters(sources._json).search('pubchem', 'caffeine', 2)
    assert result['total_count'] == 23 and [r['external_id'] for r in result['results']] == ['11']
    assert result['results'][0]['access'] == 'needs_resolution'
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_chembl_search_paging_and_metadata_only_rows():
    def handle(request):
        assert request.url.params['offset'] == '10' and request.url.params['limit'] == '10'
        return httpx.Response(200, json={'page_meta': {'total_count': 17}, 'molecules': [
            {'molecule_chembl_id': 'CHEMBL113', 'pref_name': 'Caffeine', 'molecule_structures': {'molfile': 'entity'}},
            {'molecule_chembl_id': 'CHEMBL1', 'pref_name': 'Only metadata', 'molecule_structures': None}]})
    sources = OpenDataSources(transport=httpx.MockTransport(handle))
    result = await ScientificBatchAdapters(sources._json).search('chembl', 'caffeine', 2)
    assert result['total_count'] == 17 and len(result['results']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['Kepler', '"Kepler"'])
async def test_nasa_local_paging_is_stable_and_full_table_scope_is_explicit(query):
    def handle(request):
        assert request.url.params['query'] == 'select pl_name,hostname from pscomppars order by pl_name'
        return httpx.Response(200, json=[{'pl_name': f'Kepler-{i} b', 'hostname': f'Kepler-{i}'} for i in range(23)])
    sources = OpenDataSources(transport=httpx.MockTransport(handle))
    adapter = ScientificBatchAdapters(sources._json)
    result = await adapter.search('nasa-exoplanet', query, 2)
    assert result['total_count'] == 23 and len(result['results']) == 10
    assert result['results'][0]['external_id'] == 'Kepler-10 b'
    full = await adapter.search('nasa-exoplanet', 'exoplanets', 1)
    assert full['results'][0]['external_id'] == 'pscomppars'
    assert full['results'][0]['coverage']['scope'] == '全部 pscomppars 行与列'
    assert (await adapter.search('nasa-exoplanet', 'exoplanets', 2))['results'] == []


@pytest.mark.parametrize('source,external,payload', [
    ('chembl', 'CHEMBL113', {'molecule_chembl_id': 'CHEMBL113', 'molecule_structures': None}),
    ('chembl', 'CHEMBL113', {'molecule_chembl_id': 'CHEMBL999', 'molecule_structures': {'molfile': 'entity'}}),
    ('pubchem', '2519', {'PC_Compounds': [{'id': {'id': {'cid': 2519}}}]}),
    ('pubchem', '2519', {'PC_Compounds': [{'id': {'id': {'cid': 999}}, 'atoms': {'aid': [1]}}]}),
    ('nasa-exoplanet', 'Kepler-22 b', [{'pl_name': 'Wrong record'}]),
])
@pytest.mark.asyncio
async def test_resolve_requires_matching_record_and_entity_data(source, external, payload):
    sources = OpenDataSources(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    assert await ScientificBatchAdapters(sources._json).resolve({'source_id': source, 'external_id': external,
        'url': 'https://evil.test/file', 'metadata': {'url': 'https://evil.test/file'}}) == []


@pytest.mark.parametrize('source,external,payload,change', [
    ('chembl', 'CHEMBL113', {'molecule_chembl_id': 'CHEMBL113', 'molecule_structures': {'molfile': 'v1'}},
        lambda row: row['molecule_structures'].update(molfile='v2')),
    ('pubchem', '2519', {'PC_Compounds': [{'id': {'id': {'cid': 2519}}, 'atoms': {'aid': [1, 2]}, 'bonds': {'order': [1]}}]},
        lambda row: row['PC_Compounds'][0]['bonds'].update(order=[2])),
    ('nasa-exoplanet', 'Kepler-22 b', [{'pl_name': 'Kepler-22 b', 'pl_orbper': 290.1}],
        lambda row: row[0].update(pl_orbper=290.2)),
])
@pytest.mark.asyncio
async def test_entity_changes_invalidate_selection_and_source_terms_are_retained(source, external, payload, change):
    sources = OpenDataSources(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    adapter = ScientificBatchAdapters(sources._json)
    candidate = {'source_id': source, 'external_id': external, 'url': 'https://evil.test/file', 'license': 'invented'}
    first, = await adapter.resolve(candidate)
    assert first['license'].startswith('https://') and first['license'] != 'invented'
    assert safe_file_url(source, first['url']) and first['size'] is None
    change(payload)
    second, = await adapter.resolve(candidate)
    assert second['id'] != first['id']


@pytest.mark.asyncio
async def test_ena_pins_public_sequence_version_and_refuses_old_candidate():
    payload = {'summaries': [{'accession': 'AB123456', 'statusDescription': 'public', 'dataType': 'SEQUENCE', 'sequenceLength': 3, 'version': 2}]}
    sources = OpenDataSources(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    adapter = ScientificBatchAdapters(sources._json)
    option, = await adapter.resolve({'source_id': 'ena', 'external_id': 'AB123456'})
    assert '/AB123456.2?' in option['url'] and option['version'] == '2'
    with pytest.raises(SourceConnectorError, match='invalid_candidate'):
        await adapter.resolve({'source_id': 'ena', 'external_id': 'AB123456.1'})


@pytest.mark.asyncio
async def test_metabolights_metadata_is_never_an_entity_file():
    def handle(request):
        if request.url.path.endswith('/files'):
            return httpx.Response(200, json={'study': [
                {'file': 'i_Investigation.txt', 'type': 'metadata_investigation', 'status': 'active'},
                {'file': 's_samples.txt', 'type': 'metadata_sample', 'status': 'active'},
                {'file': 'a_assay.txt', 'type': 'metadata_assay', 'status': 'active'},
                {'file': 'rice_measurements.tsv', 'type': 'data', 'status': 'active', 'timestamp': '2026'}]})
        return httpx.Response(200, json={'mtblsStudy': {'studyStatus': 'Public', 'read_access': True, 'revisionNumber': 2}})
    sources = OpenDataSources(transport=httpx.MockTransport(handle))
    option, = await ScientificBatchAdapters(sources._json).resolve({'source_id': 'metabolights', 'external_id': 'MTBLS1'})
    assert option['source_filename'] == 'MTBLS1_rice_measurements.tsv'


@pytest.mark.asyncio
async def test_facade_verifies_and_reads_complete_entity_file(tmp_path, monkeypatch):
    monkeypatch.setenv('OPEN_DATA_DOWNLOAD_ROOT', str(tmp_path))
    entity = b'Caffeine\n  DataLab\n\n  1  0  0  0  0  0  0  0  0  0999 V2000\n    0.0 0.0 0.0 C\nM  END\n$$$$\n'
    def handle(request):
        if request.url.path.endswith('.json'):
            return httpx.Response(200, json={'molecule_chembl_id': 'CHEMBL113', 'molecule_structures': {'molfile': entity.decode()}})
        assert str(request.url) == URLS['chembl']
        return httpx.Response(200, content=entity, headers={'Content-Type': 'chemical/x-mdl-sdfile'})
    sources = OpenDataSources(transport=httpx.MockTransport(handle))
    candidate = {'source_id': 'chembl', 'external_id': 'CHEMBL113'}
    verified = await sources.verify(candidate)
    assert len(verified) == 1 and verified[0]['verification']['sample_bytes'] == len(entity)
    stream, filename, _, receipt = await sources.download(candidate, {**verified[0], 'url': 'https://evil.test/file'})
    try:
        assert stream.read() == entity and filename.endswith('.sdf')
        assert receipt['sha256'] == hashlib.sha256(entity).hexdigest()
        assert receipt['license'] == 'https://creativecommons.org/licenses/by-sa/3.0/'
    finally:
        stream.close()


def test_definitions_have_official_branding_and_only_accepted_sources_enabled():
    assert {row['id'] for row in DEFINITIONS} == set(SOURCES) == set(SOURCE_INFO)
    assert all(row['homepage'].startswith('https://') and row['logo_url'].startswith('https://') for row in SOURCE_INFO.values())
    ngdc = next(row for row in DEFINITIONS if row['id'] == 'ngdc-gwh')
    assert ngdc['enabled'] is True
    assert {row['id'] for row in DEFINITIONS if row['enabled']} == set(SOURCES)
    assert all('待验收' in row['limitation'] and row['connector_enabled'] is False for row in DEFINITIONS if not row['enabled'])


def test_brand_names_keep_official_multiword_names():
    names = {row["id"]: row["connector_title"] for row in DEFINITIONS}
    assert names["ngdc-gwh"] == "NGDC GWH"
    assert names["rcsb-pdb"] == "RCSB PDB"
    assert names["nasa-exoplanet"] == "NASA Exoplanet"
    assert names["metabolomics-workbench"] == "Metabolomics Workbench"


def test_structure_prefix_does_not_claim_complete_coordinates():
    from app.infrastructure.external.data_sources.open_data_scientific_batch_adapters import is_scientific_observation_sample
    prefix = b'data_1ABC\n_entry.id 1ABC\n'
    assert is_scientific_observation_sample('rcsb-pdb', prefix, '1ABC')
    assert not is_scientific_observation_sample('rcsb-pdb', prefix, '1ABC', complete=True)
    assert not is_scientific_observation_sample('rcsb-pdb', prefix, '2XYZ')
    assert is_scientific_observation_sample('rcsb-pdb', prefix + b'_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\n', '1ABC', complete=True)


def test_measurement_json_rejects_metadata_only_and_other_records():
    from app.infrastructure.external.data_sources.open_data_scientific_batch_adapters import is_scientific_observation_sample
    payload = [{'METABOLOMICS WORKBENCH': {'STUDY_ID': 'ST000001'}}]
    def accepted():
        return is_scientific_observation_sample('metabolomics-workbench', json.dumps(payload).encode(), 'ST000001', complete=True)
    assert not accepted()
    payload[0]['TEST_METABOLITE_DATA'] = {'Data': [{'metabolite': 'example', 'sample_1': '3.5'}]}
    assert accepted()
    payload[0]['METABOLOMICS WORKBENCH']['STUDY_ID'] = 'ST000002'
    assert not accepted()


@pytest.mark.asyncio
async def test_structure_endpoint_plain_error_never_becomes_downloaded_data(tmp_path, monkeypatch):
    monkeypatch.setenv('OPEN_DATA_DOWNLOAD_ROOT', str(tmp_path))
    s = OpenDataSources(transport=httpx.MockTransport(lambda request: httpx.Response(200, text='The requested data is unavailable.')))
    option = {'source_id': 'rcsb-pdb', 'external_id': '1ABC', 'url': 'https://files.rcsb.org/download/1ABC.cif',
              'filename': '1ABC.cif', 'content_type': 'chemical/x-cif', 'size': None, 'checksum': None}
    assert await s._probe_file(option) is None
    with pytest.raises(SourceConnectorError, match='no_observations'):
        await s._stream_file({'source_id': 'rcsb-pdb', 'external_id': '1ABC'}, option, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['Oryza sativa', '"Oryza sativa"'])
async def test_ngdc_uses_numeric_record_page_and_preserves_annotation_identity(query):
    url = URLS['ngdc-gwh']
    def handle(request):
        if request.url.path.endswith('/specific'):
            assert request.url.params['start'] == '10'
            assert request.url.params['q'] == f'({query}) AND GWH'
            return httpx.Response(200, json={'code': 200, 'result': {'data': {'recordsFiltered': 12, 'data': [
                {'id': 'GWHAAAA00000001', 'title': 'Rice example', 'type': 'gwh_assembly',
                 'url': 'https://ngdc.cncb.ac.cn/gwh/Assembly/12/show', 'attrs': {'scientific_name': 'Oryza sativa'}}]}}})
        assert request.url.path.endswith('/assembly/GWHAAAA00000001')
        return httpx.Response(200, json={'id': '12', 'assemblyAccession': 'GWHAAAA00000001',
            'releaseTime': '2026-01-01', 'organism': 'Oryza sativa', 'organismType': 'Plants', 'ftpPathFeature': url.replace('.gff.', '.feature.')})
    sources = OpenDataSources(transport=httpx.MockTransport(handle))
    adapter = ScientificBatchAdapters(sources._json)
    result = await adapter.search('ngdc-gwh', query, 2)
    candidate, = result['results']
    assert candidate['source_url'] == 'https://ngdc.cncb.ac.cn/gwh/Assembly/12/show'
    candidate['metadata'] = {'url': 'https://evil.test/file'}
    option, = await adapter.resolve(candidate)
    assert option['record_url'] == candidate['source_url'] and option['license'] is None
    assert option['version'] == 'GWHAAAA00000001:2026-01-01'
    assert option['source_filename'].endswith('.feature.gz') and option['kind'] == '完整基因注释表'


@pytest.mark.parametrize('url', ['https://evil.test/Assembly/12/show', 'https://ngdc.cncb.ac.cn/gwh/Assembly/../show'])
@pytest.mark.asyncio
async def test_ngdc_search_rejects_untrusted_record_links(url):
    payload = {'code': 200, 'result': {'data': {'recordsFiltered': 1, 'data': [{'id': 'GWHAAAA00000001',
        'title': 'Rice example', 'type': 'gwh_assembly', 'url': url}]}}}
    sources = OpenDataSources(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    with pytest.raises(SourceConnectorError, match='invalid_response'):
        await ScientificBatchAdapters(sources._json).search('ngdc-gwh', 'Oryza sativa', 1)
