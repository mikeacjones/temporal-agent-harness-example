from __future__ import annotations

import os

from fastapi import HTTPException

from claude_harness.claude_agent import (
    MIN_THINKING_BUDGET_TOKENS,
    ClaudeThinkingConfig,
    ClaudeThinkingMode,
)
from simple_chat_agent.api.anthropic_models import (
    AnthropicModelCatalog,
    default_thinking_mode,
    get_anthropic_model_catalog,
)
from simple_chat_agent.api.schemas import ThinkingSessionRequest


def default_model(model_catalog: AnthropicModelCatalog | None = None) -> str:
    catalog = model_catalog or get_anthropic_model_catalog()
    return catalog.default_model


def good_place_enabled() -> bool:
    return os.environ.get("SIMPLE_CHAT_GOOD_PLACE", "1").lower() in ("1", "true", "yes")


def thinking_config_from_request(
    request: ThinkingSessionRequest,
    *,
    model: str,
    max_tokens: int,
) -> ClaudeThinkingConfig | None:
    if not request.enabled:
        return None
    mode = request.mode or default_thinking_mode_for_model(model)
    if mode == "adaptive":
        return ClaudeThinkingConfig(
            enabled=True,
            mode="adaptive",
            effort=request.effort,
        )
    if max_tokens <= MIN_THINKING_BUDGET_TOKENS:
        raise HTTPException(
            status_code=400,
            detail="max_tokens must be greater than 1024 for extended thinking.",
        )
    budget_tokens = min(
        max(request.budget_tokens, MIN_THINKING_BUDGET_TOKENS),
        max_tokens - 1,
    )
    return ClaudeThinkingConfig(
        enabled=True,
        mode="enabled",
        budget_tokens=budget_tokens,
    )


def default_thinking_mode_for_model(model_id: str) -> ClaudeThinkingMode:
    model = get_anthropic_model_catalog().model_by_id(model_id)
    mode = default_thinking_mode(model)
    return "adaptive" if mode == "adaptive" else "enabled"
