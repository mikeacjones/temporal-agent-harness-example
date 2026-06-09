from __future__ import annotations

import copy
import json
from contextlib import suppress
from typing import Any, Mapping, cast

from agent_harness.llm_guards import LlmGuardExecution


def copy_mapping(value: Any) -> dict[str, Any]:
    """Return a JSON-like dict copy, accepting SDK model objects when possible."""
    if isinstance(value, dict):
        return copy.deepcopy(dict(cast(Mapping[str, Any], value)))
    return object_to_dict(value)


def copy_optional_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return copy_mapping(value)


def mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [copy_mapping(item) for item in value]


def object_to_dict(
    value: Any,
    *,
    by_alias: bool = False,
    exclude_none: bool = True,
) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return copy.deepcopy(dict(cast(Mapping[str, Any], value)))
    if hasattr(value, "to_dict"):
        return copy_mapping(value.to_dict())
    if hasattr(value, "model_dump"):
        return cast(
            dict[str, Any],
            value.model_dump(
                mode="json",
                by_alias=by_alias,
                exclude_none=exclude_none,
            ),
        )
    return {}


def optional_object_to_dict(value: Any) -> dict[str, Any] | None:
    result = object_to_dict(value)
    return result or None


def json_object(value: str) -> dict[str, Any]:
    if not value:
        return {}
    with suppress(json.JSONDecodeError):
        decoded = json.loads(value)
        return copy_mapping(decoded) if isinstance(decoded, dict) else {}
    return {}


def json_preview(value: Any, *, max_chars: int = 2_000) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True)
    except TypeError:
        encoded = repr(value)
    if len(encoded) <= max_chars:
        return encoded
    return encoded[-max_chars:]


def provider_metadata(
    *,
    provider: str,
    provider_type: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    return {
        "name": provider,
        "type": provider_type,
        "data": copy_mapping(data),
    }


def provider_data(
    block: Mapping[str, Any],
    *,
    provider: str,
) -> dict[str, Any] | None:
    metadata = block.get("provider")
    if not isinstance(metadata, dict):
        return None
    if metadata.get("name") != provider:
        return None
    data = metadata.get("data")
    return copy_mapping(data) if isinstance(data, dict) else None


def guard_response_dict(
    execution: LlmGuardExecution,
    *,
    model: str,
    message: dict[str, Any],
    stop_reason: str,
) -> dict[str, Any]:
    response = copy_mapping(execution.response) or {
        "id": "guard:llm",
        "model": model,
        "message": message,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {},
    }
    response["guard_action"] = execution.action.value
    response["guard_reason"] = execution.reason
    return response


def non_retryable_http_status(status_code: int | None) -> bool:
    if status_code is None or status_code in {408, 409, 429}:
        return False
    return 400 <= status_code < 500


def needs_refusal_fallback(
    *,
    stop_reason: str | None,
    refusal_stop_reasons: set[str],
    guard_action: str | None,
    response_text: str,
) -> bool:
    if stop_reason not in refusal_stop_reasons:
        return False
    if guard_action is not None:
        return False
    return not response_text.strip()


def refusal_fallback_text(
    *,
    fallback: str,
    stop_details: dict[str, Any] | None,
    details_text: str,
) -> str:
    if not details_text:
        return fallback
    return f"{fallback}\n\n{details_text}"
