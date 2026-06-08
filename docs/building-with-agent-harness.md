# Building A New Project With `agent_harness`

This guide shows how to start a new Temporal agent project using the reusable
`agent_harness` folder from this repository.

`agent_harness` is not published to PyPI today. Treat it as vendored source:
copy the folder into your application repository, import it directly, and own
your version of the harness the same way you would own any internal platform
library.

For the smallest runnable example in this repo, see
[`basic_file_agent`](../basic_file_agent/README.md).

## What You Are Building

A minimal harness-based project has four pieces:

1. A Temporal workflow that receives a prompt and creates an agent.
2. A `ToolSet` containing the tools the model is allowed to call.
3. Activities for non-deterministic work, including the provider API call and
   any tool side effects.
4. A worker process that registers the workflow, provider activity, and generic
   tool/guard routers.

The harness owns the provider-neutral agent loop. Your app still owns the
workflow shape, product state, tools, credentials, auth, deployment, and any UI
or API.

## Start A New `uv` Project

Create a Python 3.12 project:

```bash
uv init my-temporal-agent --python 3.12
cd my-temporal-agent
```

Copy the harness source into the project:

```bash
cp -R /path/to/temporal-agent-harness-example/agent_harness ./agent_harness
```

Install the core dependencies plus one provider SDK:

```bash
uv add temporalio pydantic anthropic
```

Provider-specific dependencies:

| Provider | Dependency |
| --- | --- |
| Claude | `anthropic` |
| ChatGPT | `openai` |
| Gemini | `google-genai` |

Optional harness features may need more dependencies:

| Feature | Dependency |
| --- | --- |
| MCP tools | `mcp httpx` |
| FastAPI API server | `fastapi uvicorn sse-starlette python-multipart` |
| S3 or AWS-backed app storage | `boto3` |

## Suggested Project Layout

```text
my-temporal-agent/
  agent_harness/
  my_agent/
    __init__.py
    workflow.py
    tools.py
    worker.py
    start_workflow.py
  pyproject.toml
```

Keep workflow orchestration in `workflow.py`, tool declarations in `tools.py`,
and worker registration in `worker.py`. Add an API or UI later only if the
product needs one.

## Define Tools

Tools are workflow functions decorated with `@tool(...)`. A tool can make
deterministic decisions directly, but side effects should go through
`ctx.activity(...)`.

```python
from agent_harness.tool_types import ToolType
from agent_harness.tools import ToolContext, ToolResult, tool


@tool(
    name="read_file",
    description="Read a UTF-8 file from the workspace.",
    tool_type=ToolType.READ,
)
async def read_file(ctx: ToolContext, path: str) -> ToolResult:
    payload = await ctx.activity(
        _read_file_activity,
        args={"path": path},
    )
    return ToolResult(payload=payload, error=False)


async def _read_file_activity(path: str) -> dict:
    return {"path": path, "content": "..."}
```

For mutating tools, generate an idempotency key and pass it to the downstream
system when possible:

```python
@tool(
    name="write_file",
    description="Write a UTF-8 file to the workspace.",
    tool_type=ToolType.MUTATING,
)
async def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    payload = await ctx.activity(
        _write_file_activity,
        args={
            "path": path,
            "content": content,
            "idempotency_key": ctx.idempotency_key(path, content),
        },
    )
    return ToolResult(payload=payload, error=False)
```

## Create The Workflow

Construct the provider-specific convenience agent inside workflow code. This
example uses Claude.

```python
from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from agent_harness.agent import ContinueAsNewPolicy
    from agent_harness.guards import GuardPolicy
    from agent_harness.messages import message_text
    from agent_harness.providers.claude import ClaudeAgent
    from agent_harness.tools import ToolSet
    from my_agent.tools import read_file, write_file


@dataclass
class AgentRequest:
    prompt: str
    instructions: str
    model: str = "claude-sonnet-4-5"


@dataclass
class AgentResult:
    message: str
    stop_reason: str | None
    turns: int


@workflow.defn
class FileAgentWorkflow:
    @workflow.run
    async def run(self, request: AgentRequest) -> AgentResult:
        tools = ToolSet(
            guard_policy=GuardPolicy(
                required_pre=frozenset(),
                required_post=frozenset(),
            )
        )
        tools.add_tool(read_file, write_file)

        agent = ClaudeAgent(
            request.instructions,
            tools,
            model=request.model,
            stream_id=workflow.info().workflow_id,
            continue_as_new_policy=ContinueAsNewPolicy(enabled=False),
        )
        result = await agent.run(request.prompt)
        return AgentResult(
            message=message_text(result.message),
            stop_reason=result.stop_reason,
            turns=result.turns,
        )
```

