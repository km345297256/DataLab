"""Synthetic regressions for repeated filename checks; no live model calls."""
import json

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.source_repair import build_source_repair_spec
from app.domain.services.skills.skill_creation.source_response import SourceResponseAdapter, SourceResponseError
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from test_skill_creation_gateway import make_gateway, requests


REQUEST = {"path": "scripts/analyze.py", "purpose": "Analyze the current selected input.",
           "invocation": "python scripts/analyze.py --input current --output results"}


def test_syntax_block_preserves_output_role_before_privacy_validation():
    old_output, old_input, other_output = "old-result.csv", "old-source.csv", "other-result.json"
    privacy = PrivacyContext(tuple(PrivateValue(name, "source_filename")
        for name in (old_output, old_input, other_output)),
        generated_output_filenames=frozenset({old_output, other_output}))
    outside_alias = privacy.redact_text(other_output)
    source = (f"OTHER = '{other_output}'\n\n# fixed public context\n\n"
              f"INPUT = '{old_input}'\nOUTPUT = '{old_output}'\n"
              "if True print('syntax problem')\n"
              f"# A literal {outside_alias} is not provenance for this block.\n"
              "value = 7\n# fixed suffix\n")
    document = {**REQUEST, "content": source}
    adapter = SourceResponseAdapter(HelperScript, REQUEST, "script")
    with pytest.raises(SourceResponseError) as caught:
        adapter.validate_document(document)
    spec = build_source_repair_spec(document, adapter, privacy, caught.value.code, caught.value.details)
    blocks = spec.public_blocks(privacy)
    assert len(blocks) == 1
    assert blocks[0]["output_repair_references"] == [
        {"reference": privacy.redact_text(old_output), "role": "generated_output"}]
    assert privacy.redact_text(old_input) in blocks[0]["source"]
    assert outside_alias in blocks[0]["source"]  # Never inferred from a model-authored alias.
    public = json.dumps(blocks)
    assert all(name not in public for name in (old_output, old_input, other_output))
    assert not privacy.scan_document(blocks)
    assert document["content"] == source


@pytest.mark.parametrize("category", ["identity", "credential", "host_path", "url"])
def test_other_sensitive_meanings_do_not_gain_output_rename_guidance(category):
    name = "historical-result.csv"
    privacy = PrivacyContext((PrivateValue(name, "source_filename"), PrivateValue(name, category)),
                             generated_output_filenames=frozenset({name}))
    document = {"content": f"OUTPUT = '{name}'"}
    issues = privacy.scan_document(document)
    assert issues and not any(issue.role for issue in issues)
    assert not privacy.output_repair_references(document)
    with pytest.raises(SkillPackageValidationError) as caught:
        validate_portable_document(document, privacy)
    assert caught.value.code == "private_package_content"


def test_opaque_reference_colliding_with_known_sensitive_value_is_not_exposed():
    name = "historical-result.csv"
    privacy = PrivacyContext((PrivateValue(name, "source_filename"),
                              PrivateValue("{input_file_1}", "credential")),
                             generated_output_filenames=frozenset({name}))
    document = {**REQUEST, "content": f"OUTPUT = '{name}'\nvalue = 3\n"}
    spec = build_source_repair_spec(document, SourceResponseAdapter(HelperScript, REQUEST, "script"),
                                    privacy, "private_package_content")
    assert not privacy.output_repair_references(document)
    blocks = spec.public_blocks(privacy)
    assert all("output_repair_references" not in block for block in blocks)
    assert not privacy.scan_document(blocks)


