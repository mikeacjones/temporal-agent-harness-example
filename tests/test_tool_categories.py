from __future__ import annotations

from enum import StrEnum
import unittest

from agent_harness.guards import GuardContext, GuardPolicy, GuardResult
from agent_harness.tools import ToolContext, ToolResult, ToolSet, guard, tool


class ExampleToolCategory(StrEnum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"


@guard(name="allow_write_file", fulfills=ExampleToolCategory.WRITE_FILE)
async def allow_write_file(ctx: GuardContext) -> GuardResult:
    return GuardResult(passed=True)


@tool(
    name="write_file",
    description="Write a file.",
    tool_type=ExampleToolCategory.WRITE_FILE,
    pre_guards=[allow_write_file],
)
async def write_file(ctx: ToolContext, path: str) -> ToolResult:
    return ToolResult(payload={"path": path}, error=False)


@tool(
    name="unguarded_write_file",
    description="Write a file without a guard.",
    tool_type=ExampleToolCategory.WRITE_FILE,
)
async def unguarded_write_file(ctx: ToolContext, path: str) -> ToolResult:
    return ToolResult(payload={"path": path}, error=False)


async def dynamic_write_file(ctx: ToolContext, args: dict) -> ToolResult:
    return ToolResult(payload=args, error=False)


class ToolCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_tool_category_can_require_guard(self) -> None:
        tools = ToolSet(
            guard_policy=GuardPolicy(
                required_pre=frozenset({ExampleToolCategory.WRITE_FILE}),
            )
        )
        tools.add_tool(write_file)

        result = await tools.execute_tool("write_file", {"path": "notes.txt"})

        self.assertFalse(result.error)
        self.assertEqual(result.payload, {"path": "notes.txt"})

    async def test_custom_tool_category_rejects_missing_required_guard(self) -> None:
        tools = ToolSet(
            guard_policy=GuardPolicy(
                required_pre=frozenset({ExampleToolCategory.WRITE_FILE}),
            )
        )
        tools.add_tool(unguarded_write_file)

        with self.assertRaisesRegex(ValueError, "requires at least one pre guard"):
            await tools.execute_tool("unguarded_write_file", {"path": "notes.txt"})

    def test_dynamic_tools_normalize_custom_tool_category(self) -> None:
        tools = ToolSet()
        tools.add_dynamic_tool(
            name="dynamic_write_file",
            description="Write a file dynamically.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            tool_type=ExampleToolCategory.WRITE_FILE,
            fn=dynamic_write_file,
        )

        self.assertEqual(
            tools.get_tool("dynamic_write_file").tool_type,
            "write_file",
        )
