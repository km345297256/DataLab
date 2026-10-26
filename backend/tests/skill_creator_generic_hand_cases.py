"""Independent TEXT/XML inputs and literal expectations, never model payloads.

These cases are frozen from the human-authored task contracts before inspecting
generated implementations. Interface binding may select declared CLI/output
paths later; it must never change these inputs, results, or rejection outcomes.
"""
from copy import deepcopy

from skill_creator_generic_scenarios import TEXT_EXPECTED, TEXT_INPUT, XML_EXPECTED, XML_INPUT


def success(case_id, data, expected, *, kind="boundary"):
    return {"id": case_id, "kind": kind, "input": data, "expected": expected, "reject": False}


def rejection(case_id, data):
    return {"id": case_id, "kind": "invalid", "input": data, "reject": True}


TEXT_CASES = [
    success("text-normal-nfc-crlf", deepcopy(TEXT_INPUT), deepcopy(TEXT_EXPECTED), kind="normal"),
    success("text-empty-default", {"text": ""}, {"text": "", "line_count": 0, "changed": False}),
    success("text-empty-final-newline", {"text": "", "final_newline": True},
            {"text": "", "line_count": 0, "changed": False}),
    success("text-idempotent", {"text": "Café\n  beta\n"},
            {"text": "Café\n  beta\n", "line_count": 2, "changed": False}),
    success("text-mixed-line-endings", {"text": "a\rb\r\nc\n"},
            {"text": "a\nb\nc\n", "line_count": 3, "changed": True}),
    success("text-indent-and-blank-lines", {"text": "  alpha \t\n \t\n\tbeta\t "},
            {"text": "  alpha\n\n\tbeta", "line_count": 3, "changed": True}),
    success("text-trim-disabled", {"text": "  a \t\r\n b\t", "trim_trailing": False},
            {"text": "  a \t\n b\t", "line_count": 2, "changed": True}),
    success("text-both-nondefault-options", {"text": " A \t", "trim_trailing": False, "final_newline": True},
            {"text": " A \t\n", "line_count": 1, "changed": True}),
    success("text-retain-existing-blank-final-line", {"text": "a\n\n", "final_newline": False},
            {"text": "a\n\n", "line_count": 2, "changed": False}),
    success("text-only-trimmable-whitespace", {"text": " \t", "final_newline": True},
            {"text": "", "line_count": 0, "changed": True}),
    success("text-preserve-nonbreaking-space", {"text": " a\u00a0\t "},
            {"text": " a\u00a0", "line_count": 1, "changed": True}),
    success("text-splitlines-unicode-separators", {"text": "a\u2028b\u0085c\n"},
            {"text": "a\u2028b\u0085c\n", "line_count": 3, "changed": False}),
    success("text-hangul-nfc", {"text": "\u1100\u1161"},
            {"text": "가", "line_count": 1, "changed": True}),
    success("text-whitespace-preserved-by-option", {"text": " \t", "trim_trailing": False},
            {"text": " \t", "line_count": 1, "changed": False}),
    success("text-final-newline-not-duplicated", {"text": "a\n", "final_newline": True},
            {"text": "a\n", "line_count": 1, "changed": False}),
    rejection("text-required-field", {}),
    rejection("text-null", {"text": None}),
    rejection("text-number", {"text": 4}),
    rejection("text-boolean", {"text": True}),
    rejection("text-array", {"text": ["a"]}),
    rejection("text-trim-null", {"text": "a", "trim_trailing": None}),
    rejection("text-trim-number", {"text": "a", "trim_trailing": 1}),
    rejection("text-trim-string", {"text": "a", "trim_trailing": "false"}),
    rejection("text-newline-null", {"text": "a", "final_newline": None}),
    rejection("text-newline-number", {"text": "a", "final_newline": 0}),
    rejection("text-newline-string", {"text": "a", "final_newline": "true"}),
]


