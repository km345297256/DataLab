"""Bounded local source corrections preserve raw authoring and all full gates."""
import copy
import json
from types import SimpleNamespace

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import CoverageError
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.utils.model_response import ModelEmptyResponseError, ModelIncompleteResponseError
from test_skill_creation_gateway import make_gateway, requests
from test_skill_creation_source_response import REQUEST, METADATA


OLD = "historic-generated-table.csv"
SOURCE = '''# Header and all unselected source bytes are authoritative.
import json

# This table declares the public result filename.
ARTIFACTS = {"table": "historic-generated-table.csv"}

# Separate fixed method from the output declaration.
# Keep all numeric constants and parameter identities.


def calculate(value):
    total = value + 2
    return total


# No scientific assertions are edited by a source patch.
def main():
    return calculate(3)


if __name__ == "__main__":
    main()
'''


def privacy():
    return PrivacyContext((PrivateValue(OLD, "source_filename"),),
                          generated_output_filenames=frozenset({OLD}))


def setup(responses, *, context=None, native=False):
    gateway, calls, usage = make_gateway([], privacy=context or privacy())
    pending = iter(responses)
    options = []

    async def invoke(messages, **kwargs):
        calls.append(messages)
        options.append(copy.deepcopy(kwargs))
        response = next(pending)
        if callable(response):
            response = response(json.loads(messages[-1].content))
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, AIMessage) else AIMessage(content=json.dumps(response))

    gateway.model = SimpleNamespace(ainvoke=invoke)
    if native:
        gateway.model.capabilities = SimpleNamespace(json_object="adapter")
    return gateway, calls, options, usage


def patch(request, transform=lambda text: text):
    return {"repairs": [{"block_id": block["block_id"], "replacement": transform(block["source"])}
                        for block in request["repair_feedback"]["source_blocks"]]}


def replace_output(request):
    references = request["repair_feedback"].get("details", {}).get("output_repair_references")
    if references is None:
        references = request["repair_feedback"]["source_repair_context"]["feedback"]["details"]["output_repair_references"]
    mapping = {row["reference"]: "public-table.csv" for row in references}
    def transform(source):
        for before, after in mapping.items():
            source = source.replace(before, json.dumps(after))
        return source
    return patch(request, transform)


def check(gateway):
    return lambda value: validate_portable_document(value, gateway.privacy)


@pytest.mark.asyncio
async def test_syntax_then_privacy_uses_new_bound_blocks_and_preserves_unselected_bytes():
    broken = SOURCE.replace("total = value + 2", "total = value +")
    gateway, calls, _, _ = setup([AIMessage(content=broken),
        lambda row: patch(row, lambda part: part.replace("total = value +", "total = value + 2")),
        replace_output])
    visited = []
    def validate(value):
        visited.append(value.content)
        check(gateway)(value)
    result = await gateway.generate(HelperScript, "implementation", "Return complete source preserving all methods.",
                                   {"script_request": REQUEST}, validate)
    rows = requests(calls)
    assert result.content == SOURCE.strip().replace(OLD, "public-table.csv")
    assert len(visited) == 2  # Syntax failure never reaches downstream checks.
    assert rows[1]["repair_feedback"]["reason"] == "script_syntax_invalid"
    assert rows[2]["repair_feedback"]["reason"] == "private_package_content"
    assert rows[1]["repair_feedback"]["source_blocks"] != rows[2]["repair_feedback"]["source_blocks"]
    assert rows[2]["repair_feedback"]["source_repair_context"]["feedback"]["reason"] == "private_package_content"
    assert all(OLD not in json.dumps(row) for row in rows)
    assert "只返回指定 JSON repairs" in calls[1][0].content
    assert "only complete replacements" in rows[1]["repair_feedback"]["hint"]
    assert result.model_dump(exclude={"content"}) == METADATA


