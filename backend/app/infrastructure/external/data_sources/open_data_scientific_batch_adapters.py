"""Public scientific entity files, with explicit record/study granularity.

Official API references are recorded in open_data_scientific_batch_definitions.
Every resolver reloads source-owned records; caller metadata never chooses URLs.
"""
from __future__ import annotations

import json
import math
import mimetypes
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_scientific_batch_definitions import DEFINITIONS

SOURCES = tuple(row['id'] for row in DEFINITIONS)
PAGE_SIZE = 10
EBI = 'https://www.ebi.ac.uk'
MW = 'https://www.metabolomicsworkbench.org/rest/study'
NASA = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync'
_PATTERNS = {
    'ngdc-gwh': r'GWH[A-Z]{4}[0-9]{8}', 'uniprot': r'[A-Z0-9]{6,10}',
    'rcsb-pdb': r'[0-9][A-Z0-9]{3}', 'ena': r'[A-Z][A-Z0-9_]{2,24}(?:\.[1-9][0-9]*)?',
    'chembl': r'CHEMBL[1-9][0-9]{0,12}', 'pubchem': r'[1-9][0-9]{0,15}',
    'pride': r'PXD[0-9]{6,12}', 'metabolights': r'MTBLS[1-9][0-9]{0,12}',
    'metabolomics-workbench': r'ST[0-9]{6,12}', 'nasa-exoplanet': r'[A-Za-z0-9 .+_()-]{1,120}',
}


def _identifier(source, value):
    if not isinstance(value, str) or not re.fullmatch(_PATTERNS[source], value):
        raise SourceConnectorError('invalid_candidate')
    return value


def _parts(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError()
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    params = dict(pairs)
    if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.fragment or len(pairs) != len(params) or '\\' in path
            or '%' in path or re.search(r'%2f|%5c|%2e', parsed.path, re.I)
            or any(ord(c) < 32 or ord(c) == 127 for c in path) or any(p in {'.', '..'} for p in path.split('/'))):
        raise ValueError()
    return parsed.hostname, path, params


def safe_file_url(source, url):
    try:
        host, path, params = _parts(url)
        if source == 'ngdc-gwh':
            return host == 'download.cncb.ac.cn' and not params and bool(re.fullmatch(r'/gwh/[A-Za-z_]+/[A-Za-z0-9_.-]+/GWH[A-Z]{4}[0-9]{8}\.(?:genome\.fasta|RNA\.fasta|CDS\.fasta|Protein\.faa|gff|feature)\.gz', path))
        if source == 'uniprot':
            return host == 'rest.uniprot.org' and not params and bool(re.fullmatch(r'/uniprotkb/[A-Z0-9]{6,10}\.fasta', path))
        if source == 'rcsb-pdb':
            return host == 'files.rcsb.org' and not params and bool(re.fullmatch(r'/download/[0-9][A-Z0-9]{3}\.cif', path))
        if source == 'ena':
            return host == 'www.ebi.ac.uk' and params == {'download': 'true'} and bool(re.fullmatch(r'/ena/browser/api/fasta/[A-Z][A-Z0-9_]{2,24}\.[1-9][0-9]*', path))
        if source == 'chembl':
            return host == 'www.ebi.ac.uk' and not params and bool(re.fullmatch(r'/chembl/api/data/molecule/CHEMBL[1-9][0-9]{0,12}\.sdf', path))
        if source == 'pubchem':
            return host == 'pubchem.ncbi.nlm.nih.gov' and params == {'record_type': '2d'} and bool(re.fullmatch(r'/rest/pug/compound/cid/[1-9][0-9]{0,15}/SDF', path))
        if source == 'pride':
            return host == 'ftp.pride.ebi.ac.uk' and not params and bool(re.fullmatch(r'/pride/data/archive/[0-9]{4}/(?:0[1-9]|1[0-2])/PXD[0-9]{6,12}/(?:[^/%]+/)*[^/%]+', path))
        if source == 'metabolights':
            return (host == 'www.ebi.ac.uk' and bool(re.fullmatch(r'/metabolights/ws/studies/MTBLS[1-9][0-9]*/download', path))
                    and set(params) == {'file'} and _relative_file(params['file']))
        if source == 'metabolomics-workbench':
            return host == 'www.metabolomicsworkbench.org' and not params and bool(re.fullmatch(r'/rest/study/study_id/ST[0-9]{6,12}/mwtab', path))
        if source == 'nasa-exoplanet':
            return (host == 'exoplanetarchive.ipac.caltech.edu' and path == '/TAP/sync' and set(params) == {'query', 'format'}
                    and params['format'] == 'csv' and bool(re.fullmatch(r"select \* from pscomppars(?: where pl_name = '[A-Za-z0-9 .+_()-]{1,120}')?", params['query'])))
    except (ValueError, KeyError, TypeError):
        pass
    return False


