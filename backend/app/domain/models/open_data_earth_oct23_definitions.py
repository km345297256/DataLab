"""Curated oceanographic, geophysical, astronomical and cartographic sources."""

DEFINITIONS = (
    dict(id='heasarc', title='NASA HEASARC 天文观测目录', connector_title='HEASARC',
         description='按天文目录名称或英文主题检索 NASA HEASARC，获取官方完整 TDAT 压缩数据表。',
         domains=('heasarc.gsfc.nasa.gov',), subjects=('astronomy', 'astrophysics', 'stars', 'galaxies', 'X-ray', 'gamma-ray'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='仅返回官方整表下载目录中存在的公开目录；完整 TDAT.gz 保留观测列、单位和原始格式。天文目录或观测索引表不等于原始望远镜图像、光谱或事件文件；文件由源站周期更新，保存获取时哈希。'),
    dict(id='earthscope', enabled=False, connector_enabled=False, title='EarthScope 地震原始波形', connector_title='EarthScope',
         description='按明确台网、台站、通道和时间段获取 EarthScope 公开归档的原始 miniSEED 波形。',
         domains=('service.earthscope.org',), subjects=('seismology', 'earthquakes', 'geophysics', 'waveforms'),
         languages=('en', 'zh'), protocol='rest', discovery_mode='observations', acquisition='files',
         limitation='必须提供 net/sta/loc/cha/start/end，例如 net=IU sta=ANMO loc=00 cha=BHZ start=2020-01-01T00:00:00 end=2020-01-01T00:01:00。空位置码用--；单个明确通道，不猜测台站，不支持主题全文检索或实时轮询。原始波形保留数据间隙与仪器计数，不等于经响应校正的地面运动；源站可能要求缩小过大的请求。官方公告将于2026-12-15停止匿名HTTP下载；本连接器保持停用，等待账户或公开桶接入。'),
    dict(id='natural-earth', title='Natural Earth 全球地理底图', connector_title='Natural Earth',
         description='检索 Natural Earth 官方三个比例尺的文化与自然地理矢量目录，获取完整 Shapefile ZIP。',
         domains=('www.naturalearthdata.com', 'naciscdn.org'),
         subjects=('geography', 'cartography', 'GIS', 'boundaries', 'coastlines', 'rivers', 'lakes', 'cities'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='支持 10m、50m、110m 三个制图比例尺和文化/自然地理矢量图层；10m指1:1000万，并非10米分辨率。每条结果明确比例尺、图层和版本，获取完整ZIP。数据经过制图概括，边界采用源站描述的事实管辖表达，不代替测量级或法定边界数据。许可为Public Domain。'),
    dict(id='worldclim', title='WorldClim 全球气候栅格', connector_title='WorldClim',
         description='按气候变量和空间分辨率检索 WorldClim 2.1，获取官方完整 GeoTIFF ZIP。',
         domains=('www.worldclim.org', 'worldclim.org', 'geodata.ucdavis.edu'),
         subjects=('climate', 'bioclimatic variables', 'precipitation', 'temperature', 'elevation', 'GIS'),
         languages=('en', 'zh'), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='接入2.1版历史气候常年值（1970–2000）与其SRTM派生高程；支持10/5/2.5角分与30角秒，气候变量每包12个月，生物气候每包19变量，高程每包1图层。不是逐年原始观测或未来情景。学术及其他非商业使用免费；商业使用或再分发须源站事先许可。'),

    dict(id='noaa-ghcn-daily', title='NOAA NCEI 全球逐日气候台站', connector_title='NOAA GHCN-D',
         description='按英文国家、台站名称或台站编号检索全球陆地气候站，获取完整单站逐日原始记录。',
         domains=('www.ncei.noaa.gov',), subjects=('climate', 'weather', 'temperature', 'precipitation', 'snow', 'daily observations'),
         languages=('en',), protocol='rest', discovery_mode='observations', acquisition='files',
         limitation='完整获取指定台站全部现有月份和要素的官方.dly文件，保留原始定宽格式、缺测值与质量/测量/来源标志；不同台站与要素的覆盖期不同，不等于全国完整覆盖。部分站只有降水，温度以0.1°C、降水以0.1mm保存，其他要素单位见源站格式说明。数据会修订，保存官方发布版本与获取哈希。'),

)

SOURCE_INFO = {
    'heasarc': dict(homepage='https://heasarc.gsfc.nasa.gov/', country='美国 / 全球',
                    documentation='https://heasarc.gsfc.nasa.gov/FTP/heasarc/dbase/tdat_files/README'),
    'earthscope': dict(homepage='https://www.earthscope.org/', country='美国 / 全球',
                      documentation='https://service.earthscope.org/fdsnws/dataselect/1/'),
    'natural-earth': dict(homepage='https://www.naturalearthdata.com/', country='国际',
                         documentation='https://www.naturalearthdata.com/about/terms-of-use/'),
    'worldclim': dict(homepage='https://www.worldclim.org/', country='国际 / 全球',
                     documentation='https://www.worldclim.org/data/worldclim21.html'),

    'noaa-ghcn-daily': dict(homepage='https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily', country='美国 / 全球',
                           documentation='https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt'),

}