@pytest.mark.parametrize("category", ["credential", "host_path", "url", "identity"])
def test_filename_inside_other_private_span_cannot_bind_a_model_authored_alias(category):
    name = "historical-result.csv"
    private = "secret-prefix-" + name
    privacy = PrivacyContext((PrivateValue(name, "source_filename"), PrivateValue(private, category)),
                             generated_output_filenames=frozenset({name}))
    alias = privacy._file_aliases()[name]
    document = {**REQUEST, "content": f"VALUE={private!r}; unrelated={alias!r}\n"}
    spec = build_source_repair_spec(document, SourceResponseAdapter(HelperScript, REQUEST, "script"),
                                    privacy, "private_package_content")
    blocks = spec.public_blocks(privacy)
    assert alias in blocks[0]["source"]
    assert "output_repair_references" not in blocks[0]
    assert not privacy.output_repair_references(document)
    assert not any(issue.role for issue in privacy.scan_document(document))
    assert private not in json.dumps(blocks) and not privacy.scan_document(blocks)


def test_unrelated_private_span_does_not_hide_a_real_output_role():
    name = "historical-result.csv"
    privacy = PrivacyContext((PrivateValue(name, "source_filename"), PrivateValue("sensitive-token", "credential")),
                             generated_output_filenames=frozenset({name}))
    document = {**REQUEST, "content": f"OUTPUT={name!r}; unrelated='sensitive-token'\n"}
    spec = build_source_repair_spec(document, SourceResponseAdapter(HelperScript, REQUEST, "script"),
                                    privacy, "private_package_content")
    assert spec.public_blocks(privacy)[0]["output_repair_references"] == [
        {"reference": privacy.redact_text(name), "role": "generated_output"}]
    assert len(privacy.output_repair_references(document)) == 1


@pytest.mark.asyncio
async def test_six_repeated_filename_locations_repair_locally_with_existing_output_contract():
    positions = (129, 133, 137, 139, 144, 145)
    old_names = tuple(f"historical-result-{index}.csv" for index in range(6))
    output_names = tuple(f"summary-{index}.csv" for index in range(6))
    privacy = PrivacyContext(tuple(PrivateValue(name, "source_filename") for name in old_names),
                             generated_output_filenames=frozenset(old_names))
    lines = [f"# preserve scientific context line {index}" for index in range(1, 151)]
    lines[0:2] = ["def calculate(values):", "    return sum(values) / len(values)"]
    for index, line in enumerate(positions):
        lines[line - 1] = f"OUTPUT_{index} = {old_names[index]!r}"
    source = "\n".join(lines)
    mapping = {privacy.redact_text(old): new for old, new in zip(old_names, output_names)}
    observed = []

    def response(row):
        blocks = row["repair_feedback"]["source_blocks"]
        observed.extend(blocks)
        assert {name["reference"] for block in blocks
                for name in block["output_repair_references"]} == set(mapping)
        repaired = []
        for block in blocks:
            replacement = block["source"]
            for reference in block["output_repair_references"]:
                replacement = replacement.replace(reference["reference"], json.dumps(mapping[reference["reference"]]))
            repaired.append({"block_id": block["block_id"], "replacement": replacement})
        return AIMessage(content=json.dumps({"repairs": repaired}))

    gateway, calls, _ = make_gateway([], privacy=privacy)
    async def invoke(messages, **kwargs):
        calls.append(messages)
        return AIMessage(content=source) if len(calls) == 1 else response(json.loads(messages[-1].content))
    gateway.model.ainvoke = invoke
    checked = []
    def check(value):
        checked.append(value.content)
        validate_portable_document(value, privacy)
        assert "return sum(values) / len(values)" in value.content
    result = await gateway.generate(HelperScript, "implementation", "Keep all methods and output declarations.",
        {"script_request": REQUEST, "workflow": {"outputs": [{"name": name} for name in output_names]}}, check)
    expected = source
    for old, new in zip(old_names, output_names):
        expected = expected.replace(repr(old), json.dumps(new))
    assert result.content == expected
    assert len(calls) == 2 and len(checked) == 2
    assert sum(len(block["source"]) for block in observed) < len(source) / 10
    assert all(old not in json.dumps(requests(calls)) for old in old_names)
    assert not privacy.scan_document(result)