def _relative_file(value):
    return (isinstance(value, str) and 0 < len(value) <= 1000 and '%' not in value and '\\' not in value
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and all(part not in {'', '.', '..'} for part in value.split('/')))


def _pride_entity_file(value):
    if not _relative_file(value):
        return False
    name = value.rsplit('/', 1)[-1].lower()
    return not (name.endswith(('.sdrf.tsv', '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.md'))
                or re.match(r'^(?:[0-9]+[._ -]+)*(?:readme|licen[cs]e|metadata|manifest|checksum|md5sum|sha256sum)(?:[._ -]|$)', name))


def is_scientific_observation_sample(source_id, data, external_id, *, complete=False):
    """Validate source identity and entity shape without mistaking a prefix for a full file.

    Large mmCIF files may have a long annotation preamble. The prefix proves
    format and record identity, not that coordinates were already read. For
    mwTab, the resolver independently checks the study's observation counts;
    full small JSON responses must also contain actual measurement rows.
    """
    if source_id not in {'uniprot', 'ena', 'rcsb-pdb', 'metabolomics-workbench'}:
        return True
    try:
        text = data.decode('utf-8').lstrip('\ufeff \r\n\t')
    except (ValueError, UnicodeError, AttributeError):
        return False
    if source_id in {'uniprot', 'ena'}:
        lines = text.splitlines()
        if len(lines) < 2:
            return False
        external = re.escape(external_id.split('.')[0])
        header = (rf'>(?:sp|tr)\|{external}\|' if source_id == 'uniprot'
                  else rf'>ENA\|{external}\|{external}\.[1-9][0-9]*(?:\s|$)')
        alphabet = r'[ACDEFGHIKLMNPQRSTVWYBXZJUO*]+' if source_id == 'uniprot' else r'[ACGTUNRYSWKMBDHV]+'
        return bool(re.match(header, lines[0]) and all(re.fullmatch(alphabet, line.strip(), re.I) for line in lines[1:] if line.strip())
                    and any(line.strip() for line in lines[1:]))
    if source_id == 'rcsb-pdb':
        if not re.match(r'data_' + re.escape(external_id) + r'(?:\s|$)', text, re.I):
            return False
        if not re.search(r'_entry\.id\s+[\'\"]?' + re.escape(external_id) + r'(?:[\'\"]?\s|$)', text, re.I):
            return False
        return not complete or all('_atom_site.Cartn_' + axis in text for axis in 'xyz')
    if complete:
        try:
            rows = json.loads(text)
        except (ValueError, UnicodeError):
            return False
        if not isinstance(rows, list) or not rows:
            return False
        observed = False
        for row in rows:
            if not isinstance(row, dict) or (row.get('METABOLOMICS WORKBENCH') or {}).get('STUDY_ID') != external_id:
                return False
            for key, block in row.items():
                if not key.endswith('_METABOLITE_DATA') or not isinstance(block, dict):
                    continue
                for measurement in block.get('Data', []):
                    if not isinstance(measurement, dict):
                        return False
                    for field, value in measurement.items():
                        if field.lower() in {'metabolite', 'metabolite_name', 'metabolite_id'} or isinstance(value, bool):
                            continue
                        try:
                            observed |= math.isfinite(float(value))
                        except (TypeError, ValueError):
                            continue
        return observed
    return bool(re.match(r'\[\s*\{\s*"METABOLOMICS WORKBENCH"\s*:\s*\{', text)
                and re.search(r'"STUDY_ID"\s*:\s*"' + re.escape(external_id) + '"', text[:2048]))


