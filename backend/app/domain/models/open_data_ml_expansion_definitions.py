"""Public machine-learning dataset repositories with real file acquisition."""

DEFINITIONS = (
    dict(id='uci-ml', title='UCI Machine Learning Repository', connector_title='UCI',
         description='按名称或数据集编号查找 UCI 官方机器学习数据，获取源站完整 CSV。',
         domains=('archive.ics.uci.edu',), subjects=('machine learning', 'classification', 'regression', 'education', 'all disciplines'),
         languages=('en',), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='检索官方完整名称目录；仅接入官方 API 提供直接 CSV 的数据集，其他仅有网页或外链的条目不展示。源站可能更新文件，获取时记录内容哈希。'),
    dict(id='huggingface-datasets', title='Hugging Face Datasets', connector_title='Hugging Face',
         enabled=True, connector_enabled=True,
         description='检索公开且无需授权的 Hugging Face 数据集，按固定提交版本获取原始表格、多媒体及压缩数据文件。',
         domains=('huggingface.co', 'cas-bridge.xethub.hf.co', 'cdn-lfs.huggingface.co', 'cdn-lfs-us-1.hf.co', 'cdn-lfs-eu-1.hf.co', 'us.aws.cdn.hf.co', 'us.gcp.cdn.hf.co'),
         subjects=('machine learning', 'natural language', 'computer vision', 'audio', 'tabular data', 'all disciplines'),
         languages=('en', 'zh'), protocol='rest', discovery_mode='repository', acquisition='files',
         limitation='按仓库短名称或完整仓库名检索，如 Tibetan、common_voice、scikit-learn/iris；不作为数据内容全文搜索。仅支持已公开且不需同意条款的数据集。每个关键词最多检索前 1000 条；解析清单中的全部实体文件，按提交哈希固定版本，保留分片及原格式。脚本、说明和模型权重不作为数据文件。'),
)

SOURCE_INFO = {
    'uci-ml': dict(homepage='https://archive.ics.uci.edu/', country='美国 / 国际', documentation='https://github.com/uci-ml-repo/ucimlrepo'),
    'huggingface-datasets': dict(homepage='https://huggingface.co/datasets', country='国际', documentation='https://huggingface.co/docs/hub/datasets-downloading'),
}
