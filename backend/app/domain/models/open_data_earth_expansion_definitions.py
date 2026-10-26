"""Official observation services and physical-science files; live validation required."""

DEFINITIONS = (
    dict(id="usgs-earthquake", title="USGS 全球地震观测", connector_title="USGS Earthquakes",
         description="按时间、震级和经纬度范围查找 USGS 地震事件观测，获取所选范围完整原生 CSV。",
         domains=("earthquake.usgs.gov",), subjects=("earthquakes", "seismology", "earth sciences"),
         languages=("en", "zh"), protocol="rest", discovery_mode="observations", acquisition="csv_snapshot",
         limitation="使用 start=2024-01-01 end=2024-02-01 minmagnitude=5；可加 minlatitude/maxlatitude/minlongitude/maxlongitude，或 eventid=事件编号。仅输入 earthquake/地震时默认最近30天全球M≥4.5，结果会注明范围。不支持任意主题全文搜索。USGS单次最多20,000事件，超出须缩小时间或区域，不截断下载。",),
    dict(id="nasa-power", title="NASA POWER 气象与太阳能数据", connector_title="NASA POWER",
         description="按明确经纬度、日期和气象参数获取 NASA POWER 原分辨率逐日时间序列 CSV。",
         domains=("power.larc.nasa.gov",), subjects=("climate", "meteorology", "solar energy", "agriculture", "engineering"),
         languages=("en", "zh"), protocol="rest", discovery_mode="observations", acquisition="csv_snapshot",
         limitation="必须提供 latitude/longitude/start/end，例如 latitude=39.9 longitude=116.4 start=20240101 end=20240107 parameters=T2M,PRECTOTCORR。日期从1981年起，使用UTC。支持T2M/T2M_MIN/T2M_MAX/PRECTOTCORR/RH2M/WS10M/PS/ALLSKY_SFC_SW_DWN，未指定参数默认T2M；不自动猜地点或把主题词当坐标。参数可用期以源站为准。",),
    dict(id="noaa-gml", title="NOAA GML 温室气体观测", connector_title="NOAA GML",
         description="查找 NOAA GML 的 CO2、CH4、N2O、SF6 月平均观测序列，获取完整原生 CSV。",
         domains=("gml.noaa.gov",), subjects=("climate", "greenhouse gases", "carbon dioxide", "methane", "atmospheric science"),
         languages=("en", "zh"), protocol="rest", discovery_mode="observations", acquisition="files",
         limitation="本入口为明确的四种温室气体月均指标目录匹配，支持气体名称或化学式；CO2为Mauna Loa，CH4/N2O/SF6为全球海洋地表平均。不是任意地点或主题全文检索。原文件保留缺失值、质量说明及NOAA引用政策；近期值会修订。",),
    dict(id="cern-open-data", title="CERN Open Data 粒子物理", connector_title="CERN Open Data",
         description="检索 CERN 公开实验与派生数据，获取真实事件表、ROOT及其他实体数据文件。",
         domains=("opendata.cern.ch",), subjects=("physics", "particle physics", "high energy physics", "accelerator experiments"),
         languages=("en",), protocol="rest", discovery_mode="repository", acquisition="files",
         query_semantics="all_terms",
         limitation="仅解析Dataset记录中的在线实体文件，不把软件、教程、文档或文件索引当数据。保留源文件格式、文件版本及Adler32等校验码；派生/教学数据的适用范围见原记录，不能视作完整原始实验数据。",),
)

SOURCE_INFO = {
    "usgs-earthquake": dict(homepage="https://earthquake.usgs.gov/", country="美国", documentation="https://earthquake.usgs.gov/fdsnws/event/1/"),
    "nasa-power": dict(homepage="https://power.larc.nasa.gov/", country="美国", documentation="https://power.larc.nasa.gov/docs/services/api/temporal/daily/"),
    "noaa-gml": dict(homepage="https://gml.noaa.gov/", country="美国", documentation="https://gml.noaa.gov/ccgg/trends/data.html"),
    "cern-open-data": dict(homepage="https://opendata.cern.ch/", country="国际", documentation="https://cernopendata-client.readthedocs.io/en/latest/usage.html"),
}