def _ngdc_record_url(url):
    if not isinstance(url, str) or not re.fullmatch(r'https://ngdc\.cncb\.ac\.cn/gwh/Assembly/[1-9][0-9]{0,15}/show', url):
        raise SourceConnectorError('invalid_response')
    return url


def _result(rows, total):
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
        raise SourceConnectorError('invalid_response')
    return {'results': rows, 'total_count': total}


def _candidate(source, external, title, description='', **fields):
    from app.infrastructure.external.data_sources.open_data_sources import _candidate as candidate
    _identifier(source, external)
    return candidate(source, external, title, description, fields.pop('record_url', None) or _record_url(source, external), **fields)


def _record_url(source, external):
    return {
        'ngdc-gwh': f'https://ngdc.cncb.ac.cn/gwh/Assembly/{external}/show',
        'uniprot': f'https://www.uniprot.org/uniprotkb/{external}',
        'rcsb-pdb': f'https://www.rcsb.org/structure/{external}',
        'ena': f'{EBI}/ena/browser/view/{external}',
        'chembl': f'{EBI}/chembl/compound_report_card/{external}/',
        'pubchem': f'https://pubchem.ncbi.nlm.nih.gov/compound/{external}',
        'pride': f'{EBI}/pride/archive/projects/{external}',
        'metabolights': f'{EBI}/metabolights/{external}',
        'metabolomics-workbench': f'https://www.metabolomicsworkbench.org/data/DRCCMetadata.php?StudyID={external}',
        'nasa-exoplanet': 'https://exoplanetarchive.ipac.caltech.edu/overview/' + quote(external, safe=''),
    }[source]


def _option(source, external, url, filename, *, version=None, license=None, kind='源站文件', identity=None, record_url=None):
    from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity
    if not safe_file_url(source, url):
        raise SourceConnectorError('unsafe_download_url')
    return {'id': _identity(source, external, version, url, identity), 'source_id': source, 'external_id': external,
            'filename': _filename(filename), 'source_filename': filename, 'size': None, 'url': url,
            'content_type': mimetypes.guess_type(filename)[0] or 'application/octet-stream',
            'record_url': record_url or _record_url(source, external), 'version': version, 'license': license,
            'checksum': None, 'available': True, 'reason': '', 'kind': kind}


