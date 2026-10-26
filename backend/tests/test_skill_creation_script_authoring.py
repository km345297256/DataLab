"""Long scripts are assembled from complete checked units without partial joins."""
import ast
import copy
import json
from unittest.mock import Mock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import CoverageError
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.script_authoring import generate_modular_script
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_skill_creation_gateway import PRIVATE, make_gateway, requests


CONTRACT = {"requirements": [{"id": "R001", "kind": "method", "statement": "Compute the input row count without changing input.", "source_ids": ["S001"]}]}
REQUEST = {"path": "scripts/count_rows.py", "purpose": "Count current input rows with an explicit output destination.",
           "invocation": "python scripts/count_rows.py --input INPUT --output OUTPUT", "requirement_ids": ["R001"]}
PLAN = {"imports": ["import argparse", "import csv", "from pathlib import Path"], "entrypoint": "main", "functions": [
    {"unit_id": "U001", "name": "count_rows", "signature": "def count_rows(rows):", "purpose": "Count all parsed data rows without modifying them.", "requirement_ids": ["R001"], "depends_on": []},
    {"unit_id": "U002", "name": "main", "signature": "def main():", "purpose": "Read the current input and write only to independent output.", "requirement_ids": ["R001"], "depends_on": ["U001"]},
]}
COUNT = {"unit_id": "U001", "content": "def count_rows(rows):\n    return len(rows)"}
MAIN = {"unit_id": "U002", "content": '''def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if Path(args.input).resolve() == Path(args.output).resolve():
        raise ValueError('Input must remain read only')
    with open(args.input) as source:
        rows = list(csv.DictReader(source))
    Path(args.output).write_text(str(count_rows(rows)))
'''}


async def author(responses, *, payload=None, stage="implementation", check=None):
    gateway, calls, _ = make_gateway(responses)
    result = await generate_modular_script(gateway, HelperScript, stage, "Preserve the complete method.",
        payload or {"script_request": copy.deepcopy(REQUEST), "method_contract": CONTRACT}, check, CONTRACT)
    return result, requests(calls)


@pytest.mark.asyncio
async def test_complete_functions_assemble_one_checked_script_and_preserve_metadata():
    check = Mock()
    script, calls = await author([PLAN, COUNT, MAIN], check=check)
    assert script.model_dump(exclude={"content"}) == {key: REQUEST[key] for key in ("path", "purpose", "invocation")}
    tree = ast.parse(script.content)
    assert [node.name for node in tree.body if isinstance(node, ast.FunctionDef)] == ["count_rows", "main"]
    assert isinstance(tree.body[-1], ast.If) and len(tree.body[-1].body) == 1
    assert script.content.count('if __name__ == "__main__":') == 1
    compile(script.content, script.path, "exec")
    check.assert_called_once_with(script)
    assert [call["authoring_phase"] for call in calls] == ["plan", "unit", "unit"]
    assert calls[-1]["accepted_functions"] == [COUNT]


@pytest.mark.asyncio
async def test_truncated_later_unit_does_not_regenerate_accepted_function_or_echo_partial_text():
    partial = AIMessage(content='{"content":"private truncated garbage', response_metadata={"finish_reason": "length"})
    script, calls = await author([PLAN, COUNT, partial, MAIN])
    assert [call.get("function_request", {}).get("unit_id") for call in calls] == [None, "U001", "U002", "U002"]
    assert calls[2]["accepted_functions"] == calls[3]["accepted_functions"] == [COUNT]
    assert "private truncated garbage" not in json.dumps(calls) and "private truncated garbage" not in script.content
    assert "previous_candidate" not in calls[-1]


@pytest.mark.asyncio
async def test_unfinishable_unit_stops_without_final_check_or_restarting_accepted_units():
    partial = AIMessage(content='{"content":"partial', response_metadata={"finish_reason": "length"})
    check = Mock()
    gateway, calls, _ = make_gateway([PLAN, COUNT, partial, partial])
    with pytest.raises(SkillCreationError) as failure:
        await generate_modular_script(gateway, HelperScript, "implementation", "Complete method", {"script_request": REQUEST}, check, CONTRACT)
    assert failure.value.code == "output_truncated"
    assert [call.get("function_request", {}).get("unit_id") for call in requests(calls)] == [None, "U001", "U002", "U002"]
    check.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_coverage", "duplicate_function", "unknown_dependency", "import_statement", "default_call", "annotation_call"])