The example disables required mutating-tool guards to keep the first project
small. For a real project, keep the default guard policy and attach a pre-guard
to mutating, admin, or MCP tools.

The example also disables continue-as-new handling. For long-running or
multi-turn agents, keep continue-as-new enabled and carry
`AgentResult.continuation_state` into the next run.

## Register The Worker

The worker must register:

- your workflow;
- the provider API activity;
- `run_tool_activity`;
- `run_guard_activity` if a tool guard or LLM guard calls `ctx.activity(...)`.

```python
import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from agent_harness.guards import run_guard_activity
from agent_harness.providers.claude import call_agent_api
from agent_harness.tools import run_tool_activity
from my_agent.workflow import FileAgentWorkflow


TASK_QUEUE = os.environ.get("TASK_QUEUE", "my-agent")


async def main() -> None:
    client = await Client.connect(
        os.environ.get("TEMPORAL_ENDPOINT", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[FileAgentWorkflow],
        activities=[
            call_agent_api,
            run_tool_activity,
            # Needed only when guards call ctx.activity(...). Registered here
            # because most real projects eventually add guarded tools.
            run_guard_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
```

## Run Locally

Start a local Temporal server:

```bash
temporal server start-dev --ip 0.0.0.0
```

Start your worker:

```bash
ANTHROPIC_API_KEY=sk-ant-... uv run python -m my_agent.worker
```

Start a workflow from a small client script, Temporal CLI, or your own API
server. The [`basic_file_agent/start_workflow.py`](../basic_file_agent/start_workflow.py)
file is a complete tiny client.

## Production Readiness Checklist

Before turning a small example into a real product, decide these up front:

- How workflows continue as new and what state is carried forward.
- Which tools are mutating and how each mutating activity is made idempotent.
- Which tool types require pre-guards or post-guards.
- Whether tool failures should become model-visible tool errors or fail the run.
- Which provider is used and how provider credentials are resolved in
  activities.
- How context is compacted, snapshotted, and bounded.
- Whether sideband streaming is needed and what happens when stream delivery
  fails.
- How attachments, artifacts, and external payload storage expire.
- How activity timeouts, retries, heartbeats, and cancellation are configured.
- How workflow histories are replay-tested before deployment.
- How worker versioning and task queues are managed.

## Appendix: Harness API Reference

This appendix documents the public shape used by the example. It is source-level
documentation for the vendored harness, not a generated API reference.

### Agent

Module: `agent_harness.agent`

`Agent` is the provider-neutral loop. Most apps construct a provider-specific
subclass such as `ClaudeAgent`, `ChatGptAgent`, or `GeminiAgent`.

Constructor concepts:

| Argument | Meaning |
| --- | --- |
| `system_prompt` | Instructions sent as provider system/developer context. |
| `tools` | `ToolSet` available to the model. |
| `provider` | `AgentProvider` implementation; set by provider convenience classes. |
| `model` | Provider model name. |
| `max_tokens` | Provider response token budget. |
| `tool_names` | Optional allow-list from the registered `ToolSet`. |
| `stream_id` | Correlation id for sideband stream events. |
| `activity_options` | Default options for tool activity routing. |
| `llm_guard_activity_options` | Default options for LLM guard activity routing. |
| `pre_llm_guards` / `post_llm_guards` | Request/response guard functions. |
| `context_manager_factory` | Factory for a custom `ContextManager`. |
| `max_context_tokens` | Maximum provider context window to target. |
| `context_safety_margin_tokens` | Reserved input safety margin. |
| `context_chars_per_token` | Fallback token estimator. |
| `continue_as_new_policy` | Enables or disables mid-run continuation suggestions. |

Important methods:

| Method | Use |
| --- | --- |
| `run(user_prompt, attachments=None, state=None, max_turns=20)` | Execute the model/tool loop. Use `state` to resume after continue-as-new. |
| `steer(message, mode="immediate")` | Add out-of-band steering during a run. |
| `interrupt(message, partial_response_policy="discard")` | Interrupt an in-flight provider call or tool call. |
| `restore_idle_state(state)` | Restore compacted context between turns. |
| `context_snapshot()` | Return full context snapshot. |
| `compacted_state()` | Return compacted `AgentState` suitable for idle carry-forward. |
| `effective_user_prompt()` | Return the post-pre-guard initiating user prompt. |

