"""The generation schema distinguishes files from paths inside JSON documents."""
import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_validation import (
    JSONArrayLengthAssertion, JSONValueAssertion, validation_generation_schema,
)


@pytest.mark.parametrize("definition", ["JSONValueAssertion", "JSONArrayLengthAssertion"])
def test_both_json_assertions_publish_distinct_file_and_pointer_contracts(definition):
    fields = validation_generation_schema()["$defs"][definition]["properties"]
    path = fields["path"]["description"]
    pointer = fields["pointer"]["description"]
    assert "Relative output file path" in path and "report.json" in path
    assert "Do not start with /" in path and "not a JSON field path" in path
    assert "pointer instead" in path
    assert "RFC 6901" in pointer and "/paired/1/n" in pointer
    assert "zero-based" in pointer and "empty string" in pointer
    assert "~0" in pointer and "~1" in pointer
    assert "not a filesystem path" in pointer
    assert fields["path"]["type"] == fields["pointer"]["type"] == "string"


def test_other_generation_paths_explicitly_name_their_respective_file_role():
    definitions = validation_generation_schema()["$defs"]
    for name, suffix in [("SyntheticCSVInput", "csv"), ("SyntheticJSONInput", "json")]:
        description = definitions[name]["properties"]["path"]["description"]
        assert "Synthetic input filename" in description
        assert "single filename without directories" in description
        assert "{input:input." + suffix + "}" in description
    output = definitions["CSVRowsAssertion"]["properties"]["path"]["description"]
    assert "Relative output file path" in output and "wide.csv" in output
    assert "match and fields" in output
    script = definitions["ScriptValidationPlan"]["properties"]["script_path"]["description"]
    assert "Exact declared helper script path" in script and "scripts/analyze.py" in script


@pytest.mark.parametrize("kind,model,extra", [
    ("json_value", JSONValueAssertion, {"expected": {"value": 2, "atol": 0, "rtol": 0}}),
    ("json_array_length", JSONArrayLengthAssertion, {"length": 2}),
])
def test_schema_guidance_does_not_rewrite_paths_pointers_or_scientific_expectations(kind, model, extra):
    document = {"type": kind, "path": "results/report.json", "pointer": "/paired/1/n", **extra}
    value = model.model_validate(document)
    assert value.model_dump() == document
    assert model.model_validate({**document, "pointer": ""}).pointer == ""
    assert model.model_validate({**document, "pointer": "/field~1name/~0value"}).pointer == "/field~1name/~0value"
    with pytest.raises(ValidationError):
        model.model_validate({**document, "path": "/paired/1/n"})
    with pytest.raises(ValidationError):
        model.model_validate({**document, "pointer": "paired/1/n"})
