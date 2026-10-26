from app.domain.services.prompts.execution import EXECUTION_PROMPT
from app.domain.services.tools.message import MessageToolkit


def test_execution_prompt_limits_message_ask_user_to_blocking_cases():
    assert "Default to continuing the task independently" in EXECUTION_PROMPT
    assert "Use message_ask_user only when execution is blocked" in EXECUTION_PROMPT
    assert "Do not use message_ask_user for optional preferences" in EXECUTION_PROMPT
    assert "authentication, captcha, verification code" in EXECUTION_PROMPT


def test_message_ask_user_tool_description_is_not_generic_clarification():
    tool = MessageToolkit().get_tool("message_ask_user")

    assert tool is not None
    assert "Use only when execution is blocked" in tool.description
    assert "Do not use for optional preferences" in tool.description
    assert "ask instead of guessing" in tool.description
    assert "First inspect the selected data" in tool.description
    assert "reasonable assumption and continue" not in tool.description


def test_question_schema_supports_reason_and_bounded_options():
    tool = MessageToolkit().get_tool("message_ask_user")
    fields = tool.args_schema.model_fields
    assert "reason" in fields
    assert "options" in fields
