"""Artificial byte-preservation and authority tests; no model or execution."""
from copy import deepcopy
from dataclasses import replace
import ast
import json

import pytest

from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.script_authoring import ScriptFunctionUnit
from app.domain.services.skills.skill_creation.source_repair import (
    SourceRepairRejected, apply_source_repairs, build_source_repair_spec,
    source_repair_response_schema,
)
from app.domain.services.skills.skill_creation.source_response import SourceResponseAdapter, SourceResponseError


META = {"path": "scripts/tool.py", "purpose": "Compute the requested public summaries.",
        "invocation": "python scripts/tool.py --input input.json --output result.json"}
PRIVATE = "private-input.csv"


def adapter():
    return SourceResponseAdapter(HelperScript, dict(META), "script")


def context():
    return PrivacyContext((PrivateValue(PRIVATE, "source_filename"),))


def document(source):
    return {**META, "content": source}


def privacy_spec(source, privacy=None):
    raw = document(source)
    spec = build_source_repair_spec(raw, adapter(), privacy or context(), "private_package_content")
    assert spec is not None
    return raw, spec


def corrections(spec, transform=lambda text: text):
    return {"repairs": [{"block_id": block.block_id,
                         "replacement": transform(spec.source[block.start:block.end])}
                        for block in spec.blocks]}


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_syntax_diagnostic_selects_real_physical_lines_and_preserves_unselected_bytes(newline):
    lines = ["", "# 前文 😀\u2028 remains one physical line", "first = 17", "second = 23",
             "third = 31", "fourth = 41", "fifth = 43", "if True print('bad')",
             "sixth = 47", "seventh = 53", "eighth = 59", "# tail whitespace follows", "", ""]
    source = newline.join(lines)
    raw = document(source)
    with pytest.raises(SourceResponseError) as caught:
        adapter().validate_document(raw)
    assert caught.value.details["line"] == 8
    spec = build_source_repair_spec(raw, adapter(), PrivacyContext(), caught.value.code, caught.value.details)
    assert spec is not None
    assert [(b.start_line, b.end_line) for b in spec.blocks] == [(6, 10)]
    merged = apply_source_repairs(raw, spec, corrections(spec, lambda s: s.replace("if True print('bad')", "if True: print('good')")))
    assert merged["content"] == source.replace("if True print('bad')", "if True: print('good')")
    checked = adapter().validate_document(merged)
    accepted = HelperScript.model_validate(checked)
    assert accepted.content.encode() == merged["content"].encode()
    assert accepted.content.startswith(newline) and accepted.content.endswith(newline * 2)
    assert raw["content"] == source


def test_syntax_range_builder_does_not_require_ast_parsing(monkeypatch):
    raw = document("prefix = 1\nif True print('bad')\nsuffix = 2\n")
    with pytest.raises(SourceResponseError) as caught:
        adapter().validate_document(raw)
    monkeypatch.setattr(ast, "parse", lambda *a, **k: (_ for _ in ()).throw(AssertionError("No AST gate")))
    spec = build_source_repair_spec(raw, adapter(), PrivacyContext(), caught.value.code, caught.value.details)
    assert spec is not None


@pytest.mark.parametrize("details", [{}, {"line": None}, {"line": True}, {"line": 0},
    {"line": -1}, {"line": 900}, {"line": "1"}, {"line": 2, "end_line": 1},
    {"line": 1, "end_line": 900}])
def test_missing_or_invalid_syntax_location_has_no_local_edit_authority(details):
    assert build_source_repair_spec(document("x = )\n"), adapter(), PrivacyContext(),
                                    "script_syntax_invalid", details) is None


@pytest.mark.parametrize("count", [1, 20, 33, 73])
def test_privacy_complete_scan_selects_all_disjoint_spans_without_limit(count):
    source = "\n".join(f"# {PRIVATE}\nvalue_{i} = {i}\n# public spacer" for i in range(count)) + "\n"
    raw, spec = privacy_spec(source)
    assert len(context().scan_document(raw)) == min(count, 32)
    assert len(spec.blocks) == count
    schema = source_repair_response_schema(spec).model_json_schema()["properties"]["repairs"]
    assert schema["minItems"] == schema["maxItems"] == count
    assert PRIVATE not in json.dumps(spec.public_blocks(context()))
    assert PRIVATE not in repr(spec)
    merged = apply_source_repairs(raw, spec, corrections(spec, lambda text: text.replace(PRIVATE, "selected input")))
    assert merged["content"] == source.replace(PRIVATE, "selected input")
    assert not context().scan_document(merged, limit=None)
    assert raw["content"] == source


