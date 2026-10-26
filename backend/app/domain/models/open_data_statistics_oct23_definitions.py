"""National statistics and WHO sources with complete entity-data acquisition."""
DEFINITIONS = (
    dict(id='malaysia-data-gov', title='data.gov.my 马来西亚政府开放数据', connector_title='data.gov.my',
         description='检索马来西亚官方人口、经济、教育、卫生和公共服务数据，获取完整 CSV 或 Parquet。',
         domains=('data.gov.my', 'storage.data.gov.my', 'storage.dosm.gov.my'),
         subjects=('Malaysia', 'population', 'economics', 'education', 'health', 'public services'),
         languages=('en', 'ms'), protocol='rest', discovery_mode='repository', acquisition='files',
         query_semantics='all_terms',
         limitation='按英文、马来文名称或数据集编号检索官方目录，仅获取详情页提供的完整 CSV/Parquet；不把网页的最新一期预览当作完整数据。按源站 CC BY 4.0 署名；各表的地区、时间及统计口径以源说明为准。'),
    dict(id='norway-ssb', title='SSB 挪威国家统计局', connector_title='Statistics Norway',
         description='检索挪威官方人口、经济、教育、社会和环境统计表，获取全维度完整 CSV。',
         domains=('data.ssb.no', 'www.ssb.no'), subjects=('Norway', 'population', 'economics', 'education', 'society', 'environment'),
         languages=('en', 'no'), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='按英文关键词或表号检索。默认获取表中全部时期和维度；超过源站每次 800000 个单元格的限制时分为互不重叠的文件，须获取全部分片才是全表。关键词不筛选地区或年份，源站会修订数据。CC BY 4.0。'),
    dict(id='who-gho', title='WHO 全球卫生观测数据', connector_title='WHO GHO',
         description='检索世界卫生组织公开卫生指标，获取所有公开国家、时期及细分维度的完整 JSON 数据快照。',
         domains=('ghoapi.azureedge.net', 'www.who.int'), subjects=('public health', 'mortality', 'life expectancy', 'health systems', 'disease burden'),
         languages=('en',), protocol='rest', discovery_mode='observations', acquisition='files',
         limitation='按英文指标名称或指标代码检索，分页取得完整指标而非首批记录，保留缺失值和所有原始字段。范围为该指标全部公开观测；关键词不筛选国家或时期。按 WHO 数据使用条款署名并用于公共卫生目的；第三方数据遵循各自条款。'),
)
SOURCE_INFO = {
    'malaysia-data-gov': dict(homepage='https://data.gov.my/', country='马来西亚', documentation='https://developer.data.gov.my/static-api/data-catalogue', institution='马来西亚政府'),
    'norway-ssb': dict(homepage='https://www.ssb.no/en/statbank/', country='挪威', documentation='https://www.ssb.no/en/api/pxwebapiv2', institution='Statistics Norway'),
    'who-gho': dict(homepage='https://www.who.int/data/gho', country='国际', documentation='https://www.who.int/data/gho/info/gho-odata-api', institution='World Health Organization'),
}
