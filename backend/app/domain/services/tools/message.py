from ast import And
from typing import List, Optional, Union
from app.domain.services.tools.base import BaseToolkit
from app.domain.models.tool_result import ToolResult
from langchain.tools import tool
from pydantic import ValidationError
from app.domain.models.request_intent import ClarificationQuestion


class MessageToolkit(BaseToolkit):
    """Message tool class, providing message sending functions for user interaction"""

    name: str = "message"
    
    def __init__(self):
        """Initialize message tool class"""
        super().__init__()

    @tool(parse_docstring=True)
    async def message_notify_user(
        self,
        text: str
    ) -> ToolResult:
        """Send a message to user without requiring a response. Use for acknowledging receipt of messages, providing progress updates, reporting task completion, or explaining changes in approach.
        
        Args:
            text: Message text to display to user
        """
            
        # Return success result, actual UI display logic implemented by caller
        return ToolResult(success=True, message="OK")
    
    @tool(parse_docstring=True)
    async def message_ask_user(
        self,
        text: str,
        attachments: Optional[Union[str, List[str]]] = None,
        suggest_user_takeover: Optional[str] = None,
        reason: Optional[str] = None,
        options: Optional[List[str]] = None,
    ) -> ToolResult:
        """Ask user a question and wait for response. Use only when execution is blocked by important ambiguity, missing required information, explicit user confirmation, authentication, verification codes, sensitive actions, or browser takeover. First inspect the selected data and available evidence. If plausible interpretations would materially change the objective, scope, statistical meaning or result and evidence cannot resolve them, ask instead of guessing. Do not use for optional preferences, non-blocking details that tools can verify, or progress updates.
        
        Args:
            text: Question text to present to user
            attachments: (Optional) List of question-related files or reference materials
            suggest_user_takeover: (Optional) Suggested operation for user takeover (enum: "none" or "browser")
            reason: (Optional) Brief explanation of why the answer changes the analysis
            options: (Optional) Two to four concrete alternatives, or omit for a free-text answer
        """
            
        try:
            ClarificationQuestion(id="question", question=text, reason=reason or "", options=options or [])
        except ValidationError:
            return ToolResult(success=False, message="Question or options are invalid", data={"error_code": "invalid_user_question"})
        # The execution boundary persists the question before pausing.
        return ToolResult(success=True)