def test_adjacent_and_overlapping_diagnostics_merge_before_selecting_blocks():
    source = f"# {PRIVATE} {PRIVATE}\n# {PRIVATE}\nvalue = 3\n"
    raw, spec = privacy_spec(source)
    assert len(spec.blocks) == 1
    assert (spec.blocks[0].start_line, spec.blocks[0].end_line) == (1, 2)
    assert apply_source_repairs(raw, spec, corrections(spec))["content"] == source


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("literal", [
    "'\\x2fUsers\\x2fprivate\\x2fsource.csv'",
    "('/Us'\n         'ers/private/source.csv')",
    "('private-'\n         'input.csv')",
])
def test_decoded_and_adjacent_multiline_literals_use_full_source_match_span(literal, newline):
    source = f"# safe prefix\npath = {literal}\n# safe suffix\n".replace("\n", newline)
    raw, spec = privacy_spec(source)
    block = spec.blocks[0]
    assert block.start_line == 2
    assert block.end_line == 2 + literal.count("\n")
    public = json.dumps(spec.public_blocks(context()))
    assert "source.csv" not in public and "input.csv" not in public and "\\\\x2fUsers" not in public
    replacement = "path = current_input" + newline
    merged = apply_source_repairs(raw, spec, {"repairs": [{"block_id": block.block_id, "replacement": replacement}]})
    assert merged["content"] == "# safe prefix" + newline + replacement + "# safe suffix" + newline


def test_syntax_window_cannot_expose_fragment_of_a_cross_boundary_private_match():
    private = "hidden-one\nhidden-two\nhidden-three\nhidden-four\nhidden-five"
    privacy = PrivacyContext((PrivateValue(private, "credential"),))
    raw = document('text = """'+private+'"""\nvalue = 0\nif True print(2)\nend = 3\n')
    with pytest.raises(SourceResponseError) as caught:
        adapter().validate_document(raw)
    spec = build_source_repair_spec(raw, adapter(), privacy, caught.value.code, caught.value.details)
    public = json.dumps(spec.public_blocks(privacy))
    assert "hidden-" not in public
    assert private in raw["content"]


def test_multiblock_replacements_can_change_lengths_without_moving_other_targets():
    source = f"# {PRIVATE}\nkeep_one = 13\n# {PRIVATE}\nkeep_two = 29\n"
    raw, spec = privacy_spec(source)
    wire = corrections(spec)
    wire["repairs"][0]["replacement"] = "# generic one\n# another public comment\n"
    wire["repairs"][1]["replacement"] = "# generic two\n"
    wire["repairs"].reverse()  # Wire order is irrelevant; host order is immutable.
    result = apply_source_repairs(raw, spec, wire)
    assert result["content"] == "# generic one\n# another public comment\nkeep_one = 13\n# generic two\nkeep_two = 29\n"
    assert {k: v for k, v in result.items() if k != "content"} == META


@pytest.mark.parametrize("change", ["path", "purpose", "invocation", "one_byte", "line_endings", "extra"])
def test_any_original_document_change_rejects_stale_scope(change):
    raw, spec = privacy_spec(f"# {PRIVATE}\nx = 1\n")
    changed = deepcopy(raw)
    if change == "one_byte": changed["content"] += " "
    elif change == "line_endings": changed["content"] = changed["content"].replace("\n", "\r\n")
    elif change == "extra": changed["other"] = "extra"
    else: changed[change] += "x"
    with pytest.raises(SourceRepairRejected, match="Local source") as caught:
        apply_source_repairs(changed, spec, corrections(spec))
    assert caught.value.reason == "source_candidate_changed"


@pytest.mark.parametrize("attack", ["missing", "duplicate", "unknown", "extra", "path", "old_text",
    "action", "object", "list", "null", "number", "bool", "surrogate"])
