from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent_harness.providers.claude import (
    DEFAULT_THINKING_BUDGET_TOKENS,
    ClaudeThinkingEffort,
    ClaudeThinkingMode,
)

DEFAULT_THINKING_EFFORT: ClaudeThinkingEffort = "max"


class ThinkingSessionRequest(BaseModel):
    enabled: bool = False
    mode: ClaudeThinkingMode | None = None
    budget_tokens: int = DEFAULT_THINKING_BUDGET_TOKENS
    effort: ClaudeThinkingEffort = DEFAULT_THINKING_EFFORT


class CreateSessionRequest(BaseModel):
    system_prompt: str = "You are a concise test chatbot."
    model: str | None = None
    max_tokens: int | None = None
    max_turns: int = 20
    thinking: ThinkingSessionRequest = Field(default_factory=ThinkingSessionRequest)
    initial_message: str | None = None


class MessageRequest(BaseModel):
    message: str
    attachment_ids: list[str] = Field(default_factory=list)


class SteerRequest(MessageRequest):
    mode: Literal["immediate", "after_next_tool_result"] = "immediate"


class AttachmentTextRequest(BaseModel):
    name: str = "pasted-text.txt"
    content: str
    mime_type: str = "text/plain"


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["allow", "always_allow", "deny"]


class McpServerRequest(BaseModel):
    label: str
    server_url: str
    tool_prefix: str
    auth_mode: Literal["none", "bearer", "oauth"] = "none"
    bearer_token: str | None = None


class McpServerEnabledRequest(BaseModel):
    enabled: bool
