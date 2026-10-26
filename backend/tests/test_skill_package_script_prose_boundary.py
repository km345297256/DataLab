"""Python source is not Markdown; privacy and syntactic validity still apply."""
import json

import pytest

from app.domain.services.skills.skill_creation.models import SkillRecipe
from app.domain.services.skills.skill_creation.package import build_skill_package, validate_recipe
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.script_policy import SkillScriptValidationError
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from test_session_skill_creator import recipe_payload
from test_skill_creation_script_policy import script


SOURCE_BODIES = [
    "handlers = {'count': len}\nvalues = [1, 2]\nresult = handlers['count'](values)\n"
    "Path(args.output).write_text(str(result))",
    "values = [1, 2]\n(Path(args.output)).write_text(str(len(values)))",
]


def recipe_for(body):
    helper = script(body)
    document = recipe_payload()
    document["scripts"] = [{"path": helper.path, "content": helper.content,
        "invocation": helper.invocation, "purpose": "Compute a portable summary from the selected input."}]
    return SkillRecipe.model_validate(document)


@pytest.mark.parametrize("body", SOURCE_BODIES)
def test_index_call_or_cross_line_output_source_remains_byte_identical(body):
    recipe = recipe_for(body)
    original_document = recipe.model_dump(mode="json")
    original_bytes = recipe.scripts[0].content.encode("utf-8")
    validate_recipe(recipe)
    assert recipe.model_dump(mode="json") == original_document
    _, references, scripts = build_skill_package(recipe)
    assert scripts[recipe.scripts[0].path].encode("utf-8") == original_bytes
    assert "content" not in json.loads(references["references/workflow.json"])["scripts"][0]


@pytest.mark.parametrize("pattern", ["handlers[key](value)", "values = [1, 2]\n(output_dir).write_text(value)"])
@pytest.mark.parametrize("location", ["goal", "purpose", "step", "expected"])
def test_only_source_content_is_excluded_from_prose_link_validation(pattern, location):
    recipe = recipe_for(SOURCE_BODIES[0])
    prose = "Review the declared calculation before use: " + pattern
    if location == "goal":
        recipe.goal = prose
    elif location == "purpose":
        recipe.scripts[0].purpose = prose
    elif location == "step":
        recipe.steps[0].action = prose
    else:
        recipe.validation_cases[0].expected = prose
    with pytest.raises(SkillPackageValidationError) as caught:
        validate_recipe(recipe)
    assert caught.value.code == "unverified_document_link"


@pytest.mark.parametrize("category,private", [
    ("source_filename", "selected-private-cohort.csv"),
    ("identity", "private-owner-fixture"),
    ("credential", "synthetic-secret-fixture"),
    ("host_path", "/private/fixture/input.csv"),
])
def test_source_still_passes_the_complete_private_content_check(category, private):
    recipe = recipe_for(SOURCE_BODIES[0] + "\n# " + private)
    privacy = PrivacyContext((PrivateValue(private, category),))
    with pytest.raises(SkillPackageValidationError) as caught:
        validate_recipe(recipe, privacy)
    assert caught.value.code == "private_package_content"
    assert any(item["category"] == category for item in caught.value.details["privacy_issues"])
    assert private not in json.dumps(caught.value.details)


def test_source_link_exclusion_cannot_bypass_python_syntax_validation():
    recipe = recipe_for(SOURCE_BODIES[0] + "\nif True:")
    with pytest.raises(SkillScriptValidationError) as caught:
        validate_recipe(recipe)
    assert caught.value.code == "script_syntax_invalid"


def test_package_static_admission_does_not_assert_runtime_input_write_safety():
    recipe = recipe_for(SOURCE_BODIES[0] + "\nPath(args.input).write_text('changed')")
    original = recipe.scripts[0].content
    validate_recipe(recipe)
    assert recipe.scripts[0].content == original


@pytest.mark.parametrize("operation", ["eval('1 + 2')", "Path(args.output).unlink()",
    "import os\nos.system('unused command')"])
def test_saved_script_does_not_reintroduce_an_api_name_denylist(operation):
    recipe = recipe_for(SOURCE_BODIES[0] + "\n" + operation)
    validate_recipe(recipe)
