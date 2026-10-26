"""Verified government catalogs; regional portals are explicitly identified.

Each entry has a working anonymous CKAN API and at least one fully downloaded
entity-data file. Publisher coverage is deliberately explicit, not an arbitrary
URL-following crawler. The facade performs live content verification per result.
"""

_BASE = {
    "protocol": "rest", "discovery_mode": "repository", "acquisition": "files",
    "authentication": "none", "license_mode": "per_dataset", "query_semantics": "all_terms",
    "automatic_discovery": True, "connector_enabled": True, "connector_registration": "general",
    "read_operations": ("search",), "enabled": True,
}
_UNAVAILABLE = {
    "argentina-data-gov": "当前运行环境访问官方检索接口返回 403，恢复公开访问并复核后启用。",
    "australia-nsw": "当前运行环境访问官方文件节点返回 403，恢复公开下载并复核后启用。",
    "australia-sa": "当前运行环境访问官方检索接口返回 403，恢复公开访问并复核后启用。",
    "canada-ontario": "当前运行环境访问官方检索接口持续返回 429，恢复服务并复核后启用。",
    "italy-dati": "当前运行环境访问官方文件节点返回 403，恢复公开下载并复核后启用。",
    "uruguay-open-data": "当前运行环境访问官方检索接口持续超时，恢复服务并复核后启用。",
}


def _definition(source, name, title, description, domains, languages, subjects, coverage):
    return dict(_BASE, id=source, connector_title=name, title=title, description=description,
                domains=domains, languages=languages, subjects=subjects,
                enabled=source not in _UNAVAILABLE, connector_enabled=source not in _UNAVAILABLE,
                limitation=_UNAVAILABLE.get(source, "") + f"当前接入{coverage}的公开原始文件；仅展示已核验可读取的实体数据，保留来源许可与格式。门户其他发布方需逐批验证接入。")


