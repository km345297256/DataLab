"""Historical output provenance improves guidance, never grants an exemption."""
import json

import pytest

from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, generation_privacy
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError


def context():
    return PrivacyContext((PrivateValue("old-input.csv", "source_filename"),
                           PrivateValue("old-table.csv", "source_filename"),
                           PrivateValue("old-metadata.json", "source_filename")),
                          generated_output_filenames=frozenset({"old-table.csv", "old-metadata.json"}))


@pytest.mark.parametrize("stage", ["implementation", "synthetic_script_repair", "synthetic_build_repair",
                                   "synthetic_method_repair", "synthetic_reference_implementation", "unknown"])
def test_output_role_survives_stage_selection_without_pointer_permissions(stage):
    base = context()
    scoped = generation_privacy(PrivacyContext(base.entries, frozenset({"/checks/value"}), True,
        base.generated_output_filenames), HelperScript, stage, {})
    assert scoped.generated_output_filenames == base.generated_output_filenames
    assert not scoped.review_json_pointers and not scoped.coverage_amendment_pointers
    value = {"content": "ARTIFACTS = {'table': 'old-table.csv'}"}
    issues = scoped.scan_document(value)
    assert issues and all(item.role == "generated_output" for item in issues)
    assert all("not an analysis input" in item.hint for item in issues)
    with pytest.raises(SkillPackageValidationError) as error:
        validate_portable_document(value, scoped)
    assert error.value.code == "private_package_content"


@pytest.mark.parametrize("source", [
    "ARTIFACTS = {'table': 'old-table.csv'}",
    "ARTIFACTS = {'table': u'old-table.csv'}",
    r"ARTIFACTS = {'table': 'old\x2dtable.csv'}",
    "ARTIFACTS = {'table': ('old-'\n # a comment\n 'table.csv')}",
    "# historical generated output old-table.csv\nvalue = 1",
])
def test_plain_encoded_concatenated_and_commented_outputs_have_private_safe_guidance(source):
    privacy = context()
    value = {"content": source}
    issues = privacy.scan_document(value)
    assert any(issue.role == "generated_output" for issue in issues)
    references = privacy.output_repair_references(value)
    assert len(references) == 1
    assert references[0]["reference"] == privacy.redact_text("old-table.csv")
    assert references[0]["suggested_portable_name"].endswith(".csv")
    public = json.dumps([issue.as_dict() for issue in issues] + references)
    assert "old-table.csv" not in public and not privacy.scan_document(public)
    assert value["content"] == source  # author decides; no source rewrite
    assert not privacy.scan_document(privacy.redact_document(value))
    assert references[0]["reference"] in privacy.redact_document(value)["content"]


@pytest.mark.parametrize("value", [
    {"content": "ARTIFACTS = {'table': 'old-input.csv'}"},
    {"description": "This generated output old-input.csv is called a result."},
])
def test_model_output_words_and_variable_names_cannot_reclassify_input(value):
    privacy = context()
    assert privacy.scan_document(value)
    assert not any(issue.role for issue in privacy.scan_document(value))
    assert not privacy.output_repair_references(value)


def test_unproven_filename_remains_unclassified_and_blocked():
    privacy = PrivacyContext(context().entries)
    value = {"content": "ARTIFACTS = {'table': 'old-table.csv'}"}
    assert privacy.scan_document(value)
    assert not any(issue.role for issue in privacy.scan_document(value))
    assert not privacy.output_repair_references(value)


def test_proposals_avoid_candidate_and_known_private_names_and_preserve_format():
    base = context()
    privacy = PrivacyContext((*base.entries, PrivateValue("artifact_002.csv", "source_filename")),
                             generated_output_filenames=base.generated_output_filenames)
    value = {"content": "a='old-table.csv'\nb='old-metadata.json'\nc='ARTIFACT_001.CSV'"}
    proposals = privacy.output_repair_references(value)
    assert len(proposals) == 2
    table = next(item for item in proposals if item["reference"] == privacy.redact_text("old-table.csv"))
    assert table["suggested_portable_name"] == "artifact_003.csv"
    assert all(not privacy.scan_document(item["suggested_portable_name"]) for item in proposals)


def test_multiple_occurrences_use_one_binding_without_exposing_private_values():
    privacy = context()
    value = {"content": "first='old-table.csv'\nsecond='old-table.csv'\nthird='old-table.csv'"}
    assert len(privacy.output_repair_references(value)) == 1
    assert privacy.redact_document(value)["content"].count(privacy.redact_text("old-table.csv")) == 3


@pytest.mark.parametrize("occupied", [
    {"content": r"other = 'artifact_\x30\x30\x31.csv'"},
    {"content": "other = ('artifact_' '001.csv')"},
    {"content": "other = ('artifact_'\n # preserve this product\n '001.csv')"},
    {"content": r"other = {'artifact_\x30\x30\x31.csv': 'existing'}"},
    {"outputs": {"artifact_001.csv": "existing"}},
])
def test_proposals_avoid_decoded_python_names_and_mapping_keys(occupied):
    privacy = context()
    document = {"new_source": {"content": "new = 'old-table.csv'"}, "already_authored": occupied}
    references = privacy.output_repair_references(document)
    assert len(references) == 1
    assert references[0]["suggested_portable_name"] == "artifact_002.csv"


def test_decoded_occupied_names_contribute_to_collision_search_extent():
    privacy = context()
    encoded = "\n".join("other_" + str(index) + " = " + repr("artifact_")[0:-1] +
                        "".join("\\x" + format(ord(c), "02x") for c in f"{index:03}.csv") + "'"
                        for index in range(1, 31))
    references = privacy.output_repair_references({"content": encoded + "\nnew = 'old-table.csv'"})
    assert references[0]["suggested_portable_name"] == "artifact_031.csv"


def test_nonfilename_private_categories_cannot_receive_output_role():
    private = "sk-privatecredential123456"
    privacy = PrivacyContext((PrivateValue(private, "credential"),),
                             generated_output_filenames=frozenset({private}))
    issues = privacy.scan_document({"content": "value=" + repr(private)})
    assert issues and not any(issue.role for issue in issues)
    assert not privacy.output_repair_references({"content": "value=" + repr(private)})


def test_source_models_preserve_whitespace_only_for_content():
    from app.domain.services.skills.skill_creation.script_authoring import ScriptFunctionUnit
    code = "\r\n# independent caller supplied source whitespace\r\ndef run():\r\n    return 1\r\n\r\n"
    script = HelperScript(path=" scripts/run.py ", purpose=" current input analysis ",
                          invocation=" python scripts/run.py --input current --output result ", content=code)
    assert script.content.encode() == code.encode()
    assert script.path == "scripts/run.py"
    assert ScriptFunctionUnit(unit_id=" U001 ", content=code).content.encode() == code.encode()
