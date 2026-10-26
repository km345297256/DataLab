from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.application.services.open_data_connector_registration import open_data_registration, open_data_registrations
from app.domain.models import open_data_registry as registry
from app.domain.services.tools.data_discovery import DISCOVERY_GUIDANCE, DataDiscoveryToolkit
from app.domain.services.tools.tool_contract import audited_public_data_read


def test_disabled_source_is_not_exposed_or_treated_as_trusted_public_read(monkeypatch):
    disabled = replace(registry.get_source('zenodo'), id='unvalidated-source', enabled=False)
    monkeypatch.setattr(registry, 'SOURCE_DEFINITIONS', (*registry.SOURCE_DEFINITIONS, disabled))
    assert registry.get_source('unvalidated-source') is None
    assert registry.get_source('unvalidated-source', include_disabled=True) == disabled
    assert 'unvalidated-source' not in {source['id'] for source in registry.source_catalog()}
    assert 'unvalidated-source' not in {connector.manifest.id for connector in open_data_registrations(SimpleNamespace())}
    assert 'unvalidated-source' not in registry.public_connector_read_operations()
    with pytest.raises(ValueError):
        open_data_registration(SimpleNamespace(), 'unvalidated-source')


def test_credentialed_future_source_never_gains_anonymous_read_approval(monkeypatch):
    credentialed = replace(registry.get_source('zenodo'), id='private-example', authentication='api_key')
    monkeypatch.setattr(registry, 'SOURCE_DEFINITIONS', (*registry.SOURCE_DEFINITIONS, credentialed))
    toolkit = DataDiscoveryToolkit(user_id='owner', session_id='session', service=object())
    assert not audited_public_data_read(toolkit.get_tool('data_connector_call'),
                                       {'connector_id': 'private-example', 'operation': 'search'})


def test_legacy_ncbi_connectors_keep_their_existing_download_implementations():
    assert registry.get_source('ncbi-geo').connector_registration == 'legacy'
    assert registry.get_source('ncbi-sra').connector_registration == 'legacy'
    assert {'ncbi-geo', 'ncbi-sra'}.isdisjoint(source.id for source in registry.connector_sources())
    assert registry.get_source('datacite').discovery_mode == 'doi_index'
    assert registry.get_source('datacite').acquisition == 'resolved_files'


def test_agent_guidance_and_catalog_include_every_active_source():
    assert registry.SOURCES == frozenset(source['id'] for source in registry.CATALOG)
    for source in registry.source_definitions():
        if source.automatic_discovery:
            assert f'{source.id}: {source.description}' in DISCOVERY_GUIDANCE
        assert source.authentication == 'none'


def test_oct23_twenty_verified_sources_have_real_adapters_and_connector_entries():
    from app.domain.models.open_data_earth_oct23_definitions import DEFINITIONS as earth
    from app.domain.models.open_data_repositories_oct23_definitions import DEFINITIONS as repositories
    from app.domain.models.open_data_statistics_oct23_definitions import DEFINITIONS as statistics
    from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
    ids = {row['id'] for row in (*earth, *repositories, *statistics) if row.get('enabled', True)}
    ids.update({'uniprot', 'rcsb-pdb', 'ena', 'pride', 'metabolights', 'metabolomics-workbench'})
    sources = OpenDataSources()
    connectors = {entry.manifest.id for entry in open_data_registrations(sources)}
    assert len(ids) == 20
    assert ids <= registry.SOURCES & sources._adapters.keys() & connectors
    assert all(registry.get_source(source_id).authentication == 'none' for source_id in ids)
    assert 'earthscope' not in registry.SOURCES | connectors
    assert 'qdr' not in registry.SOURCES | connectors


def test_first_batch_only_exposes_sources_with_verified_anonymous_downloads():
    connectors = {source.id for source in registry.connector_sources()}
    for source_id in ('harvard-dataverse', 'openml'):
        assert source_id in registry.SOURCES and source_id in connectors
        assert source_id in registry.public_connector_read_operations()
    for source_id in ('figshare', 'dryad'):
        assert registry.get_source(source_id, include_disabled=True) is not None
        assert source_id not in registry.SOURCES and source_id not in connectors
        assert source_id not in registry.public_connector_read_operations()
    assert registry.get_source('dryad', include_disabled=True).authentication == 'oauth2'


@pytest.mark.parametrize('doi,expected', [
    ('10.5281/zenodo.123', ('zenodo', '123')),
    ('10.7910/DVN/WZBHHR', ('harvard-dataverse', 'doi:10.7910/dvn/wzbhhr')),
    ('10.5683/SP3/ABC123', ('borealis', 'doi:10.5683/sp3/abc123')),
    ('10.34894/ABC123', ('dataverse-nl', 'doi:10.34894/abc123')),
    ('10.57745/ABC123', ('recherche-data-gouv', 'doi:10.57745/abc123')),
    ('10.1594/PANGAEA.841672', ('pangaea', '10.1594/pangaea.841672')),
    ('10.5281/zenodo.123/more', None), ('10.5281/zenodo.0', None),
    ('10.7910/DVN/', None), ('10.1234/unknown', None),
    ('https://doi.org/10.7910/DVN/EXAMPLE', None),
    ('10.7910/DVN/EXAMPLE?redirect=evil', None),
    ('10.5061/dryad.123456', None),
])
def test_doi_resolution_is_confined_to_enabled_registered_namespaces(doi, expected):
    assert registry.doi_repository(doi) == expected


def test_disabled_repository_is_never_resolved_from_doi(monkeypatch):
    monkeypatch.setattr(registry, 'SOURCE_DEFINITIONS', tuple(
        replace(source, enabled=False) if source.id == 'borealis' else source
        for source in registry.SOURCE_DEFINITIONS))
    assert registry.doi_repository('10.5683/SP3/ABC123') is None


def test_explicit_selection_can_include_entire_expanded_catalog():
    from app.domain.models.open_data import SearchInput
    request = SearchInput(query='soil moisture', source_ids=list(registry.SOURCES))
    assert set(request.source_ids) == registry.SOURCES
