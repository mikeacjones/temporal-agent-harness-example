from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from temporalio import workflow

from agent_harness.activity_options import ActivityOptions
from agent_harness.llm_guards import LlmGuardExecution
from agent_harness.messages import AgentMessage

ProviderActivity = Callable[..., Any]
ProviderStopReason = str


@dataclass
class ProviderRequest:
    system_prompt: str
    model: str
    max_tokens: int
    tools: list[dict]
    chat_history: list[dict]
    stream_id: str | None = None
    stream_sequence: int | None = None


@dataclass
class ProviderResponse:
    id: str
    model: str
    message: dict
    stop_reason: ProviderStopReason | None
    stop_sequence: str | None
    usage: dict
    guard_action: str | None = None
    guard_reason: str | None = None
    stop_details: dict | None = None


class AgentProvider(Protocol):
    """Provider-specific adapter used by the provider-neutral agent loop."""

    @property
    def name(self) -> str:
        pass

    @property
    def activity(self) -> ProviderActivity:
        pass

    @property
    def activity_options(self) -> ActivityOptions:
        pass

    def estimate_request_tokens(
        self,
        *,
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> int:
        pass

    def create_request(
        self,
        *,
        system_prompt: str,
        model: str,
        max_tokens: int,
        tools: list[dict[str, Any]],
        chat_history: list[AgentMessage],
        stream_id: str | None,
        stream_sequence: int | None,
    ) -> ProviderRequest:
        pass

    def request_chat_history(self, request: ProviderRequest) -> list[AgentMessage]:
        pass

    def replace_request_chat_history(
        self,
        request: ProviderRequest,
        chat_history: list[AgentMessage],
    ) -> ProviderRequest:
        pass

    def request_to_dict(self, request: ProviderRequest) -> dict[str, Any]:
        pass

    def request_from_dict(self, request: dict[str, Any]) -> ProviderRequest:
        pass

    def response_to_dict(self, response: ProviderResponse) -> dict[str, Any]:
        pass

    def response_from_dict(self, response: dict[str, Any]) -> ProviderResponse:
        pass

    def response_from_guard_execution(
        self,
        execution: LlmGuardExecution,
        *,
        model: str,
    ) -> ProviderResponse:
        pass

    def response_with_visible_refusal(
        self,
        response: ProviderResponse,
    ) -> ProviderResponse:
        pass

    def response_message(self, response: ProviderResponse) -> AgentMessage:
        pass

    def stop_reason_for_max_turns(self) -> ProviderStopReason:
        pass


def start_provider_activity(
    provider: AgentProvider,
    request: ProviderRequest,
) -> workflow.ActivityHandle[ProviderResponse]:
    return workflow.start_activity(
        provider.activity,
        request,
        summary=provider.name,
        **provider.activity_options.to_execute_activity_kwargs(),
    )
