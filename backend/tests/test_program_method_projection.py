"""Private host evidence projects to structurally valid, non-replayable Python."""
import ast
import hashlib

import pytest

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.program_attempt import ProgramAttemptView
from app.domain.services.program_method_evidence import private_method_evidence_for_event
from app.domain.services.skills import program_method_evidence as projection


SECRET = "synthetic-private-method-canary"


def host_record(source):
    """Use the actual successful native-event evidence gate and byte contract."""
    encoded = source.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    event = ToolEvent(seq=7, tool_call_id="native-projection-call", tool_name="shell",
        function_name="program_run", function_args={}, status=ToolStatus.CALLED,
        function_result={"success": True},
        program_attempt=ProgramAttemptView(identity="a" * 64, state="succeeded", returncode=0))
    event._program_method_evidence = {
        "schema": "program-method-evidence/v1", "source_digest": digest,
        "source_snapshot": {"version": 1, "encoding": "utf-8", "content": source,
                            "size_bytes": len(encoded), "sha256": digest},
        "operation_id": "native-projection-operation", "script_path": "/home/ubuntu/output/method.py"}
    record = private_method_evidence_for_event(event)
    assert record is not None
    assert SECRET not in event.model_dump_json()
    return record


def project(source):
    # Parsing/compilation only; tests never execute the historical source or
    # resulting reference, and no model or real user session is involved.
    compile(source, "<synthetic-original>", "exec")
    references = projection.method_program_references([host_record(source)], private_values={SECRET})
    assert len(references) == 1
    reference = references[0]
    assert reference["reference_only"] is True
    assert reference["provenance"] == "host_verified_successful_source"
    assert SECRET not in str(reference)
    assert "/home/ubuntu" not in str(reference)
    assert "native-projection-operation" not in str(reference)
    tree = ast.parse(reference["source"])
    compile(tree, "<synthetic-projection>", "exec")
    return tree, reference["source"]


def calls(tree):
    return [ast.dump(node.func, include_attributes=False) for node in ast.walk(tree) if isinstance(node, ast.Call)]


@pytest.mark.parametrize("source", [
    f'result = f"{SECRET} prefix {{mean(values) + offset!r}} {SECRET} suffix"',
    f'result = f\'{{measure("{SECRET}")=}}\'',
    f'result = f"{SECRET} {{mean(values):{{width}}.{{precision}}f}}"',
    f'result = f\'{SECRET} {{f"{SECRET} {{mean(values):.3f}}"}}\'',
    f'result = f"{{mean(values):{SECRET}}}"',
])
def test_fstrings_preserve_calls_operators_dynamic_formats_and_remove_all_literal_text(source):
    original = ast.parse(source)
    tree, _ = project(source)
    assert calls(tree) == calls(original)
    original_operators = [type(node.op) for node in ast.walk(original) if isinstance(node, ast.BinOp)]
    assert [type(node.op) for node in ast.walk(tree) if isinstance(node, ast.BinOp)] == original_operators
    assert sum(isinstance(node, ast.JoinedStr) for node in ast.walk(tree)) == sum(
        isinstance(node, ast.JoinedStr) for node in ast.walk(original))
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            assert all(isinstance(value, ast.FormattedValue) or
                       isinstance(value, ast.Constant) and isinstance(value.value, str) for value in node.values)
    original_dynamic_names = {node.id for node in ast.walk(original) if isinstance(node, ast.Name)}
    assert original_dynamic_names <= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


@pytest.mark.parametrize("pattern", [f'"{SECRET}"', "7", "-7", "3j", "-3j", "3+4j", "-3-4j"])
def test_match_values_remain_value_patterns_never_capture_patterns(pattern):
    source = f"def classify(value):\n match value:\n  case {pattern}:\n   return score(value + 1)\n  case _:\n   return score(value - 1)\n"
    tree, _ = project(source)
    match = next(node for node in ast.walk(tree) if isinstance(node, ast.Match))
    assert isinstance(match.cases[0].pattern, ast.MatchValue)
    assert isinstance(match.cases[0].pattern.value, ast.Attribute)
    assert isinstance(match.cases[1].pattern, ast.MatchAs) and match.cases[1].pattern.name is None
    assert calls(tree) == calls(ast.parse(source))
    assert [type(node.op) for node in ast.walk(tree) if isinstance(node, ast.BinOp)] == [ast.Add, ast.Sub]