def test_only_exact_host_block_ids_and_string_replacements_are_accepted(attack):
    raw, spec = privacy_spec(f"# {PRIVATE}\nx = 1\n# {PRIVATE}\ny = 2\n")
    wire = corrections(spec)
    if attack == "missing": wire["repairs"].pop()
    elif attack == "duplicate": wire["repairs"][1] = dict(wire["repairs"][0])
    elif attack == "unknown": wire["repairs"][0]["block_id"] = "B999"
    elif attack == "extra": wire["repairs"].append(dict(wire["repairs"][0]))
    elif attack in {"path", "old_text", "action"}: wire["repairs"][0][attack] = "foreign"
    else: wire["repairs"][0]["replacement"] = {"object": {}, "list": [], "null": None,
        "number": 1, "bool": True, "surrogate": "\ud800"}[attack]
    with pytest.raises(SourceRepairRejected):
        apply_source_repairs(raw, spec, wire)


@pytest.mark.parametrize("attack", ["reorder", "source", "range", "block_digest", "document_digest"])
def test_mutating_host_selection_or_bound_source_cannot_authorize_another_edit(attack):
    raw, spec = privacy_spec(f"# {PRIVATE}\nx = 1\n# {PRIVATE}\ny = 2\n")
    altered = spec
    if attack == "reorder": altered = replace(spec, blocks=tuple(reversed(spec.blocks)))
    elif attack == "source": altered = replace(spec, source=spec.source + " ")
    elif attack == "range": altered = replace(spec, blocks=(replace(spec.blocks[0], end=spec.blocks[0].end+1), *spec.blocks[1:]))
    elif attack == "block_digest": altered = replace(spec, blocks=(replace(spec.blocks[0], original_digest="bad"), *spec.blocks[1:]))
    else: altered = replace(spec, document_digest="bad")
    with pytest.raises(SourceRepairRejected):
        apply_source_repairs(raw, altered, corrections(spec))


def test_foreign_metadata_or_non_source_privacy_requires_other_repair_boundary():
    raw, _ = privacy_spec(f"# {PRIVATE}\nx = 1\n")
    assert build_source_repair_spec({**raw, "path": "scripts/other.py"}, adapter(), context(), "private_package_content") is None
    private_meta = {**META, "purpose": f"Analyze {PRIVATE} using declared methods."}
    foreign = SourceResponseAdapter(HelperScript, private_meta, "script")
    assert build_source_repair_spec({**private_meta, "content": raw["content"]}, foreign, context(), "private_package_content") is None
    assert build_source_repair_spec(raw, adapter(), context(), "method_review_rejected") is None
    assert build_source_repair_spec(document("x = 1\n"), adapter(), context(), "private_package_content") is None
    assert build_source_repair_spec(document("x = '\ud800'\n"), adapter(), context(), "script_syntax_invalid", {"line": 1}) is None


def test_merged_source_still_must_pass_syntax_and_privacy_including_deleted_content():
    raw, spec = privacy_spec(f"# {PRIVATE}\nx = 1\n")
    unchanged = apply_source_repairs(raw, spec, corrections(spec))
    assert context().scan_document(unchanged)
    invalid = apply_source_repairs(raw, spec, corrections(spec, lambda _: "if True print(1)\n"))
    with pytest.raises(SourceResponseError) as caught:
        adapter().validate_document(invalid)
    assert caught.value.candidate == invalid
    full, selected = privacy_spec(f"# {PRIVATE}\nx = 1  # {PRIVATE}\n")
    deleted = apply_source_repairs(full, selected, corrections(selected, lambda _: ""))
    with pytest.raises(SourceResponseError, match="empty_python_artifact"):
        adapter().validate_document(deleted)


def test_function_artifact_metadata_and_whitespace_survive_the_same_protocol():
    meta = {"unit_id": "U001"}
    bound = SourceResponseAdapter(ScriptFunctionUnit, meta, "function")
    source = f"\r\ndef compute(value):\r\n    # {PRIVATE}\r\n    return value * 2\r\n\r\n"
    raw = {**meta, "content": source}
    spec = build_source_repair_spec(raw, bound, context(), "private_package_content")
    merged = apply_source_repairs(raw, spec, corrections(spec, lambda s: s.replace(PRIVATE, "selected input")))
    checked = ScriptFunctionUnit.model_validate(bound.validate_document(merged))
    assert checked.content.encode() == source.replace(PRIVATE, "selected input").encode()
    assert checked.unit_id == "U001"


@pytest.mark.parametrize("mutation", [{"path": "scripts/other.py"}, {"content": None}, {"unknown": 1}])
def test_bound_document_adapter_rejects_metadata_drift_without_executing(mutation):
    raw = document("raise AssertionError('not executed during compilation')\n")
    with pytest.raises(SourceResponseError):
        adapter().validate_document({**raw, **mutation})
