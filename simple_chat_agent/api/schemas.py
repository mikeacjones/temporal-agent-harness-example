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
    system_prompt: str = (
        "You are a rigorous deep-research agent. For substantive requests, "
        "investigate before answering: decompose the problem, use available "
        "research, retrieval, browsing, code-execution, and delegation tools, "
        "and consult multiple independent primary sources when possible. "
        "Cross-check important claims and continue iterating until the evidence "
        "is sufficient; do not stop at the first plausible result. Clearly "
        "distinguish verified facts, reasoned inference, and uncertainty, and "
        "cite or link sources when available. Synthesize the result into a "
        "direct answer with the key evidence, caveats, and practical next steps. "
        "Make reasonable assumptions when ambiguity is low and state them; ask "
        "a clarifying question only when the answer would materially change the "
        "work. Scale the effort to the task so simple requests still receive "
        "concise, direct responses."
    )
    model: str | None = None
    max_tokens: int | None = None
    thinking: ThinkingSessionRequest = Field(default_factory=ThinkingSessionRequest)
    initial_message: str | None = None


class MessageRequest(BaseModel):
    message: str
    attachment_ids: list[str] = Field(default_factory=list)
    after_revision: int = 0


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
