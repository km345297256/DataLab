"""Anonymous mainland observation feeds, with deliberately narrow coverage."""

DEFINITIONS = (
    dict(id="cenc-earthquake", title="中国地震台网当前地震目录", connector_title="中国地震台网",
         description="检索中国地震台网官网当前公布的地震目录，获取包含事件时间、震级、经纬度和深度的完整原生 JSON。",
         domains=("www.ceic.ac.cn",), subjects=("earthquake", "seismology", "China recent earthquakes"),
         languages=("zh", "en"), protocol="rest", discovery_mode="observations", acquisition="files",
         limitation="仅覆盖官网当前 JSON 目录公布的时间范围，不是历史地震全库。结果明确标注实际起止时间和事件数；关键词用于发现目录，下载保留整个当前目录，不按关键词截断事件。源站会更新目录，不提供固定历史版本。"),
    dict(id="nmc-weather", title="中央气象台城市最新实况", connector_title="中央气象台",
         description="按明确的中文城市或省份名称，从官方站点目录查找最新气温、湿度、降水和风的实际观测，获取原生 JSON。",
         domains=("www.nmc.cn",), subjects=("China current weather", "city weather observations", "meteorology"),
         languages=("zh",), protocol="rest", discovery_mode="observations", acquisition="files",
         limitation="必须提供中文城市/省份名称或官方站点代码，不默认选择城市。每条结果是一个城市最近一次实况快照，不是历史气候序列，也不是预报；9999 为源站缺测标记，原样保留。商业使用须事先取得中央气象台书面授权，遵守记录中的官方网站声明。"),
)

SOURCE_INFO = {
    "cenc-earthquake": dict(homepage="https://www.ceic.ac.cn/", country="中国大陆",
        documentation="https://www.ceic.ac.cn/", entity_url="https://www.ceic.ac.cn/data/data.json"),
    "nmc-weather": dict(homepage="https://www.nmc.cn/", country="中国大陆",
        documentation="https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
        license_url="https://www.nmc.cn/publish/cms/view/722665831f0a4e98800c41e691444963.html"),
}
