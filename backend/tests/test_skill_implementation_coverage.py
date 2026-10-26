"""Deferred method coverage is checked before a helper can be checkpointed."""
import pytest

from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_session_skill_creator import helper_script, recipe_payload
from test_skill_creation_checkpoint_integration import Gateway, pipeline, response, verifier
from test_skill_creation_checkpoints import MemoryCache


@pytest.mark.asyncio
async def test_missing_script_method_is_repaired_before_checkpoint_or_package_assembly():
    cache, verify, implementations = MemoryCache(), verifier(), 0
    script, recipe = helper_script(), recipe_payload()
    recipe["implementation"] = {"kind": "parameterized_scripts",
        "reason": "All method anchors must occur in the assigned complete implementation.",
        "scripts": [{key: value for key, value in script.items() if key != "content"} |
                    {"requirement_ids": ["R001"], "implementation_source_ids": []}]}

    def reply(stage, payload):
        nonlocal implementations
        result = response(stage, payload, recipe)
        if stage == "method_requirements":
            result["requirements"][0]["statement"] += " Preserve HC3 method."
        elif stage == "workflow":
            # The method was assigned solely to the pending helper. Metadata
            # or prose must not make its missing implementation look complete.
            for step in result["steps"]:
                step["requirement_ids"] = []
        elif stage == "implementation":
            implementations += 1
            if implementations > 1:
                # This tests lexical coverage only; scientific review and
                # execution have separate tests and are isolated by verifier.
                result["content"] += "\nhc3 = 1\n"
        return result

    gateway = Gateway(reply)
    with pytest.raises(SkillCreationError) as failure:
        await pipeline(cache, gateway, verify).run()
    assert (failure.value.stage, failure.value.code) == ("implementation", "method_identifiers_lost")
    verify.assert_not_awaited()
    result = await pipeline(cache, gateway, verify).run()
    assert "hc3 = 1" in result.recipe.scripts[0].content
    stages = [stage for stage, _ in gateway.calls]
    assert stages.count("method_requirements") == stages.count("workflow") == 1
    assert stages.count("implementation") == 2
    verify.assert_awaited_once()