`AgentResult` fields:

| Field | Meaning |
| --- | --- |
| `message` | Final generic assistant message dict. |
| `stop_reason` | Provider stop reason. |
| `turns` | Provider-call loop count. |
| `continuation_state` | `AgentState` to pass into the next run when continue-as-new is needed. |
| `needs_continue_as_new` | Convenience property for `continuation_state is not None`. |
| `guard_action` / `guard_reason` | LLM guard outcome. |
| `terminated` | True when an LLM guard terminated the run. |
| `stop_details` | Provider-specific stop metadata. |

`AgentState` contains the context snapshot, completed turn count, and LLM guard
state. Carry it across continue-as-new if the agent returns it.

### Provider Implementations

Module: `agent_harness.providers.interface`

`AgentProvider` is the provider contract. Provider methods that run in workflow
code must be deterministic conversion functions. Network I/O belongs in the
provider activity.

Required provider methods:

| Method | Responsibility |
| --- | --- |
| `name` | Stable provider label. |
| `activity` | Temporal activity that calls the provider SDK. |
| `activity_options` | Default provider activity options. |
| `estimate_request_tokens(...)` | Estimate request overhead for context budgeting. |
| `create_request(...)` | Convert generic messages/tools into provider request. |
| `request_chat_history(...)` | Extract generic history from a provider request. |
| `replace_request_chat_history(...)` | Replace request history after guard/windowing changes. |
| `replace_request_stream_attempt(...)` | Add retry-attempt metadata for stream consumers. |
| `request_to_dict(...)` / `request_from_dict(...)` | Lossless request serialization. |
| `response_to_dict(...)` / `response_from_dict(...)` | Lossless response serialization. |
| `response_from_guard_execution(...)` | Build a provider-shaped guard response. |
| `response_with_visible_refusal(...)` | Normalize provider refusals. |
| `response_message(...)` | Convert provider response into generic assistant message. |
| `stop_reason_for_max_turns()` | Stop reason used when `max_turns` is reached. |

Provider convenience classes live under `agent_harness.providers`.

### Tools

Module: `agent_harness.tools`

Use `@tool(...)` to mark a workflow function as a model tool.

```python
@tool(
    name="tool_name",
    description="Clear model-visible description.",
    tool_type=ToolType.READ,
    pre_guards=[...],
    post_guards=[...],
)
async def my_tool(ctx: ToolContext, arg: str) -> ToolResult:
    ...
```

Tool parameters must be typed. `ToolContext` is injected and omitted from the
model schema. Other typed parameters become the tool input schema through
Pydantic.

`ToolResult(payload: dict, error: bool)` is the model-visible result. Return
`error=True` for expected tool failures that the model should handle.

`ToolSet` methods:

| Method | Use |
| --- | --- |
| `add_tool(*tools)` | Register standalone decorated tool functions. |
| `add_provider(provider)` | Register decorated methods from a provider object. |
| `add_dynamic_tool(...)` | Register a runtime schema and raw-args function. |
| `add_guard(*guards)` | Register standalone tool guards. |
| `add_mcp_provider(provider)` | Register a dynamic MCP provider adapter. |
| `tool_names()` | List registered tool names. |
| `tool_schemas(names=None)` | Return model-facing schemas. |
| `execute_tool(...)` | Execute a tool from workflow code. Usually called by `Agent`. |

`ToolContext` methods:

| Method | Use |
| --- | --- |
| `activity(fn, ..., args={...})` | Schedule a side-effecting function through `agent_harness.run_tool_activity`. |
| `idempotency_key(*parts)` | Stable key derived from stream id, tool name, tool call id, and extra parts. |
| `tool_names()` / `tool_schemas()` | Inspect the current tool set. |

`ctx.activity(...)` accepts common Temporal activity options: task queue,
timeouts, heartbeat timeout, retry policy, cancellation type, activity id,
versioning intent, and priority.

Register `run_tool_activity` with the worker whenever a tool calls
`ctx.activity(...)`.

### Tool Activity Runtime Context

Module: `agent_harness.activity_router`

