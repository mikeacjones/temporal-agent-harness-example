from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, cast

from anthropic.types import MessageParam, ToolResultBlockParam

ContextSnapshot = dict[str, Any]


class ContextManager(Protocol):
    async def initialize(self, user_prompt: str) -> None:
        pass

    def restore(self, snapshot: ContextSnapshot) -> None:
        pass

    def snapshot(self) -> ContextSnapshot:
        pass

    async def messages_for_model(self) -> list[MessageParam]:
        pass

    async def record_assistant_message(self, message: MessageParam) -> None:
        pass

    async def record_tool_results(
        self, tool_results: list[ToolResultBlockParam]
    ) -> None:
        pass


ContextManagerFactory = Callable[[], ContextManager]


@dataclass
class SlidingWindowContextManager:
    max_recent_messages: int = 20
    preserve_initial_user_message: bool = True
    clear_old_tool_results: bool = True
    max_tool_result_chars: int | None = None
    _messages: list[MessageParam] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.max_recent_messages < 2:
            raise ValueError("max_recent_messages must be at least 2")
        if (
            self.max_tool_result_chars is not None
            and self.max_tool_result_chars < 1
        ):
            raise ValueError("max_tool_result_chars must be at least 1")

    async def initialize(self, user_prompt: str) -> None:
        self._messages = [MessageParam(role="user", content=user_prompt)]

    def restore(self, snapshot: ContextSnapshot) -> None:
        version = snapshot.get("version")
        if version != 1:
            raise ValueError(f"Unsupported context snapshot version: {version}")

        messages = snapshot.get("messages")
        if not isinstance(messages, list):
            raise ValueError("Context snapshot messages must be a list")

        self._messages = [_message_from_snapshot(message) for message in messages]

    def snapshot(self) -> ContextSnapshot:
        return {
            "version": 1,
            "messages": [_message_to_snapshot(message) for message in self._messages],
        }

    async def messages_for_model(self) -> list[MessageParam]:
        selected = self._selected_messages()
        latest_tool_result_index = _latest_tool_result_index(selected)
        return [
            _message_for_model(
                message,
                clear_tool_results=index != latest_tool_result_index
                and self.clear_old_tool_results,
                max_tool_result_chars=None
                if index == latest_tool_result_index
                else self.max_tool_result_chars,
            )
            for index, message in enumerate(selected)
        ]

    async def record_assistant_message(self, message: MessageParam) -> None:
        self._messages.append(_normalize_message(message))

    async def record_tool_results(
        self, tool_results: list[ToolResultBlockParam]
    ) -> None:
        if not tool_results:
            return

        self._messages.append(
            _normalize_message(MessageParam(role="user", content=tool_results))
        )

    def _selected_messages(self) -> list[MessageParam]:
        if len(self._messages) <= self.max_recent_messages:
            return _drop_orphaned_tool_results(self._messages)

        start_index = len(self._messages) - self.max_recent_messages
        selected = list(self._messages[start_index:])

        if self.preserve_initial_user_message and start_index > 0:
            selected = [self._messages[0], *selected]

        return _drop_orphaned_tool_results(selected)


def _drop_orphaned_tool_results(messages: list[MessageParam]) -> list[MessageParam]:
    selected: list[MessageParam] = []

    for message in messages:
        if _message_has_block_type(message, "tool_result"):
            if not selected or not _message_has_block_type(selected[-1], "tool_use"):
                continue

        selected.append(message)

    return selected


def _latest_tool_result_index(messages: list[MessageParam]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if _message_has_block_type(messages[index], "tool_result"):
            return index

    return None


def _message_for_model(
    message: MessageParam,
    *,
    clear_tool_results: bool,
    max_tool_result_chars: int | None,
) -> MessageParam:
    content = message["content"]
    if isinstance(content, str):
        return MessageParam(role=message["role"], content=content)

    blocks = [
        _block_for_model(
            block,
            clear_tool_results=clear_tool_results,
            max_tool_result_chars=max_tool_result_chars,
        )
        for block in content
    ]
    return MessageParam(role=message["role"], content=blocks)


def _block_for_model(
    block: Any,
    *,
    clear_tool_results: bool,
    max_tool_result_chars: int | None,
) -> Any:
    block_copy = _copy_block(block)
    if not isinstance(block_copy, dict):
        return block_copy

    if block_copy.get("type") != "tool_result":
        return block_copy

    if clear_tool_results:
        return _cleared_tool_result_block(block_copy)

    if max_tool_result_chars is None:
        return block_copy

    content = block_copy.get("content")
    if isinstance(content, str) and len(content) > max_tool_result_chars:
        block_copy["content"] = json.dumps(
            {
                "truncated": True,
                "original_chars": len(content),
                "preview": content[:max_tool_result_chars],
            }
        )

    return block_copy


def _cleared_tool_result_block(block: dict[str, Any]) -> dict[str, Any]:
    tool_use_id = block.get("tool_use_id")
    block["content"] = json.dumps(
        {
            "cleared": True,
            "reason": "Older tool result omitted from model context.",
            "tool_use_id": tool_use_id,
            "tool_result_ref": None
            if tool_use_id is None
            else f"tool_result:{tool_use_id}",
            "original_chars": _content_length(block.get("content")),
        }
    )
    return block


def _content_length(content: Any) -> int | None:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return len(json.dumps(content))
    return None


def _normalize_message(message: MessageParam) -> MessageParam:
    return _message_from_snapshot(_message_to_snapshot(message))


def _message_to_snapshot(message: MessageParam) -> dict[str, Any]:
    content = message["content"]
    if isinstance(content, str):
        snapshot_content: str | list[Any] = content
    else:
        snapshot_content = [_block_to_snapshot(block) for block in content]

    return {
        "role": message["role"],
        "content": snapshot_content,
    }


def _message_from_snapshot(message: Any) -> MessageParam:
    if not isinstance(message, dict):
        raise ValueError("Context snapshot message must be a dict")

    role = message.get("role")
    if role not in ("user", "assistant"):
        raise ValueError(f"Invalid context snapshot role: {role}")

    content = message.get("content")
    if not isinstance(content, str) and not isinstance(content, list):
        raise ValueError("Context snapshot content must be a string or list")

    return MessageParam(role=role, content=content)


def _block_to_snapshot(block: Any) -> dict[str, Any]:
    block_dict = _block_as_mapping(block)
    return {key: value for key, value in block_dict.items()}


def _copy_block(block: Any) -> Any:
    if isinstance(block, dict):
        return dict(cast(Mapping[str, Any], block))
    return block


def _message_has_block_type(message: MessageParam, block_type: str) -> bool:
    content = message["content"]
    if isinstance(content, str):
        return False

    for block in content:
        block_dict = _block_as_mapping(block)
        if block_dict.get("type") == block_type:
            return True

    return False


def _block_as_mapping(block: Any) -> Mapping[str, Any]:
    if isinstance(block, dict):
        return cast(Mapping[str, Any], block)
    if hasattr(block, "to_dict"):
        return cast(Mapping[str, Any], block.to_dict())
    return {}
