"""Mathematical objects and numerical optimization instances, not papers."""
DEFINITIONS = (
    dict(id='oeis', title='OEIS 整数序列数据库', connector_title='OEIS',
         description='按数列项、A 编号或英文名称检索整数序列，获取官方发布的 b-file 数值表。',
         domains=('oeis.org',), subjects=('mathematics', 'number theory', 'combinatorics', 'integer sequences', '数学', '数论', '组合数学'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='获取官方已发布 b-file 中的全部有限项，保留任意精度整数和原始索引；不表示无限数列的全部项。仅有说明而无发布数值文件的记录不提供获取。遵循 CC BY-SA 4.0，并引用序列页面。'),
    dict(id='miplib', title='MIPLIB 混合整数优化基准库', connector_title='MIPLIB',
         description='检索 ZIB 维护的完整 MIPLIB 2017 实例目录，获取含目标函数、约束和变量的原始 MPS 压缩文件。',
         domains=('miplib.zib.de',), subjects=('mathematics', 'optimization', 'operations research', 'mixed integer programming', '数学', '运筹学', '优化计算'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files', query_semantics='all_terms',
         limitation='按实例名称、英文结构标签、提交者和分组检索 MIPLIB 2017 全目录；返回完整 .mps.gz 模型，不把求解状态、图片或实例索引作为数据。使用时引用 MIPLIB 2017，并遵循源站及实例权利说明。'),
    dict(id='lmfdb', title='LMFDB 数论数据库', connector_title='LMFDB',
         enabled=False, automatic_discovery=False, connector_enabled=False,
         description='检索有理数域上的椭圆曲线，获取含 Weierstrass 系数、导子、秩及其他数学不变量的完整 JSON 记录。',
         domains=('www.lmfdb.org',), subjects=('mathematics', 'number theory', 'algebra', 'elliptic curves', '数学', '数论', '代数', '椭圆曲线'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='暂未启用：2026-10-23 公开 API 在本系统环境触发人机验证，不能稳定自动获取。已准备有理数域椭圆曲线 ec_curvedata 适配，待官方访问条件允许后重新验收。'),
)
SOURCE_INFO = {
    'oeis': dict(homepage='https://oeis.org/', documentation='https://oeis.org/wiki/JSON_Format,_Compressed_Files', institution='OEIS Foundation', country='国际'),
    'miplib': dict(homepage='https://miplib.zib.de/', documentation='https://miplib.zib.de/download.html', institution='Zuse Institute Berlin', country='德国'),
    'lmfdb': dict(homepage='https://www.lmfdb.org/', documentation='https://www.lmfdb.org/api/', institution='LMFDB collaboration', country='国际'),
}