async def test_invalid_manifest_is_repaired_before_any_function_is_generated(mutation):
    bad = copy.deepcopy(PLAN)
    if mutation == "missing_coverage":
        for spec in bad["functions"]:
            spec["requirement_ids"] = ["R999"]
    elif mutation == "duplicate_function":
        bad["functions"][1]["name"] = "count_rows"
    elif mutation == "unknown_dependency":
        bad["functions"][1]["depends_on"] = ["U999"]
    elif mutation == "import_statement":
        bad["imports"][0] += "\nprint('extra top level')"
    elif mutation == "default_call":
        bad["functions"][0]["signature"] = "def count_rows(rows=make_data()):"
    else:
        bad["functions"][0]["signature"] = "def count_rows(rows: make_data()):"
    _, calls = await author([bad, PLAN, COUNT, MAIN])
    assert [call["authoring_phase"] for call in calls] == ["plan", "plan", "unit", "unit"]
    assert calls[1]["repair_feedback"]["reason"] == "invalid_schema"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"unit_id": "U999", "content": COUNT["content"]},
    {"unit_id": "U001", "content": "def count_rows(other):\n    return len(other)"},
    {"unit_id": "U001", "content": COUNT["content"] + "\nprint('extra top level')"},
    {"unit_id": "U001", "content": COUNT["content"] + "\ndef count_rows(rows):\n    return 999"},
    {"unit_id": "U001", "content": "@decorate\n" + COUNT["content"]},
    {"unit_id": "U001", "content": "def count_rows(rows):\n    import os\n    return len(rows)"},
    {"unit_id": "U001", "content": "def count_rows(rows):\n    return missing_compute(rows)"},
    {"unit_id": "U001", "content": "def count_rows(rows):\n    return main()"},
    {"unit_id": "U001", "content": "def count_rows(rows):\n    return f'未完成 {len(rows)'"},
])
async def test_invalid_function_cannot_change_frozen_interface_or_add_top_level_code(bad):
    replacement = COUNT
    try:
        ast.parse(bad["content"])
    except SyntaxError:
        replacement = {"repairs": [{"block_id": "B001", "replacement": COUNT["content"]}]}
    script, calls = await author([PLAN, bad, replacement, MAIN])
    assert [call.get("function_request", {}).get("unit_id") for call in calls] == [None, "U001", "U001", "U002"]
    assert calls[2]["repair_feedback"]["reason"] == "script_syntax_invalid"
    assert "return 999" not in script.content


@pytest.mark.asyncio
async def test_private_unit_is_rejected_and_redacted_before_repair():
    bad = {**COUNT, "content": "def count_rows(rows):\n    label = '" + PRIVATE + "'\n    return len(rows)"}
    script, calls = await author([PLAN, bad,
        {"repairs": [{"block_id": "B001", "replacement": ""}]}, MAIN])
    assert calls[2]["repair_feedback"]["reason"] == "private_package_content"
    assert PRIVATE not in json.dumps(calls) and PRIVATE not in script.content
    assert calls[2]["schema"]["title"] == "SourceBlockRepairs"
    assert COUNT["content"] in script.content


@pytest.mark.asyncio
async def test_private_manifest_is_repaired_before_creating_functions():
    bad = copy.deepcopy(PLAN)
    bad["functions"][0]["purpose"] += " for " + PRIVATE
    _, calls = await author([bad, PLAN, COUNT, MAIN])
    assert [call["authoring_phase"] for call in calls] == ["plan", "plan", "unit", "unit"]
    assert calls[1]["repair_feedback"]["reason"] == "private_package_content"
    assert PRIVATE not in json.dumps(calls)


@pytest.mark.asyncio
async def test_manifest_cannot_silently_drop_a_required_method():
    contract = copy.deepcopy(CONTRACT)
    contract["requirements"].append({**contract["requirements"][0], "id": "R002"})
    request = {**REQUEST, "requirement_ids": ["R001", "R002"]}
    gateway, calls, _ = make_gateway([PLAN, PLAN])
    with pytest.raises(SkillCreationError) as failure:
        await generate_modular_script(gateway, HelperScript, "implementation", "Keep both methods.",
            {"script_request": request}, None, contract)
    assert failure.value.code == "invalid_schema"
    assert all(call["authoring_phase"] == "plan" for call in requests(calls))
    assert requests(calls)[1]["repair_feedback"]["details"]["authoring_issue"] == "required_method_coverage_missing"


@pytest.mark.asyncio
async def test_complete_assembly_repairs_only_main_and_rechecks_method_coverage():
    incomplete = {**MAIN, "content": MAIN["content"].replace("str(count_rows(rows))", "str(len(rows))")}
    def check_count_method(candidate):
        if "str(count_rows(rows))" not in candidate.content:
            raise CoverageError("method_identifiers_lost")
    check = Mock(side_effect=check_count_method)
    script, calls = await author([PLAN, COUNT, incomplete, {"unit_ids": ["U002"]}, MAIN], check=check)
    assert [call["authoring_phase"] for call in calls] == ["plan", "unit", "unit", "repair_plan", "unit"]
    assert [call.get("function_request", {}).get("unit_id") for call in calls].count("U001") == 1
    assert calls[-1]["assembly_repair"] is True
    assert calls[-1]["manifest"] == PLAN
    assert calls[-1]["assembly_diagnostic"]["reason"] == "method_identifiers_lost"
    assert MAIN["content"].strip() in script.content
    assert COUNT["content"] in script.content
    assert check.call_count == 2
    check.assert_called_with(script)


