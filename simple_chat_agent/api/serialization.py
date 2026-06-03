from __future__ import annotations

import time
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import Response

from simple_chat_agent.common.attachments import (
    attachment_dicts,
    generated_artifacts,
)
from simple_chat_agent.common.store import ArtifactRecord
from simple_chat_agent.worker.workflow import (
    SimpleChatSnapshot,
    TranscriptDeltaResult,
    TranscriptPage,
)


def state_to_dict(
    state: Any,
    *,
    artifacts: list[ArtifactRecord] | None = None,
) -> dict[str, Any]:
    if is_dataclass(state):
        state_dict = asdict(state)
    elif isinstance(state, dict):
        state_dict = dict(state)
    else:
        raise TypeError(f"Unsupported state type: {type(state).__name__}")

    if artifacts is not None:
        state_dict["artifacts"] = artifact_dicts(generated_artifacts(artifacts))
        state_dict["attachments"] = attachment_dicts(artifacts)
    return state_dict


def state_patch_to_dict(state: Any) -> dict[str, Any]:
    state_dict = state_to_dict(state)
    for key in (
        "transcript",
        "transcript_offset",
        "transcript_total",
        "transcript_has_more_before",
        "transcript_length",
        "transcript_revision",
    ):
        state_dict.pop(key, None)
    return state_dict


def snapshot_to_dict(
    snapshot: SimpleChatSnapshot,
    *,
    artifacts: list[ArtifactRecord],
) -> dict[str, Any]:
    state = state_to_dict(snapshot.state, artifacts=artifacts)
    page = transcript_page_to_dict(snapshot.transcript_page)
    state["transcript"] = page["messages"]
    state["transcript_offset"] = page["start"]
    state["transcript_total"] = page["total"]
    state["transcript_has_more_before"] = page["has_more_before"]
    state["transcript_revision"] = max(
        int(state.get("transcript_revision") or 0),
        int(page.get("revision") or 0),
    )
    state["transcript_length"] = page["total"]
    return state


def transcript_page_to_dict(page: TranscriptPage) -> dict[str, Any]:
    return {
        "messages": [
            asdict(message) if is_dataclass(message) else dict(message)
            for message in page.messages
        ],
        "start": page.start,
        "end": page.end,
        "total": page.total,
        "revision": page.transcript_revision,
        "has_more_before": page.start > 0,
    }


def transcript_delta_result_to_dict(
    result: TranscriptDeltaResult,
) -> dict[str, Any]:
    return {
        "from_revision": result.from_revision,
        "to_revision": result.to_revision,
        "needs_snapshot": result.needs_snapshot,
        "transcript_length": result.transcript_length,
        "status": result.status,
        "pending_messages": result.pending_messages,
        "active_message_index": result.active_message_index,
        "state_revision": result.state_revision,
        "deltas": [
            {
                "revision": delta.revision,
                "index": delta.index,
                "message": (
                    asdict(delta.message)
                    if is_dataclass(delta.message)
                    else dict(delta.message)
                ),
            }
            for delta in result.deltas
        ],
    }


def set_transcript_headers(response: Response, state: dict[str, Any]) -> None:
    response.headers["X-Transcript-Start"] = str(state.get("transcript_offset", 0))
    response.headers["X-Transcript-End"] = str(
        int(state.get("transcript_offset", 0)) + len(state.get("transcript", []))
    )
    response.headers["X-Transcript-Total"] = str(
        state.get("transcript_total", len(state.get("transcript", [])))
    )


def record_timing(
    timings: list[tuple[str, float]],
    name: str,
    started: float,
) -> None:
    timings.append((name, (time.perf_counter() - started) * 1000))


def server_timing(timings: list[tuple[str, float]]) -> str:
    return ", ".join(f"{name};dur={duration:.1f}" for name, duration in timings)


def artifact_dicts(artifacts: list[ArtifactRecord]) -> list[dict[str, Any]]:
    return [artifact_dict(artifact) for artifact in artifacts]


def artifact_dict(artifact: ArtifactRecord) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "conversation_id": artifact.conversation_id,
        "workflow_id": artifact.workflow_id,
        "name": artifact.name,
        "mime_type": artifact.mime_type,
        "size_bytes": artifact.size_bytes,
        "created_at": artifact.created_at,
        "metadata": artifact.metadata,
        "view_url": f"/api/artifacts/{artifact.artifact_id}",
        "download_url": f"/api/artifacts/{artifact.artifact_id}/download",
    }


def conversation_title(message: str) -> str:
    normalized = " ".join(message.split())
    if not normalized:
        return "New chat"
    if len(normalized) <= 64:
        return normalized
    return f"{normalized[:61]}..."
