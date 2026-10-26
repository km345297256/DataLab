"""Trusted source capabilities shared by discovery, connectors and query planning.

A definition is not an integration: only enabled entries are exposed. Enable a
new source after its search, file resolution and actual-content checks work.
An index may discover DOI metadata, but that metadata never counts as acquired
data; its resolver must still produce a supported, readable entity-data file.
"""
from dataclasses import dataclass
import re
from typing import Literal

from app.domain.models.open_data_research_batch_definitions import DEFINITIONS as RESEARCH_DEFINITIONS
from app.domain.models.open_data_eastasia_batch_definitions import DEFINITIONS as EASTASIA_DEFINITIONS
from app.domain.models.open_data_scientific_batch_definitions import DEFINITIONS as SCIENTIFIC_DEFINITIONS
from app.domain.models.open_data_government_batch_definitions import DEFINITIONS as GOVERNMENT_DEFINITIONS
from app.domain.models.open_data_ml_expansion_definitions import DEFINITIONS as ML_DEFINITIONS
from app.domain.models.open_data_earth_expansion_definitions import DEFINITIONS as EARTH_DEFINITIONS
from app.domain.models.open_data_statistics_expansion_definitions import DEFINITIONS as STATISTICS_DEFINITIONS
from app.domain.models.open_data_regional_expansion_definitions import DEFINITIONS as REGIONAL_DEFINITIONS
from app.domain.models.open_data_research_oct22_definitions import DEFINITIONS as RESEARCH_OCT22_DEFINITIONS
from app.domain.models.open_data_international_oct22_definitions import DEFINITIONS as INTERNATIONAL_OCT22_DEFINITIONS
from app.domain.models.open_data_france_oct22_definitions import DEFINITIONS as FRANCE_OCT22_DEFINITIONS
from app.domain.models.open_data_domestic_oct22_definitions import DEFINITIONS as DOMESTIC_OCT22_DEFINITIONS
from app.domain.models.open_data_statistics_oct23_definitions import DEFINITIONS as STATISTICS_OCT23_DEFINITIONS
from app.domain.models.open_data_repositories_oct23_definitions import DEFINITIONS as REPOSITORIES_OCT23_DEFINITIONS
from app.domain.models.open_data_earth_oct23_definitions import DEFINITIONS as EARTH_OCT23_DEFINITIONS
from app.domain.models.open_data_math_foundation_definitions import DEFINITIONS as MATH_FOUNDATION_DEFINITIONS
from app.domain.models.open_data_physics_space_foundation_definitions import DEFINITIONS as PHYSICS_SPACE_FOUNDATION_DEFINITIONS
from app.domain.models.open_data_chem_materials_foundation_definitions import DEFINITIONS as CHEM_MATERIALS_FOUNDATION_DEFINITIONS
from app.domain.models.open_data_astronomy_foundation_definitions import DEFINITIONS as ASTRONOMY_FOUNDATION_DEFINITIONS


@dataclass(frozen=True)
class SourceDefinition:
    id: str
    title: str
    connector_title: str
    description: str
    domains: tuple[str, ...]
    subjects: tuple[str, ...]
    languages: tuple[str, ...]
    protocol: Literal["rest", "entrez", "dataverse"]
    discovery_mode: Literal["repository", "doi_index", "observations"]
    acquisition: Literal["files", "resolved_files", "csv_snapshot"]
    limitation: str
    authentication: Literal["none", "api_key", "oauth2"] = "none"
    license_mode: Literal["per_dataset", "cc_by_4"] = "per_dataset"
    query_semantics: Literal["provider_default", "all_terms"] = "provider_default"
    automatic_discovery: bool = True
    connector_enabled: bool = True
    connector_registration: Literal["general", "legacy"] = "general"
    read_operations: tuple[str, ...] = ("search",)
    # Only trusted repository identifiers are derived from DOI metadata. The
    # index's landing-page URLs never become arbitrary download destinations.
    doi_prefixes: tuple[str, ...] = ()
    doi_external_id: Literal["doi", "dataverse_pid", "numeric_suffix"] = "doi"
    enabled: bool = True

    def planning_capabilities(self) -> dict:
        """Small, non-secret model input, never interpreted as executable URLs."""
        return {"id": self.id, "description": self.description,
                "subjects": list(self.subjects), "languages": list(self.languages),
                "discovery_mode": self.discovery_mode, "acquisition": self.acquisition,
                "authentication": self.authentication, "query_semantics": self.query_semantics,
                "limitation": self.limitation}


