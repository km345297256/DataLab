"""Small artificial workflows for acceptance across unrelated skill domains.

Inputs and expectations are authored before model generation. These are
acceptance fixtures, never special cases in the production creator.
"""


def normalize_document(data):
    import unicodedata

    original = data["text"]
    trim = data.get("trim_trailing", True)
    newline = data.get("final_newline", False)
    if not isinstance(original, str) or type(trim) is not bool or type(newline) is not bool:
        raise ValueError("text must be a string and options must be booleans")
    text = unicodedata.normalize("NFC", original).replace("\r\n", "\n").replace("\r", "\n")
    if trim:
        text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    if newline and text and not text.endswith("\n"):
        text += "\n"
    return {"text": text, "line_count": len(text.splitlines()), "changed": text != original}


def inventory_documents(data):
    import xml.etree.ElementTree as ET

    documents = data["documents"]
    if not isinstance(documents, list):
        raise ValueError("documents must be a list")
    seen, results = set(), []
    for item in documents:
        if not isinstance(item, dict):
            raise ValueError("document must be an object")
        name, content = item["name"], item["xml"]
        if not isinstance(name, str) or not name or name in seen or not isinstance(content, str):
            raise ValueError("unique nonempty names and XML strings are required")
        if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
            raise ValueError("document type and entity declarations are not supported")
        root = ET.fromstring(content)
        seen.add(name)
        elements = list(root.iter())
        results.append({"name": name, "root_tag": root.tag.rsplit("}", 1)[-1],
                        "element_count": len(elements),
                        "attribute_count": sum(len(element.attrib) for element in elements)})
    return {"documents": results, "totals": {"documents": len(results),
        "elements": sum(item["element_count"] for item in results),
        "attributes": sum(item["attribute_count"] for item in results)}}


TEXT_REQUEST = (
    "将文档文本规范化流程保存为可复用技能。输入JSON含text字符串，trim_trailing和final_newline是可选布尔参数，"
    "默认分别为true、false；显式null或非布尔值必须拒绝，text非字符串也必须拒绝。"
    "使用标准库unicodedata将文本转为Unicode NFC，然后将CRLF和单独CR统一成LF。"
    "trim_trailing为true时仅删除每行末尾的空格和制表符，保留行首缩进、空白行和全部正文。"
    "final_newline为true时，仅为非空且末尾没有LF的结果追加一个LF；false不删除已有LF。"
    "结果JSON固定包含text、line_count、changed；line_count按Python str.splitlines的逻辑行数，"
    "changed表示最终文本与原text是否不同。空字符串合法，行数为0，不强行追加换行。"
    "输入只读，输出写独立位置。正常、边界、无效检查应覆盖组合字符、换行格式、两个非默认选项、空文本和类型拒绝。"
)

XML_REQUEST = (
    "将结构化文档盘点流程保存为可复用技能。输入JSON含documents数组，每项包含非空且唯一的name字符串与xml字符串。"
    "使用Python标准库xml.etree.ElementTree解析文档，不手写XML解析器。非法XML、重复名字、非法字段类型必须明确拒绝。"
    "本流程不支持DOCTYPE和ENTITY声明，检测到任一声明须拒绝，不能替换、忽略或展开。"
    "按输入顺序输出每篇文档的name、root_tag、element_count、attribute_count；root_tag去除命名空间，"
    "element_count包含根节点，attribute_count累计所有元素实际属性，命名空间声明不算普通属性。"
    "输出JSON包含documents和totals；totals含documents、elements、attributes三个总计数，须与逐文档计数一致。"
    "空documents数组合法，结果为空数组且总计均为0。不复制XML正文进入结果，不修改输入。"
    "正常、边界、无效验证应覆盖嵌套元素、命名空间、空列表、损坏XML和重复名字。"
)

TEXT_INPUT = {"text": "Cafe\u0301  \r\n  beta\t\r\n", "trim_trailing": True, "final_newline": False}
TEXT_EXPECTED = {"text": "Café\n  beta\n", "line_count": 2, "changed": True}
XML_INPUT = {"documents": [
    {"name": "first", "xml": '<r xmlns="urn:example" code="a"><item/><item flag="yes"/></r>'},
    {"name": "second", "xml": "<note>text</note>"},
]}
XML_EXPECTED = {"documents": [
    {"name": "first", "root_tag": "r", "element_count": 3, "attribute_count": 2},
    {"name": "second", "root_tag": "note", "element_count": 1, "attribute_count": 0},
], "totals": {"documents": 2, "elements": 4, "attributes": 2}}
