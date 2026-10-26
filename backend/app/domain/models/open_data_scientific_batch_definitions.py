"""Scientific sources; only sources with completed entity-file validation are enabled."""

import re

# Each enabled source has a complete entity-file download in the acceptance report.
VALIDATED_SOURCES = frozenset({"ngdc-gwh", "chembl", "pubchem", "nasa-exoplanet",
                               "uniprot", "rcsb-pdb", "ena", "pride", "metabolights", "metabolomics-workbench"})


def _definition(id, title, description, domains, subjects, limitation, languages=("en",)):
    enabled = id in VALIDATED_SOURCES
    if not enabled and "待验收" not in limitation:
        limitation = "待验收，当前未启用。" + limitation
    return dict(id=id, title=title, connector_title=re.split(r"\s+(?=[\u4e00-\u9fff])", title, maxsplit=1)[0], description=description,
                domains=tuple(domains), subjects=tuple(subjects), languages=languages,
                protocol="rest", discovery_mode="repository", acquisition="files", limitation=limitation,
                enabled=enabled, connector_enabled=enabled)


DEFINITIONS = (
    _definition("ngdc-gwh", "NGDC GWH 国家基因组仓库", "检索中国国家基因组科学数据中心公开组装，获取完整注释表及来源文件。",
        ("ngdc.cncb.ac.cn", "download.cncb.ac.cn"), ("genomics", "Chinese research data", "genome assemblies", "biodiversity"),
        "仅接入 GWH 自有公开组装；注释表与序列文件保留原格式及组装版本，不包含受控数据。"),
    _definition("uniprot", "UniProt 蛋白质序列", "按蛋白质名称、基因或物种检索 UniProt，获取所选记录的完整 FASTA 序列。",
        ("www.ebi.ac.uk", "rest.uniprot.org", "www.uniprot.org"), ("proteins", "protein sequences", "biochemistry", "molecular biology"),
        "每条结果对应一条蛋白质记录；下载完整序列，不代表整个物种蛋白质组。保留获取时的序列与条目版本，源站当前记录可能继续更新。"),
    _definition("rcsb-pdb", "RCSB PDB 分子结构", "检索实验测定的生物大分子结构，获取完整 PDBx/mmCIF 原生坐标文件。",
        ("search.rcsb.org", "data.rcsb.org", "files.rcsb.org", "www.rcsb.org"), ("structural biology", "proteins", "molecular structure", "crystallography"),
        "每条结果对应一个已公开实验结构，获取当前完整坐标文件并记录修订日期；不是固定历史归档。不把结构说明元数据当成坐标数据。"),
    _definition("ena", "ENA 核酸序列档案", "按主题检索 European Nucleotide Archive 已公开核酸序列，并获取完整 FASTA。",
        ("www.ebi.ac.uk",), ("nucleotide sequences", "genomics", "genetics", "biodiversity"),
        "每条结果对应一个公开序列记录，并固定其序列版本；原始测序 FASTQ 仍由 SRA 连接器获取。"),
    _definition("chembl", "ChEMBL 化合物结构", "检索 ChEMBL 化合物名称及分子编号，获取包含真实化学结构的完整 SDF 文件。",
        ("www.ebi.ac.uk",), ("chemistry", "drug discovery", "molecular structures", "pharmacology"),
        "每条结果对应一个分子结构；本入口不将文献或靶点说明当作实验活性数据。"),
    _definition("pubchem", "PubChem 化学数据", "按化合物名称或 CID 查找 PubChem 结构记录，下载原生二维 SDF。",
        ("pubchem.ncbi.nlm.nih.gov",), ("chemistry", "molecular structures", "materials", "biochemistry"),
        "按化合物名称或 CID 精确查找；每条结果为完整化合物结构，不代表所有生物测定数据。遵循来源使用政策，不假定所有贡献数据采用统一许可。"),
    _definition("pride", "PRIDE 蛋白质组数据", "检索 PRIDE 公开蛋白质组研究并获取已发布质谱、肽段鉴定和定量结果文件。",
        ("www.ebi.ac.uk", "ftp.pride.ebi.ac.uk"), ("proteomics", "mass spectrometry", "protein quantification", "life sciences"),
        "只获取公开研究的质谱、鉴定、定量和序列文件，排除说明及样本描述附件；保留原格式与项目许可，完整列出当前项目可获取的文件。"),
    _definition("metabolights", "MetaboLights 代谢组数据", "检索 MetaboLights 公开研究，下载代谢物测量表及公开原始实验文件。",
        ("www.ebi.ac.uk",), ("metabolomics", "metabolites", "mass spectrometry", "NMR", "life sciences"),
        "仅展示明确公开研究中的实体数据；调查说明、样本描述和目录本身不算可获取实验数据。"),
    _definition("metabolomics-workbench", "Metabolomics Workbench 代谢组", "按研究标题检索 NIH Metabolomics Workbench，获取包含全部分析和测量值的原生 mwTab JSON。",
        ("www.metabolomicsworkbench.org",), ("metabolomics", "lipidomics", "metabolite measurements", "life sciences"),
        "按研究标题或 ST 编号查找；仅返回来源确认有代谢物测量值的研究，获取研究的完整 mwTab JSON，包括所有公开分析。各研究许可不同，以来源实际声明为准。"),
    _definition("nasa-exoplanet", "NASA Exoplanet 系外行星", "查找 NASA 系外行星档案中已确认行星的物理和轨道参数，下载完整单行星 CSV 或全部复合参数表。",
        ("exoplanetarchive.ipac.caltech.edu",), ("astronomy", "astrophysics", "exoplanets", "planetary science"),
        "支持行星或恒星名称，以及系外行星复合参数全表；明确区分单行星记录和全表。全表是源站动态快照，无固定版本；下载后记录实际文件哈希。"),
)