@pytest.mark.asyncio
async def test_second_failed_assembly_stops_after_exactly_one_local_repair_cycle():
    gateway, calls, _ = make_gateway([PLAN, COUNT, MAIN, {"unit_ids": ["U002"]}, MAIN])
    check = Mock(side_effect=CoverageError("method_identifiers_lost"))
    with pytest.raises(CoverageError) as failure:
        await generate_modular_script(gateway, HelperScript, "implementation", "Preserve method.",
            {"script_request": REQUEST}, check, CONTRACT)
    assert failure.value.code == "method_identifiers_lost"
    assert [call["authoring_phase"] for call in requests(calls)].count("repair_plan") == 1
    assert len(calls) == 5
    assert check.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("unknown host defect"), CoverageError("method_review_rejected"), CoverageError("unregistered_assembly_error")])
async def test_unknown_error_or_scientific_rejection_cannot_trigger_local_repair(failure):
    gateway, calls, _ = make_gateway([PLAN, COUNT, MAIN])
    with pytest.raises(type(failure)) as caught:
        await generate_modular_script(gateway, HelperScript, "implementation", "Preserve method.",
            {"script_request": REQUEST}, Mock(side_effect=failure), CONTRACT)
    assert caught.value is failure
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [{"unit_ids": ["U999"]}, {"unit_ids": ["U002", "U002"]}, {"unit_ids": []}])
async def test_local_repair_selection_cannot_add_duplicate_or_omit_function_ids(selection):
    check = Mock(side_effect=[CoverageError("method_identifiers_lost"), None])
    _, calls = await author([PLAN, COUNT, MAIN, selection, {"unit_ids": ["U002"]}, MAIN], check=check)
    assert [call["authoring_phase"] for call in calls] == ["plan", "unit", "unit", "repair_plan", "repair_plan", "unit"]
    assert calls[4]["repair_feedback"]["reason"] == "invalid_schema"
    assert check.call_count == 2


@pytest.mark.asyncio
async def test_known_coverage_failure_can_repair_one_function_with_only_redacted_diagnostics():
    failure = CoverageError("method_identifiers_lost", details={"missing_anchors": [{"identifier": PRIVATE, "locations": ["/steps/0/action"]}]})
    check = Mock(side_effect=[failure, None])
    repaired = {**COUNT, "content": "def count_rows(rows):\n    return len(list(rows))"}
    script, calls = await author([PLAN, COUNT, MAIN, {"unit_ids": ["U001"]}, repaired], check=check)
    assert check.call_count == 2
    assert calls[3]["assembly_diagnostic"]["reason"] == "method_identifiers_lost"
    assert PRIVATE not in json.dumps(calls)
    assert repaired["content"] in script.content and MAIN["content"].strip() in script.content
    assert [call.get("function_request", {}).get("unit_id") for call in calls].count("U002") == 1


@pytest.mark.asyncio
async def test_synthetic_repair_preserves_original_metadata_and_frozen_oracle():
    frozen = {"scripts": [{"script_path": REQUEST["path"], "cases": [{"requirement_ids": ["R001"], "expected_exit_code": 0, "assertions": [{"expected": 2}]}]}]}
    original = {**{key: REQUEST[key] for key in ("path", "purpose", "invocation")}, "content": "# Previous complete implementation remains reference only."}
    payload = {"script": original, "script_request": REQUEST, "method_contract": CONTRACT,
               "frozen_validation_plan": frozen, "observed_validation": {"status": "failed"}}
    before = copy.deepcopy(payload)
    check = Mock(side_effect=[CoverageError("method_identifiers_lost"), None])
    script, calls = await author([PLAN, COUNT, MAIN, {"unit_ids": ["U002"]}, MAIN],
                                payload=payload, stage="synthetic_script_repair", check=check)
    assert payload == before
    assert script.model_dump(exclude={"content"}) == {key: original[key] for key in ("path", "purpose", "invocation")}
    assert all(call["frozen_validation_plan"] == frozen for call in calls)
    assert all(call["observed_validation"] == {"status": "failed"} for call in calls)
    assert check.call_count == 2
    assert [call["authoring_phase"] for call in calls] == ["plan", "unit", "unit", "repair_plan", "unit"]


@pytest.mark.asyncio
async def test_total_long_code_is_preserved_without_arbitrary_length_truncation():
    # A deterministic large function is sufficient to show the assembly never
    # truncates successful complete units or silently drops scientific clauses.
    lines = ["    tally = len(rows)", *[f"    tally = tally + 0  # retained computation clause {index}" for index in range(900)], "    return tally"]
    long_count = {"unit_id": "U001", "content": "def count_rows(rows):\n" + "\n".join(lines)}
    script, calls = await author([PLAN, long_count, MAIN])
    assert len(script.content) > 50000
    assert long_count["content"] in script.content
    assert "retained computation clause 899" in script.content
    assert len(calls) == 3
