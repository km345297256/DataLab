"""Independent institutional and national repositories verified for original data."""

REPOSITORY_CONFIG = {
    'asu-research-data': dict(base_url='https://dataverse.asu.edu', pid_pattern=r'10\.48349/ASU/[A-Za-z0-9]{4,24}', prefix='10.48349/asu/',
        storage=(('dataverse-prod-user-files.s3.us-west-2.amazonaws.com', r'/10\.48349/ASU/[A-Za-z0-9]{4,24}/[A-Za-z0-9._-]+'),),
        title='ASU Research Data 亚利桑那州立大学', country='美国', subjects=('all disciplines', 'human behavior', 'artificial intelligence', 'education'), languages=('en',)),
    'juelich-data': dict(base_url='https://data.fz-juelich.de', pid_pattern=r'10\.26165/JUELICH-DATA/[A-Za-z0-9]{4,24}', prefix='10.26165/juelich-data/',
        storage=(), title='Jülich DATA 于利希研究中心', country='德国', subjects=('physics', 'energy', 'chemistry', 'neuroscience', 'environment', 'engineering'), languages=('en', 'de')),
    'repod': dict(base_url='https://repod.icm.edu.pl', pid_pattern=r'10\.18150/[A-Za-z0-9]{4,24}', prefix='10.18150/',
        storage=(), title='RepOD 波兰开放研究数据', country='波兰', subjects=('all disciplines', 'materials', 'chemistry', 'linguistics', 'social sciences'), languages=('en', 'pl')),
    'deic-dataverse': dict(base_url='https://dataverse.deic.dk', pid_pattern=r'10\.60612/DATADK/[A-Za-z0-9]{4,24}', prefix='10.60612/datadk/',
        storage=(), title='DeiC 丹麦国家研究数据仓库', country='丹麦', subjects=('all disciplines', 'humanities', 'environment', 'social sciences'), languages=('en', 'da')),
    'bonndata': dict(base_url='https://bonndata.uni-bonn.de', pid_pattern=r'10\.60507/FK2/[A-Za-z0-9]{4,24}', prefix='10.60507/fk2/',
        storage=(), title='bonndata 波恩大学研究数据', country='德国', subjects=('all disciplines', 'agriculture', 'biodiversity', 'mathematics', 'digital humanities'), languages=('en', 'de')),
    'tudo-data': dict(base_url='https://data.tu-dortmund.de', pid_pattern=r'10\.17877/(?:TUDODATA|RESOLV)-[0-9]{4}-[A-Za-z0-9]{4,24}', prefix='10.17877/',
        storage=(), title='TUDOdata 多特蒙德工业大学研究数据', country='德国', subjects=('all disciplines', 'physics', 'chemistry', 'engineering', 'education'), languages=('en', 'de')),
}

DEFINITIONS = tuple(dict(
    id=key, title=cfg['title'], connector_title={'asu-research-data':'ASU Research Data','juelich-data':'Jülich DATA','repod':'RepOD','deic-dataverse':'DeiC Dataverse','bonndata':'bonndata','tudo-data':'TUDOdata'}[key],
    description=f"检索{cfg['title']}正式发布的研究数据，获取公开的原始数据表、观测、标注和归档文件。",
    domains=(cfg['base_url'].split('://')[1], *(host for host, _ in cfg['storage'])),
    subjects=cfg['subjects'], languages=cfg['languages'], protocol='dataverse',
    discovery_mode='repository', acquisition='files', query_semantics='all_terms',
    doi_prefixes=(cfg['prefix'],), doi_external_id='dataverse_pid',
    limitation='仅接入本仓库已发布、无禁运、无需登录或填写访客问卷的原始实体数据；排除仅外部收录、说明文档和代码文件。保留原格式、版本、源许可及校验和，不按文件数量截断清单。',
) for key, cfg in REPOSITORY_CONFIG.items()) + (
    dict(id='muenster-datastore', title='Münster datastore 明斯特大学研究数据', connector_title='Münster datastore',
        description='检索明斯特大学正式发布的跨学科研究数据，获取教育实验、材料、生态等研究的公开原始文件。',
        domains=('datastore.uni-muenster.de', 's3.uni-muenster.de'), subjects=('all disciplines', 'education', 'materials', 'chemistry', 'ecology'),
        languages=('en', 'de'), protocol='rest', discovery_mode='repository', acquisition='files', query_semantics='all_terms',
        limitation='仅获取明确公开、无禁运的已发布数据集实体文件；不展示论文、说明、代码或需要申请的数据。记录标识固定发布版本，保留完整文件清单、原格式与源站摘要。'),
)

SOURCE_INFO = {key: dict(homepage=cfg['base_url']+'/', country=cfg['country'], documentation='https://guides.dataverse.org/en/latest/api/dataaccess.html') for key, cfg in REPOSITORY_CONFIG.items()}
SOURCE_INFO['muenster-datastore'] = dict(homepage='https://datastore.uni-muenster.de/', country='德国', documentation='https://datastore.uni-muenster.de/about')
