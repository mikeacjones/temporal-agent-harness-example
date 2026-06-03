from __future__ import annotations

import copy
import json
from typing import Any, Literal, Mapping, cast

AgentRole = Literal["user", "assistant"]
AgentMessage = dict[str, Any]
AgentBlock = dict[str, Any]
ToolUseBlock = dict[str, Any]
ToolResultBlock = dict[str, Any]


def message(role: AgentRole, content: str | list[AgentBlock]) -> AgentMessage:
    return {"role": role, "content": copy.deepcopy(content)}


def text_message(role: AgentRole, text: str) -> AgentMessage:
    return message(role, text)


def text_block(text: str) -> AgentBlock:
    return {"type": "text", "text": text}


def tool_use_block(
    *,
    tool_use_id: str,
    name: str,
    input: dict[str, Any],
    provider: dict[str, Any] | None = None,
) -> ToolUseBlock:
    block: ToolUseBlock = {
        "type": "tool_use",
        "id": tool_use_id,
        "name": name,
        "input": copy.deepcopy(input),
    }
    if provider is not None:
        block["provider"] = copy.deepcopy(provider)
    return block


def tool_result_block(
    *,
    tool_use_id: str,
    content: str,
    is_error: bool = False,
    provider: dict[str, Any] | None = None,
) -> ToolResultBlock:
    block: ToolResultBlock = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }
    if provider is not None:
        block["provider"] = copy.deepcopy(provider)
    return block


def provider_block(
    *,
    provider: str,
    provider_type: str,
    data: dict[str, Any],
) -> AgentBlock:
    return {
        "type": "provider",
        "provider": provider,
        "provider_type": provider_type,
        "data": copy.deepcopy(data),
    }


def normalize_message(value: Mapping[str, Any]) -> AgentMessage:
    role = value.get("role")
    if role not in ("user", "assistant"):
        raise ValueError(f"Invalid agent message role: {role}")

    content = value.get("content")
    if isinstance(content, str):
        return message(cast(AgentRole, role), content)
    if isinstance(content, list):
        return message(
            cast(AgentRole, role),
            [_normalize_block(block) for block in content],
        )

    raise ValueError("Agent message content must be a string or list")


def block_to_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


def message_text(value: Mapping[str, Any]) -> str:
    content = value.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    return "".join(_text_from_block(block) for block in content)


def visible_user_message_text(value: Mapping[str, Any]) -> str:
    content = value.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    visible_blocks: list[str] = []
    for block in content:
        text = _text_from_block(block)
        if text.lstrip().startswith("<attachments>"):
            continue
        if text:
            visible_blocks.append(text)
    return "\n".join(visible_blocks)


def tool_use_blocks(value: Mapping[str, Any]) -> list[ToolUseBlock]:
    content = value.get("content")
    if not isinstance(content, list):
        return []

    blocks: list[ToolUseBlock] = []
    for block in content:
        block_dict = _block_mapping(block)
        if block_dict.get("type") == "tool_use":
            blocks.append(block_to_dict(block_dict))
    return blocks


def json_content(value: Any) -> str:
    return json.dumps(value)


def _normalize_block(value: Any) -> AgentBlock:
    block = _block_mapping(value)
    if not block:
        raise ValueError("Agent message block must be a dict-like object")
    block_type_value = block.get("type")
    if not isinstance(block_type_value, str):
        raise ValueError("Agent message block type must be a string")
    return block_to_dict(block)


def _text_from_block(value: Any) -> str:
    block = _block_mapping(value)
    if block.get("type") == "text":
        text = block.get("text")
        return text if isinstance(text, str) else ""
    if block.get("type") == "refusal":
        refusal = block.get("refusal")
        return refusal if isinstance(refusal, str) else ""
    return ""


def _block_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, dict):
        return cast(Mapping[str, Any], value)
    if hasattr(value, "to_dict"):
        return cast(Mapping[str, Any], value.to_dict())
    return {}
