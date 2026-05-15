from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Mapping, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolParam, ToolResultBlockParam
from temporalio import activity, workflow

from .activity_options import ActivityOptions
from .context_manager import (
    ContextManagerFactory,
    ContextSnapshot,
    SlidingWindowContextManager,
)
from .tools import ToolResult, ToolSet

ClaudeStopReason = Literal[
    "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"
]

DEFAULT_CLAUDE_ACTIVITY_OPTIONS = ActivityOptions(
    start_to_close_timeout=timedelta(minutes=2)
)


@dataclass(frozen=True)
class ContinueAsNewPolicy:
    enabled: bool = True


class ClaudeAgent:
    def __init__(
        self,
        system_prompt: str,
        tools: ToolSet,
        *,
        model: str,
        max_tokens: int = 4096,
        tool_names: list[str] | None = None,
        stream_id: str | None = None,
        activity_options: ActivityOptions | None = None,
        claude_activity_options: ActivityOptions | None = None,
        context_manager_factory: ContextManagerFactory | None = None,
        continue_as_new_policy: ContinueAsNewPolicy | None = None,
    ):
        self._system_prompt = system_prompt
        self._tools = tools
        self._model = model
        self._max_tokens = max_tokens
        self._tool_names = tool_names
        self._stream_id = stream_id
        self._activity_options = activity_options
        self._claude_activity_options = (
            claude_activity_options or DEFAULT_CLAUDE_ACTIVITY_OPTIONS
        )
        self._context_manager_factory: ContextManagerFactory = (
            context_manager_factory or SlidingWindowContextManager
        )
        self._continue_as_new_policy = (
            continue_as_new_policy or ContinueAsNewPolicy()
        )
        self._context = self._context_manager_factory()
        self._context_initialized = False

    async def run(
        self,
        user_prompt: str | None = None,
        *,
        state: ClaudeAgentState | None = None,
        max_turns: int = 20,
    ) -> ClaudeAgentResult:
        if state is None:
            if user_prompt is None:
                raise ValueError("user_prompt is required when state is not provided")
            if self._context_initialized:
                await self._context.record_user_message(user_prompt)
            else:
                await self._context.initialize(user_prompt)
                self._context_initialized = True
            completed_turns = 0
        else:
            self._context.restore(state.context_snapshot)
            self._context_initialized = True
            completed_turns = state.turns

        tool_schemas = self._tools.tool_schemas(self._tool_names)

        for turn in range(completed_turns + 1, max_turns + 1):
            response = await workflow.execute_activity(
                call_claude,
                ClaudeRequest(
                    system_prompt=self._system_prompt,
                    model=self._model,
                    max_tokens=self._max_tokens,
                    tools=[_tool_param_to_dict(tool) for tool in tool_schemas],
                    chat_history=[
                        _message_param_to_dict(message)
                        for message in await self._context.messages_for_model()
                    ],
                ),
                summary="claude",
                **self._claude_activity_options.to_execute_activity_kwargs(),
            )

            response_message = cast(MessageParam, response.message)
            await self._context.record_assistant_message(response_message)

            if response.stop_reason != "tool_use":
                return ClaudeAgentResult(
                    message=response.message,
                    stop_reason=response.stop_reason,
                    turns=turn,
                )

            tool_results = await self._execute_requested_tools(response_message)
            await self._context.record_tool_results(tool_results)
            if self._should_return_continue_as_new():
                return ClaudeAgentResult(
                    message=response.message,
                    stop_reason=response.stop_reason,
                    turns=turn,
                    continuation_state=ClaudeAgentState(
                        context_snapshot=self._context.snapshot(),
                        turns=turn,
                    ),
                )

        return ClaudeAgentResult(
            message={
                "role": "assistant",
                "content": f"Stopped after reaching max_turns={max_turns}.",
            },
            stop_reason="max_tokens",
            turns=max_turns,
        )

    def _should_return_continue_as_new(self) -> bool:
        return (
            self._continue_as_new_policy.enabled
            and workflow.info().is_continue_as_new_suggested()
        )

    async def _execute_tool(self, tool_name: str, **kwargs: Any) -> ToolResult:
        if self._tool_names is not None and tool_name not in self._tool_names:
            return ToolResult(
                payload={"error": f"Tool is not available to this agent: {tool_name}"},
                error=True,
            )
        return await self._tools.execute_tool(
            tool_name,
            kwargs,
            stream_id=self._stream_id,
            activity_options=self._activity_options,
        )

    async def _execute_requested_tools(
        self, message: MessageParam
    ) -> list[ToolResultBlockParam]:
        return await asyncio.gather(
            *[
                self._execute_requested_tool(block)
                for block in _tool_use_blocks(message)
            ]
        )

    async def _execute_requested_tool(
        self, block: dict[str, Any]
    ) -> ToolResultBlockParam:
        tool_name = cast(str, block["name"])
        tool_input = cast(dict[str, Any], block["input"])
        tool_use_id = cast(str, block["id"])

        try:
            result = await self._execute_tool(tool_name, **tool_input)
        except Exception as err:
            result = ToolResult(
                payload={"error": str(err), "type": type(err).__name__},
                error=True,
            )

        return ToolResultBlockParam(
            type="tool_result",
            tool_use_id=tool_use_id,
            content=json.dumps(result.payload),
            is_error=result.error,
        )


@dataclass
class ClaudeAgentState:
    context_snapshot: ContextSnapshot
    turns: int


@dataclass
class ClaudeAgentResult:
    message: dict[str, Any]
    stop_reason: ClaudeStopReason | None
    turns: int
    continuation_state: ClaudeAgentState | None = None

    @property
    def needs_continue_as_new(self) -> bool:
        return self.continuation_state is not None


@dataclass
class ClaudeRequest:
    system_prompt: str
    model: str
    max_tokens: int
    tools: list[dict[str, Any]]
    chat_history: list[dict[str, Any]]


@dataclass
class ClaudeResponse:
    id: str
    model: str
    message: dict[str, Any]
    stop_reason: ClaudeStopReason | None
    stop_sequence: str | None
    usage: dict[str, Any]


@activity.defn
async def call_claude(request: ClaudeRequest) -> ClaudeResponse:
    create_params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": request.system_prompt,
        "messages": request.chat_history,
    }
    if request.tools:
        create_params["tools"] = request.tools

    async with AsyncAnthropic(max_retries=0) as client:
        response = await client.messages.create(**create_params)

    return ClaudeResponse(
        id=response.id,
        model=response.model,
        message={
            "role": response.role,
            "content": [block.to_dict() for block in response.content],
        },
        stop_reason=response.stop_reason,
        stop_sequence=response.stop_sequence,
        usage=response.usage.to_dict(),
    )


def _tool_use_blocks(message: MessageParam) -> list[dict[str, Any]]:
    content = message["content"]
    if isinstance(content, str):
        return []

    blocks: list[dict[str, Any]] = []
    for block in content:
        block_dict = (
            dict(cast(Mapping[str, Any], block))
            if isinstance(block, dict)
            else block.to_dict()
        )
        if block_dict.get("type") == "tool_use":
            blocks.append(block_dict)
    return blocks


def _message_param_to_dict(message: MessageParam) -> dict[str, Any]:
    content = message["content"]
    if isinstance(content, str):
        message_content: str | list[Any] = content
    else:
        message_content = [_block_to_dict(block) for block in content]

    return {
        "role": message["role"],
        "content": message_content,
    }


def _tool_param_to_dict(tool: ToolParam) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], tool))


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(cast(Mapping[str, Any], block))
    return cast(dict[str, Any], block.to_dict())
