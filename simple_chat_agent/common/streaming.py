from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from redis.exceptions import RedisError

from agent_harness.streaming import StreamEvent, StreamSink
from simple_chat_agent.common.redis_streams import RedisStreamStore

STREAM_DIR = Path(".simple_chat_streams")
LOGGER = logging.getLogger(__name__)

_configured_redis_store: RedisStreamStore | None = None


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
    """Compatibility sink: POST events to the API-owned internal endpoint.

    Redis-backed deployments write directly to Redis. This remains available so
    old workers and externally hosted sandbox executors can continue to publish
    during a rolling migration.
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
                response = await client.post(
                    self._url,
                    json=asdict(event),
                    headers={"X-Stream-Token": self._token},
                )
                response.raise_for_status()
        except Exception as error:
            # Visibility-only; never fail the activity on a streaming hiccup.
            LOGGER.warning("HTTP stream event delivery failed: %s", error)


class RedisStreamSink:
    """Deployment sink: append worker events directly to Redis Streams."""

    def __init__(self, store: RedisStreamStore) -> None:
        self.store = store

    async def start(self) -> None:
        await self.store.start()

    async def emit(self, event: StreamEvent) -> None:
        if event.stream_id is None:
            return

        idempotency_key = uuid4().hex
        for attempt in range(3):
            try:
                await self.store.append_event(
                    event.stream_id,
                    "stream",
                    asdict(event),
                    idempotency_key=idempotency_key,
                )
                return
            except (RedisError, OSError, TimeoutError) as error:
                if attempt == 2:
                    LOGGER.warning("Redis stream event delivery failed: %s", error)
                    raise
                await asyncio.sleep(0.05 * (2**attempt))

    async def close(self) -> None:
        await self.store.close()


def configured_stream_sink() -> StreamSink:
    """Select the stream sink from the environment.

    Redis is the deployment backplane. The HTTP sink is retained for rolling
    compatibility, and local development falls back to per-stream JSONL files.
    """
    global _configured_redis_store
    _configured_redis_store = None
    redis_store = RedisStreamStore.from_env(socket_timeout=2.0)
    if redis_store is not None:
        _configured_redis_store = redis_store
        return RedisStreamSink(redis_store)

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
    redis_store = _configured_redis_store
    close_store = False
    if redis_store is None:
        redis_store = RedisStreamStore.from_env(socket_timeout=timeout)
        close_store = redis_store is not None
    if redis_store is not None:
        try:
            await redis_store.append_event(
                stream_id,
                event,
                data,
                idempotency_key=idempotency_key,
            )
        finally:
            if close_store:
                await redis_store.close()
        return

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
