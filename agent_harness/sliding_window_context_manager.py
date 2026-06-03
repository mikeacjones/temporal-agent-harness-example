from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, cast

from .attachments import AttachmentRef, attachment_manifest_text
from .context_manager import (
    ContextSnapshot,
    ContextTokenBudget,
    DEFAULT_CHARS_PER_TOKEN,
)
from .messages import (
    AgentBlock,
    AgentMessage,
    AgentRole,
    ToolResultBlock,
    block_to_dict,
    message,
    normalize_message,
    text_block,
)


@dataclass
class SlidingWindowContextManager:
    max_recent_messages: int = 20
    preserve_initial_user_message: bool = True
    # Budget-driven retention: keep every tool result in full and let
    # messages_for_model's token-budget path shed the oldest/largest only when
    # actually over budget. Set True to eagerly stub every tool result except
    # the most recent one (the old aggressive behavior).
    clear_old_tool_results: bool = False
    max_tool_result_chars: int | None = None
    _messages: list[AgentMessage] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.max_recent_messages < 2:
            raise ValueError("max_recent_messages must be at least 2")
        if (
            self.max_tool_result_chars is not None
            and self.max_tool_result_chars < 1
        ):
            raise ValueError("max_tool_result_chars must be at least 1")

    async def initialize(
        self,
        user_prompt: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> None:
        self._messages = []
        await self.record_user_message(user_prompt, attachments=attachments)

    async def record_user_message(
        self,
        user_prompt: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> None:
        self._messages.append(
            message("user", _user_message_content(user_prompt, attachments or []))
        )

    def restore(self, snapshot: ContextSnapshot) -> None:
        version = snapshot.get("version")
        if version != 2:
            raise ValueError(f"Unsupported context snapshot version: {version}")

        messages = snapshot.get("messages")
        if not isinstance(messages, list):
            raise ValueError("Context snapshot messages must be a list")

        self._messages = [_message_from_snapshot(message) for message in messages]

    def snapshot(self) -> ContextSnapshot:
        return {
            "version": 2,
            "messages": [_message_to_snapshot(message) for message in self._messages],
        }

    async def messages_for_model(
        self,
        token_budget: ContextTokenBudget | None = None,
    ) -> list[AgentMessage]:
        selected = self._selected_messages()
        latest_tool_result_index = _latest_tool_result_index(selected)
        messages = [
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
        if token_budget is None:
            return messages

        return _fit_messages_to_token_budget(
            messages,
            token_budget,
            preserve_first_message=self.preserve_initial_user_message,
        )

    async def full_messages(self) -> list[dict[str, Any]]:
        """The complete durable history (un-windowed, un-cleared) as dicts.

        Guards receive this so they can inspect/mutate the whole conversation;
        windowing/tool-clearing happen afterward in messages_for_model purely
        for the outbound request.
        """
        return [_message_to_snapshot(message) for message in self._messages]

    async def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace the durable history (e.g. with a guard-censored version)."""
        self._messages = [_message_from_snapshot(message) for message in messages]

    def message_count(self) -> int:
        return len(self._messages)

    async def record_assistant_message(self, message: AgentMessage) -> None:
        self._messages.append(_normalize_message(message))

    async def record_tool_results(
        self, tool_results: list[ToolResultBlock]
    ) -> None:
        if not tool_results:
            return

        self._messages.append(
            _normalize_message(
                message("user", [block_to_dict(block) for block in tool_results])
            )
        )

    def _selected_messages(self) -> list[AgentMessage]:
        if len(self._messages) <= self.max_recent_messages:
            return _drop_incomplete_tool_exchanges(self._messages)

        start_index = len(self._messages) - self.max_recent_messages
        selected = list(self._messages[start_index:])

        if self.preserve_initial_user_message and start_index > 0:
            selected = [self._messages[0], *selected]

        return _drop_incomplete_tool_exchanges(selected)


def _drop_incomplete_tool_exchanges(
    messages: list[AgentMessage],
) -> list[AgentMessage]:
    selected: list[AgentMessage] = []
    index = 0

    while index < len(messages):
        message = messages[index]
        tool_use_ids = _tool_use_ids(message)

        if tool_use_ids:
            next_message = messages[index + 1] if index + 1 < len(messages) else None
            if next_message is None or not _has_tool_results_for(
                next_message,
                tool_use_ids,
            ):
                text_only_message = _without_tool_use_blocks(message)
                if text_only_message is not None:
                    selected.append(text_only_message)
                index += 1
                continue

            selected.append(message)
            selected.append(next_message)
            index += 2
            continue

        if _message_has_block_type(message, "tool_result"):
            index += 1
            continue

        selected.append(message)
        index += 1

    return selected


def _user_message_content(
    user_prompt: str,
    attachments: list[AttachmentRef],
) -> str | list[AgentBlock]:
    manifest = attachment_manifest_text(attachments)
    if not manifest:
        return user_prompt

    blocks: list[AgentBlock] = []
    if user_prompt:
        blocks.append(text_block(user_prompt))
    blocks.append(text_block(manifest))
    return blocks


def _has_tool_results_for(
    message: AgentMessage,
    tool_use_ids: set[str],
) -> bool:
    return tool_use_ids.issubset(_tool_result_ids(message))


def _tool_use_ids(message: AgentMessage) -> set[str]:
    return _block_ids(message, "tool_use", "id")


def _tool_result_ids(message: AgentMessage) -> set[str]:
    return _block_ids(message, "tool_result", "tool_use_id")


def _block_ids(
    message: AgentMessage,
    block_type: str,
    id_key: str,
) -> set[str]:
    content = message["content"]
    if isinstance(content, str):
        return set()

    ids: set[str] = set()
    for block in content:
        block_dict = _block_as_mapping(block)
        if block_dict.get("type") == block_type:
            block_id = block_dict.get(id_key)
            if isinstance(block_id, str):
                ids.add(block_id)

    return ids


def _without_tool_use_blocks(message: AgentMessage) -> AgentMessage | None:
    content = message["content"]
    if isinstance(content, str):
        return message

    blocks = [
        _copy_block(block)
        for block in content
        if _block_as_mapping(block).get("type") != "tool_use"
    ]
    if not blocks:
        return None

    return _message_for_role(message, blocks)


def _latest_tool_result_index(messages: list[AgentMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if _message_has_block_type(messages[index], "tool_result"):
            return index

    return None


def _fit_messages_to_token_budget(
    messages: list[AgentMessage],
    token_budget: ContextTokenBudget,
    *,
    preserve_first_message: bool,
) -> list[AgentMessage]:
    if _estimated_tokens(messages, token_budget) <= token_budget.input_token_budget:
        return messages

    groups = _message_groups(messages)
    prefix: list[list[AgentMessage]] = []
    if preserve_first_message and groups:
        prefix = [groups.pop(0)]

    while len(groups) > 1:
        candidate = _flatten_message_groups([*prefix, *groups])
        if _estimated_tokens(candidate, token_budget) <= token_budget.input_token_budget:
            return candidate
        groups.pop(0)

    compacted = _flatten_message_groups([*prefix, *groups])
    compacted = _truncate_tool_results_to_budget(compacted, token_budget)
    if _estimated_tokens(compacted, token_budget) <= token_budget.input_token_budget:
        return compacted

    return _truncate_text_to_budget(compacted, token_budget)


def _message_groups(messages: list[AgentMessage]) -> list[list[AgentMessage]]:
    groups: list[list[AgentMessage]] = []
    index = 0

    while index < len(messages):
        message = messages[index]
        tool_use_ids = _tool_use_ids(message)
        if tool_use_ids and index + 1 < len(messages):
            next_message = messages[index + 1]
            if _has_tool_results_for(next_message, tool_use_ids):
                groups.append([message, next_message])
                index += 2
                continue

        groups.append([message])
        index += 1

    return groups


def _flatten_message_groups(
    groups: list[list[AgentMessage]],
) -> list[AgentMessage]:
    return [message for group in groups for message in group]


def _truncate_tool_results_to_budget(
    messages: list[AgentMessage],
    token_budget: ContextTokenBudget,
) -> list[AgentMessage]:
    compacted = [_normalize_message(message) for message in messages]

    while _estimated_tokens(compacted, token_budget) > token_budget.input_token_budget:
        location = _largest_tool_result_location(compacted)
        if location is None:
            return compacted

        message_index, block_index, original = location
        excess_tokens = (
            _estimated_tokens(compacted, token_budget)
            - token_budget.input_token_budget
        )
        excess_chars = math.ceil(excess_tokens * token_budget.chars_per_token)
        target_chars = max(0, min(len(original) - 1, len(original) - excess_chars - 500))
        if target_chars >= len(original):
            target_chars = max(0, len(original) // 2)

        truncated = _truncated_payload(
            original,
            preview_chars=target_chars,
            reason="Tool result truncated to fit model context budget.",
        )
        if len(truncated) >= len(original):
            return compacted

        _set_block_content(compacted[message_index], block_index, truncated)

    return compacted


def _truncate_text_to_budget(
    messages: list[AgentMessage],
    token_budget: ContextTokenBudget,
) -> list[AgentMessage]:
    compacted = [_normalize_message(message) for message in messages]

    while _estimated_tokens(compacted, token_budget) > token_budget.input_token_budget:
        location = _largest_text_location(compacted)
        if location is None:
            return compacted

        message_index, block_index, original = location
        excess_tokens = (
            _estimated_tokens(compacted, token_budget)
            - token_budget.input_token_budget
        )
        excess_chars = math.ceil(excess_tokens * token_budget.chars_per_token)
        target_chars = max(0, min(len(original) - 1, len(original) - excess_chars - 500))
        if target_chars >= len(original):
            target_chars = max(0, len(original) // 2)

        truncated_text = _truncated_text(
            original,
            preview_chars=target_chars,
            reason="Message text truncated to fit model context budget.",
        )
        if len(truncated_text) >= len(original):
            return compacted

        if block_index is None:
            compacted[message_index] = _message_for_role(
                compacted[message_index],
                truncated_text,
            )
        else:
            _set_block_content(compacted[message_index], block_index, truncated_text)

    return compacted


def _largest_tool_result_location(
    messages: list[AgentMessage],
) -> tuple[int, int, str] | None:
    largest: tuple[int, int, str] | None = None

    for message_index, message in enumerate(messages):
        content = message["content"]
        if isinstance(content, str):
            continue
        for block_index, block in enumerate(content):
            block_dict = _block_as_mapping(block)
            if block_dict.get("type") != "tool_result":
                continue
            block_content = block_dict.get("content")
            if not isinstance(block_content, str):
                continue
            if largest is None or len(block_content) > len(largest[2]):
                largest = (message_index, block_index, block_content)

    return largest


def _largest_text_location(
    messages: list[AgentMessage],
) -> tuple[int, int | None, str] | None:
    largest: tuple[int, int | None, str] | None = None

    for message_index, message in enumerate(messages):
        content = message["content"]
        if isinstance(content, str):
            if largest is None or len(content) > len(largest[2]):
                largest = (message_index, None, content)
            continue

        for block_index, block in enumerate(content):
            block_dict = _block_as_mapping(block)
            if block_dict.get("type") != "text":
                continue
            text = block_dict.get("text")
            if not isinstance(text, str):
                continue
            if largest is None or len(text) > len(largest[2]):
                largest = (message_index, block_index, text)

    return largest


def _set_block_content(
    message: AgentMessage,
    block_index: int,
    content: str,
) -> None:
    blocks = message["content"]
    if isinstance(blocks, str):
        raise TypeError("Cannot set block content on a string message")

    block = _copy_block(blocks[block_index])
    if not isinstance(block, dict):
        raise TypeError("Cannot set content on a non-dict block")
    if block.get("type") == "text":
        block["text"] = content
    else:
        block["content"] = content
    blocks[block_index] = block


def _truncated_payload(
    content: str,
    *,
    preview_chars: int,
    reason: str,
) -> str:
    return json.dumps(
        {
            "truncated": True,
            "reason": reason,
            "original_chars": len(content),
            "preview": content[:preview_chars],
        }
    )


def _truncated_text(
    content: str,
    *,
    preview_chars: int,
    reason: str,
) -> str:
    return (
        f"[{reason} Original chars: {len(content)}.]\n\n"
        f"{content[:preview_chars]}"
    )


def estimate_token_count(
    value: Any,
    *,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
) -> int:
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be greater than 0")
    return math.ceil(len(json.dumps(value, separators=(",", ":"))) / chars_per_token)


def _estimated_tokens(
    messages: list[AgentMessage],
    token_budget: ContextTokenBudget,
) -> int:
    return estimate_token_count(
        [_message_to_snapshot(message) for message in messages],
        chars_per_token=token_budget.chars_per_token,
    )


def _message_for_model(
    message: AgentMessage,
    *,
    clear_tool_results: bool,
    max_tool_result_chars: int | None,
) -> AgentMessage:
    content = message["content"]
    if isinstance(content, str):
        return _message_for_role(message, content)

    blocks = [
        _block_for_model(
            block,
            clear_tool_results=clear_tool_results,
            max_tool_result_chars=max_tool_result_chars,
        )
        for block in content
    ]
    return _message_for_role(message, blocks)


def _message_for_role(
    source: AgentMessage,
    content: str | list[AgentBlock],
) -> AgentMessage:
    role = source.get("role")
    if role not in ("user", "assistant"):
        raise ValueError(f"Invalid agent message role: {role}")
    return message(cast(AgentRole, role), content)


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


def _normalize_message(message: AgentMessage) -> AgentMessage:
    return _message_from_snapshot(_message_to_snapshot(message))


def _message_to_snapshot(message: AgentMessage) -> dict[str, Any]:
    content = message["content"]
    if isinstance(content, str):
        snapshot_content: str | list[Any] = content
    else:
        snapshot_content = [_block_to_snapshot(block) for block in content]

    return {
        "role": message["role"],
        "content": snapshot_content,
    }


def _message_from_snapshot(message: Any) -> AgentMessage:
    if not isinstance(message, dict):
        raise ValueError("Context snapshot message must be a dict")

    role = message.get("role")
    if role not in ("user", "assistant"):
        raise ValueError(f"Invalid context snapshot role: {role}")

    content = message.get("content")
    if not isinstance(content, str) and not isinstance(content, list):
        raise ValueError("Context snapshot content must be a string or list")

    return normalize_message({"role": role, "content": content})


def _block_to_snapshot(block: Any) -> dict[str, Any]:
    block_dict = _block_as_mapping(block)
    return block_to_dict(block_dict)


def _copy_block(block: Any) -> Any:
    if isinstance(block, dict):
        return block_to_dict(cast(Mapping[str, Any], block))
    return block


def _message_has_block_type(message: AgentMessage, block_type: str) -> bool:
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