DOCUMENT_CASES = [
    success("xml-normal-nested-namespace", deepcopy(XML_INPUT), deepcopy(XML_EXPECTED), kind="normal"),
    success("xml-empty-list", {"documents": []},
            {"documents": [], "totals": {"documents": 0, "elements": 0, "attributes": 0}}),
    success("xml-one-root", {"documents": [{"name": "single", "xml": '<root a="1" b="2"/>'}]},
            {"documents": [{"name": "single", "root_tag": "root", "element_count": 1, "attribute_count": 2}],
             "totals": {"documents": 1, "elements": 1, "attributes": 2}}),
    success("xml-prefixed-attributes-and-nesting", {"documents": [{"name": "namespace",
        "xml": '<s:root xmlns:s="urn:s" xmlns:a="urn:a" a:flag="yes"><s:child id="1"><leaf/></s:child></s:root>'}]},
        {"documents": [{"name": "namespace", "root_tag": "root", "element_count": 3, "attribute_count": 2}],
         "totals": {"documents": 1, "elements": 3, "attributes": 2}}),
    success("xml-preserve-order", {"documents": [{"name": "z", "xml": "<last><child/></last>"},
                                                   {"name": "a", "xml": "<first/>"}]},
            {"documents": [{"name": "z", "root_tag": "last", "element_count": 2, "attribute_count": 0},
                           {"name": "a", "root_tag": "first", "element_count": 1, "attribute_count": 0}],
             "totals": {"documents": 2, "elements": 3, "attributes": 0}}),
    success("xml-predefined-entities-and-text", {"documents": [{"name": "text",
        "xml": '<r message="a &amp; b">&lt;body&gt;<c/>tail &amp; text</r>'}]},
        {"documents": [{"name": "text", "root_tag": "r", "element_count": 2, "attribute_count": 1}],
         "totals": {"documents": 1, "elements": 2, "attributes": 1}}),
    success("xml-unique-name-control", {"documents": [{"name": "first", "xml": "<r/>"},
                                                       {"name": "second", "xml": "<r/>"}]},
            {"documents": [{"name": "first", "root_tag": "r", "element_count": 1, "attribute_count": 0},
                           {"name": "second", "root_tag": "r", "element_count": 1, "attribute_count": 0}],
             "totals": {"documents": 2, "elements": 2, "attributes": 0}}),
    rejection("xml-documents-null", {"documents": None}),
    rejection("xml-documents-object", {"documents": {}}),
    rejection("xml-item-string", {"documents": ["<r/>"]}),
    rejection("xml-name-missing", {"documents": [{"xml": "<r/>"}]}),
    rejection("xml-name-empty", {"documents": [{"name": "", "xml": "<r/>"}]}),
    rejection("xml-name-number", {"documents": [{"name": 1, "xml": "<r/>"}]}),
    rejection("xml-content-missing", {"documents": [{"name": "a"}]}),
    rejection("xml-content-null", {"documents": [{"name": "a", "xml": None}]}),
    rejection("xml-content-number", {"documents": [{"name": "a", "xml": 3}]}),
    rejection("xml-duplicate-name", {"documents": [{"name": "first", "xml": "<r/>"},
                                                    {"name": "first", "xml": "<r/>"}]}),
    rejection("xml-malformed", {"documents": [{"name": "a", "xml": "<r><item></r>"}]}),
    rejection("xml-doctype", {"documents": [{"name": "a", "xml": "<!DOCTYPE r><r/>"}]}),
    rejection("xml-entity-declaration", {"documents": [{"name": "a", "xml": '<!ENTITY name "text"><r/>'}]}),
    rejection("xml-internal-entity-declaration", {"documents": [{"name": "a",
        "xml": '<!DOCTYPE r [<!ENTITY name "text">]><r>&name;</r>'}]}),
]


GENERIC_HAND_CASES = {"text": TEXT_CASES, "document": DOCUMENT_CASES}


def independent_cases(scenario):
    """Return a copy so runtime interface binding cannot mutate the fixtures."""
    return deepcopy(GENERIC_HAND_CASES[scenario])