@pytest.mark.asyncio
async def test_privacy_selection_includes_every_match_even_if_diagnostics_show_only_one():
    source = SOURCE + '\n# separated second private reference\nSECOND = "' + OLD + '"\n'
    gateway, calls, _, _ = setup([AIMessage(content=source), replace_output])
    def truncated_diagnostics(value):
        issues = gateway.privacy.scan_document(value)
        if issues:
            raise SkillPackageValidationError("private_package_content",
                details={"privacy_issues": [issues[0].as_dict()]})
    result = await gateway.generate(HelperScript, "implementation", "Preserve methods.",
                                   {"script_request": REQUEST}, truncated_diagnostics)
    row = requests(calls)[1]
    assert len(row["repair_feedback"]["details"]["privacy_issues"]) == 1
    assert len(row["repair_feedback"]["source_blocks"]) == 2
    assert result.content.count("public-table.csv") == 2 and OLD not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [AIMessage(content="{"), AIMessage(content=""),
    AIMessage(content='{"repairs":', response_metadata={"finish_reason": "length"}),
    ModelEmptyResponseError(), ModelIncompleteResponseError("max_tokens"),
    {"repairs": [{"block_id": "B001", "replacement": 42}]}],
    ids=["malformed", "empty", "length", "driver-empty", "driver-length", "wrong-value-type"])
async def test_unusable_patch_keeps_original_scope_diagnostic_and_output_roles(failure, monkeypatch):
    import app.domain.services.skills.skill_creation.script_authoring as authoring
    async def forbidden(*args, **kwargs):
        pytest.fail("A patch truncation must not restart full modular authoring")
    monkeypatch.setattr(authoring, "generate_modular_script", forbidden)
    gateway, calls, _, _ = setup([AIMessage(content=SOURCE), failure, replace_output])
    result = await gateway.generate(HelperScript, "implementation", "Preserve methods.",
                                   {"script_request": REQUEST}, check(gateway))
    first, second = requests(calls)[1:]
    assert first["previous_candidate"] == second["previous_candidate"]
    assert first["schema"] == second["schema"]
    assert first["repair_feedback"]["source_blocks"] == second["repair_feedback"]["source_blocks"]
    context = second["repair_feedback"]["source_repair_context"]
    assert context == first["repair_feedback"]["source_repair_context"]
    assert context["feedback"]["details"]["output_repair_references"][0]["role"] == "generated_output"
    assert "source_repair_context" not in context["feedback"]
    assert OLD not in json.dumps(requests(calls))
    assert result.content == SOURCE.strip().replace(OLD, "public-table.csv")


@pytest.mark.asyncio
async def test_valid_merge_then_domain_failure_replaces_patch_with_latest_source_context():
    clean = SOURCE.strip().replace(OLD, "public-table.csv")
    final = clean.replace("value + 2", "value + 3")
    gateway, calls, _, _ = setup([AIMessage(content=SOURCE), replace_output,
                                 ModelEmptyResponseError(), AIMessage(content=final)])
    def validate(value):
        check(gateway)(value)
        if "value + 2" in value.content:
            raise CoverageError("method_identifiers_lost", ["R001"])
    result = await gateway.generate(HelperScript, "implementation", "Preserve method.",
                                   {"script_request": REQUEST}, validate)
    second, malformed = requests(calls)[2:]
    assert second["response_format"]["type"] == "python_source"
    assert "schema" not in second and "source_blocks" not in second["repair_feedback"]
    assert "source_repair_context" not in second["repair_feedback"]
    assert second["previous_candidate"]["content"] == clean
    assert malformed["previous_candidate"] == second["previous_candidate"]
    assert malformed["repair_feedback"]["prior_unrechecked_feedback"] == second["repair_feedback"]
    assert result.content == final


@pytest.mark.asyncio
async def test_source_patch_does_not_expand_same_diagnostic_or_total_budget():
    gateway, calls, _, _ = setup([AIMessage(content=SOURCE), AIMessage(content="{"),
                                 AIMessage(content="{"), replace_output])
    with pytest.raises(SkillCreationError) as error:
        await gateway.generate(HelperScript, "implementation", "Preserve method.",
                               {"script_request": REQUEST}, check(gateway))
    assert error.value.code == "invalid_json" and len(calls) == 3
    assert gateway.repair_policy.max_generations == 4 and gateway.repair_policy.max_same_diagnostic == 2