Activity functions can request `ToolActivityContext` by type annotation:

```python
from agent_harness.activity_router import ToolActivityContext


async def long_activity(activity_ctx: ToolActivityContext) -> dict:
    activity_ctx.heartbeat({"phase": "started"})
    ...
```

`ToolActivityContext` exposes tool name, step, stream id, activity id, attempt,
heartbeat timeout, and `heartbeat(details=None, force=False)`.

If the activity has a heartbeat timeout, the generic router also sends
conservative automatic heartbeats. Manual heartbeats are coalesced.

### Tool Types And Guard Policy

Module: `agent_harness.tool_types`

`ToolType` values:

- `READ`
- `MUTATING`
- `MCP`
- `ADMIN`

Module: `agent_harness.guards`

`GuardPolicy` controls which tool types require guards:

```python
from agent_harness.guards import GuardPolicy
from agent_harness.tool_types import ToolType

policy = GuardPolicy(
    required_pre=frozenset({ToolType.MUTATING, ToolType.ADMIN}),
    required_post=frozenset(),
)
tools = ToolSet(guard_policy=policy)
```

The default policy requires pre-guards for `MUTATING`, `MCP`, and `ADMIN`
tools.

### Tool Guards

Module: `agent_harness.guards` and `agent_harness.tools`

Use `@guard(...)` to create a pre/post tool guard:

```python
from agent_harness.guards import GuardContext, GuardResult
from agent_harness.tools import guard
from agent_harness.tool_types import ToolType


@guard(name="allow_write", fulfills=ToolType.MUTATING)
async def allow_write(ctx: GuardContext) -> GuardResult:
    return GuardResult(passed=True)
```

Attach it to a tool:

```python
@tool(
    name="write_file",
    description="Write a file.",
    tool_type=ToolType.MUTATING,
    pre_guards=[allow_write],
)
async def write_file(...):
    ...
```

`GuardContext` includes guard name, tool name, tool type, tool args, optional
tool result for post-guards, stream id, and `activity(...)` for side effects.

`GuardResult(passed=False, reason=..., llm_payload=...)` returns a model-visible
tool error. Register `run_guard_activity` with the worker when guards call
`ctx.activity(...)` or when LLM guards call activities.

### LLM Guards

Module: `agent_harness.llm_guards`

LLM guards inspect or rewrite provider requests and responses, separate from
tool-specific guards.

Pre-guards receive a provider request. Post-guards receive both request and
response. Return:

- `LlmGuardResult.allow(...)` to continue, optionally with modified request,
  response, message, or state;
- `LlmGuardResult.block(...)` to return a refusal-like response;
- `LlmGuardResult.terminate(...)` to terminate the agent run.

Attach them during agent construction:

```python
agent = ClaudeAgent(
    instructions,
    tools,
    model="claude-sonnet-4-5",
    pre_llm_guards=[my_pre_guard],
    post_llm_guards=[my_post_guard],
)
```

LLM guards may call `ctx.activity(...)`, which uses the generic guard activity
router with readable summaries like `llm_guard:pre:guard_name`.

### Context Managers

Module: `agent_harness.context_manager`

`ContextManager` is a protocol. Implement it when you want custom memory or
windowing behavior.

Required methods:

| Method | Responsibility |
| --- | --- |
| `initialize(user_prompt, attachments=None)` | Start context for a new turn/run. |
| `record_user_message(...)` | Append a user/control message. |
| `record_assistant_message(message)` | Append provider response. |
| `record_tool_results(tool_results)` | Append tool result blocks. |
| `restore(snapshot)` | Restore from carried state. |
| `full_context_snapshot()` | Return complete durable snapshot. |
| `continuation_context_snapshot(token_budget=None)` | Return compacted snapshot for continue-as-new. |
| `messages_for_model(token_budget=None)` | Return model-visible history. |
| `full_messages()` | Return unwindowed history for guards. |
| `replace_messages(messages)` | Persist guard-censored history. |
| `message_count()` | Current message count. |

`ContextTokenBudget` describes the model context budget after reserving output
tokens, fixed request overhead, and a safety margin.

Module: `agent_harness.sliding_window_context_manager`

`SlidingWindowContextManager` is the default implementation. It keeps recent
messages, preserves the initial user message by default, drops incomplete tool
exchanges, can compact tool results, and inserts a compaction marker when old
messages are removed.