SOURCE_INFO = {
    "ngdc-gwh": dict(homepage="https://ngdc.cncb.ac.cn/gwh/", logo_url="https://ngdc.cncb.ac.cn/favicon.ico", country="中国", documentation="https://ngdc.cncb.ac.cn/gwh/api_documents"),
    "uniprot": dict(homepage="https://www.uniprot.org/", logo_url="https://www.uniprot.org/favicon.ico", country="国际", documentation="https://www.uniprot.org/help/api_queries"),
    "rcsb-pdb": dict(homepage="https://www.rcsb.org/", logo_url="https://www.rcsb.org/favicon.ico", country="美国 / 国际", documentation="https://search.rcsb.org/"),
    "ena": dict(homepage="https://www.ebi.ac.uk/ena/", logo_url="https://www.ebi.ac.uk/favicon.ico", country="英国 / 国际", documentation="https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/advanced-search.html"),
    "chembl": dict(homepage="https://www.ebi.ac.uk/chembl/", logo_url="https://www.ebi.ac.uk/favicon.ico", country="英国 / 国际", documentation="https://chembl.gitbook.io/chembl-interface-documentation/web-services/chembl-data-web-services"),
    "pubchem": dict(homepage="https://pubchem.ncbi.nlm.nih.gov/", logo_url="https://pubchem.ncbi.nlm.nih.gov/favicon.ico", country="美国", documentation="https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest"),
    "pride": dict(homepage="https://www.ebi.ac.uk/pride/", logo_url="https://www.ebi.ac.uk/favicon.ico", country="英国 / 国际", documentation="https://www.ebi.ac.uk/pride/ws/archive/v2/docs/api-guide.html"),
    "metabolights": dict(homepage="https://www.ebi.ac.uk/metabolights/", logo_url="https://www.ebi.ac.uk/favicon.ico", country="英国 / 国际", documentation="https://www.ebi.ac.uk/training/online/courses/metabolights-quick-tour/getting-data-from-metabolights/"),
    "metabolomics-workbench": dict(homepage="https://www.metabolomicsworkbench.org/", logo_url="https://www.metabolomicsworkbench.org/favicon.ico", country="美国", documentation="https://www.metabolomicsworkbench.org/tools/MWRestAPIv1.2.pdf"),
    "nasa-exoplanet": dict(homepage="https://exoplanetarchive.ipac.caltech.edu/", logo_url="https://exoplanetarchive.ipac.caltech.edu/favicon.ico", country="美国", documentation="https://exoplanetarchive.ipac.caltech.edu/docs/API_resources.html"),
}
