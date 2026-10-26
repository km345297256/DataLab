"""Negative scientific decisions route to bounded edits without a new design."""
import copy

import pytest

from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_creation.review_form import review_form_adapter, ReviewFormError
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_skill_implementation_revision_pipeline import (
    Authoring, make_pipeline, without_source, adverse_entries,
)
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_creation_checkpoint_integration import verifier
from test_skill_review_form import payload as review_payload, form as review_form


def localized_author(negative="method", *, scope_change=None, edit=None, verdicts=None):
    author = Authoring(verdicts or [negative, "accepted"], scripts=2, edit=edit)
    original = author.reply

    def reply(stage, payload):
        value = original(stage, payload)
        if stage == "method_review" and (not value["implementation_appropriate"]
                or any(item["status"] != "preserved" for item in value["requirements"])
                or any(item["status"] != "preserved" for item in value["sources"])):
            for item in value["requirements"]:
                if item["status"] != "preserved":
                    item["explanation"] = "工作流正确保留了此方法，但第一个脚本的结果字段没有实现约定的核验。"
            value["repair_scope"] = {
                "kind": "implementation", "requirement_ids": ["R001"],
                "source_ids": [item["source_id"] for item in value["sources"] if item["status"] != "preserved"],
                "script_paths": [author.scripts[0]["path"]],
            }
            if scope_change:
                scope_change(value["repair_scope"])
            author.review_documents[-1] = copy.deepcopy(value)
        return value
    author.gateway.reply = reply
    return author


@pytest.mark.asyncio
@pytest.mark.parametrize("negative", ["method", "source", "mixed"])
async def test_localized_scientific_negative_edits_only_its_bound_script(negative):
    author, cache, verify = localized_author(negative), MemoryCache(), verifier()
    result = await make_pipeline(author, cache, verify).run()
    old, new = [item["skill_package"] for item in author.calls("method_review")]
    assert without_source(old) == without_source(new)
    assert old["scripts"][1] == new["scripts"][1]
    assert "'field_1'" in new["scripts"][0]["content"]
    assert result.recipe.model_dump() == new
    assert len(author.calls("workflow")) == 1
    assert len(author.calls("implementation")) == 2
    [revision] = author.calls("implementation_revision")
    assert revision["review"] == author.review_documents[0]
    assert revision["revision_scope"]["script_paths"] == [author.scripts[0]["path"]]
    assert revision["revision_scope"]["requirement_ids"] == ["R001"]
    assert len(adverse_entries(cache)) == 1
    saved = await builds(cache).get_revision()
    assert saved.review.model_dump() == author.review_documents[0]
    statuses = [call.args[2] for call in author.gateway._progress.await_args_list]
    assert statuses == ["revision_required", "accepted"]
    verify.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    lambda scope: scope.update(kind="workflow"),
    lambda scope: scope.update(kind="none"),
    lambda scope: scope.update(requirement_ids=[]),
    lambda scope: scope.update(requirement_ids=["R999"]),
    lambda scope: scope.update(source_ids=["S999"]),
    lambda scope: scope.update(script_paths=[]),
    lambda scope: scope.update(script_paths=["scripts/unknown.py"]),
    lambda scope: scope.update(script_paths=scope["script_paths"] * 2),
])
async def test_incomplete_or_untrusted_localization_cannot_authorize_source_only_repair(change):
    author = localized_author(scope_change=change)
    await make_pipeline(author, MemoryCache()).run()
    assert author.calls("implementation_revision") == []
    assert len(author.calls("workflow")) == 2
    assert len(author.calls("method_review")) == 2


@pytest.mark.asyncio
async def test_localization_cannot_edit_another_existing_script():
    def edit(payload):
        return {"edits": [{"script_path": payload["previous_skill_package"]["scripts"][1]["path"],
                           "before": "'field_0'", "after": "'field_1'"}]}
    author, verify = localized_author(edit=edit), verifier()
    with pytest.raises(SkillCreationError) as error:
        await make_pipeline(author, MemoryCache(), verify).run()
    assert error.value.code == "implementation_revision_invalid"
    assert len(author.calls("method_review")) == 1
    verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_localized_negative_stops_without_a_new_reviewer_vote():
    author, cache, verify = localized_author(verdicts=["method", "method"]), MemoryCache(), verifier()
    with pytest.raises(SkillCreationError) as error:
        await make_pipeline(author, cache, verify).run()
    assert error.value.code == "method_review_rejected"
    assert len(author.calls("method_review")) == 2
    assert len(author.calls("implementation_revision")) == 1
    assert len(author.calls("workflow")) == 1
    assert len(adverse_entries(cache)) == 2
    verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_requirement_cannot_be_relabelled_as_a_code_defect():
    author = localized_author()
    original = author.gateway.reply
    def reply(stage, payload):
        value = original(stage, payload)
        if stage == "method_review" and "repair_scope" in value:
            value["requirements"][0]["status"] = "unsupported"
        return value
    author.gateway.reply = reply
    await make_pipeline(author, MemoryCache()).run()
    assert author.calls("implementation_revision") == []
    assert len(author.calls("workflow")) == 2


@pytest.mark.asyncio
async def test_source_negative_requires_scope_for_every_retained_source_obligation():
    author = localized_author("source")
    original = author.gateway.reply
    def reply(stage, payload):
        value = original(stage, payload)
        if stage == "method_requirements":
            value["requirements"].append({**value["requirements"][0], "id": "R002",
                "statement": "保留来源要求的输入与输出质量核验。"})
        return value
    author.gateway.reply = reply
    await make_pipeline(author, MemoryCache()).run()
    assert author.calls("implementation_revision") == []
    assert len(author.calls("workflow")) == 2


def localized_form():
    payload = review_payload()
    payload["skill_package"] = {"scripts": [{"path": "scripts/analyze.py"}]}
    form = review_form()
    form["requirements"]["R017"]["status"] = "missing"
    form["repair_scope"] = {"kind": "implementation", "requirement_ids": ["R017"],
                            "source_ids": [], "script_paths": ["scripts/analyze.py"]}
    return payload, form


def test_review_form_binds_localization_to_host_targets_without_upgrading_verdict():
    payload, form = localized_form()
    adapter = review_form_adapter(MethodReview, "method_review", payload)
    schema = adapter.request_schema()
    scope = schema["properties"]["repair_scope"]
    assert "repair_scope" in schema["required"]
    assert scope["properties"]["script_paths"]["items"]["enum"] == ["scripts/analyze.py"]
    assert scope["properties"]["requirement_ids"]["items"]["enum"] == ["R017", "R003"]
    value = MethodReview.model_validate(adapter.document(form))
    assert value.requirements[0].status == "missing"
    assert value.repair_scope.model_dump() == form["repair_scope"]
    assert adapter.observe(("requirements", "R017", "status"), "missing")
    assert "repair_scope" not in review_form_adapter(MethodReview, "synthetic_reference_review", payload).request_schema()["properties"]


@pytest.mark.parametrize("field,value", [("script_paths", ["private-unknown.py"]),
    ("requirement_ids", ["R999"]), ("source_ids", ["S999"]), ("script_paths", ["scripts/analyze.py"] * 2)])
def test_review_form_rejects_unknown_or_duplicate_scope_identity_without_echoing_it(field, value):
    payload, form = localized_form()
    form["repair_scope"][field] = value
    with pytest.raises(ReviewFormError) as error:
        review_form_adapter(MethodReview, "method_review", payload).document(form)
    assert error.value.fields == ["repair_scope", field]
    assert "private-unknown" not in str(error.value)
