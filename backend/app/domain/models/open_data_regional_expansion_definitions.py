"""Regional public data verified through anonymous complete file downloads."""

DEFINITIONS = (
    {
        "id": "macau-open-data", "connector_title": "澳门政府数据开放平台", "title": "澳门政府开放数据",
        "description": "检索澳门官方社会、经济、教育、环境及公共服务数据，获取无需登记的原始文件。",
        "domains": ("data.gov.mo", "api.data.gov.mo"), "languages": ("zh", "pt", "en"),
        "subjects": ("Macao", "Macau", "population", "economics", "education", "environment", "public services"),
        "protocol": "rest", "discovery_mode": "repository", "acquisition": "files",
        "authentication": "none", "license_mode": "per_dataset", "query_semantics": "provider_default",
        "automatic_discovery": True, "connector_enabled": True, "connector_registration": "general",
        "read_operations": ("search",), "enabled": True,
        "limitation": "仅接入官方标记为完全开放且提供实体文件的数据；登记开放及仅有接口说明的记录不展示。使用数据须按源站条款注明来源与获取日期。",
    },
    {
        "id": "un-sdg", "connector_title": "UN SDG", "title": "联合国可持续发展目标数据",
        "description": "检索联合国可持续发展目标指标，按国家或地区获取包含全部可用观测的官方 JSON 时间序列。",
        "domains": ("unstats.un.org",), "languages": ("en",),
        "subjects": ("sustainable development", "poverty", "education", "gender equality", "energy", "environment", "economics"),
        "protocol": "rest", "discovery_mode": "repository", "acquisition": "files",
        "authentication": "none", "license_mode": "per_dataset", "query_semantics": "all_terms",
        "automatic_discovery": True, "connector_enabled": True, "connector_registration": "general",
        "read_operations": ("search",), "enabled": True,
        "limitation": "按指标英文名称、指标编号或序列代码检索；未指定地区时仅返回明确标注的 World 全球汇总。其他地区用 geo=M49编码检索，例如 EG_ACS_ELEC geo=156 获取中国。每条结果和文件只对应该地区完整时间序列，保留官方 JSON 与维度。未标注许可时不推定开放授权。",
    },
)
