"""Astronomical measurement tables and original public telescope products."""

DEFINITIONS = (
    dict(id='cds-vizier', title='CDS VizieR 天文测量表', connector_title='VizieR',
         description='按英文主题、目录编号检索 VizieR，获取官方归档中对应的完整天文测量数据表。',
         domains=('vizier.cds.unistra.fr', 'tapvizier.cds.unistra.fr', 'cdsarc.cds.unistra.fr'),
         subjects=('astronomy', 'astrophysics', 'stars', 'galaxies', 'photometry', 'spectroscopy'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='范围为 VizieR 可检索数据表中能对应到官方归档原始 DAT 文件的测量表；完整获取所选表，不截取前若干行，不等于整篇论文的全部附属文件或全目录所有图像。保留原始定宽格式、缺测符号和单位说明链接；表格会修订，保存源站文件日期及获取哈希。使用须引用原始作者和 CDS，具体许可见目录说明。'),
    dict(id='mast', title='MAST 空间望远镜科学数据', connector_title='MAST',
         description='按目标名称、任务或观测编号检索 MAST，获取公开的完整 FITS 图像、光谱与光变文件。',
         domains=('mast.stsci.edu', 'archive.stsci.edu'),
         subjects=('astronomy', 'astrophysics', 'Hubble', 'JWST', 'TESS', 'Kepler', 'spectra', 'light curves'),
         languages=('en',), protocol='rest', discovery_mode='observations', acquisition='files',
         limitation='裸查询按目标名称精确匹配；也支持 mission=IUE observation=lwp13058、target="M101"、mission=TESS 等精确条件，可组合。仅列公开观测直接关联的 SCIENCE FITS 产品（含 IUE 归档压缩光谱），排除专有期、预览、辅助文件和观测索引。保留源产品的处理级别，不代表未经校准的原始仪器读数。每个文件完整获取，但不是任务全部数据；重处理会更新产品。数据多数为公有领域，HLSP/特定集合有单独许可与署名要求，不能统一视为 CC0。'),
)

SOURCE_INFO = {
    'cds-vizier': dict(homepage='https://vizier.cds.unistra.fr/', country='法国 / 全球',
                      documentation='https://vizier.cds.unistra.fr/doc/asu-summary.htx',
                      logo_url='https://vizier.cds.unistra.fr/vizier/vizier.png',
                      logo_source='https://vizier.cds.unistra.fr/'),
    'mast': dict(homepage='https://archive.stsci.edu/', country='美国 / 全球',
                 documentation='https://mast.stsci.edu/api/v0/pyex.html',
                 logo_url='https://mast.stsci.edu/api/v0/mastlogo_thumb.png',
                 logo_source='https://mast.stsci.edu/api/v0/'),
}
