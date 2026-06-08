from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_harness.tool_types import ToolType
from agent_harness.tools import ToolContext, ToolResult, tool


WORKSPACE_ROOT_ENV = "BASIC_FILE_AGENT_WORKSPACE"
DEFAULT_WORKSPACE_ROOT = "./basic_file_agent_workspace"
DEFAULT_MAX_READ_CHARS = 20_000


@tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file from the agent workspace. The path must be "
        "relative to the workspace root."
    ),
    tool_type=ToolType.READ,
)
async def read_file(
    ctx: ToolContext,
    path: str,
    max_chars: int = DEFAULT_MAX_READ_CHARS,
) -> ToolResult:
    payload = await ctx.activity(
        _read_file_activity,
        args={"path": path, "max_chars": max_chars},
    )
    return ToolResult(payload=payload, error=False)


@tool(
    name="write_file",
    description=(
        "Write UTF-8 text to a file in the agent workspace. The path must be "
        "relative to the workspace root. Existing files are overwritten."
    ),
    tool_type=ToolType.MUTATING,
)
async def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    payload = await ctx.activity(
        _write_file_activity,
        args={
            "path": path,
            "content": content,
            "idempotency_key": ctx.idempotency_key(path, content),
        },
    )
    return ToolResult(payload=payload, error=False)


async def _read_file_activity(path: str, max_chars: int) -> dict[str, Any]:
    root, resolved = _workspace_path(path)
    if not resolved.exists():
        return {
            "path": _relative_path(root, resolved),
            "exists": False,
            "content": "",
            "truncated": False,
        }
    if not resolved.is_file():
        raise ValueError(f"Workspace path is not a file: {path}")

    content = resolved.read_text(encoding="utf-8")
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]

    return {
        "path": _relative_path(root, resolved),
        "exists": True,
        "content": content,
        "truncated": truncated,
        "chars": len(content),
    }


async def _write_file_activity(
    path: str,
    content: str,
    idempotency_key: str,
) -> dict[str, Any]:
    root, resolved = _workspace_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return {
        "path": _relative_path(root, resolved),
        "bytes": len(content.encode("utf-8")),
        "idempotency_key": idempotency_key,
    }


def _workspace_path(path: str) -> tuple[Path, Path]:
    requested = Path(path)
    if requested.is_absolute():
        raise ValueError("Path must be relative to the workspace root")

    root = Path(os.environ.get(WORKSPACE_ROOT_ENV, DEFAULT_WORKSPACE_ROOT)).resolve()
    resolved = (root / requested).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Path must stay inside the workspace root")

    return root, resolved


def _relative_path(root: Path, resolved: Path) -> str:
    return resolved.relative_to(root).as_posix()
