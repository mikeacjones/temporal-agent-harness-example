from __future__ import annotations

import re
from typing import Literal
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response

from simple_chat_agent.common.store import AppStore, ArtifactRecord


def artifact_response(
    store: AppStore,
    artifact: ArtifactRecord,
    *,
    disposition: Literal["inline", "attachment"],
) -> Response:
    try:
        content = store.read_artifact_bytes(artifact)
    except Exception as err:
        raise HTTPException(status_code=404, detail="Artifact file not found") from err

    return Response(
        content,
        media_type=(
            artifact.mime_type
            if disposition == "attachment"
            else safe_inline_media_type(artifact.mime_type)
        ),
        headers={
            "Content-Disposition": content_disposition(disposition, artifact.name),
            "X-Content-Type-Options": "nosniff",
        },
    )


def content_disposition(disposition: str, filename: str) -> str:
    ascii_filename = re.sub(r'["\\\r\n]+', "_", filename) or "artifact"
    encoded_filename = quote(filename, safe="")
    return (
        f'{disposition}; filename="{ascii_filename}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )


def safe_inline_media_type(mime_type: str) -> str:
    if mime_type == "application/pdf":
        return mime_type
    if mime_type.startswith("image/") and mime_type != "image/svg+xml":
        return mime_type
    if mime_type.startswith("audio/") or mime_type.startswith("video/"):
        return mime_type
    return "text/plain; charset=utf-8"
