from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Mapping, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolParam, ToolResultBlockParam
from temporalio import activity, workflow
from temporalio.exceptions import is_cancelled_exception

from .activity_options import ActivityOptions
from .context_manager import (
    ContextManagerFactory,
    ContextSnapshot,
    ContextTokenBudget,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS,
    DEFAULT_MAX_CONTEXT_TOKENS,
    SlidingWindowContextManager,
    estimate_token_count,
)
from .streaming import StreamContext, stream_sink_configured
from .tools import ToolResult, ToolSet

ClaudeStopReason = Literal[
    "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"
]
SteeringMode = Literal["immediate", "after_next_tool_result"]
InterruptPartialResponsePolicy = Literal["discard"]

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
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        context_safety_margin_tokens: int = DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS,
        context_chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
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
        self._max_context_tokens = max_context_tokens
        self._context_safety_margin_tokens = context_safety_margin_tokens
        self._context_chars_per_token = context_chars_per_token
        self._context_manager_factory: ContextManagerFactory = (
            context_manager_factory or SlidingWindowContextManager
        )
        self._continue_as_new_policy = (
            continue_as_new_policy or ContinueAsNewPolicy()
        )
        self._context = self._context_manager_factory()
        self._context_initialized = False
        self._pending_immediate_steering: list[str] = []
        self._pending_after_tool_steering: list[str] = []
        self._pending_interrupts: list[str] = []
        self._interrupt_requested = False
        self._claude_call_sequence = 0

    def steer(
        self,
        message: str,
        *,
        mode: SteeringMode = "immediate",
    ) -> None:
        if mode == "immediate":
            self._pending_immediate_steering.append(message)
            return
        if mode == "after_next_tool_result":
            self._pending_after_tool_steering.append(message)
            return

        raise ValueError(f"Unknown steering mode: {mode}")

    def interrupt(
        self,
        message: str,
        *,
        partial_response_policy: InterruptPartialResponsePolicy = "discard",
    ) -> None:
        if partial_response_policy != "discard":
            raise ValueError(
                "Only partial_response_policy='discard' is currently supported"
            )

        self._pending_interrupts.append(message)
        self._interrupt_requested = True

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
        turn = completed_turns

        while turn < max_turns:
            if self._interrupt_requested:
                await self._flush_interrupt_context()
            await self._flush_immediate_context()
            response = await self._call_claude(tool_schemas)
            if response is None:
                await self._flush_interrupt_context()
                continue

            turn += 1

            if self._interrupt_requested:
                await self._flush_interrupt_context()
                continue

            response_message = cast(MessageParam, response.message)
            tool_use_blocks = _tool_use_blocks(response_message)
            await self._context.record_assistant_message(response_message)

            if not tool_use_blocks:
                if self._interrupt_requested:
                    await self._flush_interrupt_context()
                    continue

                return ClaudeAgentResult(
                    message=response.message,
                    stop_reason=response.stop_reason,
                    turns=turn,
                )

            tool_results = await self._execute_requested_tools(tool_use_blocks)
            await self._context.record_tool_results(tool_results)
            await self._flush_after_tool_context()

            if self._interrupt_requested:
                await self._flush_interrupt_context()
                continue

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

    async def _call_claude(
        self, tool_schemas: list[ToolParam]
    ) -> ClaudeResponse | None:
        self._claude_call_sequence += 1
        tool_params = [_tool_param_to_dict(tool) for tool in tool_schemas]
        context_budget = ContextTokenBudget(
            max_context_tokens=self._max_context_tokens,
            reserved_output_tokens=self._max_tokens,
            reserved_input_tokens=estimate_token_count(
                {
                    "system": self._system_prompt,
                    "tools": tool_params,
                },
                chars_per_token=self._context_chars_per_token,
            ),
            safety_margin_tokens=self._context_safety_margin_tokens,
            chars_per_token=self._context_chars_per_token,
        )
        claude_handle = workflow.start_activity(
            call_claude,
            ClaudeRequest(
                system_prompt=self._system_prompt,
                model=self._model,
                max_tokens=self._max_tokens,
                tools=tool_params,
                chat_history=[
                    _message_param_to_dict(message)
                    for message in await self._context.messages_for_model(
                        context_budget
                    )
                ],
                stream_id=self._stream_id,
                stream_sequence=self._claude_call_sequence,
            ),
            summary="claude",
            **self._claude_activity_options.to_execute_activity_kwargs(),
        )

        try:
            await workflow.wait_condition(
                lambda: self._interrupt_requested or claude_handle.done()
            )
            if self._interrupt_requested:
                await self._discard_interrupted_claude_call(claude_handle)
                return None

            return await claude_handle
        except BaseException as err:
            if self._interrupt_requested and is_cancelled_exception(err):
                return None
            raise

    async def _discard_interrupted_claude_call(
        self, claude_handle: workflow.ActivityHandle[ClaudeResponse]
    ) -> None:
        if not claude_handle.done():
            claude_handle.cancel()

        try:
            await claude_handle
        except BaseException as err:
            if is_cancelled_exception(err):
                return
            raise

    async def _flush_immediate_context(self) -> None:
        await self._flush_steering_messages(
            self._pending_immediate_steering,
            tag="steering",
            description=(
                "This is out-of-band user steering. Use it to adjust how you "
                "proceed, but do not treat it as a new task."
            ),
        )

    async def _flush_after_tool_context(self) -> None:
        await self._flush_steering_messages(
            self._pending_after_tool_steering,
            tag="steering",
            description=(
                "This is out-of-band user steering supplied after a tool result. "
                "Use it to adjust the next reasoning step."
            ),
        )

    async def _flush_interrupt_context(self) -> None:
        await self._flush_steering_messages(
            self._pending_interrupts,
            tag="interrupt",
            description=(
                "The in-progress assistant response was interrupted by the user. "
                "Discard any uncommitted partial assistant output and use this "
                "new context before continuing."
            ),
        )
        self._interrupt_requested = False

    async def _flush_steering_messages(
        self,
        messages: list[str],
        *,
        tag: str,
        description: str,
    ) -> None:
        while messages:
            await self._context.record_user_message(
                _formatted_control_message(
                    tag=tag,
                    description=description,
                    message=messages.pop(0),
                )
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
        self, tool_use_blocks: list[dict[str, Any]]
    ) -> list[ToolResultBlockParam]:
        return await asyncio.gather(
            *[
                self._execute_requested_tool(block)
                for block in tool_use_blocks
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
    stream_id: str | None = None
    stream_sequence: int | None = None


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
        if request.stream_id is None or not stream_sink_configured():
            response = await _create_claude_message(client, create_params)
        else:
            response = await _stream_claude_message(
                client,
                create_params,
                stream_id=request.stream_id,
                stream_sequence=request.stream_sequence,
            )

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


async def _create_claude_message(
    client: AsyncAnthropic,
    create_params: dict[str, Any],
) -> Any:
    message_task = asyncio.create_task(client.messages.create(**create_params))
    cancel_task = asyncio.create_task(activity.wait_for_cancelled())

    try:
        done, _pending = await asyncio.wait(
            {message_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            message_task.cancel()
            with suppress(asyncio.CancelledError):
                await message_task
            raise asyncio.CancelledError()

        return message_task.result()
    finally:
        cancel_task.cancel()
        with suppress(asyncio.CancelledError):
            await cancel_task


async def _stream_claude_message(
    client: AsyncAnthropic,
    create_params: dict[str, Any],
    *,
    stream_id: str,
    stream_sequence: int | None,
) -> Any:
    stream = StreamContext(stream_id=stream_id, tool_name="claude")
    await stream.emit({"sequence": stream_sequence}, kind="claude_start")

    async with client.messages.stream(**create_params) as message_stream:
        cancel_task = asyncio.create_task(activity.wait_for_cancelled())
        text_iterator = message_stream.text_stream.__aiter__()
        try:
            while True:
                next_text_task = asyncio.create_task(anext(text_iterator))
                done, _pending = await asyncio.wait(
                    {next_text_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if cancel_task in done:
                    next_text_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await next_text_task
                    await stream.emit(
                        {"sequence": stream_sequence},
                        kind="claude_cancelled",
                    )
                    raise asyncio.CancelledError()

                try:
                    text = next_text_task.result()
                except StopAsyncIteration:
                    break

                if text:
                    await stream.emit(
                        {"sequence": stream_sequence, "text": text},
                        kind="claude_text_delta",
                    )
        finally:
            cancel_task.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_task

        response = await message_stream.get_final_message()

    await stream.emit(
        {
            "id": response.id,
            "model": response.model,
            "sequence": stream_sequence,
            "stop_reason": response.stop_reason,
            "usage": response.usage.to_dict(),
        },
        kind="claude_complete",
    )
    return response


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


def _formatted_control_message(
    *,
    tag: str,
    description: str,
    message: str,
) -> str:
    return f"<{tag}>\n{description}\n\n{message}\n</{tag}>"
