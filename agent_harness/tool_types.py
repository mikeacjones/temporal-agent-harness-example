from collections.abc import Iterable
from enum import StrEnum


ToolCategory = str


class ToolType(StrEnum):
    READ = "read"
    MUTATING = "mutating"
    MCP = "mcp"
    ADMIN = "admin"


def normalize_tool_category(value: object) -> ToolCategory:
    if not isinstance(value, str):
        raise TypeError("Tool category values must be strings or string enums")
    if not value:
        raise ValueError("Tool category values must not be empty")
    return str(value)


def tool_category_set(
    values: ToolCategory | object | Iterable[ToolCategory | object],
) -> frozenset[ToolCategory]:
    if isinstance(values, str):
        return frozenset({normalize_tool_category(values)})

    try:
        iterator = iter(values)
    except TypeError:
        return frozenset({normalize_tool_category(values)})

    categories = frozenset(normalize_tool_category(value) for value in iterator)
    if not categories:
        raise ValueError("Tool category set must contain at least one category")
    return categories
