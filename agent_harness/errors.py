from __future__ import annotations

from typing import Any


class UserFacingAgentError(Exception):
    """Expected failure that can be safely shown in product state."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type or type(self).__name__
        self.payload = payload


class UserFacingToolError(UserFacingAgentError):
    """Expected tool failure that should be returned to the model as data."""

    def to_tool_payload(self) -> dict[str, Any]:
        if self.payload is not None:
            return dict(self.payload)
        return {
            "error": self.message,
            "type": self.error_type,
        }
