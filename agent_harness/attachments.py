from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


AttachmentContentKind = Literal[
    "text",
    "code",
    "markdown",
    "image",
    "pdf",
    "binary",
    "paste",
]


@dataclass(frozen=True)
class AttachmentRef:
    attachment_id: str
    name: str
    mime_type: str
    size_bytes: int
    content_kind: AttachmentContentKind = "binary"
    source: str = "user_attachment"
    text_preview: str = ""
    text_chars: int | None = None
    expires_at: str = ""
    metadata: dict = field(default_factory=dict)


def attachment_ref_from_mapping(value: Any) -> AttachmentRef:
    if isinstance(value, AttachmentRef):
        return value
    if not isinstance(value, dict):
        raise ValueError("Attachment ref must be a dict")
    metadata = dict(value.get("metadata") or {})
    return AttachmentRef(
        attachment_id=str(value.get("attachment_id") or value.get("artifact_id") or ""),
        name=str(value.get("name") or ""),
        mime_type=str(value.get("mime_type") or "application/octet-stream"),
        size_bytes=int(value.get("size_bytes") or 0),
        content_kind=_content_kind(value.get("content_kind") or "binary"),
        source=str(value.get("source") or "user_attachment"),
        text_preview=str(value.get("text_preview") or ""),
        text_chars=(
            int(value["text_chars"])
            if value.get("text_chars") is not None
            else None
        ),
        expires_at=str(value.get("expires_at") or metadata.get("expires_at") or ""),
        metadata=metadata,
    )


def attachment_ref_to_dict(ref: AttachmentRef) -> dict[str, Any]:
    return {
        "attachment_id": ref.attachment_id,
        "name": ref.name,
        "mime_type": ref.mime_type,
        "size_bytes": ref.size_bytes,
        "content_kind": ref.content_kind,
        "source": ref.source,
        "text_preview": ref.text_preview,
        "text_chars": ref.text_chars,
        "expires_at": ref.expires_at,
        "metadata": dict(ref.metadata),
    }


def attachment_manifest_text(attachments: list[AttachmentRef]) -> str:
    if not attachments:
        return ""

    lines = [
        "<attachments>",
        "The user attached the following files to this turn. Use the "
        "read_attachment tool with an attachment_id when you need attachment "
        "contents that are not already visible in the user message.",
        "Attachments are retained for a limited time; if an attachment is "
        "expired or unavailable, explain that clearly to the user.",
        "",
    ]
    for index, attachment in enumerate(attachments, start=1):
        preview = attachment.text_preview.strip()
        lines.append(
            f"{index}. {attachment.name} "
            f"(attachment_id={attachment.attachment_id}, "
            f"mime_type={attachment.mime_type}, "
            f"kind={attachment.content_kind}, "
            f"size_bytes={attachment.size_bytes})"
        )
        if attachment.expires_at:
            lines.append(f"   expires_at: {attachment.expires_at}")
        if preview:
            lines.append(f"   preview: {preview}")
    lines.append("</attachments>")
    return "\n".join(lines)


def _content_kind(value: Any) -> AttachmentContentKind:
    if value in {
        "text",
        "code",
        "markdown",
        "image",
        "pdf",
        "binary",
        "paste",
    }:
        return value
    return "binary"
