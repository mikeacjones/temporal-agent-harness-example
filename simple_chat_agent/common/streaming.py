from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from agent_harness.streaming import StreamEvent, StreamSink

STREAM_DIR = Path(".simple_chat_streams")


def stream_path(stream_id: str) -> Path:
    return STREAM_DIR / f"{stream_id}.jsonl"


class JsonlStreamSink:
    """Local-dev sink: append events to a per-stream JSONL file on disk."""

    def emit(self, event: StreamEvent) -> None:
        if event.stream_id is None:
            return

        STREAM_DIR.mkdir(parents=True, exist_ok=True)
        with stream_path(event.stream_id).open(
            "a",
            encoding="utf-8",
            buffering=1,
        ) as stream:
            stream.write(json.dumps(asdict(event), default=str))
            stream.write("\n")
            stream.flush()


class HttpStreamSink:
    """Deployment sink: POST each event to the API-owned internal stream API.

    Lets the worker run in a separate pod from the API — the API owns stream
    state and serves SSE to browsers. Best-effort: emit() must never break the
    activity, so POST failures are swallowed (the durable truth is the workflow
    state; streaming is visibility only).
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 2.0) -> None:
        self._url = f"{base_url.rstrip('/')}/internal/stream"
        self._token = token
        self._timeout = timeout

    async def emit(self, event: StreamEvent) -> None:
        if event.stream_id is None:
            return
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(
                    self._url,
                    json=asdict(event),
                    headers={"X-Stream-Token": self._token},
                )
        except Exception:
            # Visibility-only; never fail the activity on a streaming hiccup.
            pass


def configured_stream_sink() -> StreamSink:
    """Select the stream sink from the environment.

    Uses the API-owned HTTP endpoint when SIMPLE_CHAT_STREAM_SINK_URL is set
    (deployment); otherwise the local JSONL file sink (local dev).
    """
    base_url = os.environ.get("SIMPLE_CHAT_STREAM_SINK_URL", "").strip()
    if base_url:
        return HttpStreamSink(base_url, os.environ.get("SIMPLE_CHAT_STREAM_TOKEN", ""))
    return JsonlStreamSink()


def stream_event_envelope(
    event: str,
    data: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return {
        "event": event,
        "data": data,
        "idempotency_key": idempotency_key or "",
    }


def append_local_stream_event(
    stream_id: str,
    event: str,
    data: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> None:
    STREAM_DIR.mkdir(parents=True, exist_ok=True)
    path = stream_path(stream_id)
    if idempotency_key and path.exists():
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("idempotency_key") == idempotency_key:
                        return
        except OSError:
            pass

    with path.open("a", encoding="utf-8", buffering=1) as stream:
        stream.write(
            json.dumps(
                stream_event_envelope(
                    event,
                    data,
                    idempotency_key=idempotency_key,
                ),
                default=str,
            )
        )
        stream.write("\n")
        stream.flush()


async def emit_durable_stream_event(
    stream_id: str,
    event: str,
    data: dict[str, Any],
    *,
    idempotency_key: str,
    timeout: float = 10.0,
) -> None:
    base_url = os.environ.get("SIMPLE_CHAT_STREAM_SINK_URL", "").strip()
    if not base_url:
        append_local_stream_event(
            stream_id,
            event,
            data,
            idempotency_key=idempotency_key,
        )
        return

    token = os.environ.get("SIMPLE_CHAT_STREAM_TOKEN", "")
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/internal/stream/event",
            json={
                "stream_id": stream_id,
                "event": event,
                "data": data,
                "idempotency_key": idempotency_key,
            },
            headers={"X-Stream-Token": token},
        )
        response.raise_for_status()
