"""Public gravitational-wave and heliophysics entity archives."""

DEFINITIONS = (
    dict(id='gwosc', title='GWOSC 引力波开放科学中心', connector_title='GWOSC',
         description='按公开引力波事件名或发布目录检索，获取对应版本的原始应变 HDF5 文件。',
         domains=('gwosc.org',), subjects=('physics', 'gravitational waves', 'relativity', 'black holes', 'neutron stars', 'LIGO', 'Virgo', 'KAGRA', '物理', '引力波', '相对论', '黑洞', '中子星'),
         languages=('en', 'zh'), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='覆盖官方API v2公开事件、候选和已标记注入记录的发布版本；候选不等于确认探测，类别以源站目录为准。查询事件名（如GW150914）或目录名（如GWTC-1-confident）；GWOSC或引力波可浏览全目录。仅接入发布版本提供的完整HDF5应变文件，保留探测器、GPS起点、持续秒数、采样率、数据质量和版本；事件窗口不代表整轮运行的连续数据。无应变文件的参数估计版本不展示为可获取数据。CC BY 4.0，遵循GWOSC署名要求。'),
    dict(id='nasa-cdaweb', title='NASA CDAWeb 日地空间观测', connector_title='NASA CDAWeb',
         description='检索 NASA SPDF 的任务、仪器和日地空间观测数据集，获取官方原始 CDF 文件。',
         domains=('cdaweb.gsfc.nasa.gov', 'spdf.gsfc.nasa.gov'),
         subjects=('space physics', 'heliophysics', 'solar wind', 'magnetic field', 'plasma', 'magnetosphere', 'ionosphere', 'spacecraft', '空间', '空间物理', '太阳风', '磁场', '等离子体', '磁层', '电离层'),
         languages=('en', 'zh'), protocol='rest', discovery_mode='observations', acquisition='files',
         limitation='按英文任务、仪器、主题或数据集ID检索；可加start=2009-06-01 end=2009-06-02指定UTC闭区间。无时间条件时使用源站公布的完整覆盖期，不擅自缩短时间。获取与区间相交的完整原始CDF文件，实际覆盖可超出查询边界；保留源文件全部变量和缺测/质量标志，不生成裁切或插值数据。排除仅元数据的master CDF；不包含虚拟变量或服务端增强元数据。SPDF通常仅保存最新修订，获取时保留版本与哈希；许可及署名以各数据产品为准。'),
)

SOURCE_INFO = {
    'gwosc': dict(homepage='https://gwosc.org/', country='国际',
                  documentation='https://gwosc.org/api/v2/',
                  logo_url='https://gwosc.org/static/images/gwosc_logo.svg'),
    'nasa-cdaweb': dict(homepage='https://cdaweb.gsfc.nasa.gov/', country='美国 / 国际任务',
                       documentation='https://cdaweb.gsfc.nasa.gov/WebServices/REST/',
                       logo_url='https://cdaweb.gsfc.nasa.gov/images/NASA_Insignia-RGB-2023.png'),
}
