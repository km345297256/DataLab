"""An implementation revision is an atomic edit of exact original source."""
from copy import deepcopy
import json
import traceback

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_creation.implementation_revision import (
    ImplementationEdit, ImplementationEditError, ImplementationEdits,
    apply_implementation_edits,
)
from app.domain.services.skills.skill_creation.models import HelperScript, SkillRecipe
from app.domain.services.skills.skill_creation.script_policy import (
    SkillScriptValidationError, validate_script,
)


FIRST = '# Keep original whitespace.\r\n\r\ndef first():\r\n    return "α"\r\n\r\n# trailing  \r\n'
SECOND = '# An independently declared helper.\ndef second():\n    return "β"\n'


@pytest.fixture
def recipe():
    return SkillRecipe(
        name="source-transaction", description="Perform a reusable computation using the current selected input.",
        triggers=["Compute selected input", "Check computation"], goal="Produce the declared result from current input.",
        applicability=["The current input provides the declared measurements."],
        parameters=[{"name": "input_path", "description": "The current selected read-only input file.",
                     "value_type": "path", "required": True, "default": None,
                     "constraints": "Never alter the selected input."}],
        data_requirements=["Read the declared measurements and units."], dependencies=["Python runtime is available."],
        steps=[{"action": "Validate all selected input measurements.", "decision": "Reject invalid measurements.",
                "verification": "Check declared validity conditions."},
               {"action": "Compute and export the declared result.", "decision": "Stop if computation is undefined.",
                "verification": "Check all declared output conditions."}],
        outputs=[{"name": "Result document", "format": "JSON", "content": "The complete computed result.",
                  "acceptance": "All declared result fields are present."}],
        validation_cases=[{"kind": kind, "input": f"Artificial {kind} measurements.",
                           "expected": "The declared behavior is observed."}
                          for kind in ("normal", "boundary", "invalid")],
        limitations=["Only the original declared method is supported."],
        scripts=[HelperScript(path=f"scripts/{name}.py", purpose="Perform the declared reusable computation.",
                              invocation=f"python scripts/{name}.py --input current.json --output result.json", content=source)
                 for name, source in (("first", FIRST), ("second", SECOND))],
        coverage=[{"requirement_id": "R001", "locations": ["/steps/0", "/scripts/0"]}],
        implementation={"kind": "parameterized_scripts", "reason": "Execute the original reusable calculations.",
                        "scripts": [{"path": f"scripts/{name}.py", "purpose": "Perform the declared reusable computation.",
                                     "invocation": f"python scripts/{name}.py --input current.json --output result.json",
                                     "requirement_ids": ["R001"], "implementation_source_ids": ["P001"]}
                                    for name in ("first", "second")]},
    )


def change(before, after, path="scripts/first.py"):
    return {"script_path": path, "before": before, "after": after}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def test_simultaneous_multiscript_edits_preserve_every_unselected_value_and_byte(recipe):
    original = recipe.model_dump(mode="json")
    payload = {"edits": [change('return "β"', 'return "δ"', "scripts/second.py"),
                         change('# trailing  \r\n', ''),
                         change('return "α"', 'return "γ"')]}
    old_payload = deepcopy(payload)
    revised = apply_implementation_edits(recipe, payload)
    expected = deepcopy(original)
    expected["scripts"][0]["content"] = FIRST.replace('return "α"', 'return "γ"').replace('# trailing  \r\n', '')
    expected["scripts"][1]["content"] = SECOND.replace('return "β"', 'return "δ"')
    assert canonical(revised.model_dump(mode="json")) == canonical(expected)
    assert canonical(recipe.model_dump(mode="json")) == canonical(original)
    assert payload == old_payload
    assert revised.scripts[0].content.encode() == expected["scripts"][0]["content"].encode()
    revised.parameters[0].description = "Independent copy after the transaction."
    assert recipe.parameters[0].description == original["parameters"][0]["description"]


def test_all_anchors_are_resolved_in_original_never_cascading_replacements(recipe):
    payload = {"edits": [change('"α"', '"new-anchor"'), change('"new-anchor"', '"another"')]}
    original = recipe.model_dump(mode="json")
    with pytest.raises(ImplementationEditError) as caught:
        apply_implementation_edits(recipe, payload)
    assert caught.value.details == {"reason": "anchor_missing", "edit_indexes": [1]}
    assert recipe.model_dump(mode="json") == original


@pytest.mark.parametrize(("edits", "reason", "indexes"), [
    ([change("private-missing-anchor", "replacement")], "anchor_missing", [0]),
    ([change("\r\n", "\n")], "anchor_ambiguous", [0]),
    ([change('"α"', '"γ"'), change('return "α"', 'return "δ"')], "overlapping_edits", [0, 1]),
    ([change('"α"', '"γ"'), change('"α"', '"δ"')], "overlapping_edits", [0, 1]),
    ([change('"α"', '"α"')], "unchanged_edit", [0]),
    ([change('"α"', '"γ"', "scripts/private-unknown.py")], "unknown_script", [0]),
    ([change('"α"', '"γ"', " scripts/first.py ")], "unknown_script", [0]),
])
def test_invalid_transactions_are_atomic_with_only_fixed_safe_feedback(recipe, edits, reason, indexes):
    original = recipe.model_dump(mode="json")
    with pytest.raises(ImplementationEditError) as caught:
        apply_implementation_edits(recipe, {"edits": edits})
    assert caught.value.code == "implementation_revision_invalid"
    assert caught.value.details == {"reason": reason, "edit_indexes": indexes}
    public = str(caught.value) + canonical(caught.value.details)
    assert "private-missing-anchor" not in public and "private-unknown" not in public
    assert recipe.model_dump(mode="json") == original


