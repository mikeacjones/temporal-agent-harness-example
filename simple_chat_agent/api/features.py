from __future__ import annotations

import os


def env_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def demo_workspace_mode() -> bool:
    return env_flag("SIMPLE_CHAT_DEMO_WORKSPACE", default=False)


def demo_workspaces_enabled() -> bool:
    if demo_workspace_mode():
        return False
    return env_flag("SIMPLE_CHAT_DEMO_WORKSPACES_ENABLED", default=False)


def github_tools_enabled() -> bool:
    return env_flag(
        "SIMPLE_CHAT_GITHUB_TOOLS_ENABLED",
        default=not demo_workspace_mode(),
    )
