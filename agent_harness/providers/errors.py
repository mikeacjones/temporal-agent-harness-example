from __future__ import annotations


def status_error_is_context_window_exceeded(
    status_code: int | None,
    message: str,
) -> bool:
    if status_code not in {400, 413}:
        return False
    return looks_like_context_window_error(message)


def looks_like_context_window_error(message: str) -> bool:
    text = message.lower()
    context_markers = (
        "context length",
        "context window",
        "maximum context",
        "prompt is too long",
        "prompt too long",
        "prompt exceeds",
        "input is too long",
        "input too long",
        "input tokens",
        "messages are too long",
        "request too large",
    )
    if any(marker in text for marker in context_markers):
        return True
    if "token" not in text and "tokens" not in text:
        return False
    if "max_tokens" in text or "max_output_tokens" in text:
        return False
    return any(marker in text for marker in ("prompt", "input", "context", "messages"))


def counted_tokens_exceed_context(
    *,
    input_tokens: int,
    max_output_tokens: int,
    context_token_limit: int | None,
) -> bool:
    if context_token_limit is None:
        return False
    return input_tokens + max_output_tokens > context_token_limit


def counted_context_window_message(
    *,
    provider: str,
    input_tokens: int,
    max_output_tokens: int,
    context_token_limit: int,
) -> str:
    return (
        f"{provider} counted {input_tokens} input tokens plus "
        f"{max_output_tokens} reserved output tokens, exceeding the configured "
        f"context limit of {context_token_limit}."
    )
