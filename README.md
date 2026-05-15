# Temporal Agent Harness Example

This repo is not trying to be a generic agent SDK.

It is an example of how a team might build a small, opinionated agent harness that matches its own operational needs: durable execution, readable Temporal history, explicit tool categories, runtime guard enforcement, and narrowly controlled model access.

The current harness is Claude-specific on purpose. The interesting part is not "how to call an LLM"; it is how the agent loop, tool registry, guard policy, and Temporal execution model fit together.

## What This Shows

- Agent loops can be ordinary Temporal workflow code.
- LLM calls can be isolated in one activity.
- Tool implementations can orchestrate durable work instead of being simple request/response functions.
- Guards can be enforced by the harness at runtime, not left to every tool author to remember.
- Event history can stay readable even when many tools share one generic activity implementation.
- Optional sideband streaming can provide non-durable progress updates without coupling product UX to Temporal history.

## Core Shape

The Claude call is an activity. Tool execution happens from workflow code. If a tool needs side effects, it calls through `ctx.activity(...)`, which routes through a generic activity while setting a useful Temporal summary.

```python
@TOOLS.tool(
    name="lookup_customer",
    description="Look up a customer by id.",
    tool_type=ToolType.READ,
)
async def lookup_customer(ctx: ToolContext, customer_id: str) -> ToolResult:
    payload = await ctx.activity(
        _lookup_customer_activity,
        args={"customer_id": customer_id},
    )
    return ToolResult(payload=payload, error=False)
```

If no `step` is provided, the activity summary is the tool name:

```python
await ctx.activity(_lookup_customer_activity)
# summary: "lookup_customer"
```

If a tool has multiple activity steps, the tool author names them:

```python
await ctx.activity(_load_customer, step="load")
await ctx.activity(_update_customer, step="update")
# summaries: "lookup_customer:load", "lookup_customer:update"
```

This keeps the activity type generic while making Temporal history useful to humans.

## Guards

Tools are categorized with `ToolType`. The harness can require guards for specific categories. Today, `ToolType.ADMIN` requires a pre-guard by default.

```python
@TOOLS.guard(name="require_ops_approval", fulfills=ToolType.ADMIN)
async def require_ops_approval(ctx: GuardContext) -> GuardResult:
    approval = await ctx.activity(
        _request_ops_approval,
        step="approval",
        args={"tool_name": ctx.tool_name, "tool_args": ctx.tool_args},
    )

    if not approval["approved"]:
        return GuardResult(
            passed=False,
            reason="ops_approval_denied",
            llm_payload={
                "error": "Ops approval denied",
                "reason": "ops_approval_denied",
            },
        )

    return GuardResult(passed=True, internal_payload=approval)
```

The protected tool declares the guard explicitly:

```python
@TOOLS.tool(
    name="restart_service",
    description="Restart a production service.",
    tool_type=ToolType.ADMIN,
    pre_guards=[require_ops_approval],
)
async def restart_service(ctx: ToolContext, service_name: str) -> ToolResult:
    result = await ctx.activity(
        _restart_service,
        args={"service_name": service_name},
    )
    return ToolResult(payload=result, error=False)
```

Pre-guard failure prevents the tool from running. Post-guard failure prevents the model from receiving the raw tool result and returns the guard's `llm_payload` instead.

This does not make it impossible for a developer to write a bad guard. It does make missing guard coverage explicit and runtime-enforced.

## What This Unlocks

Because tools are workflow code, they can do more than call one function:

- Start child workflows for delegated work.
- Wait for durable approval flows, signals, updates, timers, or external state.
- Fan out to multiple activities and summarize each step clearly.
- Apply organization-specific guard policy before and after execution.
- Stream best-effort progress to a sideband sink when one is configured.

That means examples in this repo should be read less as "agent demos" and more as "harness capability demos." The agent is the vehicle; the point is showing what the harness makes easy, observable, and harder to misuse.

## Worker Registration

Applications using this harness should register the Claude activity plus the generic tool and guard routers:

```python
from claude_harness.claude_agent import call_claude
from claude_harness.tools import run_guard_activity, run_tool_activity

activities = [
    call_claude,
    run_tool_activity,
    run_guard_activity,
]
```

The Anthropic SDK reads credentials from `ANTHROPIC_API_KEY`.

## Non-Goals

- A provider-neutral agent framework.
- A complete authorization system.
- A replacement for application-specific workflows.
- A polished library API.

The goal is to make the architectural tradeoffs concrete enough that another
team could adapt the pattern to its own internal agent platform.