DEFINITIONS = (
    _definition("canada-open-data", "Canada Open Government", "加拿大联邦开放数据",
                "检索加拿大联邦开放数据门户并获取加拿大统计局发布的原始统计数据包。",
                ("open.canada.ca", "www150.statcan.gc.ca"), ("en", "fr"),
                ("Canada", "population", "economics", "agriculture", "labour", "official statistics"), "加拿大统计局"),
    _definition("uk-data-gov", "data.gov.uk", "英国政府开放数据",
                "检索英国公共数据目录，获取已接入地方政府发布的人口、社会与公共服务数据。",
                ("ckan.publishing.service.gov.uk", "www.data.gov.uk", "dataworks.calderdale.gov.uk"), ("en",),
                ("United Kingdom", "Calderdale", "population", "public services", "local government"), "Calderdale 地方政府"),
    _definition("switzerland-open-data", "opendata.swiss", "瑞士国家开放数据",
                "检索瑞士国家开放数据门户并获取联邦统计局原始统计表。",
                ("opendata.swiss", "ckan.opendata.swiss", "dam-api.bfs.admin.ch"), ("de", "fr", "it", "en"),
                ("Switzerland", "transport", "population", "economics", "official statistics"), "瑞士联邦统计局"),
    _definition("germany-govdata", "GovData", "德国政府开放数据",
                "检索德国 GovData 联邦与地方数据目录，获取柏林州与鲁尔地区官方节点的公开数据文件。",
                ("www.govdata.de", "opendata.ruhr", "www.berlin.de"), ("de", "en"),
                ("Germany", "Berlin", "Ruhr", "population", "urban planning", "economics", "local government"), "柏林州和鲁尔地区官方数据节点"),
    _definition("ireland-data-gov", "data.gov.ie", "爱尔兰政府开放数据",
                "检索爱尔兰国家开放数据目录，获取中央统计局发布的 CSV 与电子表格。",
                ("data.gov.ie", "ws.cso.ie"), ("en", "ga"),
                ("Ireland", "population", "economics", "labour", "agriculture", "official statistics"), "爱尔兰中央统计局"),
    _definition("japan-e-gov", "e-Gov Data Portal", "日本 e-Gov 开放数据",
                "检索日本国家开放数据目录，获取总务省信息通信白皮书的原始 CSV 统计表。",
                ("data.e-gov.go.jp", "www.soumu.go.jp"), ("ja", "en"),
                ("Japan", "telecommunications", "internet", "media", "digital economy", "official statistics"), "日本总务省信息通信统计"),
    _definition("argentina-data-gov", "datos.gob.ar", "阿根廷国家开放数据",
                "检索阿根廷国家开放数据目录，获取官方数据基础设施托管的原始文件。",
                ("datos.gob.ar", "infra.datos.gob.ar"), ("es",),
                ("Argentina", "population", "economics", "labour", "agriculture", "government statistics"), "阿根廷官方 infra.datos.gob.ar 数据托管节点"),
    _definition("uruguay-open-data", "Datos Abiertos Uruguay", "乌拉圭国家开放数据",
                "检索乌拉圭国家开放数据目录，获取门户托管的社会、经济与公共服务数据文件。",
                ("catalogodatos.gub.uy",), ("es",),
                ("Uruguay", "population", "economics", "social care", "public health", "government statistics"), "乌拉圭国家门户直接托管资源"),
    _definition("italy-dati", "dati.gov.it", "意大利国家开放数据",
                "检索意大利国家数据目录，获取已接入的米兰市政府原始数据文件。",
                ("www.dati.gov.it", "dati.comune.milano.it"), ("it", "en"),
                ("Italy", "Milan", "population", "urban planning", "social indicators", "local government"), "米兰市政府开放数据门户"),
    _definition("canada-bc", "BC Data Catalogue", "加拿大不列颠哥伦比亚省开放数据",
                "检索加拿大 BC 省官方数据目录，获取省级人口、生态、教育与公共服务原始文件。",
                ("catalogue.data.gov.bc.ca",), ("en",),
                ("Canada", "British Columbia", "population", "ecology", "education", "public health", "regional government"), "BC 省门户直接托管资源"),
    _definition("canada-ontario", "Ontario Data Catalogue", "加拿大安大略省开放数据",
                "检索加拿大安大略省官方开放数据，获取人口、交通、经济与公共服务原始文件。",
                ("data.ontario.ca",), ("en", "fr"),
                ("Canada", "Ontario", "population", "transport", "economics", "public services", "regional government"), "安大略省门户直接托管资源"),
    _definition("canada-quebec", "Données Québec", "加拿大魁北克省开放数据",
                "检索加拿大魁北克省官方开放数据，获取社会、经济与公共服务原始数据文件。",
                ("www.donneesquebec.ca",), ("fr", "en"),
                ("Canada", "Québec", "population", "economics", "public services", "regional government"), "魁北克省门户直接托管资源"),
    _definition("australia-nsw", "Data.NSW", "澳大利亚新南威尔士州开放数据",
                "检索新南威尔士州开放数据门户，获取州交通部门发布的人口预测与交通原始数据。",
                ("data.nsw.gov.au", "opendata.transport.nsw.gov.au"), ("en",),
                ("Australia", "New South Wales", "population projections", "transport", "urban planning", "regional government"), "新南威尔士州交通部门"),
    _definition("australia-sa", "Data SA", "澳大利亚南澳州开放数据",
                "检索南澳州官方开放数据，获取公共卫生、教育、人口与公共服务原始数据文件。",
                ("data.sa.gov.au",), ("en",),
                ("Australia", "South Australia", "public health", "education", "population", "regional government"), "南澳州门户直接托管资源"),
    _definition("australia-vic", "DataVic", "澳大利亚维多利亚州开放数据",
                "检索维多利亚州开放数据门户，获取州卫生部门发布的调查与公共卫生原始表格。",
                ("discover.data.vic.gov.au", "www.health.vic.gov.au"), ("en",),
                ("Australia", "Victoria", "public health", "population health", "health survey", "regional government"), "维多利亚州卫生部门"),
    _definition("berlin-open-data", "Berlin Open Data", "德国柏林州开放数据",
                "检索德国柏林州开放数据目录，获取州政府发布的人口、性别平等与社会统计原始表格。",
                ("datenregister.berlin.de", "daten.berlin.de", "www.berlin.de"), ("de", "en"),
                ("Germany", "Berlin", "population", "gender equality", "social statistics", "regional government"), "柏林州妇女与性别平等部门"),
)