@pytest.mark.parametrize("pattern", [
    f'{{"{SECRET}": value}}',
    f'{{"{SECRET}": [value, *rest], -7: extra, **remaining}}',
    f'{{"{SECRET}": {{3+4j: value}}}}',
])
def test_mapping_pattern_keys_use_valid_value_lookups_and_preserve_binding_structure(pattern):
    source = f"match record:\n case {pattern}:\n  result = score(value)\n case _:\n  result = None\n"
    tree, _ = project(source)
    original = ast.parse(source)
    mappings = [node for node in ast.walk(tree) if isinstance(node, ast.MatchMapping)]
    originals = [node for node in ast.walk(original) if isinstance(node, ast.MatchMapping)]
    assert len(mappings) == len(originals)
    for changed, before in zip(mappings, originals):
        assert len(changed.keys) == len(before.keys)
        assert all(isinstance(key, ast.Attribute) for key in changed.keys)
        assert changed.rest == before.rest
    bindings = lambda value: sorted((type(node).__name__, node.name) for node in ast.walk(value)
                                   if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name)
    assert bindings(tree) == bindings(original)
    assert calls(tree) == calls(original)


@pytest.mark.parametrize("source", [
    f'def method():\n "{SECRET}"',
    f'async def method():\n "{SECRET}"',
    f'class Method:\n "{SECRET}"',
    f'if selected:\n "{SECRET}"\nelse:\n "{SECRET}"',
    f'while selected:\n "{SECRET}"\nelse:\n "{SECRET}"',
    f'for value in values:\n "{SECRET}"\nelse:\n "{SECRET}"',
    f'with resource:\n "{SECRET}"',
    f'try:\n "{SECRET}"\nexcept Exception:\n "{SECRET}"\nelse:\n "{SECRET}"\nfinally:\n "{SECRET}"',
    f'match value:\n case 0:\n  "{SECRET}"\n case _:\n  "{SECRET}"',
    f'async def method():\n async for value in values:\n  "{SECRET}"\n async with resource:\n  "{SECRET}"',
])
def test_literal_only_suites_keep_valid_pass_statements(source):
    tree, _ = project(source)
    assert any(isinstance(node, ast.Pass) for node in ast.walk(tree))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If,
                             ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
                             ast.Try, ast.ExceptHandler, ast.match_case)):
            assert node.body


@pytest.mark.parametrize("source", [
    "[] = []", "() = []", "del []", "del ()",
    "for [] in rows:\n pass", "for () in rows:\n pass", "with resource as []:\n pass",
    "result = [value for [] in rows]",
])
def test_empty_collection_targets_keep_their_store_or_delete_context(source):
    original = ast.parse(source)
    tree, _ = project(source)
    targets = lambda value: [(type(node).__name__, type(node.ctx).__name__, len(node.elts))
        for node in ast.walk(value) if isinstance(node, (ast.List, ast.Tuple)) and not isinstance(node.ctx, ast.Load)]
    assert targets(tree) == targets(original)
    assert targets(tree)


def test_symbolic_scientific_expression_and_assignment_target_structure_survive():
    source = f'''"{SECRET}"
coefficients, residuals = fit(design, observations)
leverage = diag(design @ pinv(design.T @ design) @ design.T)
variance = (residuals / (1 - leverage)) ** 2
symbolic = [variance, variance ** 2]
'''
    original = ast.parse(source)
    tree, _ = project(source)
    assert calls(tree) == calls(original)
    assert [type(node.op) for node in ast.walk(tree) if isinstance(node, ast.BinOp)] == [
        type(node.op) for node in ast.walk(original) if isinstance(node, ast.BinOp)]
    targets = lambda value: [ast.dump(node.targets[0], include_attributes=False)
                            for node in value.body if isinstance(node, ast.Assign)]
    assert targets(tree) == targets(original)
    assert any(isinstance(node, ast.List) and isinstance(node.ctx, ast.Load) for node in ast.walk(tree))


