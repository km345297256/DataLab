"""Freeze independently authored examples before any creator model call."""
import pytest
import xml.etree.ElementTree as ET

from skill_creator_generic_scenarios import (
    TEXT_INPUT, TEXT_EXPECTED, XML_INPUT, XML_EXPECTED,
    normalize_document, inventory_documents,
)


def test_pre_authored_text_and_document_expectations():
    assert normalize_document(TEXT_INPUT) == TEXT_EXPECTED
    assert inventory_documents(XML_INPUT) == XML_EXPECTED


@pytest.mark.parametrize(("data", "expected"), [
    ({"text": ""}, {"text": "", "line_count": 0, "changed": False}),
    ({"text": "", "final_newline": True}, {"text": "", "line_count": 0, "changed": False}),
    ({"text": " A \t", "trim_trailing": False, "final_newline": True},
     {"text": " A \t\n", "line_count": 1, "changed": True}),
    ({"text": "a\n\nb\n"}, {"text": "a\n\nb\n", "line_count": 3, "changed": False}),
])
def test_text_boundary_and_nondefault_options(data, expected):
    assert normalize_document(data) == expected


@pytest.mark.parametrize("data", [
    {"text": 2}, {"text": None}, {"text": "a", "trim_trailing": None},
    {"text": "a", "trim_trailing": 1}, {"text": "a", "final_newline": "true"},
])
def test_text_invalid_types(data):
    with pytest.raises(ValueError):
        normalize_document(data)


def test_empty_document_inventory():
    assert inventory_documents({"documents": []}) == {
        "documents": [], "totals": {"documents": 0, "elements": 0, "attributes": 0}}


@pytest.mark.parametrize("documents", [
    [{"name": "a", "xml": "<r>"}],
    [{"name": "a", "xml": "<r/>"}, {"name": "a", "xml": "<s/>"}],
    [{"name": "", "xml": "<r/>"}],
    [{"name": "a", "xml": None}],
    [{"name": "a", "xml": "<!DOCTYPE r><r/>"}],
    None,
])
def test_document_invalid_inputs(documents):
    with pytest.raises((ValueError, ET.ParseError)):
        inventory_documents({"documents": documents})
