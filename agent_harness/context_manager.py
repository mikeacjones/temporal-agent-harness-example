from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .attachments import AttachmentRef
from .messages import AgentMessage, ToolResultBlock

ContextSnapshot = dict
DEFAULT_MAX_CONTEXT_TOKENS = 200_000
DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS = 4_000
DEFAULT_CHARS_PER_TOKEN = 4.0


class ContextManager(Protocol):
    async def initialize(
        self,
        user_prompt: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> None:
        pass

    async def record_user_message(
        self,
        user_prompt: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> None:
        pass

    def restore(self, snapshot: ContextSnapshot) -> None:
        pass

    def snapshot(self) -> ContextSnapshot:
        pass

    async def messages_for_model(
        self,
        token_budget: ContextTokenBudget | None = None,
    ) -> list[AgentMessage]:
        pass

    async def full_messages(self) -> list[dict[str, Any]]:
        pass

    async def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        pass

    def message_count(self) -> int:
        pass

    async def record_assistant_message(self, message: AgentMessage) -> None:
        pass

    async def record_tool_results(
        self, tool_results: list[ToolResultBlock]
    ) -> None:
        pass


ContextManagerFactory = Callable[[], ContextManager]


@dataclass(frozen=True)
class ContextTokenBudget:
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    reserved_output_tokens: int = 4_096
    reserved_input_tokens: int = 0
    safety_margin_tokens: int = DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN

    def __post_init__(self) -> None:
        if self.max_context_tokens < 1:
            raise ValueError("max_context_tokens must be at least 1")
        if self.reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens cannot be negative")
        if self.reserved_input_tokens < 0:
            raise ValueError("reserved_input_tokens cannot be negative")
        if self.safety_margin_tokens < 0:
            raise ValueError("safety_margin_tokens cannot be negative")
        if self.chars_per_token <= 0:
            raise ValueError("chars_per_token must be greater than 0")
        if self.input_token_budget < 1:
            raise ValueError(
                "Context token budget leaves no room for messages; reduce "
                "max_tokens, reserved input, or safety margin"
            )

    @property
    def input_token_budget(self) -> int:
        return (
            self.max_context_tokens
            - self.reserved_output_tokens
            - self.reserved_input_tokens
            - self.safety_margin_tokens
        )
