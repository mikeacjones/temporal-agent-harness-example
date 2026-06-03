from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_harness.streaming import StreamContext
from agent_harness.tool_types import ToolType
from agent_harness.tools import ToolContext, ToolResult, tool

from simple_chat_agent.common.attachments import (
    artifact_is_user_attachment,
    attachment_dict,
    decode_text_attachment,
    sniff_attachment_text_kind,
    text_like_content_kind,
)

READ_ATTACHMENT_TOOL = "read_attachment"
MAX_ATTACHMENT_READ_CHARS = 50_000


class AttachmentProvider:
    def __init__(
        self,
        *,
        user_ref: Callable[[], str | None],
        workflow_id: Callable[[], str],
    ) -> None:
        self._user_ref = user_ref
        self._workflow_id = workflow_id

    @tool(
        name=READ_ATTACHMENT_TOOL,
        description=(
            "Read a user-provided attachment for this chat by attachment_id. "
            "Use this when the user attached a file or paste block and you need "
            "its text contents. For non-text files, the tool returns metadata "
            "and explains that direct text extraction is unavailable."
        ),
        tool_type=ToolType.READ,
    )
    async def read_attachment(
        self,
        ctx: ToolContext,
        attachment_id: str,
        max_chars: int = 12_000,
    ) -> ToolResult:
        user_ref = self._user_ref()
        if user_ref is None:
            return ToolResult(
                payload={"error": "Attachment identity context is not available."},
                error=True,
            )

        payload = await ctx.activity(
            _read_attachment_activity,
            args={
                "user_id": user_ref,
                "workflow_id": self._workflow_id(),
                "attachment_id": attachment_id,
                "max_chars": max_chars,
            },
        )
        return ToolResult(payload=payload, error="error" in payload)


async def _read_attachment_activity(
    user_id: str,
    workflow_id: str,
    attachment_id: str,
    max_chars: int,
    *,
    stream: StreamContext,
) -> dict[str, Any]:
    normalized_limit = max(1, min(int(max_chars or 12_000), MAX_ATTACHMENT_READ_CHARS))
    await stream.emit(
        {
            "attachment_id": attachment_id,
            "max_chars": normalized_limit,
        },
        kind="attachment_read_start",
    )

    from simple_chat_agent.common.store import AppStore

    store = AppStore()
    artifact = store.get_artifact(user_id=user_id, artifact_id=attachment_id)
    if artifact is None or not artifact_is_user_attachment(artifact):
        payload = {"error": "Attachment not found.", "attachment_id": attachment_id}
        await stream.emit(payload, kind="attachment_read_error")
        return payload
    if artifact.workflow_id != workflow_id:
        payload = {"error": "Attachment not found.", "attachment_id": attachment_id}
        await stream.emit(payload, kind="attachment_read_error")
        return payload

    attachment = attachment_dict(artifact)
    content_kind = attachment.get("content_kind")
    content_bytes = store.read_artifact_bytes(artifact)
    if not text_like_content_kind(str(content_kind) if content_kind else None):
        detected_kind, detected_mime = sniff_attachment_text_kind(
            name=artifact.name,
            mime_type=artifact.mime_type,
            content=content_bytes,
        )
        if text_like_content_kind(detected_kind):
            content_kind = detected_kind
            attachment["content_kind"] = detected_kind
            attachment["mime_type"] = detected_mime
            metadata = dict(attachment.get("metadata") or {})
            metadata["detected_content_kind"] = detected_kind
            metadata["detected_mime_type"] = detected_mime
            attachment["metadata"] = metadata

    if not text_like_content_kind(str(content_kind) if content_kind else None):
        payload = {
            "attachment": attachment,
            "content_available": False,
            "reason": (
                "This attachment is not a text-like file. Provider-native "
                "inspection may be available for supported models, otherwise "
                "ask the user for a text version."
            ),
        }
        await stream.emit(
            {"attachment_id": attachment_id, "content_available": False},
            kind="attachment_read_complete",
        )
        return payload

    content = decode_text_attachment(
        content_bytes,
        mime_type=str(attachment.get("mime_type") or artifact.mime_type),
    )
    truncated = len(content) > normalized_limit
    payload = {
        "attachment": attachment,
        "content_available": True,
        "content": content[:normalized_limit],
        "truncated": truncated,
        "original_chars": len(content),
        "returned_chars": min(len(content), normalized_limit),
    }
    await stream.emit(
        {
            "attachment_id": attachment_id,
            "content_available": True,
            "truncated": truncated,
            "returned_chars": payload["returned_chars"],
        },
        kind="attachment_read_complete",
    )
    return payload