class ScientificBatchAdapters:
    def __init__(self, fetch_json):
        self._json = fetch_json

    async def search(self, source, query, page):
        if source not in SOURCES:
            raise SourceConnectorError('unknown_source')
        if type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError('invalid_cursor')
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise SourceConnectorError('invalid_query')
        query = query.strip()
        # The shared planner preserves quotes for literal lookups. PubChem
        # expects the name value in its path, not repository query syntax.
        if source in {'pubchem', 'metabolomics-workbench'} and re.fullmatch(r'"[^"\n]+"', query):
            query = query[1:-1]
        try:
            return await getattr(self, '_search_' + source.replace('-', '_'))(query, page)
        except (KeyError, ValueError, TypeError, AttributeError, IndexError):
            raise SourceConnectorError('invalid_response') from None

    async def resolve(self, candidate):
        source = candidate['source_id']
        if source not in SOURCES:
            raise SourceConnectorError('unknown_source')
        external = _identifier(source, candidate['external_id'])
        try:
            return await getattr(self, '_resolve_' + source.replace('-', '_'))(external)
        except (KeyError, ValueError, TypeError, AttributeError, IndexError):
            raise SourceConnectorError('invalid_response') from None

    async def _ebi_search(self, source, domain, query, page):
        data = await self._json(f'{EBI}/ebisearch/ws/rest/{domain}', {
            'query': query, 'format': 'json', 'size': PAGE_SIZE, 'start': (page - 1) * PAGE_SIZE,
            'fields': 'name,description'})
        rows = []
        for item in data['entries']:
            external = item.get('acc') if source == 'uniprot' else item['id']
            if not isinstance(external, str) or not re.fullmatch(_PATTERNS[source], external):
                continue
            fields = item.get('fields', {})
            name = (fields.get('name') or [item['id']])[0]
            description = ' '.join(fields.get('description') or [])
            rows.append(_candidate(source, external, name, description))
        return _result(rows, data['hitCount'])

    async def _search_uniprot(self, query, page):
        if re.fullmatch(_PATTERNS['uniprot'], query):
            query = 'acc:' + query
        result = await self._ebi_search('uniprot', 'uniprot', query, page)
        ids = [row['external_id'] for row in result['results']]
        if not ids:
            return result
        # EBI's description field is empty for many proteins. A compact official
        # batch adds protein names and species so relevance has actual evidence.
        data = await self._json('https://rest.uniprot.org/uniprotkb/accessions', {
            'accessions': ','.join(ids), 'format': 'json',
            'fields': 'accession,id,protein_name,gene_names,organism_name'})
        rows = {row['primaryAccession']: row for row in data['results'] if row.get('primaryAccession') in ids}
        for candidate in result['results']:
            row = rows.get(candidate['external_id'])
            if not row:
                continue
            protein = row.get('proteinDescription') or {}
            name = (protein.get('recommendedName') or {}).get('fullName', {}).get('value')
            if not name:
                name = next((item.get('fullName', {}).get('value') for item in protein.get('submissionNames', []) if item.get('fullName', {}).get('value')), candidate['title'])
            species = (row.get('organism') or {}).get('scientificName', '')
            genes = ', '.join(g.get('geneName', {}).get('value', '') for g in row.get('genes', []))
            candidate.update(title=f'{name} ({candidate["external_id"]})',
                             description=f'{species}；{genes}；完整蛋白质序列 FASTA。',
                             license='https://creativecommons.org/licenses/by/4.0/')
        return result

    async def _resolve_uniprot(self, external):
        row = await self._json(f'https://rest.uniprot.org/uniprotkb/{external}.json')
        if row.get('primaryAccession') != external or not row.get('sequence', {}).get('value'):
            return []
        audit = row.get('entryAudit', {})
        version = f"{audit.get('sequenceVersion', '')}:{audit.get('entryVersion', '')}"
        return [_option('uniprot', external, f'https://rest.uniprot.org/uniprotkb/{external}.fasta',
                        f'uniprot_{external}.fasta', version=version, license='https://creativecommons.org/licenses/by/4.0/',
                        kind='完整蛋白质序列', identity=row['sequence']['value'])]

    async def _search_ena(self, query, page):
        if re.fullmatch(_PATTERNS['ena'], query):
            query = 'id:' + query.split('.')[0]
        return await self._ebi_search('ena', 'embl', query, page)

    async def _resolve_ena(self, external):
        data = await self._json(f'{EBI}/ena/browser/api/summary/{external}', {'format': 'json'})
        rows = data['summaries']
        base = external.split('.')[0]
        row = next((r for r in rows if r.get('accession') == base), None)
        if not row or row.get('statusDescription') != 'public' or row.get('dataType') != 'SEQUENCE' or not row.get('sequenceLength'):
            return []
        version = row['version']
        if type(version) is not int or version < 1:
            raise SourceConnectorError('invalid_response')
        pinned = f'{base}.{version}'
        if '.' in external and external != pinned:
            raise SourceConnectorError('invalid_candidate')
        return [_option('ena', external, f'{EBI}/ena/browser/api/fasta/{pinned}?download=true',
                        f'ena_{pinned}.fasta', version=str(version), kind='完整核酸序列')]

    async def _search_rcsb_pdb(self, query, page):
        payload = {'query': {'type': 'terminal', 'service': 'full_text', 'parameters': {'value': query}},
                   'return_type': 'entry', 'request_options': {'paginate': {'start': (page - 1) * PAGE_SIZE, 'rows': PAGE_SIZE},
                                                              'results_content_type': ['experimental']}}
        data = await self._json('https://search.rcsb.org/rcsbsearch/v2/query', {'json': json.dumps(payload)})
        ids = [_identifier('rcsb-pdb', r['identifier']) for r in data.get('result_set', [])]
        # A single GraphQL batch loads titles without ten serialized requests.
        if not ids:
            return _result([], 0)
        metadata = await self._json('https://data.rcsb.org/graphql', method='POST', json={
            'query': '{ entries(entry_ids:' + json.dumps(ids) + ') { rcsb_id struct { title } } }'})
        titles = {r['rcsb_id']: r.get('struct', {}).get('title', r['rcsb_id']) for r in metadata['data']['entries'] if r}
        return _result([_candidate('rcsb-pdb', i, titles.get(i, i), '完整实验结构坐标，PDBx/mmCIF 格式。', formats=['cif']) for i in ids], data['total_count'])

    async def _resolve_rcsb_pdb(self, external):
        row = await self._json(f'https://data.rcsb.org/rest/v1/core/entry/{external}')
        if row.get('rcsb_id') != external or row.get('rcsb_accession_info', {}).get('status_code') != 'REL':
            return []
        version = row['rcsb_accession_info'].get('revision_date')
        return [_option('rcsb-pdb', external, f'https://files.rcsb.org/download/{external}.cif',
                        f'pdb_{external}.cif', version=version, kind='完整结构坐标',
                        license='https://creativecommons.org/publicdomain/zero/1.0/')]

    async def _search_chembl(self, query, page):
        data = await self._json(f'{EBI}/chembl/api/data/molecule/search.json', {'q': query, 'limit': PAGE_SIZE, 'offset': (page - 1) * PAGE_SIZE})
        return _result([_candidate('chembl', r['molecule_chembl_id'], r.get('pref_name') or r['molecule_chembl_id'],
                                  '完整分子结构，SDF 格式。', formats=['sdf']) for r in data['molecules'] if r.get('molecule_structures')], data['page_meta']['total_count'])

    async def _resolve_chembl(self, external):
        row = await self._json(f'{EBI}/chembl/api/data/molecule/{external}.json')
        if row.get('molecule_chembl_id') != external or not (row.get('molecule_structures') or {}).get('molfile'):
            return []
        return [_option('chembl', external, f'{EBI}/chembl/api/data/molecule/{external}.sdf',
                        f'chembl_{external}.sdf', license='https://creativecommons.org/licenses/by-sa/3.0/',
                        kind='完整化学结构', identity=row['molecule_structures']['molfile'])]

    async def _search_pubchem(self, query, page):
        mode = 'cid' if re.fullmatch(_PATTERNS['pubchem'], query) else 'name'
        data = await self._json('https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/' + mode + '/' + quote(query, safe='') + '/cids/JSON')
        ids = data['IdentifierList']['CID']
        if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids):
            raise SourceConnectorError('invalid_response')
        selected = ids[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        if not selected:
            return _result([], len(ids))
        props = await self._json('https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/' + ','.join(map(str, selected)) + '/property/Title,MolecularFormula/JSON')
        return _result([_candidate('pubchem', str(r['CID']), r['Title'], r.get('MolecularFormula', '') + '；完整二维 SDF 结构。', formats=['sdf'])
                        for r in props['PropertyTable']['Properties'] if r['CID'] in selected], len(ids))

    async def _resolve_pubchem(self, external):
        data = await self._json(f'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{external}/JSON', {'record_type': '2d'})
        rows = data['PC_Compounds']
        row = next((r for r in rows if str(r.get('id', {}).get('id', {}).get('cid')) == external), None)
        if not row or not row.get('atoms'):
            return []
        return [_option('pubchem', external, f'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{external}/SDF?record_type=2d',
                        f'pubchem_{external}.sdf', kind='完整化学结构', identity=row,
                        license='https://pubchem.ncbi.nlm.nih.gov/docs/downloads')]

    async def _search_pride(self, query, page):
        return await self._ebi_search('pride', 'pride', query, page)

    async def _resolve_pride(self, external):
        row = await self._json(f'{EBI}/pride/ws/archive/v2/projects/{external}')
        if row.get('accession') != external or not row.get('publicationDate'):
            return []
        result, seen, page = [], set(), 0
        while True:
            files = await self._json(f'{EBI}/pride/ws/archive/v2/projects/{external}/files',
                                     {'pageSize': 100, 'page': page})
            if not isinstance(files, list) or len(files) > 100:
                raise SourceConnectorError('invalid_response')
            for f in files:
                if external not in f.get('projectAccessions', []):
                    raise SourceConnectorError('invalid_candidate')
                accession = f.get('accession')
                if not isinstance(accession, str) or not accession:
                    raise SourceConnectorError('invalid_response')
                if accession in seen:
                    raise SourceConnectorError('inconsistent_source_snapshot')
                seen.add(accession)
                if not _pride_entity_file(f.get('fileName')):
                    continue
                for link in f.get('publicFileLocations', []):
                    url = link.get('value', '')
                    if url.startswith('ftp://ftp.pride.ebi.ac.uk/'):
                        url = 'https://' + url[len('ftp://'):]
                    if safe_file_url('pride', url) and f'/{external}/' in urlsplit(url).path:
                        # Legacy fileSizeBytes can be uncompressed or stale. The
                        # actual HTTP representation determines transfer length.
                        result.append(_option('pride', external, url, f['fileName'], version=f.get('updatedDate'),
                                              license=row.get('license'), identity=accession))
                        break
            if len(files) < 100:
                return result
            page += 1

    async def _search_metabolights(self, query, page):
        return await self._ebi_search('metabolights', 'metabolights', f'({query}) AND id:MTBLS*', page)

    async def _resolve_metabolights(self, external):
        record = await self._json(f'{EBI}/metabolights/ws/studies/{external}')
        row = record['mtblsStudy']
        if row.get('studyStatus') != 'Public' or row.get('read_access') is not True:
            return []
        files = (await self._json(f'{EBI}/metabolights/ws/studies/{external}/files', {'include_raw_data': 'true'}))['study']
        result = []
        for f in files:
            if f.get('directory') or f.get('status') != 'active' or f.get('type') in {'metadata_investigation', 'metadata_sample', 'metadata_assay'}:
                continue
            filename = f['file']
            url = f'{EBI}/metabolights/ws/studies/{external}/download?' + urlencode({'file': filename})
            result.append(_option('metabolights', external, url, f'{external}_{filename}',
                                  version=str(row.get('revisionNumber', '')) + ':' + str(f.get('timestamp', '')),
                                  license=row.get('datasetLicenseUrl') or row.get('datasetLicense')))
        return result

    async def _search_metabolomics_workbench(self, query, page):
        field = 'study_id' if re.fullmatch(_PATTERNS['metabolomics-workbench'], query) else 'study_title'
        payload = await self._json(f'{MW}/{field}/{quote(query, safe="")}/summary')
        if not isinstance(payload, dict):
            raise SourceConnectorError('invalid_response')
        rows = [payload] if 'study_id' in payload else list(payload.values())
        rows = [r for r in rows if isinstance(r, dict) and r.get('study_id')]
        selected = rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        return _result([_candidate('metabolomics-workbench', r['study_id'], r['study_title'],
                                  r.get('species', '') + '；完整研究 mwTab JSON，包含实验测量值。',
                                  license=r.get('license_url') or r.get('license'), formats=['json']) for r in selected], len(rows))

    async def _resolve_metabolomics_workbench(self, external):
        row = await self._json(f'{MW}/study_id/{external}/summary')
        if row.get('study_id') != external or not row.get('release_date'):
            return []
        counts = await self._json(f'{MW}/study_id/{external}/number_of_metabolites')
        if not isinstance(counts, dict):
            raise SourceConnectorError('invalid_response')
        analyses = [counts] if 'study_id' in counts else list(counts.values())
        observed = []
        for analysis in analyses:
            if not isinstance(analysis, dict) or analysis.get('study_id') != external:
                raise SourceConnectorError('invalid_response')
            count = str(analysis.get('num_metabolites', ''))
            if not count.isdigit():
                raise SourceConnectorError('invalid_response')
            if int(count) > 0:
                observed.append((analysis.get('analysis_id'), count))
        if not observed:
            return []
        return [_option('metabolomics-workbench', external, f'{MW}/study_id/{external}/mwtab',
                        f'{external}_mwtab.json', version=str(row.get('version', '')) + ':' + str(row.get('revision_no', '')),
                        license=row.get('license_url') or row.get('license'), kind='完整研究测量数据', identity=observed)]

    async def _search_ngdc_gwh(self, query, page):
        data = await self._json('https://ngdc.cncb.ac.cn/search/api/specific', {
            'q': f'({query}) AND GWH', 'db': 'gwh', 'length': PAGE_SIZE, 'start': (page - 1) * PAGE_SIZE})
        if str(data.get('code')) != '200':
            raise SourceConnectorError('upstream_unavailable')
        payload = data['result']['data']
        return _result([_candidate('ngdc-gwh', r['id'], r['title'],
                                  r.get('attrs', {}).get('scientific_name') or r.get('description', ''), formats=['fasta.gz', 'gff.gz', 'feature.gz'],
                                  record_url=_ngdc_record_url(r.get('url')))
                        for r in payload['data'] if r.get('type') == 'gwh_assembly' and re.fullmatch(_PATTERNS['ngdc-gwh'], r['id'])], payload['recordsFiltered'])

    async def _resolve_ngdc_gwh(self, external):
        row = await self._json(f'https://ngdc.cncb.ac.cn/gwh/api/public/assembly/{external}')
        if row.get('assemblyAccession') != external or not row.get('releaseTime'):
            return []
        result = []
        record_url = _ngdc_record_url(f"https://ngdc.cncb.ac.cn/gwh/Assembly/{row['id']}/show")
        for key in ('ftpPathFeature', 'ftpPathGff', 'ftpPathDna', 'ftpPathRna', 'ftpPathCds', 'ftpPathProtein'):
            url = row.get(key)
            if not url:
                continue
            if not safe_file_url('ngdc-gwh', url) or not urlsplit(url).path.rsplit('/', 1)[-1].startswith(external + '.'):
                raise SourceConnectorError('unsafe_download_url')
            result.append(_option('ngdc-gwh', external, url, urlsplit(url).path.rsplit('/', 1)[-1],
                                  version=external + ':' + str(row['releaseTime']), record_url=record_url,
                                  kind='完整基因注释表' if key == 'ftpPathFeature' else '完整组装序列或注释'))
        return result

    async def _search_nasa_exoplanet(self, query, page):
        # This small metadata catalogue contains names only, not downloaded
        # observations. Stable local paging avoids TAP's unsupported OFFSET.
        rows = await self._json(NASA, {'query': 'select pl_name,hostname from pscomppars order by pl_name', 'format': 'json'})
        if not isinstance(rows, list):
            raise SourceConnectorError('invalid_response')
        terms = query.lower().strip().strip('"')
        generic = terms in {'exoplanet', 'exoplanets', 'planet', 'planets', '系外行星', '系外行星数据', 'pscomppars'}
        if generic:
            candidates = [_candidate('nasa-exoplanet', 'pscomppars', 'NASA 系外行星复合参数完整表',
                '获取全部已确认行星的复合参数行和全部列；这是复合参数表，不是所有历史观测和候选行星。', formats=['csv'], coverage={'scope': '全部 pscomppars 行与列'})]
            return _result(candidates if page == 1 else [], 1)
        matched = [r for r in rows if terms in r['pl_name'].lower() or terms in r['hostname'].lower()]
        selected = matched[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        return _result([_candidate('nasa-exoplanet', r['pl_name'], r['pl_name'] + ' — 完整行星参数',
                                  f"恒星 {r['hostname']}；下载该行星复合参数记录的全部列。", formats=['csv'], coverage={'scope': '单行星完整参数记录'}) for r in selected], len(matched))

    async def _resolve_nasa_exoplanet(self, external):
        query = 'select * from pscomppars'
        if external != 'pscomppars':
            query += f" where pl_name = '{external}'"
        # pscomppars has no rowupdate field. Bind a single-planet selection
        # to its complete parameter record; the full table remains an explicit
        # unversioned dynamic snapshot whose bytes are hashed on acquisition.
        probe_query = query if external != 'pscomppars' else 'select pl_name from pscomppars order by pl_name'
        probe = await self._json(NASA, {'query': probe_query, 'format': 'json'})
        if not isinstance(probe, list) or not probe or (external != 'pscomppars' and any(r.get('pl_name') != external for r in probe)):
            return []
        url = NASA + '?' + urlencode({'query': query, 'format': 'csv'})
        return [_option('nasa-exoplanet', external, url, f'nasa_exoplanet_{external}.csv',
                        kind='完整复合参数表' if external == 'pscomppars' else '完整行星参数记录',
                        identity=probe if external != 'pscomppars' else None, license='https://exoplanetarchive.ipac.caltech.edu/docs/acknowledge.html')]