SAFE_REASONS = frozenset({"invalid_record", "integrity_unavailable", "unsupported_syntax",
                         "invalid_projection", "private_identifiers"})


def safe_failure(error, reason):
    assert isinstance(error, projection.ProgramMethodEvidenceError)
    assert error.reason == reason and error.reason in SAFE_REASONS
    assert SECRET not in str(error)
    assert "/home/ubuntu" not in str(error)
    assert "0x" not in str(error)
    assert error.__cause__ is None


@pytest.mark.parametrize("records", ["not-a-record-list", [{}], [None], [{"schema": SECRET}]])
def test_invalid_host_record_shapes_have_safe_enumerated_errors(records):
    with pytest.raises(projection.ProgramMethodEvidenceError) as error:
        projection.method_program_references(records)
    safe_failure(error.value, "invalid_record")


def test_modified_private_snapshot_cannot_be_projected_even_after_a_valid_batch_item():
    valid = host_record(f'output = measure("{SECRET}")')
    invalid = host_record(f'output = summarize("{SECRET}")')
    invalid["source_snapshot"]["content"] += "\nchanged = True"
    with pytest.raises(projection.ProgramMethodEvidenceError) as error:
        projection.method_program_references([valid, invalid])
    safe_failure(error.value, "integrity_unavailable")


@pytest.mark.parametrize("source", [
    f'value = "{SECRET}',
    f'return "{SECRET}"',
    f'match value:\n case captured:\n  print("{SECRET}")\n case _:\n  pass',
])
def test_unsupported_original_syntax_is_not_reported_with_private_lines(source):
    with pytest.raises(projection.ProgramMethodEvidenceError) as error:
        projection.method_program_references([host_record(source)])
    safe_failure(error.value, "unsupported_syntax")
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("exception", [ValueError, TypeError, RecursionError, OverflowError, SyntaxError])
def test_unparse_errors_never_escape_with_raw_nodes_or_private_source(monkeypatch, exception):
    def failing_unparse(tree):
        raise exception(f"{SECRET}: /home/ubuntu/inputs/private.csv <ast.Name object at 0x123456>")
    monkeypatch.setattr(projection.ast, "unparse", failing_unparse)
    with pytest.raises(projection.ProgramMethodEvidenceError) as error:
        projection.method_program_references([host_record(f'output = measure("{SECRET}")')])
    safe_failure(error.value, "invalid_projection")
    assert error.value.__suppress_context__ is True


def test_unparse_output_is_grammar_checked_before_reference_publication(monkeypatch):
    monkeypatch.setattr(projection.ast, "unparse", lambda tree: f'return "{SECRET}"')
    with pytest.raises(projection.ProgramMethodEvidenceError) as error:
        projection.method_program_references([host_record("output = measure(values)")])
    safe_failure(error.value, "invalid_projection")


def test_transformed_ast_is_compiled_before_unparse_or_publication(monkeypatch):
    def invalid_tree(self, tree):
        tree.body[0] = ast.copy_location(ast.Name(id="private_name", ctx=ast.Load()), tree.body[0])
        return tree
    monkeypatch.setattr(projection._MethodProjection, "visit", invalid_tree)
    with pytest.raises(projection.ProgramMethodEvidenceError) as error:
        projection.method_program_references([host_record("output = measure(values)")])
    safe_failure(error.value, "invalid_projection")


def test_private_local_identifier_is_consistently_renamed_without_raw_source_fallback():
    identifier = "synthetic_private_identifier_canary"
    references = projection.method_program_references(
        [host_record(f"{identifier} = mean(values)\nresult = {identifier} + 1")], private_values={identifier})
    assert identifier not in str(references)
    tree = ast.parse(references[0]["source"])
    assert tree.body[0].targets[0].id == tree.body[1].value.left.id
    assert references[0]["reference_only"] is True


def test_error_reason_whitelist_cannot_become_an_exception_text_escape_hatch():
    assert projection.ProgramMethodEvidenceError.REASONS == SAFE_REASONS
    with pytest.raises(ValueError) as error:
        projection.ProgramMethodEvidenceError(SECRET)
    assert SECRET not in str(error.value)
