"""Public original scientific files; each integration has live download evidence."""

DEFINITIONS = (
    dict(id="nasa-osdr", title="NASA OSDR 空间生命科学数据", connector_title="NASA OSDR",
         description="检索 NASA 开放科学数据仓库中的空间生物学及地面模拟研究，获取公开原始与处理后实验数据。",
         domains=("osdr.nasa.gov", "genelab-repo-prod.s3.amazonaws.com"), subjects=("space biology", "spaceflight", "microgravity", "genomics", "plant biology", "radiation biology"),
         languages=("en",), protocol="rest", discovery_mode="repository", acquisition="files",
         query_semantics="all_terms",
         limitation="仅检索 OSDR 自身研究，获取清单中公开且可见的实验数据文件；排除研究元数据包、分析脚本、报告、校验清单和受控访问文件。不将关联的 GEO/PRIDE 记录视为 OSDR 文件。保留源格式和文件更新时间。"),
    dict(id="ebi-biostudies", title="EMBL-EBI BioStudies 生命科学研究数据", connector_title="BioStudies",
         description="按主题检索 BioStudies 生命科学研究，获取官方托管的公开实验数据、表达矩阵与影像数据文件。",
         domains=("www.ebi.ac.uk", "ftp.ebi.ac.uk"), subjects=("life sciences", "bioimaging", "gene expression", "cancer biology", "experimental biology"),
         languages=("en",), protocol="rest", discovery_mode="repository", acquisition="files",
         query_semantics="all_terms",
         limitation="仅接入明确公开且由 BioStudies 官方文件服务托管的实体数据；外部数据库链接、论文、代码和说明文件不展示。保留研究更新时间；仅记录源站明示许可，不推定所有记录使用同一许可。"),
    dict(id="rodare", title="RODARE 赫尔姆霍茨科研数据", connector_title="RODARE",
         description="检索 Helmholtz-Zentrum Dresden-Rossendorf 研究仓库中的物理、材料、能源和工程数据，获取公开原始文件。",
         domains=("rodare.hzdr.de",), subjects=("physics", "materials science", "engineering", "energy research", "radiation research", "fluid dynamics"),
         languages=("en", "de"), protocol="rest", discovery_mode="repository", acquisition="files",
         query_semantics="all_terms", doi_prefixes=("10.14278/rodare.",), doi_external_id="numeric_suffix",
         limitation="仅返回公开 Dataset 记录中的实体数据文件；保留版本记录、许可、文件长度及官方 MD5 校验码。受限、封闭及仅有论文或软件的记录不展示。"),
)

SOURCE_INFO = {
    "nasa-osdr": dict(homepage="https://osdr.nasa.gov/bio/repo/", country="美国",
                      documentation="https://api.nasa.gov/", logo_url="https://www.nasa.gov/wp-content/themes/nasa/assets/images/nasa-logo.svg"),
    "ebi-biostudies": dict(homepage="https://www.ebi.ac.uk/biostudies/", country="欧洲 / 国际",
                          documentation="https://www.ebi.ac.uk/biostudies/help", logo_url="https://www.ebi.ac.uk/biostudies/images/logo.png"),
    "rodare": dict(homepage="https://rodare.hzdr.de/", country="德国",
                   documentation="https://rodare.hzdr.de/", logo_url="https://rodare.hzdr.de/static/favicon.ico"),
}
