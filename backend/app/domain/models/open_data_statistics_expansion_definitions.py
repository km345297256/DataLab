"""Official statistics catalogs and original observation exports.

Enable a provider only after container search/resolve/verify/full-download acceptance.
"""

VALIDATED_SOURCES = frozenset({"eurostat", "ecb", "ilostat", "unesco-uis"})


def _definition(source, name, title, description, domains, subjects, limitation):
    enabled = source in VALIDATED_SOURCES
    return dict(id=source, connector_title=name, title=title, description=description,
                domains=domains, subjects=subjects, languages=("en",), protocol="rest",
                discovery_mode="observations", acquisition="files", query_semantics="all_terms",
                limitation=("" if enabled else "待真实观测文件验收，当前未启用。") + limitation,
                enabled=enabled, connector_enabled=enabled)


DEFINITIONS = (
    _definition("eurostat", "Eurostat", "Eurostat 欧洲统计局", "按官方完整目录中的名称或代码检索欧洲经济、人口、教育和社会统计，获取原生 TSV 数据表。",
                ("ec.europa.eu",), ("Europe", "economics", "education", "population", "social statistics", "employment"),
                "主题检索定位数据集，下载覆盖所选表全部地区与时期；关键词不会自动过滤观测值。保留源更新时间，数据为最新快照，源站不提供历史数据版本。大型表可能需要更长生成时间。"),
    _definition("ecb", "ECB Data Portal", "ECB 欧洲中央银行统计", "检索欧洲中央银行官方数据流目录，获取经济、金融、汇率和支付统计的原始 CSV。",
                ("data-api.ecb.europa.eu", "data.ecb.europa.eu"), ("Europe", "economics", "finance", "exchange rates", "monetary statistics", "payments"),
                "主题检索下载完整数据流；输入 FLOW/SERIES_KEY（如 EXR/M.USD.EUR.SP00.A）可选择明确的单一序列及其全部时期。数据为源站动态快照；结构版本不冒充数据版本。"),
    _definition("ilostat", "ILOSTAT", "ILOSTAT 国际劳工统计", "检索国际劳工组织完整指标目录，获取就业、工资、劳动保障等统计的原生 CSV 压缩文件。",
                ("rplumber.ilo.org", "ilostat.ilo.org"), ("employment", "labour", "wages", "social protection", "economics", "gender statistics"),
                "每条结果对应一个指标与频率，下载包含该文件全部地区和时期；保留来源更新日期，目录与代码字典不作为观测数据下载。"),
    _definition("unesco-uis", "UNESCO UIS", "UNESCO UIS 教育与社会统计", "按 UNESCO 统计研究所完整指标目录检索教育、科研投入、文化及社会统计，获取指定发布版本的观测 CSV。",
                ("api.uis.unesco.org", "databrowser.uis.unesco.org"), ("education", "literacy", "school enrollment", "science statistics", "culture", "social statistics"),
                "每条结果对应一个指标，下载含该发布版本的所有地区和年份；原始 ZIP 内含观测 CSV 及说明。源 API 单次查询最多 100000 条，超出时不会静默截断或冒充完整数据。"),
)

SOURCE_INFO = {
    "eurostat": dict(homepage="https://ec.europa.eu/eurostat/", logo_url="https://ec.europa.eu/eurostat/o/estat-theme-ecl/images/favicon.ico", country="欧盟", documentation="https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/api-detailed-guidelines/catalogue-api/toc"),
    "ecb": dict(homepage="https://data.ecb.europa.eu/", logo_url="https://data.ecb.europa.eu/themes/custom/edp/favicon.ico", country="欧盟", documentation="https://data.ecb.europa.eu/help/api/data-examples"),
    "ilostat": dict(homepage="https://ilostat.ilo.org/", logo_url="https://www.ilo.org/favicon-96.ico", country="国际", documentation="https://ilostat.ilo.org/data/bulk/"),
    "unesco-uis": dict(homepage="https://databrowser.uis.unesco.org/", logo_url="https://databrowser.uis.unesco.org/logo-unesco-uis-extended.png", country="国际", documentation="https://api.uis.unesco.org/api/public/documentation/"),
}
