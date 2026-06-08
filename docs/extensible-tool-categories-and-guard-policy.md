# Extensible Tool Categories And Guard Policy

## Problem

The harness originally had a fixed `ToolType` enum:

- `READ`
- `MUTATING`
- `MCP`
- `ADMIN`

That was useful for the demo, but it made the reusable harness less generic
than intended. A customer or another application might not think in those four
categories. They might want categories such as:

- `customer_data_read`
- `customer_data_write`
- `payments_write`
- `file_read`
- `file_write`
- `internal_admin`

The old implementation let an app provide a different `GuardPolicy`, but only
over the harness-defined enum values. It did not let the app define its own
tool category vocabulary.

## Desired Shape

The harness should own the mechanics:

- tools declare a category;
- guards declare which categories they fulfill;
- guard policy declares which categories require pre/post guards;
- runtime validation ensures required guards are present and fulfill the tool
  category.

The application should own the vocabulary:

- what categories exist;
- what each category means;
- which categories require guards;
- what guard implementation satisfies those categories.

## Implemented Shape

`agent_harness.tool_types` now exposes:

- `ToolCategory = str`
- `ToolType`, a built-in convenience `StrEnum` for the demo/default categories
- normalization helpers for string-compatible categories

Existing code can keep using:

```python
from agent_harness.tool_types import ToolType

tool_type=ToolType.MUTATING
```

New apps can define their own string enum:

```python
from enum import StrEnum


class MyToolCategory(StrEnum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
```

Then use it everywhere the harness asks for a category:

```python
policy = GuardPolicy(
    required_pre=frozenset({MyToolCategory.WRITE_FILE}),
    required_post=frozenset(),
)
```

```python
@guard(name="approve_file_write", fulfills=MyToolCategory.WRITE_FILE)
async def approve_file_write(ctx: GuardContext) -> GuardResult:
    return GuardResult(passed=True)
```

```python
@tool(
    name="write_file",
    description="Write a file.",
    tool_type=MyToolCategory.WRITE_FILE,
    pre_guards=[approve_file_write],
)
async def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    ...
```

The harness normalizes categories to strings internally, so `StrEnum` values and
plain strings behave the same at runtime.

## Why Keep `ToolType`?

Keeping `ToolType` preserves compatibility for the existing demo application and
provides a small set of useful defaults for simple projects.

The important change is that `ToolType` is no longer the only legal vocabulary.
It is just a built-in vocabulary.

## Basic File Agent Example

`basic_file_agent` now demonstrates the app-owned category pattern:

- `BasicFileToolType.READ_FILE`
- `BasicFileToolType.WRITE_FILE`

Its workflow configures:

```python
GuardPolicy(
    required_pre=frozenset({BasicFileToolType.WRITE_FILE}),
    required_post=frozenset(),
)
```

`write_file` declares `BasicFileToolType.WRITE_FILE` and attaches
`approve_file_write`.

The approval guard intentionally always returns `passed=True`. That keeps the
example runnable without a UI or approval workflow, but it makes the wiring
clear: a real guard should check path scope, user permissions, workspace policy,
approval state, or other app-specific requirements.

## Compatibility Notes

This is source-compatible with existing decorated tools and guards that use
`ToolType`.

One semantic detail changed: guard validation now checks string category values.
That is intentional. A custom enum member with value `"write_file"` and a plain
string `"write_file"` are treated as the same category.

## Design Boundary

This keeps the harness provider- and product-agnostic:

- The harness enforces that a category requiring a guard has one.
- The harness enforces that attached guards fulfill the tool category.
- The app decides whether `write_file` is safe, sensitive, admin-like,
  customer-facing, or approval-gated.

That is the right split for a reusable harness. The harness should not bake in a
company's risk model.
