"""Retain source-backed method constants without publishing historical inputs."""
import ast

import pytest

from app.domain.services.skills.program_method_evidence import ProgramMethodEvidenceError, method_program_references
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from test_program_method_projection import host_record


def project(source, sources=(), privacy=None):
    return method_program_references([host_record(source)], method_sources=list(sources),
                                     privacy_context=privacy or PrivacyContext())[0]


def test_explicit_scientific_selectors_and_thresholds_survive_without_api_name_allowlists():
    source = '''result = arbitrary_library.unlisted_export(values, covariance="HC3", correction="holm", alpha=0.05)
limit = 0.95
observed_score = 17.25
private_rows = [{"sample": "cohort-17", "value": 17.25}]
'''
    reference = project(source, [{"kind": "request", "text": "Use HC3. Apply Holm with alpha 0.05 and confidence 0.95."}])
    tree = ast.parse(reference["source"])
    constants = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)]
    assert {"HC3", "holm", 0.05, 0.95} <= set(constants)
    assert "cohort-17" not in reference["source"] and "17.25" not in reference["source"]
    assert "arbitrary_library.unlisted_export" in reference["source"]
    assert "CURRENT_TASK_VALUES" in reference["source"]
    assert reference["reference_only"] is True
    assert "not scientific acceptance" in reference["limitations"]


@pytest.mark.parametrize("source_kind,status", [("observed_summary", "completed"), ("method", "failed"), ("request", "incomplete")])
def test_observed_results_and_unfinished_methods_do_not_establish_reusable_literals(source_kind, status):
    reference = project('value = compute(mode="observed-mode", alpha=17.25)',
                        [{"kind": source_kind, "text": "Use observed-mode and 17.25.", "execution_status": status}])
    assert "observed-mode" not in reference["source"] and "17.25" not in reference["source"]


def test_method_source_support_cannot_override_private_values_or_generic_paths():
    private = "private-method-selector"
    privacy = PrivacyContext((PrivateValue(private, "identity"),))
    reference = project(f'value = compute(mode={private!r}, source="/private/input.csv")',
                        [{"kind": "method", "text": f"Use {private} and /private/input.csv."}], privacy)
    assert private not in str(reference) and "/private" not in str(reference)
    assert not privacy.scan_document(reference)


def test_supported_scalar_does_not_unmask_literal_data_collections():
    reference = project('dataset = [["HC3", 0.05], ["HC3", 0.95]]',
                        [{"kind": "method", "text": "Use HC3 with 0.05 and 0.95."}])
    assert "HC3" not in reference["source"] and "0.05" not in reference["source"]
    assert "CURRENT_TASK_VALUES" in reference["source"]


def test_support_is_exact_not_an_incidental_substring():
    reference = project('fit(mode="HC3", alpha=0.05)',
                        [{"kind": "request", "text": "Use HC30 with threshold 10.05."}])
    assert "HC3" not in reference["source"] and "0.05" not in reference["source"]


def test_private_bindings_preserve_shadowing_parameters_and_closure_references():
    private = "private_binding_canary"
    privacy = PrivacyContext((PrivateValue(private, "identity"),))
    source = f'''{private} = 1
def outer({private}):
    def inner():
        nonlocal {private}
        {private} += 1
        return {private}
    return inner
result = outer({private})
'''
    reference = project(source, privacy=privacy)
    tree = ast.parse(reference["source"])
    compile(tree, "<portable-reference>", "exec")
    name = tree.body[0].targets[0].id
    assert tree.body[1].args.args[0].arg == name
    assert tree.body[1].body[0].body[0].names == [name]
    assert private not in str(reference) and not privacy.scan_document(reference)


def test_private_external_attribute_is_explicitly_unresolved_not_claimed_as_a_known_api():
    private = "private_external_method"
    privacy = PrivacyContext((PrivateValue(private, "identity"),))
    reference = project(f"{private} = 1\nresult = external.{private}(values)\nother = arbitrary_library.unlisted_export(values)",
                        privacy=privacy)
    placeholder = reference["unresolved_member_roles"][0]["placeholder"]
    assert reference["unresolved_member_roles"] == [{"placeholder": placeholder, "roles": ["call"], "binding_required": True}]
    assert f"external.{placeholder}(values)" in reference["source"]
    assert "arbitrary_library.unlisted_export(values)" in reference["source"]
    assert reference["reference_only"] is True
    assert "do not name known APIs" in reference["limitations"]
    assert private not in str(reference) and not privacy.scan_document(reference)


@pytest.mark.parametrize("external_use", [
    "result = external(values, private_external_name=1)",
    "from arbitrary_library import private_external_name",
    "def unrelated():\n    return private_external_name(values)",
])
def test_private_local_spelling_does_not_rewrite_external_or_unresolved_global_names(external_use):
    private = "private_external_name"
    source = f"def local():\n    {private} = 1\n    return {private}\n{external_use}"
    with pytest.raises(ProgramMethodEvidenceError) as failure:
        project(source, privacy=PrivacyContext((PrivateValue(private, "identity"),)))
    assert failure.value.reason == "private_identifiers"
    assert private not in str(failure.value)


def test_private_binding_does_not_capture_an_existing_generated_name_in_any_scope():
    private = "private_binding_canary"
    reference = project(f"{private} = 1\ndef other(binding_1):\n    return binding_1 + {private}",
                        privacy=PrivacyContext((PrivateValue(private, "identity"),)))
    tree = ast.parse(reference["source"])
    replacement = tree.body[0].targets[0].id
    assert replacement != "binding_1"
    assert tree.body[1].args.args[0].arg == "binding_1"
    assert tree.body[1].body[0].value.right.id == replacement
    assert private not in reference["source"]


def test_private_member_placeholder_preserves_local_class_binding_read_write_and_delete():
    private = "private_member_canary"
    source = f'''class Example:
    {private} = 1
    def update(self):
        self.{private} += 1
        value = self.{private}
        del self.{private}
        return value
instance = Example()
instance.{private} = 2
'''
    reference = project(source, privacy=PrivacyContext((PrivateValue(private, "identity"),)))
    tree = ast.parse(reference["source"])
    compile(tree, "<member-reference>", "exec")
    placeholder = reference["unresolved_member_roles"][0]["placeholder"]
    assert tree.body[0].body[0].targets[0].id == placeholder
    assert {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} == {placeholder}
    assert reference["unresolved_member_roles"][0]["roles"] == ["delete", "read", "write"]
    assert private not in str(reference)


def test_private_member_method_definition_and_call_share_collision_free_placeholder():
    private = "private_method_canary"
    source = f'''class Example:
    def {private}(self):
        return 1
CURRENT_TASK_MEMBER_001 = 2
result = Example().{private}()
'''
    reference = project(source, privacy=PrivacyContext((PrivateValue(private, "identity"),)))
    tree = ast.parse(reference["source"])
    placeholder = reference["unresolved_member_roles"][0]["placeholder"]
    assert placeholder != "CURRENT_TASK_MEMBER_001"
    assert tree.body[0].body[0].name == placeholder
    assert tree.body[2].value.func.attr == placeholder
    assert private not in str(reference)
