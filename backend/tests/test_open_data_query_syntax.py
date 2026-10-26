"""Provider query contracts: ordinary topic terms must not silently become OR."""
import json

import httpx
import pytest

from app.domain.models.open_data_registry import get_source
from app.domain.models.open_data_government_batch_definitions import DEFINITIONS as CKAN_DEFINITIONS
from app.infrastructure.external.data_sources.open_data_government_batch_adapters import GovernmentBatchAdapters, SOURCES as CKAN_SOURCES
from app.infrastructure.external.data_sources.open_data_query_syntax import all_terms_query
from app.infrastructure.external.data_sources.open_data_repository_adapters import _dataverse_query
from app.infrastructure.external.data_sources.open_data_research_oct22_adapters import ResearchOct22Adapters
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources


@pytest.mark.parametrize("query,expected", [
    ("  Tibetan  language  ", "Tibetan AND language"),
    ("marine sediment chemistry", "marine AND sediment AND chemistry"),
    ("藏文 语言", "藏文 AND 语言"),
    ('"Tibetan language"', '"Tibetan language"'),
    ('"sign language" acquisition', '"sign language" AND acquisition'),
    ('"language AND cognition"', '"language AND cognition"'),
    ('language OR speech', 'language OR speech'),
    ('language AND speech', 'language AND speech'),
    ('language NOT speech', 'language NOT speech'),
    ('language || speech', 'language || speech'),
    ('language && speech', 'language && speech'),
    ('title:"sign language" education', 'title:"sign language" education'),
    ('(language OR speech) education', '(language OR speech) education'),
    ('+language -speech', '+language -speech'),
    ('language^2 speech', 'language^2 speech'),
    ('year:[2020 TO 2026] language', 'year:[2020 TO 2026] language'),
    ('language\\ development speech', 'language\\ development speech'),
    ('"unclosed phrase', '"unclosed phrase'),
    ('language', 'language'),
])
def test_plain_topic_terms_are_required_without_rewriting_expert_syntax(query, expected):
    assert all_terms_query(query) == expected
    # Existing Dataverse/PANGAEA callers retain the same implementation.
    assert _dataverse_query is all_terms_query


@pytest.mark.parametrize("source", sorted(CKAN_SOURCES))
@pytest.mark.asyncio
async def test_every_ckan_portal_explicitly_requires_all_plain_topic_terms(source):
    async def fetch(url, params):
        assert url.endswith("/api/3/action/package_search")
        assert params == {"q": "marine AND chemistry", "rows": 10, "start": 10}
        return {"success": True, "result": {"results": [], "count": 0}}

    assert not (await GovernmentBatchAdapters(fetch).search(source, "marine chemistry", 2))["results"]
    assert next(row for row in CKAN_DEFINITIONS if row["id"] == source)["query_semantics"] == "all_terms"


@pytest.mark.parametrize("source", ["switzerland-open-data", "ebi-biostudies", "rodare", "zenodo"])
@pytest.mark.parametrize("query,expected", [
    ("Tibetan language", "Tibetan AND language"),
    ("quasixyznotreal cancer", "quasixyznotreal AND cancer"),
    ('"sign language" acquisition', '"sign language" AND acquisition'),
    ('"sign language"', '"sign language"'),
    ('language OR speech', 'language OR speech'),
    ('title:"sign language" education', 'title:"sign language" education'),
])
@pytest.mark.asyncio
async def test_provider_request_retains_required_subject_and_explicit_user_syntax(source, query, expected):
    calls = []

    async def fetch(url, params=None):
        calls.append((url, params))
        if source == "switzerland-open-data":
            assert params["q"] == expected
            return {"success": True, "result": {"results": [], "count": 0}}
        if source == "ebi-biostudies":
            assert params["query"] == expected
            assert params["files"] == "[1 TO *]" and params["page"] == 2
            return {"hits": [], "totalHits": 0}
        assert params["q"] == f"({expected}) AND access_right:open AND resource_type.type:dataset"
        assert params["page"] == 2
        return {"hits": {"hits": [], "total": 0}}

    if source == "zenodo":
        def handler(request):
            calls.append(request)
            assert request.url.params["q"] == expected
            assert request.url.params["type"] == "dataset"
            return httpx.Response(200, json={"hits": {"hits": [], "total": 0}})
        result = await OpenDataSources(transport=httpx.MockTransport(handler)).search(source, query)
    elif source == "switzerland-open-data":
        result = await GovernmentBatchAdapters(fetch).search(source, query, 2)
    else:
        async def text(url, params, *, accept):
            assert accept == "application/vnd.zenodo.v1+json"
            return json.dumps(await fetch(url, params))
        result = await ResearchOct22Adapters(fetch, text).search(source, query, 2)
    assert calls and not result["results"]
    assert get_source(source).query_semantics == "all_terms"


@pytest.mark.asyncio
async def test_zenodo_normalized_query_still_binds_pagination_to_original_input():
    pages = []

    def handler(request):
        assert request.url.params["q"] == "marine AND chemistry"
        pages.append(request.url.params["page"])
        return httpx.Response(200, json={"hits": {"hits": [], "total": 11}})

    source = OpenDataSources(transport=httpx.MockTransport(handler))
    first = await source.search("zenodo", "marine chemistry")
    await source.search("zenodo", "marine chemistry", first["next_cursor"])
    assert pages == ["1", "2"]
