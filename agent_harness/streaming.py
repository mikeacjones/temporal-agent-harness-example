import inspect
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Protocol

try:
    from temporalio import activity as temporal_activity
except ImportError:  # pragma: no cover - used by the minimal sandbox Lambda zip.
    temporal_activity = None


@dataclass(frozen=True)
class StreamEvent:
    stream_id: str | None
    tool_name: str | None
    step: str | None
    kind: str
    payload: object
    sequence: int


class StreamSink(Protocol):
    def emit(self, event: StreamEvent) -> Awaitable[None] | None:
        pass


class AgentStreamEventKind(StrEnum):
    AGENT_START = "agent_start"
    AGENT_TEXT_DELTA = "agent_text_delta"
    AGENT_THINKING_START = "agent_thinking_start"
    AGENT_THINKING_DELTA = "agent_thinking_delta"
    AGENT_TOOL_INPUT_START = "agent_tool_input_start"
    AGENT_TOOL_INPUT_DELTA = "agent_tool_input_delta"
    AGENT_TOOL_INPUT_COMPLETE = "agent_tool_input_complete"
    AGENT_COMPLETE = "agent_complete"
    AGENT_CANCELLED = "agent_cancelled"


@dataclass
class StreamContext:
    stream_id: str | None
    tool_name: str | None = None
    step: str | None = None
    _sequence: int = field(default=0, init=False)

    async def emit(self, payload: Any, *, kind: str = "message") -> None:
        sink = _stream_sink
        if sink is None:
            return

        self._sequence += 1
        event = StreamEvent(
            stream_id=self.stream_id,
            tool_name=self.tool_name,
            step=self.step,
            kind=kind,
            payload=payload,
            sequence=self._sequence,
        )

        try:
            result = sink.emit(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            if _raise_stream_errors:
                raise


@dataclass
class AgentStreamWriter:
    stream: StreamContext
    provider: str
    attempt: int | None = None

    @classmethod
    def for_provider(
        cls,
        *,
        stream_id: str | None,
        provider: str,
        step: str | None = None,
        attempt: int | None = None,
    ) -> "AgentStreamWriter":
        return cls(
            stream=StreamContext(
                stream_id=stream_id,
                tool_name="agent",
                step=step,
            ),
            provider=provider,
            attempt=attempt,
        )

    async def agent_started(self, *, sequence: int | None) -> None:
        await self._emit(
            AgentStreamEventKind.AGENT_START,
            {"sequence": sequence},
        )

    async def agent_cancelled(self, *, sequence: int | None) -> None:
        await self._emit(
            AgentStreamEventKind.AGENT_CANCELLED,
            {"sequence": sequence},
        )

    async def agent_completed(
        self,
        *,
        sequence: int | None,
        id: str | None,
        model: str | None,
        stop_reason: str | None,
        stop_details: dict[str, Any] | None,
        text: str,
        usage: dict[str, Any],
    ) -> None:
        await self._emit(
            AgentStreamEventKind.AGENT_COMPLETE,
            {
                "id": id,
                "model": model,
                "sequence": sequence,
                "stop_reason": stop_reason,
                "stop_details": stop_details,
                "text": text,
                "usage": usage,
            },
        )

    async def text_delta(self, *, sequence: int | None, text: str) -> None:
        await self._emit(
            AgentStreamEventKind.AGENT_TEXT_DELTA,
            {"sequence": sequence, "text": text},
        )

    async def thinking_started(
        self,
        *,
        sequence: int | None,
        content_block_index: int,
    ) -> None:
        await self._emit(
            AgentStreamEventKind.AGENT_THINKING_START,
            {
                "sequence": sequence,
                "content_block_index": content_block_index,
            },
        )

    async def thinking_delta(self, *, sequence: int | None, thinking: str) -> None:
        await self._emit(
            AgentStreamEventKind.AGENT_THINKING_DELTA,
            {"sequence": sequence, "thinking": thinking},
        )

    async def tool_input_started(
        self,
        *,
        sequence: int | None,
        content_block_index: int,
        tool_use_id: str | None,
        tool_name: str | None,
        tool_type: str | None,
    ) -> None:
        await self._emit(
            AgentStreamEventKind.AGENT_TOOL_INPUT_START,
            {
                "sequence": sequence,
                "content_block_index": content_block_index,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_type": tool_type,
            },
        )

    async def tool_input_delta(
        self,
        *,
        sequence: int | None,
        content_block_index: int,
        tool_use_id: str | None,
        tool_name: str | None,
        tool_type: str | None,
        partial_json: str,
    ) -> None:
        await self._emit(
            AgentStreamEventKind.AGENT_TOOL_INPUT_DELTA,
            {
                "sequence": sequence,
                "content_block_index": content_block_index,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_type": tool_type,
                "partial_json": partial_json,
            },
        )

    async def tool_input_completed(
        self,
        *,
        sequence: int | None,
        content_block_index: int,
        tool_use_id: str | None,
        tool_name: str | None,
        tool_type: str | None,
        input: Any,
        input_preview: str,
    ) -> None:
        await self._emit(
            AgentStreamEventKind.AGENT_TOOL_INPUT_COMPLETE,
            {
                "sequence": sequence,
                "content_block_index": content_block_index,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_type": tool_type,
                "input": input,
                "input_preview": input_preview,
            },
        )

    async def _emit(
        self,
        kind: AgentStreamEventKind,
        payload: dict[str, Any],
    ) -> None:
        await self.stream.emit(
            {
                "provider": self.provider,
                **payload,
                **_attempt_payload(self.attempt),
            },
            kind=kind.value,
        )


_stream_sink: StreamSink | None = None
_raise_stream_errors = False


def _activity_attempt() -> int | None:
    if temporal_activity is None:
        return None
    try:
        return temporal_activity.info().attempt
    except RuntimeError:
        return None


def _attempt_payload(stream_attempt: int | None) -> dict[str, int]:
    activity_attempt = _activity_attempt()
    payload: dict[str, int] = {}
    if stream_attempt is not None:
        payload["stream_attempt"] = stream_attempt
    if activity_attempt is not None:
        payload["activity_attempt"] = activity_attempt

    if stream_attempt is None:
        if activity_attempt is not None:
            payload["attempt"] = activity_attempt
        return payload

    # Keep the existing UI's single attempt value monotonic across both
    # workflow-level request retries and Temporal activity retries.
    payload["attempt"] = stream_attempt * 1000 + (activity_attempt or 1)
    return payload


def configure_stream_sink(
    sink: StreamSink | None, *, raise_stream_errors: bool = False
) -> None:
    global _stream_sink, _raise_stream_errors
    _stream_sink = sink
    _raise_stream_errors = raise_stream_errors


@dataclass
class EmitStreamEventRequest:
    stream_id: str | None
    tool_name: str | None
    step: str | None
    kind: str
    payload: object


async def emit_stream_event_activity(request: EmitStreamEventRequest) -> None:
    stream = StreamContext(
        stream_id=request.stream_id,
        tool_name=request.tool_name,
        step=request.step,
    )
    await stream.emit(request.payload, kind=request.kind)


if temporal_activity is not None:
    emit_stream_event_activity = temporal_activity.defn(
        name="agent_harness.emit_stream_event"
    )(emit_stream_event_activity)
