"""Edit fragments acquire source context only through their bound original."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import CoverageError
from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints
from app.domain.services.skills.skill_creation.implementation_revision import (
    ImplementationEdits, ImplementationEditError, apply_implementation_edits,
)
from app.domain.services.skills.skill_creation.models import SkillRecipe
from app.domain.services.skills.skill_creation.package import validate_portable_document, validate_recipe
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, generation_privacy
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from test_skill_creation_checkpoints import MemoryCache, scope
from test_skill_creation_gateway import make_gateway, requests
from test_skill_implementation_revision import recipe, change


def transaction(recipe):
    # Ordinary Python source spelling contains a slash escape which is not a
    # decoded host path. It must be checked as part of the full module.
    line = 'help_text = ' + repr('public command ' + chr(92) + '\n') + '\n'
    recipe.scripts[0].content += line
    return ImplementationEdits(edits=[change(line, line.replace('public command', 'changed command'))])


def scoped(recipe, privacy=None, *, schema=ImplementationEdits, stage="implementation_revision"):
    return generation_privacy(privacy or PrivacyContext(), schema, stage,
                              {"previous_skill_package": recipe.model_dump(mode="json")})


def test_fragments_use_complete_original_source_and_preserve_safe_exact_anchors(recipe):
    edits = transaction(recipe)
    base = PrivacyContext()
    assert base.scan_document(edits)  # The generic prose interpretation differs.
    assert not base.scan_document(recipe)
    context = scoped(recipe)
    assert context.scan_document(edits) == []
    assert context.redact_document(edits) == edits.model_dump(mode="json")
    assert context.redact_document(edits.model_dump(mode="json")) == edits.model_dump(mode="json")
    assert context.scan_document(edits.model_dump(mode="json"))  # No untyped exemption.


@pytest.mark.parametrize(("schema", "stage"), [
    (SkillRecipe, "implementation_revision"), (ImplementationEdits, "implementation"),
    (ImplementationEdits, "workflow"), (ImplementationEdits, "method_review"),
])
def test_wrong_schema_or_stage_never_grants_source_context(recipe, schema, stage):
    edits = transaction(recipe)
    assert scoped(recipe, schema=schema, stage=stage).scan_document(edits)


def test_a_schema_subclass_cannot_grant_the_exact_transaction_protocol(recipe):
    class ForeignEdits(ImplementationEdits):
        pass
    edits = transaction(recipe)
    assert scoped(recipe, schema=ForeignEdits).scan_document(edits)
    assert scoped(recipe).scan_document(ForeignEdits.model_validate(edits.model_dump()))


def test_request_scope_does_not_inherit_review_pointer_exemptions_or_mutable_original(recipe):
    edits = transaction(recipe)
    base = PrivacyContext(review_json_pointers=frozenset({"/public/rows"}), coverage_amendment_pointers=True)
    context = scoped(recipe, base)
    assert not context.review_json_pointers and context.coverage_amendment_pointers is False
    assert context.scan_document({"explanation": "The /public/rows field is expected."})
    recipe.scripts[0].content = "different original"
    assert context.scan_document(edits) == []


@pytest.mark.parametrize("after", [
    'return "private-source-canary"', 'return "\\x70rivate-source-canary"',
    'return ("private-source-" "canary")', 'return "/tmp/private-data"',
    'return "sk-abc123456789secret"',
])
def test_private_literals_and_decoded_paths_still_fail_in_merged_source(recipe, after):
    base = PrivacyContext((PrivateValue("private-source-canary", "identity"),))
    context = scoped(recipe, base)
    edits = ImplementationEdits(edits=[change('return "α"', after)])
    assert context.scan_document(edits)
    assert context.redact_document(edits) is None
    assert context.redact_document(edits.model_dump()) is None
    with pytest.raises(SkillPackageValidationError) as failure:
        validate_portable_document(edits, context)
    assert failure.value.code == "private_package_content"


def test_private_original_cannot_become_a_source_scope(recipe):
    recipe.scripts[0].content += '\nlabel = "private-source-canary"\n'
    with pytest.raises(SkillPackageValidationError) as failure:
        scoped(recipe, PrivacyContext((PrivateValue("private-source-canary", "identity"),)))
    assert failure.value.code == "private_package_content"


def test_invalid_transactions_have_no_redacted_candidate_and_no_ambient_exemption(recipe):
    context = scoped(recipe)
    invalid = ImplementationEdits(edits=[change('not in original', 'arbitrary new code')])
    with pytest.raises(ImplementationEditError) as failure:
        context.scan_document(invalid)
    assert failure.value.details["reason"] == "anchor_missing"
    assert context.redact_document(invalid) is None
    assert context.redact_document({**invalid.model_dump(), "extra": "invalid"}) is None
    assert context.redact_document({"explanation": "/tmp/private-data"}) == PrivacyContext().redact_document(
        {"explanation": "/tmp/private-data"})


@pytest.mark.asyncio
async def test_real_gateway_repair_keeps_valid_anchors_after_domain_then_format_failure(recipe):
    edits = transaction(recipe)
    context = scoped(recipe)
    gateway, calls, _ = make_gateway([
        edits.model_dump(), AIMessage(content='{"unfinished":'), edits.model_dump(),
    ], privacy=PrivacyContext())
    checked = []

    def check(value):
        validate_portable_document(value, context)
        merged = apply_implementation_edits(recipe, value)
        validate_recipe(merged)
        checked.append(merged)
        if len(checked) == 1:
            raise CoverageError("method_identifiers_lost")

    actual = await gateway.generate(ImplementationEdits, "implementation_revision", "Repair the implementation.",
        {"previous_skill_package": recipe.model_dump(mode="json")}, check)
    assert actual == edits and len(checked) == 2
    observed = requests(calls)
    assert observed[1]["previous_candidate"] == edits.model_dump(mode="json")
    assert observed[2]["previous_candidate"] == edits.model_dump(mode="json")
    assert observed[2]["repair_feedback"]["prior_unrechecked_feedback"]["reason"] == "method_identifiers_lost"


@pytest.mark.asyncio
async def test_real_gateway_rejects_unknown_anchor_then_checks_a_fresh_transaction(recipe):
    edits = transaction(recipe)
    invalid = {"edits": [change("absent-private-canary", "new value")]}
    context = scoped(recipe)
    gateway, calls, _ = make_gateway([invalid, edits.model_dump()], privacy=PrivacyContext())
    actual = await gateway.generate(ImplementationEdits, "implementation_revision", "Repair the implementation.",
        {"previous_skill_package": recipe.model_dump(mode="json")},
        lambda value: validate_portable_document(value, context))
    assert actual == edits
    feedback = requests(calls)[1]
    assert feedback["repair_feedback"]["reason"] == "implementation_revision_invalid"
    assert "previous_candidate" not in feedback
    assert "absent-private-canary" not in str(feedback)


@pytest.mark.asyncio
async def test_checkpoint_cold_and_hot_paths_recheck_exact_merged_source(recipe):
    edits = transaction(recipe)
    cache = MemoryCache()
    produce = AsyncMock(return_value=edits)
    payload = {"previous_skill_package": recipe.model_dump(mode="json")}
    first = await BuildCheckpoints(cache, scope(), privacy=PrivacyContext()).run(
        ImplementationEdits, "implementation_revision", "Repair.", payload, None, produce)
    restored = await BuildCheckpoints(cache, scope(), privacy=PrivacyContext()).run(
        ImplementationEdits, "implementation_revision", "Repair.", payload, None, produce)
    assert first == restored == edits and produce.await_count == 1
    assert next(iter(cache.entries.values()))["value"] == edits.model_dump(mode="json")


@pytest.mark.asyncio
async def test_checkpoint_damaged_private_transaction_is_revalidated_on_restore(recipe):
    edits = transaction(recipe)
    cache = MemoryCache()
    private = "private-source-canary"
    privacy = PrivacyContext((PrivateValue(private, "identity"),))
    payload = {"previous_skill_package": recipe.model_dump(mode="json")}
    produce = AsyncMock(return_value=edits)
    await BuildCheckpoints(cache, scope(), privacy=privacy).run(
        ImplementationEdits, "implementation_revision", "Repair.", payload, None, produce)
    saved = next(iter(cache.entries.values()))
    saved["value"]["edits"][0]["after"] = f'help_text = "{private}"\n'
    restored = await BuildCheckpoints(cache, scope(), privacy=privacy).run(
        ImplementationEdits, "implementation_revision", "Repair.", payload, None, produce)
    assert restored == edits and produce.await_count == 2
    assert private not in str(cache.entries)


@pytest.mark.asyncio
async def test_checkpoint_never_saves_an_invalid_or_private_transaction(recipe):
    cache = MemoryCache()
    invalid = ImplementationEdits(edits=[change('return "α"', 'return "/tmp/private-data"')])
    with pytest.raises(SkillPackageValidationError) as failure:
        await BuildCheckpoints(cache, scope(), privacy=PrivacyContext()).run(
            ImplementationEdits, "implementation_revision", "Repair.",
            {"previous_skill_package": recipe.model_dump(mode="json")}, None, AsyncMock(return_value=invalid))
    assert failure.value.code == "private_package_content"
    assert cache.entries == {}
