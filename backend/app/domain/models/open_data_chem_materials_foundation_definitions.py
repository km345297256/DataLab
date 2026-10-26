"""Specialist foundational chemistry, chemical-engineering and materials data."""
DEFINITIONS = (
    dict(id='crystallography-open-database', title='COD 开放晶体学数据库', connector_title='Crystallography Open Database',
         description='检索有机、无机、金属有机物和矿物晶体结构，获取固定修订版本的完整 CIF。',
         domains=('www.crystallography.net',), subjects=('chemistry', 'materials', 'crystallography', 'minerals', 'crystal structures'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='按英文名称、文献关键词或七位 COD 编号检索具有原子坐标的结构；获取源站固定修订版 CIF，排除标记错误或重复的记录。CC0；保留 CIF 内的原始来源与引用要求。'),
    dict(id='nist-thermoml', title='NIST ThermoML 热物性数据', connector_title='NIST ThermoML',
         description='检索实验热物性、热化学和相平衡数据，获取含数值、单位、实验条件及不确定度的完整 ThermoML XML。',
         domains=('trc.nist.gov',), subjects=('chemistry', 'chemical engineering', 'thermodynamics', 'thermophysical properties', 'phase equilibria'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='范围为 NIST/TRC 从合作期刊整理的静态实验数据归档，目前涵盖至 2019 年。按英文关键词或 DOI 检索；一份 XML 包含该记录全部已归档实验数据，保留官方 MD5、原始文献和提取日期，不表示全文或实时 TRC 数据库。按 NIST 数据声明使用。'),
    dict(id='nomad', title='NOMAD 材料计算数据', connector_title='NOMAD',
         description='检索公开材料计算记录，获取条目原始计算文件的完整 ZIP，保留原格式与官方文件清单。',
         domains=('nomad-lab.eu',), subjects=('materials', 'physics', 'chemistry', 'density functional theory', 'electronic structure'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='仅纳入已公开、处理成功且无禁运的 VASP、Gaussian、Quantum Espresso、CP2K、FHI-aims 计算条目；按化学式、元素符号或英文关键词检索。每个 ZIP 对应一条计算记录的全部原始文件，可能含同目录相关计算，不代表整个项目或全部材料；不下载受限项目。遵循条目的原始许可。'),
)
SOURCE_INFO = {
    'crystallography-open-database': dict(homepage='https://www.crystallography.net/cod/', documentation='https://wiki.crystallography.net/RESTful_API/', country='国际', institution='Crystallography Open Database', logo_url='https://www.crystallography.net/cod/images/cod-blue-yellow.svg'),
    'nist-thermoml': dict(homepage='https://trc.nist.gov/ThermoML/', documentation='https://trc.nist.gov/ThermoML/', country='美国', institution='NIST Thermodynamics Research Center', logo_url='https://trc.nist.gov/ThermoML/favicon.ico'),
    'nomad': dict(homepage='https://nomad-lab.eu/nomad-lab/', documentation='https://nomad-lab.eu/backend/api/v1/extensions/docs', country='德国 / 国际', institution='FAIRmat / NOMAD', logo_url='https://nomad-lab.eu/nomad-lab/assets/nomad.svg'),
}
