"""Public Chinese-language data portals with verified file acquisition routes."""

HK_HOSTS = ("data.weather.gov.hk", "static.data.gov.hk", "data.gov.hk", "www.statistics.gov.hk")
TW_HOSTS = ("opendata.penghu.gov.tw", "ws.kinmen.gov.tw", "data.moi.gov.tw",
            "www.ris.gov.tw", "data.ntpc.gov.tw", "datacenter.taichung.gov.tw")

DEFINITIONS = (
    dict(id="hongkong-data", title="DATA.GOV.HK 香港开放数据", connector_title="DATA.GOV.HK",
         description="检索香港官方开放数据目录，获取受支持政府机构发布的统计、气象和公共服务数据文件。",
         domains=("data.gov.hk", *HK_HOSTS), subjects=("Hong Kong", "statistics", "weather", "population", "public services"),
         languages=("en",), protocol="rest", discovery_mode="repository", acquisition="files", query_semantics="all_terms",
         limitation="官方目录查询匹配英文数据集标识；仅展示受支持官方文件主机上已核验的实体文件，未覆盖需认证接口。"),
    dict(id="taiwan-data", title="台湾政府资料开放平台", connector_title="台湾政府资料开放平台",
         description="按中文主题查询台湾政府开放资料目录，获取公开统计、人口、环境和公共服务数据文件。",
         domains=("data.gov.tw", *TW_HOSTS), subjects=("Taiwan", "statistics", "population", "environment", "public services"),
         languages=("zh-Hant", "zh", "en"), protocol="rest", discovery_mode="repository", acquisition="files",
         enabled=False, connector_enabled=False,
         limitation="目录接口已接入，当前运行环境获取原始文件超时；完成完整下载验收后启用。"),
)