@pytest.mark.asyncio
async def test_known_scientific_negative_never_becomes_source_patch():
    gateway, calls, _, _ = setup([AIMessage(content=SOURCE), replace_output])
    def rejected(value):
        raise CoverageError("method_review_rejected", ["R001"])
    with pytest.raises(SkillCreationError) as error:
        await gateway.generate(HelperScript, "implementation", "Preserve method.",
                               {"script_request": REQUEST}, rejected)
    assert error.value.code == "method_review_rejected" and len(calls) == 1


@pytest.mark.asyncio
async def test_json_mode_changes_only_patch_and_returns_to_raw_on_domain_reauthoring():
    clean = SOURCE.strip().replace(OLD, "public-table.csv")
    final = clean.replace("value + 2", "value + 3")
    gateway, calls, options, _ = setup([AIMessage(content=SOURCE), replace_output, AIMessage(content=final)], native=True)
    policy_options = {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": "high"}
    gateway.model_policy = SimpleNamespace(capacity=None, invocation_options=lambda: copy.deepcopy(policy_options))
    def validate(value):
        check(gateway)(value)
        if "value + 2" in value.content:
            raise CoverageError("method_identifiers_lost", ["R001"])
    await gateway.generate(HelperScript, "implementation", "Preserve method.", {"script_request": REQUEST}, validate)
    assert [row["response_format"] for row in options] == [None, {"type": "json_object"}, None]
    assert all({key: row[key] for key in policy_options} == policy_options for row in options)
    assert all("max_tokens" not in row and "max_output_tokens" not in row for row in options)
    rows = requests(calls)
    assert "schema" not in rows[0] and "response_format" not in rows[1] and "schema" not in rows[2]
    assert "只返回一个完整 Python 源文件" in calls[0][0].content
    assert "只返回一个完整 Python 源文件" not in calls[1][0].content


@pytest.mark.asyncio
async def test_source_patch_mode_reaches_real_sdk_wire_with_native_capacity_and_thinking():
    """Production driver + SDK; only HTTP is replaced by an offline transport."""
    import httpx
    from app.domain.services.skills.skill_creation.model_policy import resolve_skill_model_policy
    from app.infrastructure.external.llm.chat_model import create_chat_model
    from test_model_driver import RecordingMiddleware
    from test_skill_creation_deepseek_thinking import configured

    policy = resolve_skill_model_policy(configured(), {})
    middleware = RecordingMiddleware()
    middleware.output = 393216
    model = create_chat_model(policy.settings, request_middleware=middleware)
    captured = []
    clean = SOURCE.strip().replace(OLD, "public-table.csv")
    final = clean.replace("value + 2", "value + 3")

    def transport(request):
        body = json.loads(request.content)
        captured.append(body)
        prompt = json.loads(next(message["content"] for message in body["messages"]
                                 if message["role"] == "user" and message["content"].startswith("{")))
        content = SOURCE if len(captured) == 1 else json.dumps(replace_output(prompt)) if len(captured) == 2 else final
        return httpx.Response(200, json={"id": "offline-source-repair", "object": "chat.completion",
            "created": 1, "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}})

    gateway, _, _, _ = setup([])
    gateway.model, gateway.model_policy = model, policy
    def validate(value):
        check(gateway)(value)
        if "value + 2" in value.content:
            raise CoverageError("method_identifiers_lost", ["R001"])
    await model._client.root_async_client._client.aclose()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http_client:
            model._client.root_async_client._client = http_client
            result = await gateway.generate(HelperScript, "implementation", "Return complete source.",
                                            {"script_request": REQUEST}, validate)
    finally:
        model._client.root_client.close()
        await model._client.root_async_client.close()
    assert result.content == final and len(captured) == 3
    assert "response_format" not in captured[0] and "response_format" not in captured[2]
    assert captured[1]["response_format"] == {"type": "json_object"}
    assert all(row["max_tokens"] == 393216 and row["thinking"] == {"type": "enabled"}
               and row["reasoning_effort"] == "high" for row in captured)
    assert model._client.extra_body == {"thinking": {"type": "disabled"}}
    assert "reasoning_content" not in json.dumps(captured)
