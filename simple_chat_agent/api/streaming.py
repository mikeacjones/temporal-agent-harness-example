from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from typing import Any, AsyncIterator
from uuid import uuid4

from fastapi import Request

from simple_chat_agent.common.streaming import stream_path

STREAM_ACTIVE_POLL_INTERVAL_SECONDS = 0.02
STREAM_IDLE_POLL_INTERVAL_SECONDS = 0.5
STREAM_BUFFER_TTL_SECONDS = 1800.0


class StreamBroker:
    def __init__(self) -> None:
        self._buffers: dict[str, dict[str, Any]] = {}

    @property
    def http_enabled(self) -> bool:
        # When a shared stream token is configured, streaming arrives over the
        # API-owned HTTP endpoint and is served from the in-memory buffer.
        # Otherwise (local dev) it is tailed from per-stream files on disk.
        return bool(os.environ.get("SIMPLE_CHAT_STREAM_TOKEN", "").strip())

    def append(self, stream_id: str, event: dict[str, Any]) -> None:
        entry = self._ensure_buffer(stream_id)
        entry["events"].append(event)
        now = time.monotonic()
        entry["updated"] = now
        # Lazily evict whole streams that have gone idle, to bound memory.
        for stale in [
            sid
            for sid, value in self._buffers.items()
            if now - value["updated"] > STREAM_BUFFER_TTL_SECONDS
        ]:
            self._buffers.pop(stale, None)

    def clear(self, stream_id: str) -> None:
        if self.http_enabled:
            self._buffers.pop(stream_id, None)
        else:
            stream_path(stream_id).unlink(missing_ok=True)

    def cursor(self, stream_id: str) -> str:
        if self.http_enabled:
            entry = self._ensure_buffer(stream_id)
            return self._buffer_event_id(entry, len(entry["events"]))

        path = stream_path(stream_id)
        return str(path.stat().st_size if path.exists() else 0)

    async def event_stream(
        self,
        workflow_id: str,
        request: Request,
    ) -> AsyncIterator[str]:
        source = (
            self._buffer_event_stream(workflow_id, request)
            if self.http_enabled
            else self._file_event_stream(workflow_id, request)
        )
        async for chunk in source:
            yield chunk

    def _ensure_buffer(self, stream_id: str) -> dict[str, Any]:
        entry = self._buffers.get(stream_id)
        if entry is None:
            entry = {
                "events": [],
                "generation": uuid4().hex[:12],
                "updated": time.monotonic(),
            }
            self._buffers[stream_id] = entry
        return entry

    @staticmethod
    def _buffer_event_id(entry: dict[str, Any], position: int) -> str:
        return f"{entry['generation']}:{position}"

    @staticmethod
    def _parse_buffer_event_id(
        last_event_id: str | None,
        entry: dict[str, Any],
    ) -> int | None:
        if not last_event_id:
            return None
        generation, separator, position = last_event_id.partition(":")
        if separator != ":" or generation != entry.get("generation"):
            return None
        with suppress(ValueError):
            return max(0, int(position))
        return None

    @staticmethod
    def _sse(event: str, data: Any, *, event_id: str | None = None) -> str:
        prefix = f"id: {event_id}\n" if event_id is not None else ""
        return f"{prefix}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

    def _stream_reconcile_event(
        self,
        workflow_id: str,
        *,
        reason: str,
        event_id: str,
    ) -> str:
        return self._sse(
            "reconcile",
            {
                "workflow_id": workflow_id,
                "reason": reason,
            },
            event_id=event_id,
        )

    async def _file_event_stream(
        self,
        workflow_id: str,
        request: Request,
    ) -> AsyncIterator[str]:
        path = stream_path(workflow_id)
        # Resume from where this EventSource left off (the browser replays its
        # last received id on auto-reconnect, e.g. after a backgrounded tab).
        # Without this the whole stream file is re-sent on every reconnect,
        # which duplicates already-finalized turns in the UI.
        offset = 0
        needs_reconcile = False
        last_event_id = request.headers.get("last-event-id") or request.query_params.get(
            "cursor"
        )
        if last_event_id:
            try:
                offset = max(0, int(last_event_id))
            except ValueError:
                needs_reconcile = True
                offset = path.stat().st_size if path.exists() else 0
            if not path.exists() or offset > path.stat().st_size:
                needs_reconcile = True
                offset = path.stat().st_size if path.exists() else 0
        else:
            offset = path.stat().st_size if path.exists() else 0
            needs_reconcile = True

        if needs_reconcile:
            yield self._stream_reconcile_event(
                workflow_id,
                reason="stream cursor unavailable",
                event_id=str(offset),
            )
            return

        sleep_seconds = STREAM_ACTIVE_POLL_INTERVAL_SECONDS
        while not await request.is_disconnected():
            emitted = False
            if path.exists():
                if offset > path.stat().st_size:
                    offset = path.stat().st_size
                    yield self._stream_reconcile_event(
                        workflow_id,
                        reason="stream cursor reset",
                        event_id=str(offset),
                    )
                    break

                new_lines: list[tuple[str, int]] = []
                with path.open("r", encoding="utf-8") as stream:
                    stream.seek(offset)
                    while True:
                        line = stream.readline()
                        if not line:
                            break
                        new_lines.append((line, stream.tell()))
                    offset = stream.tell()

                for line, position in new_lines:
                    with suppress(json.JSONDecodeError):
                        emitted = True
                        yield self._sse("stream", json.loads(line), event_id=str(position))

            sleep_seconds = (
                STREAM_ACTIVE_POLL_INTERVAL_SECONDS
                if emitted
                else STREAM_IDLE_POLL_INTERVAL_SECONDS
            )
            await asyncio.sleep(sleep_seconds)

    async def _buffer_event_stream(
        self,
        workflow_id: str,
        request: Request,
    ) -> AsyncIterator[str]:
        # Resume by generation-scoped buffer index (the browser replays its
        # last received id). If the generation changed, this web process no
        # longer has the exact missed events and asks the browser to fetch a
        # JSON snapshot.
        entry = self._ensure_buffer(workflow_id)
        events = entry["events"]
        last_event_id = request.headers.get("last-event-id") or request.query_params.get(
            "cursor"
        )
        needs_reconcile = False
        if last_event_id:
            parsed_resume = self._parse_buffer_event_id(last_event_id, entry)
            if parsed_resume is None or parsed_resume > len(events):
                needs_reconcile = True
                resume = len(events)
            else:
                resume = parsed_resume
        else:
            resume = len(events)
            needs_reconcile = True

        if needs_reconcile:
            yield self._stream_reconcile_event(
                workflow_id,
                reason="stream cursor unavailable",
                event_id=self._buffer_event_id(entry, resume),
            )
            return
        cursor_generation = entry["generation"]

        sleep_seconds = STREAM_ACTIVE_POLL_INTERVAL_SECONDS
        while not await request.is_disconnected():
            emitted = False
            entry = self._ensure_buffer(workflow_id)
            events = entry["events"]
            if entry["generation"] != cursor_generation or resume > len(events):
                resume = len(events)
                cursor_generation = entry["generation"]
                yield self._stream_reconcile_event(
                    workflow_id,
                    reason="stream buffer reset",
                    event_id=self._buffer_event_id(entry, resume),
                )
                break

            for index in range(resume, len(events)):
                emitted = True
                yield self._sse(
                    "stream",
                    events[index],
                    event_id=self._buffer_event_id(entry, index + 1),
                )
            resume = len(events)

            sleep_seconds = (
                STREAM_ACTIVE_POLL_INTERVAL_SECONDS
                if emitted
                else STREAM_IDLE_POLL_INTERVAL_SECONDS
            )
            await asyncio.sleep(sleep_seconds)
