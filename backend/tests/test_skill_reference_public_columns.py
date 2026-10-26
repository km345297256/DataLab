"""A reference cannot redefine proven public column metadata as an answer."""
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.script_public_interface import public_script_interface
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.script_policy import validate_script
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_reference_verification import (
    _validate_reference_column_contract, verify_reference_plan,
)
from app.domain.services.skills.skill_validation import ExpectedValue, JSONValueAssertion
from test_skill_creation_gateway import make_gateway, requests
from test_skill_creation_source_response import METADATA
from test_skill_reference_verification import host_receipt, method_review, output_files
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import typed


def source(*, columns="['id', 'mean']", report_columns="columns", report_name="status.json"):
    return f'''import argparse
import csv
import json
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if Path(args.input).resolve() == Path(args.output).resolve():
        raise ValueError('Input must remain read only')
    columns = {columns}
    with (Path(args.output) / 'summary.csv').open('w') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
    report = {{'status': 'ok', 'columns': {report_columns}, 'computed_answer': 314159.265,
              'arbitrary_values': ['PRIVATE_RESULT_VALUE']}}
    (Path(args.output) / {report_name!r}).write_text(json.dumps(report))

if __name__ == '__main__':
    main()
'''


def declared():
    value = interfaces()[0]
    value.update(public_script_interface(source()))
    return value


def test_reference_preserves_proven_column_metadata_without_exposing_arbitrary_array_values():
    interface = declared()
    projected = json.dumps(interface)
    assert 'column_name_prefix' in projected
    assert 'PRIVATE_RESULT_VALUE' not in projected and '314159.265' not in projected
    _validate_reference_column_contract(source(), interface)


@pytest.mark.parametrize("candidate", [
    source(columns="['mean']"),
    source(columns="['mean', 'id']"),
], ids=["missing-column", "reordered-columns"])
def test_proven_conflicting_reference_column_sequence_fails_static_boundary(candidate):
    with pytest.raises(SkillPackageValidationError) as failure:
        _validate_reference_column_contract(candidate, declared())
    assert failure.value.code == "reference_output_interface_unconfirmed"
    assert failure.value.details == {"output": "@output/status.json", "pointer": "/columns",
                                     "reason": "declared_column_prefix_conflict"}


@pytest.mark.parametrize("candidate", [source(report_columns="[name for name in columns]"),
                                       source(report_name="other.json"), source(columns="['id']")])
def test_unproven_prefix_is_pending_runtime_observation(candidate):
    assert _validate_reference_column_contract(candidate, declared()) is False


def test_additional_columns_do_not_contradict_a_declared_prefix():
    assert _validate_reference_column_contract(source(columns="['id', 'mean', 'extra']"), declared()) is True


def test_nested_column_metadata_requires_proof_at_the_same_array_item_location():
    nested = source(report_columns="columns").replace("'columns': columns", "'tables': [{'columns': columns}]")
    interface = {**interfaces()[0], **public_script_interface(nested)}
    _validate_reference_column_contract(nested, interface)
    with pytest.raises(SkillPackageValidationError) as failure:
        _validate_reference_column_contract(source(), interface)
    assert failure.value.details["pointer"] == "/tables/*/columns"


def test_one_matching_output_write_cannot_hide_a_later_incompatible_write():
    first = source()
    incompatible = source(columns="['mean']").replace("'summary.csv'", "'other.csv'")
    # Both independent writers target the same report location. A matching
    # declaration from one writer must not mask the other's missing proof.
    combined = first.replace("def main():", "def first():").replace("    main()", "    first()") + incompatible
    with pytest.raises(SkillPackageValidationError):
        _validate_reference_column_contract(combined, declared())


@pytest.mark.asyncio
async def test_real_gateway_repairs_proven_reference_column_conflict_with_structural_feedback():
    invalid = source(columns="['mean', 'id']")
    valid = source()
    gateway, calls, _ = make_gateway([AIMessage(content=invalid), AIMessage(content=valid)])
    interface = declared()
    def check(value):
        validate_script(value)
        _validate_reference_column_contract(value.content, interface)
    result = await gateway.generate(HelperScript, "synthetic_reference_implementation", "Preserve public column metadata.",
        {"script_request": METADATA, "script_interfaces": [interface]}, check)
    assert result.content == valid.strip()
    observed = requests(calls)
    assert len(observed) == 2
    feedback = observed[1]["repair_feedback"]
    assert feedback["reason"] == "reference_output_interface_unconfirmed"
    assert feedback["fields"] == ["content"]
    assert "proven conflict" in feedback["hint"]
    assert feedback["details"]["pointer"] == "/columns"


@pytest.mark.asyncio
async def test_unknown_projection_does_not_trigger_a_model_rewrite_for_a_specific_code_form():
    unknown = source(report_columns="[name for name in columns]")
    gateway, calls, _ = make_gateway([AIMessage(content=unknown)])
    result = await gateway.generate(HelperScript, "synthetic_reference_implementation", "Preserve public column metadata.",
        {"script_request": METADATA, "script_interfaces": [declared()]},
        lambda value: _validate_reference_column_contract(value.content, declared()))
    assert result.content == unknown.strip() and len(calls) == 1


@pytest.mark.asyncio
async def test_reference_runtime_cannot_correct_a_fixed_public_column_to_another_name():
    plan = typed()
    plan.scripts[0].cases[0].assertions.append(JSONValueAssertion(
        type="json_value", path="status.json", pointer="/columns/0", expected=ExpectedValue(value="id")))
    original = plan.model_dump(mode="json")
    async def generate(schema, stage, instruction, payload, check):
        document = ({**payload["script_request"], "content": source()} if stage == "synthetic_reference_implementation"
                    else method_review(payload))
        value = schema.model_validate(document)
        check(value)
        return value
    def files(case):
        values = output_files(case)
        values["status.json"] = json.dumps({"status": "ok", "columns": ["mean"]}).encode()
        return values
    # A missing independent interface receipt cannot authorize reading the
    # reference's mismatching column as a scientific expectation correction.
    execute = AsyncMock(side_effect=lambda programs, candidate, **kwargs: host_receipt(programs, candidate, files=files))
    with pytest.raises(SkillCreationError) as failure:
        await verify_reference_plan(plan, method_contract(), sources(), [declared()], generate=generate, execute=execute)
    assert (failure.value.stage, failure.value.code) == ("synthetic_reference_execution", "reference_calculation_unconfirmed")
    from app.domain.services.skills.skill_public_output_contract import PublicOutputContractError
    assert isinstance(failure.value.__cause__, PublicOutputContractError)
    assert plan.model_dump(mode="json") == original
    execute.assert_awaited_once()