def test_overlapping_occurrences_of_one_anchor_are_ambiguous(recipe):
    recipe.scripts[0].content += "# aaaa\n"
    with pytest.raises(ImplementationEditError) as caught:
        apply_implementation_edits(recipe, {"edits": [change("aaa", "b")]})
    assert caught.value.details["reason"] == "anchor_ambiguous"


def test_adjacent_ranges_are_valid_and_edit_order_does_not_change_result(recipe):
    edits = [change('return ', 'yield '), change('"α"', '"γ"')]
    forward = apply_implementation_edits(recipe, {"edits": edits})
    reverse = apply_implementation_edits(recipe, {"edits": list(reversed(edits))})
    assert forward.model_dump() == reverse.model_dump()
    assert forward.scripts[0].content == FIRST.replace('return "α"', 'yield "γ"')


def test_distinct_changes_cannot_cancel_into_an_unchanged_transaction(recipe):
    recipe.scripts[0].content += "# UNIQUE_AB\n"
    with pytest.raises(ImplementationEditError) as caught:
        apply_implementation_edits(recipe, {"edits": [change("UNIQUE_", ""), change("AB", "UNIQUE_AB")]})
    assert caught.value.details["reason"] == "unchanged_result"


def test_ambiguous_original_script_identity_is_rejected(recipe):
    recipe.scripts.append(recipe.scripts[0].model_copy(deep=True))
    with pytest.raises(ImplementationEditError) as caught:
        apply_implementation_edits(recipe, {"edits": [change('"α"', '"γ"')]})
    assert caught.value.details == {"reason": "invalid_recipe", "edit_indexes": []}


@pytest.mark.parametrize("payload", [
    {"edits": []}, {"edits": [change("", "x")]}, {"edits": [change("x", 1)]},
    {"edits": [change(1, "x")]}, {"edits": [change("x", "y", 1)]},
    {"edits": [{**change("x", "y"), "purpose": "Change metadata"}]},
    {"edits": [change("x", "y")], "workflow": {}},
    {"edits": [{"script_path": "scripts/first.py", "before": "x"}]},
])
def test_model_cannot_change_structure_metadata_or_coerce_scalar_types(recipe, payload):
    with pytest.raises(ValidationError):
        ImplementationEdits.model_validate(payload)
    with pytest.raises(ImplementationEditError) as caught:
        apply_implementation_edits(recipe, payload)
    assert caught.value.details == {"reason": "invalid_edits", "edit_indexes": []}


def test_even_constructed_instances_are_revalidated(recipe):
    malformed = ImplementationEdits.model_construct(edits=[ImplementationEdit.model_construct(
        script_path="scripts/first.py", before="", after="x")])
    with pytest.raises(ImplementationEditError) as caught:
        apply_implementation_edits(recipe, malformed)
    assert caught.value.details["reason"] == "invalid_edits"


def test_validation_failure_traceback_does_not_render_untrusted_source(recipe):
    payload = {"edits": [change("anchor", {"private-source-canary": "value"})]}
    with pytest.raises(ImplementationEditError) as caught:
        apply_implementation_edits(recipe, payload)
    assert "private-source-canary" not in "".join(traceback.format_exception(caught.value))


def test_non_ascii_whitespace_and_line_endings_are_not_normalized(recipe):
    original = '  #\u2028separator\r\n\r\ndef first():\r\n\treturn "e\u0301"  \r\n\r\n'
    recipe.scripts[0].content = original
    before, after = '\treturn "e\u0301"  \r\n', '\treturn "é" \r\n'
    typed = ImplementationEdits(edits=[change(before, after)])
    assert typed.edits[0].before == before and typed.edits[0].after == after
    revised = apply_implementation_edits(recipe, typed)
    assert revised.scripts[0].content.encode() == original.replace(before, after).encode()
    assert recipe.scripts[0].content.encode() == original.encode()


def test_source_transaction_does_not_claim_compile_or_method_acceptance(recipe):
    validate_script(recipe.scripts[0])
    revised = apply_implementation_edits(recipe, {"edits": [change('return "α"', 'return (')]})
    with pytest.raises(SkillScriptValidationError) as caught:
        validate_script(revised.scripts[0])
    assert caught.value.code == "script_syntax_invalid"
    assert recipe.scripts[0].content == FIRST


def test_schema_has_no_artificial_edit_count_or_source_length_cap():
    schema = ImplementationEdits.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["edits"]["minItems"] == 1
    assert "maxItems" not in schema["properties"]["edits"]
    item = schema["$defs"]["ImplementationEdit"]
    assert item["additionalProperties"] is False
    assert set(item["properties"]) == {"script_path", "before", "after"}
    assert not any("maxLength" in field for field in item["properties"].values())
    assert "minLength" not in item["properties"]["after"]


def test_large_transaction_is_not_silently_capped(recipe):
    lines = [f"value_{index:03d} = {index}\n" for index in range(80)]
    recipe.scripts[0].content = "".join(lines)
    untouched = recipe.scripts[1].model_dump(mode="json")
    revised = apply_implementation_edits(recipe, {"edits": [
        change(line, line.replace(" = ", " = -")) for line in lines
    ]})
    assert revised.scripts[0].content == "".join(line.replace(" = ", " = -") for line in lines)
    assert revised.scripts[1].model_dump(mode="json") == untouched
    assert recipe.scripts[0].content == "".join(lines)