### Messages

Module: `agent_harness.messages`

Generic messages are dictionaries with `role` and `content`.

Useful helpers:

| Helper | Use |
| --- | --- |
| `text_message(role, text)` | Build a text-only generic message. |
| `message(role, content)` | Build a generic message with string or block list content. |
| `message_text(message)` | Extract visible text from assistant/user content. |
| `tool_use_block(...)` | Build a generic tool-use block. |
| `tool_result_block(...)` | Build a generic tool-result block. |
| `tool_use_blocks(message)` | Extract requested tool calls. |
| `json_content(value)` | JSON encode tool result payloads. |
| `normalize_message(value)` | Validate/copy a generic message mapping. |
| `visible_user_message_text(value)` | Extract user-visible text without attachment manifests. |

Provider adapters convert between generic messages and vendor-native schemas.

### Activity Options

Module: `agent_harness.activity_options`

`ActivityOptions` is a small dataclass wrapper around Temporal activity options:
task queue, timeouts, heartbeat timeout, retry policy, cancellation type,
versioning intent, and priority.

Use it to set defaults when constructing an agent:

```python
from datetime import timedelta

from agent_harness.activity_options import ActivityOptions

activity_options = ActivityOptions(
    start_to_close_timeout=timedelta(minutes=5),
    heartbeat_timeout=timedelta(seconds=30),
)
```

Tool and guard authors can override options per `ctx.activity(...)` call.

### Streaming

Module: `agent_harness.streaming`

Streaming is sideband UX, not durable workflow state.

`configure_stream_sink(sink, raise_stream_errors=False)` installs a process-wide
sink. `StreamContext.emit(...)` sends events to that sink if configured.

Provider activities should use `AgentStreamWriter` and its fixed event kinds:

- `agent_start`
- `agent_text_delta`
- `agent_thinking_start`
- `agent_thinking_delta`
- `agent_tool_input_start`
- `agent_tool_input_delta`
- `agent_tool_input_complete`
- `agent_complete`
- `agent_cancelled`

Tool or guard activities can accept `StreamContext` by annotation and emit their
own lower-frequency progress events.

### Attachments

Module: `agent_harness.attachments`

`AttachmentRef` is a provider-neutral reference to an uploaded file or artifact.
The harness records references and attachment manifests; the application owns
storage, extraction, authorization, and expiration.

Use provider conversion code to map supported attachments into native provider
file/image inputs. Use tools such as `read_attachment` when the provider cannot
consume the blob natively.

### MCP

Modules: `agent_harness.mcp` and `agent_harness.mcp_types`

The MCP helpers discover HTTP MCP servers and register their tools dynamically.
This is optional and usually belongs in a product app that already owns MCP
server configuration and auth resolution.

The harness keeps MCP generic: auth, user configuration, persistence, and UI
belong to the application.

### Worker Registration Checklist

Register these activities based on the features you use:

| Feature | Worker registration |
| --- | --- |
| Claude provider | `agent_harness.providers.claude.call_agent_api` |
| ChatGPT provider | ChatGPT provider activity from `providers/chatgpt.py` |
| Gemini provider | Gemini provider activity from `providers/gemini.py` |
| Tools using `ctx.activity(...)` | `agent_harness.tools.run_tool_activity` |
| Tool guards or LLM guards using `ctx.activity(...)` | `agent_harness.guards.run_guard_activity` |
| Durable application settlement events | App-specific activity, not harness-owned |
| Sideband stream persistence | App-specific stream sink and API, not harness-owned |

Register every workflow class your app starts. Register every concrete
application activity you schedule directly with `workflow.execute_activity`.
Functions called through `ctx.activity(...)` are invoked by the generic router;
they do not need their own worker registration, but they must be importable by
the worker process as module-level functions.

### Determinism Rules

Workflow code may:

- construct agents and tool sets;
- call `agent.run(...)`;
- update workflow state;
- schedule activities through `ctx.activity(...)`;
- start child workflows, wait on signals, queries, updates, timers, and
  continue-as-new.

Workflow code must not:

- call provider SDKs directly;
- read files or environment variables;
- call external HTTP APIs;
- use wall-clock time outside Temporal workflow APIs;
- use random values outside deterministic Temporal APIs;
- mutate external systems without an activity or child workflow boundary.

Provider conversion helpers and context managers run in workflow code, so keep
them pure and deterministic.