SOURCE_DEFINITIONS = (
    SourceDefinition(
        id="zenodo", title="Zenodo 跨学科研究数据", connector_title="Zenodo",
        description="检索并核验跨学科研究数据文件，保留格式、许可与公开来源。",
        domains=("zenodo.org",), subjects=("all disciplines",), languages=("en", "zh"),
        protocol="rest", discovery_mode="repository", acquisition="files",
        query_semantics="all_terms",
        doi_prefixes=("10.5281/zenodo.",), doi_external_id="numeric_suffix",
        limitation="仅返回当前已核验可读取实际数据的记录；可在聊天中的数据查找结果中选择获取。"),
    SourceDefinition(
        id="datacite", title="DataCite 数据 DOI 索引", connector_title="DataCite",
        description="按主题查找数据 DOI，并核验受支持仓库中的实际数据文件。",
        domains=("api.datacite.org", "doi.org", "zenodo.org"), subjects=("all disciplines",),
        languages=("en", "zh"), protocol="rest", discovery_mode="doi_index", acquisition="resolved_files",
        limitation="DOI 索引本身不提供数据文件；仅返回受支持仓库中已解析并核验实际文件的记录。"),
    SourceDefinition(
        id="worldbank", title="World Bank 世界发展指标", connector_title="World Bank",
        description="查找有实际观测值的世界发展指标，覆盖经济、人口、环境等统计主题。",
        domains=("api.worldbank.org", "data.worldbank.org"),
        subjects=("economics", "education", "population", "public health statistics", "environmental indicators"),
        languages=("en",), protocol="rest", discovery_mode="observations", acquisition="csv_snapshot",
        license_mode="cc_by_4",
        limitation="返回前核验接口包含实际观测值；获取后保存为 CSV。关键词命中不代表时间或地区已过滤。"),
    SourceDefinition(
        id="ncbi-geo", title="NCBI GEO 研究数据", connector_title="NCBI GEO",
        description="检索基因表达及功能基因组研究，获取公开补充文件和表达矩阵。",
        domains=("eutils.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "ftp.ncbi.nlm.nih.gov"),
        subjects=("gene expression", "functional genomics", "molecular biology"), languages=("en", "zh"),
        protocol="entrez", discovery_mode="repository", acquisition="files",
        connector_registration="legacy", read_operations=("search", "list_files"),
        limitation="检索研究主题而非一般卫生统计；仅展示已核验的公开数据文件。"),
    SourceDefinition(
        id="ncbi-sra", title="NCBI SRA 测序数据", connector_title="NCBI SRA",
        description="检索测序实验，并从 ENA 公共镜像获取原始 FASTQ。",
        domains=("eutils.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "www.ebi.ac.uk", "ftp.sra.ebi.ac.uk"),
        subjects=("sequencing", "genomics", "molecular biology"), languages=("en", "zh"),
        protocol="entrez", discovery_mode="repository", acquisition="files",
        connector_registration="legacy", read_operations=("search", "list_files"),
        limitation="仅展示具有可读取公开 FASTQ 的实验；双端文件需成组获取，不包含受控访问数据。"),
    SourceDefinition(
        id="figshare", title="Figshare 跨学科研究数据", connector_title="Figshare",
        description="查找 Figshare 公开数据集并获取记录所附的实际数据文件。",
        domains=("api.figshare.com", "figshare.com", "ndownloader.figshare.com", "s3-eu-west-1.amazonaws.com"),
        subjects=("all disciplines",), languages=("en", "zh"), protocol="rest",
        discovery_mode="repository", acquisition="files",
        limitation="只接入公开数据集的可读取文件；私有、禁运和仅有外部介绍链接的记录不展示。", enabled=False),
    SourceDefinition(
        id="dryad", title="Dryad 研究数据", connector_title="Dryad",
        description="检索 Dryad 公开研究数据集，并获取已发布版本的实际文件。",
        domains=("datadryad.org",), subjects=("all disciplines", "ecology", "evolution", "life sciences"),
        languages=("en",), protocol="rest", discovery_mode="repository", acquisition="files",
        authentication="oauth2",
        limitation="下载 API 需要访问凭证；完成授权接入和真实文件验收后启用。", enabled=False),
    SourceDefinition(
        id="harvard-dataverse", title="Harvard Dataverse 研究数据", connector_title="Harvard Dataverse",
        description="检索 Harvard Dataverse 的公开数据集，获取发布版本中的原始格式文件。",
        domains=("dataverse.harvard.edu", "dvn-cloud-iqss.s3.amazonaws.com"), subjects=("all disciplines", "social sciences"),
        languages=("en", "zh"), protocol="dataverse", discovery_mode="repository", acquisition="files",
        query_semantics="all_terms",
        doi_prefixes=("10.7910/dvn/",), doi_external_id="dataverse_pid",
        limitation="仅接入此已登记实例的公开文件；受限文件或需要填写访客信息的文件不展示。"),
    SourceDefinition(
        id="openml", title="OpenML 机器学习数据", connector_title="OpenML",
        description="查找 OpenML 公开机器学习数据集并获取可读取的数据文件。",
        domains=("www.openml.org", "openml.org"), subjects=("machine learning", "benchmark datasets", "tabular data"),
        languages=("en",), protocol="rest", discovery_mode="repository", acquisition="files",
        query_semantics="all_terms",
        limitation="仅展示有可读取数据文件的数据集；任务、模型和评估结果不作为原始数据集返回。"),
    SourceDefinition(
        id="borealis", title="Borealis 加拿大研究数据", connector_title="Borealis",
        description="检索加拿大多机构跨学科研究数据，获取公开发布版本的原始文件。",
        domains=("borealisdata.ca", "olrc2.scholarsportal.info"),
        subjects=("all disciplines", "social sciences", "environment", "health research"),
        languages=("en", "fr"), protocol="dataverse", discovery_mode="repository", acquisition="files",
        query_semantics="all_terms", doi_prefixes=("10.5683/sp/", "10.5683/sp2/", "10.5683/sp3/"),
        doi_external_id="dataverse_pid",
        limitation="仅展示本仓库已核验可读取的公开原始文件；受限、禁运及需要访客登记的文件不展示。"),
    SourceDefinition(
        id="dataverse-nl", title="DataverseNL 荷兰研究数据", connector_title="DataverseNL",
        description="检索荷兰高校与研究机构发布的跨学科数据，保留原始格式和版本信息。",
        domains=("dataverse.nl", "objectstore.surf.nl"), subjects=("all disciplines", "social sciences", "life sciences"),
        languages=("en", "nl"), protocol="dataverse", discovery_mode="repository", acquisition="files",
        query_semantics="all_terms", doi_prefixes=("10.34894/",), doi_external_id="dataverse_pid",
        limitation="仅接入本仓库可匿名获取的原始文件；保留源许可并核验发布的文件摘要。"),
    SourceDefinition(
        id="recherche-data-gouv", title="Recherche Data Gouv 法国研究数据", connector_title="Recherche Data Gouv",
        description="检索法国国家研究数据仓库中的跨学科公开数据，获取发布版本的原始文件。",
        domains=("entrepot.recherche.data.gouv.fr", "prod-datainra.opens3r-tls.stockage.inrae.fr"),
        subjects=("all disciplines", "agriculture", "environment", "social sciences"),
        languages=("fr", "en"), protocol="dataverse", discovery_mode="repository", acquisition="files",
        query_semantics="all_terms", doi_prefixes=("10.57745/",), doi_external_id="dataverse_pid",
        limitation="仅展示本仓库已核验的公开文件；不将外部收录记录或受限数据计入可获取结果。"),
    SourceDefinition(
        id="pangaea", title="PANGAEA 地球与环境数据", connector_title="PANGAEA",
        description="查找地球科学、气候、海洋及生态观测数据，获取来源发布的原始数据表。",
        domains=("api.datacite.org", "doi.pangaea.de", "www.pangaea.de"),
        subjects=("earth science", "climate", "oceanography", "ecology", "environment", "geology"),
        languages=("en",), protocol="rest", discovery_mode="repository", acquisition="files",
        query_semantics="all_terms", doi_prefixes=("10.1594/pangaea.",),
        limitation="仅展示已开放且提供可读取原生数据表的记录；需要授权或仅有外部文件链接的记录不展示。"),
    SourceDefinition(
        id="sciencedb", title="ScienceDB 科学数据银行", connector_title="ScienceDB",
        description="检索科学数据银行发布的跨学科科研数据，支持中文主题并获取公开版本的实际文件。",
        domains=("www.scidb.cn", "scidb.cn", "china.scidb.cn", "download.scidb.cn", "api.datacite.org", "doi.org"),
        subjects=("all disciplines", "Chinese research datasets", "environment", "materials", "life sciences"),
        languages=("zh", "en"), protocol="rest", discovery_mode="repository", acquisition="files",
        doi_prefixes=("10.57760/sciencedb.", "10.11922/sciencedb."),
        limitation="仅接入明确公开且无需登录的已发布文件；访问受限、禁运和申请授权数据不展示。"),
    SourceDefinition(
        id="gbif", title="GBIF 全球生物多样性数据", connector_title="GBIF",
        description="查找物种分布、分类名录和采样研究，获取受支持官方节点发布的原始数据归档。",
        domains=("api.gbif.org", "www.gbif.org", "hosted-datasets.gbif.org", "ipt.gbif.org", "ipt.gbif.fr",
                 "ipt.gbif.no", "ipt.gbif.es", "ipt.gbif.pt"),
        subjects=("biodiversity", "ecology", "taxonomy", "species occurrence", "biogeography"),
        languages=("en", "zh", "fr", "es", "pt"), protocol="rest", discovery_mode="repository", acquisition="files",
        limitation="仅展示受支持官方托管节点上可读取的原始数据归档；不将元数据或需要账户提交的下载任务视为已可获取文件。"),
)


SOURCE_DEFINITIONS += tuple(SourceDefinition(**definition)
                            for definition in (*RESEARCH_DEFINITIONS, *EASTASIA_DEFINITIONS, *SCIENTIFIC_DEFINITIONS, *GOVERNMENT_DEFINITIONS,
                                               *ML_DEFINITIONS, *EARTH_DEFINITIONS, *STATISTICS_DEFINITIONS, *REGIONAL_DEFINITIONS,
                                               *RESEARCH_OCT22_DEFINITIONS, *INTERNATIONAL_OCT22_DEFINITIONS, *FRANCE_OCT22_DEFINITIONS,
                                               *DOMESTIC_OCT22_DEFINITIONS, *STATISTICS_OCT23_DEFINITIONS,
                                               *REPOSITORIES_OCT23_DEFINITIONS, *EARTH_OCT23_DEFINITIONS,
                                               *MATH_FOUNDATION_DEFINITIONS, *PHYSICS_SPACE_FOUNDATION_DEFINITIONS,
                                               *CHEM_MATERIALS_FOUNDATION_DEFINITIONS, *ASTRONOMY_FOUNDATION_DEFINITIONS))


def source_definitions(*, include_disabled: bool = False) -> tuple[SourceDefinition, ...]:
    return tuple(source for source in SOURCE_DEFINITIONS if include_disabled or source.enabled)


def get_source(source_id: str, *, include_disabled: bool = False) -> SourceDefinition | None:
    return next((source for source in source_definitions(include_disabled=include_disabled) if source.id == source_id), None)


def source_catalog() -> list[dict[str, str]]:
    return [{"id": source.id, "title": source.title} for source in source_definitions() if source.automatic_discovery]


def connector_sources() -> tuple[SourceDefinition, ...]:
    return tuple(source for source in source_definitions()
                 if source.connector_enabled and source.connector_registration == "general")


def source_domains(source: SourceDefinition) -> tuple[str, ...]:
    if source.discovery_mode != "doi_index":
        return source.domains
    return tuple(dict.fromkeys((*source.domains, *(domain for repository in source_definitions()
                                                  if repository.doi_prefixes for domain in repository.domains))))


def doi_repository(doi: str) -> tuple[str, str] | None:
    """Resolve only registered DOI namespaces; adapters still verify ownership."""
    if not isinstance(doi, str) or not re.fullmatch(r'10\.\d{4,9}/[^\s<>"?#]{1,180}', doi):
        return None
    normalized = doi.lower()
    for source in source_definitions():
        for prefix in source.doi_prefixes:
            if not normalized.startswith(prefix.lower()) or len(normalized) == len(prefix):
                continue
            if source.doi_external_id == "numeric_suffix":
                identifier = normalized[len(prefix):]
                if not re.fullmatch(r"[1-9][0-9]{0,19}", identifier):
                    continue
            elif source.doi_external_id == "dataverse_pid":
                identifier = "doi:" + normalized
            else:
                identifier = normalized
            return source.id, identifier
    return None


def public_connector_read_operations() -> dict[str, frozenset[str]]:
    return {source.id: frozenset(source.read_operations) for source in source_definitions()
            if source.connector_enabled and source.authentication == "none"}


CATALOG = tuple(source_catalog())
SOURCES = frozenset(source["id"] for source in CATALOG)
