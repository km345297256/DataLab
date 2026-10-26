"""Original full-topic exports from international statistical organisations."""

# Enable only after the adapter and actual-container full-file acceptance pass.
VALIDATED_SOURCES = frozenset({"faostat", "bis"})


def _definition(source, name, title, description, domains, subjects, limitation):
    enabled = source in VALIDATED_SOURCES
    return dict(id=source, connector_title=name, title=title, description=description,
                domains=domains, subjects=subjects, languages=("en",), protocol="rest",
                discovery_mode="observations", acquisition="files", query_semantics="all_terms",
                limitation=limitation, enabled=enabled, connector_enabled=enabled)


DEFINITIONS = (
    _definition("faostat", "FAOSTAT", "FAOSTAT 联合国粮农统计",
                "检索联合国粮农组织完整批量数据目录，获取农业、粮食、土地利用、贸易和排放等主题的原始 CSV 压缩包。",
                ("bulks-faostat.fao.org", "www.fao.org", "faostat.fao.org"),
                ("agriculture", "food security", "nutrition", "forestry", "land use", "agricultural trade", "emissions"),
                "每条结果对应一个主题领域的完整英文标准化数据包，包含源站全部可用地区和年份及随附代码字典；关键词只定位主题，不过滤观测值。保留原始 ZIP、来源更新日期与许可条款。"),
    _definition("bis", "BIS Data Portal", "BIS 国际清算银行统计",
                "检索国际清算银行公开批量下载目录，获取银行、债务、汇率、房价、政策利率和支付等主题的完整 CSV 压缩包。",
                ("data.bis.org", "www.bis.org"),
                ("economics", "finance", "banking", "credit", "debt", "exchange rates", "property prices", "interest rates", "payments"),
                "仅接入官方批量目录提供的完整扁平 CSV ZIP；覆盖所选主题所有公开序列与时期，关键词不筛选国家或年份。源站更新日期保留为发布信息，未将数据结构版本视作观测版本。"),
)

SOURCE_INFO = {
    "faostat": dict(homepage="https://www.fao.org/faostat/en/#home", country="联合国 / 全球",
                    documentation="https://bulks-faostat.fao.org/production/datasets_E.json",
                    logo_url="https://www.fao.org/faostat/en/src/images/logo/FAO-logo.svg"),
    "bis": dict(homepage="https://data.bis.org/", country="国际",
                documentation="https://data.bis.org/bulkdownload",
                logo_url="https://data.bis.org/favicon.ico"),
}
