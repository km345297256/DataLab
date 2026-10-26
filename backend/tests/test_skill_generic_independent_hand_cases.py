"""Validate the independently frozen literals, never generated skill outputs."""
from copy import deepcopy
import json
import xml.etree.ElementTree as ET

import pytest

from skill_creator_generic_hand_cases import GENERIC_HAND_CASES, independent_cases
from skill_creator_generic_scenarios import normalize_document, inventory_documents


CASES = [(scenario, case) for scenario, cases in GENERIC_HAND_CASES.items() for case in cases]


@pytest.mark.parametrize("scenario,case", CASES, ids=[case["id"] for _, case in CASES])
def test_contract_reference_agrees_with_pre_authored_literals_without_mutating_inputs(scenario, case):
    # Expected outputs are literals in the fixture file. This only catches a
    # transcription mistake; it does not derive or update any expectation.
    function = normalize_document if scenario == "text" else inventory_documents
    data = deepcopy(case["input"])
    if case["reject"]:
        with pytest.raises((ValueError, TypeError, KeyError, ET.ParseError)):
            function(data)
    else:
        actual = function(data)
        assert json.dumps(actual, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
            case["expected"], sort_keys=True, ensure_ascii=False, allow_nan=False)
    assert data == case["input"]


def test_independent_cases_are_json_finite_unique_and_cover_all_three_kinds():
    for cases in GENERIC_HAND_CASES.values():
        assert len({case["id"] for case in cases}) == len(cases)
        assert {case["kind"] for case in cases} == {"normal", "boundary", "invalid"}
        json.dumps(cases, allow_nan=False)
        assert all((case["kind"] == "invalid") == case["reject"] for case in cases)
        assert all(("expected" in case) != case["reject"] for case in cases)


def test_interface_binding_receives_copies_not_mutable_oracle_storage():
    returned = independent_cases("text")
    returned[0]["input"]["text"] = "changed only in the local binding"
    returned[0]["expected"]["changed"] = False
    assert returned != independent_cases("text")


def test_xml_duplicate_nearby_control_changes_only_second_name():
    cases = {case["id"]: case for case in GENERIC_HAND_CASES["document"]}
    duplicate = deepcopy(cases["xml-duplicate-name"]["input"])
    duplicate["documents"][1]["name"] = "second"
    assert duplicate == cases["xml-unique-name-control"]["input"]


def test_text_nondefault_options_live_in_input_not_compensating_cli_flags():
    cases = {case["id"]: case for case in GENERIC_HAND_CASES["text"]}
    nondefault = cases["text-both-nondefault-options"]
    assert nondefault["input"]["trim_trailing"] is False
    assert nondefault["input"]["final_newline"] is True
    assert all("argv" not in case for case in cases.values())


def test_receipt_transport_preserves_literal_unicode_line_separators():
    from acceptance.check_skill_creator_generic_hand_oracle import decode_receipt
    expected = {"text": "a\u2028b\u2029c\u0085d", "line_count": 4}
    physical_line = "RECEIPT:" + json.dumps(expected, ensure_ascii=False) + "\n"
    assert len(physical_line.splitlines()) > 1
    assert decode_receipt(physical_line.encode()) == expected


def test_receipt_transport_does_not_accept_truncated_or_duplicate_records():
    from acceptance.check_skill_creator_generic_hand_oracle import decode_receipt
    with pytest.raises(json.JSONDecodeError):
        decode_receipt(b'RECEIPT:{"text":"truncated\n')
    with pytest.raises(AssertionError):
        decode_receipt(b'RECEIPT:{}\nRECEIPT:{}\n')
