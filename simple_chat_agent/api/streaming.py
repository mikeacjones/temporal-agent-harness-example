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
TURN_STREAM_REPLAY_LIMIT = 100


class StreamBroker:
    def __init__(self) -> None:
        self._buffers: dict[str, dict[str, Any]] = {}

    @property
    def http_enabled(self) -> bool:
        # When a shared stream token is configured, streaming arrives over the
        # API-owned HTTP endpoint and is served from the in-memory buffer.
        # Otherwise (local dev) it is tailed from per-stream files on disk.
        return bool(os.environ.get("SIMPLE_CHAT_STREAM_TOKEN", "").strip())

    def append(self, stream_id: str, event: dict[str, Any]) -> str:
        return self.append_event(stream_id, "stream", event)

    def append_event(
        self,
        stream_id: str,
        event: str,
        data: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        entry = self._ensure_buffer(stream_id)
        if idempotency_key:
            existing = entry["idempotency"].get(idempotency_key)
            if existing is not None:
                return existing

        entry["events"].append(
            {
                "event": event,
                "data": data,
                "idempotency_key": idempotency_key or "",
            }
        )
        cursor = self._buffer_event_id(entry, len(entry["events"]))
        if idempotency_key:
            entry["idempotency"][idempotency_key] = cursor
        now = time.monotonic()
        entry["updated"] = now
        # Lazily evict whole streams that have gone idle, to bound memory.
        for stale in [
            sid
            for sid, value in self._buffers.items()
            if now - value["updated"] > STREAM_BUFFER_TTL_SECONDS
        ]:
            self._buffers.pop(stale, None)
        return cursor

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

    def replay(
        self,
        stream_id: str,
        *,
        cursor: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        if self.http_enabled:
            return self._buffer_replay(stream_id, cursor=cursor, limit=limit)
        return self._file_replay(stream_id, cursor=cursor, limit=limit)

    async def turn_event_stream(
        self,
        workflow_id: str,
        request: Request,
        *,
        cursor: str,
    ) -> AsyncIterator[dict[str, str]]:
        current_cursor = cursor
        sleep_seconds = STREAM_ACTIVE_POLL_INTERVAL_SECONDS
        while not await request.is_disconnected():
            replay = self._replay_entries(
                workflow_id,
                cursor=current_cursor,
                limit=TURN_STREAM_REPLAY_LIMIT,
            )
            if not replay["replay_available"]:
                yield self._event(
                    "reconcile",
                    {
                        "workflow_id": workflow_id,
                        "reason": replay.get("reason")
                        or "stream replay unavailable",
                    },
                    event_id=replay.get("cursor") or current_cursor,
                )
                return

            entries = replay["entries"]
            for entry in entries:
                current_cursor = entry["id"]
                event = entry["event"]
                data = dict(entry["data"])
                if event == "turn_settled":
                    data["cursor"] = current_cursor
                yield self._event(event, data, event_id=current_cursor)
                if event == "turn_settled":
                    return

            if entries:
                sleep_seconds = STREAM_ACTIVE_POLL_INTERVAL_SECONDS
            else:
                current_cursor = replay.get("cursor") or current_cursor
                sleep_seconds = STREAM_IDLE_POLL_INTERVAL_SECONDS
            await asyncio.sleep(sleep_seconds)

    async def event_stream(
        self,
        workflow_id: str,
        request: Request,
    ) -> AsyncIterator[dict[str, str]]:
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
                "idempotency": {},
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
    def event(
        event: str,
        data: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> dict[str, str]:
        value = {
            "event": event,
            "data": json.dumps(data, default=str),
        }
        if event_id is not None:
            value["id"] = event_id
        return value

    _event = event

    def _replay_entries(
        self,
        workflow_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        if self.http_enabled:
            return self._buffer_replay_entries(workflow_id, cursor=cursor, limit=limit)
        return self._file_replay_entries(workflow_id, cursor=cursor, limit=limit)

    def _file_replay(
        self,
        workflow_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        replay = self._file_replay_entries(workflow_id, cursor=cursor, limit=limit)
        entries = replay.pop("entries", [])
        return {
            **replay,
            "events": [
                entry["data"] for entry in entries if entry["event"] == "stream"
            ],
        }

    def _file_replay_entries(
        self,
        workflow_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        path = stream_path(workflow_id)
        offset = 0
        if cursor:
            with suppress(ValueError):
                offset = max(0, int(cursor))
        if not path.exists():
            if offset == 0:
                return {
                    "entries": [],
                    "cursor": "0",
                    "replay_available": True,
                    "reason": "",
                }
            return {
                "entries": [],
                "cursor": "0",
                "replay_available": False,
                "reason": "stream file unavailable",
            }
        if offset > path.stat().st_size:
            offset = 0

        entries: list[dict[str, Any]] = []
        position = offset
        with path.open("r", encoding="utf-8") as stream:
            stream.seek(offset)
            while len(entries) < limit:
                line = stream.readline()
                if not line:
                    break
                position = stream.tell()
                with suppress(json.JSONDecodeError):
                    entries.append(
                        {
                            "id": str(position),
                            **self._entry_from_json_line(json.loads(line)),
                        }
                    )

        return {
            "entries": entries,
            "cursor": str(position),
            "replay_available": True,
            "reason": "",
        }

    def _buffer_replay(
        self,
        workflow_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        replay = self._buffer_replay_entries(workflow_id, cursor=cursor, limit=limit)
        entries = replay.pop("entries", [])
        return {
            **replay,
            "events": [
                entry["data"] for entry in entries if entry["event"] == "stream"
            ],
        }

    def _buffer_replay_entries(
        self,
        workflow_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        entry = self._buffers.get(workflow_id)
        if entry is None:
            return {
                "entries": [],
                "cursor": "",
                "replay_available": False,
                "reason": "stream buffer unavailable",
            }

        events = entry["events"]
        parsed = self._parse_buffer_event_id(cursor, entry)
        if cursor and parsed is None:
            return {
                "entries": [],
                "cursor": self._buffer_event_id(entry, len(events)),
                "replay_available": False,
                "reason": "stream cursor unavailable",
            }

        start = parsed if parsed is not None else 0
        start = min(max(0, start), len(events))
        end = min(len(events), start + limit)
        return {
            "entries": [
                {
                    "id": self._buffer_event_id(entry, index + 1),
                    "event": events[index]["event"],
                    "data": events[index]["data"],
                }
                for index in range(start, end)
            ],
            "cursor": self._buffer_event_id(entry, end),
            "replay_available": True,
            "reason": "",
        }

    async def _file_event_stream(
        self,
        workflow_id: str,
        request: Request,
    ) -> AsyncIterator[dict[str, str]]:
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
            yield self._event(
                "reconcile",
                {
                    "workflow_id": workflow_id,
                    "reason": "stream cursor unavailable",
                },
                event_id=str(offset),
            )
            return

        sleep_seconds = STREAM_ACTIVE_POLL_INTERVAL_SECONDS
        while not await request.is_disconnected():
            emitted = False
            if path.exists():
                if offset > path.stat().st_size:
                    offset = path.stat().st_size
                    yield self._event(
                        "reconcile",
                        {
                            "workflow_id": workflow_id,
                            "reason": "stream cursor reset",
                        },
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
                        entry = self._entry_from_json_line(json.loads(line))
                        yield self._event(
                            entry["event"],
                            entry["data"],
                            event_id=str(position),
                        )

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
    ) -> AsyncIterator[dict[str, str]]:
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
            yield self._event(
                "reconcile",
                {
                    "workflow_id": workflow_id,
                    "reason": "stream cursor unavailable",
                },
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
                yield self._event(
                    "reconcile",
                    {
                        "workflow_id": workflow_id,
                        "reason": "stream buffer reset",
                    },
                    event_id=self._buffer_event_id(entry, resume),
                )
                break

            for index in range(resume, len(events)):
                emitted = True
                entry_data = dict(events[index]["data"])
                if events[index]["event"] == "turn_settled":
                    entry_data["cursor"] = self._buffer_event_id(entry, index + 1)
                yield self._event(
                    events[index]["event"],
                    entry_data,
                    event_id=self._buffer_event_id(entry, index + 1),
                )
            resume = len(events)

            sleep_seconds = (
                STREAM_ACTIVE_POLL_INTERVAL_SECONDS
                if emitted
                else STREAM_IDLE_POLL_INTERVAL_SECONDS
            )
            await asyncio.sleep(sleep_seconds)

    @staticmethod
    def _entry_from_json_line(value: dict[str, Any]) -> dict[str, Any]:
        if isinstance(value.get("event"), str) and isinstance(value.get("data"), dict):
            return {
                "event": value["event"],
                "data": value["data"],
            }
        return {
            "event": "stream",
            "data": value,
        }
