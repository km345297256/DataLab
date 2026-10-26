"""French national catalog: only first-party hosted entity files."""

DEFINITIONS = (
    dict(id='france-data-gouv', title='data.gouv.fr 法国国家开放数据', connector_title='data.gouv.fr',
         enabled=True, connector_enabled=True,
         description='检索法国国家开放数据目录中的人口、经济、环境、交通及公共服务数据，获取平台托管的原始文件。',
         domains=('www.data.gouv.fr', 'static.data.gouv.fr'), subjects=('France', 'population', 'economics', 'environment', 'transport', 'public administration'),
         languages=('fr', 'en'), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='仅接入平台自身托管、无需认证的实体文件；目录中的第三方外链、接口介绍、数据字典和文档不展示。保留源文件格式、更新时间、许可及可用校验码。'),
)

SOURCE_INFO = {'france-data-gouv': dict(homepage='https://www.data.gouv.fr/', country='法国',
    documentation='https://guides.data.gouv.fr/api-de-data.gouv.fr/reference',
    logo_url='https://www.data.gouv.fr/nuxt_images/favicon.svg')}
