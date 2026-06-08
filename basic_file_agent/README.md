# Basic File Agent

`basic_file_agent` is the smallest runnable example in this repo. It uses
`agent_harness` without the chat UI, user registry, OAuth, attachments,
approvals, artifact storage, demo workspace controller, or sideband event
stream.

The workflow receives one prompt plus system instructions, lets the model call
`read_file` and `write_file`, and returns the final assistant message as the
workflow result.

## Files

| File | Purpose |
| --- | --- |
| `workflow.py` | Temporal workflow and request/result dataclasses. |
| `tools.py` | Two harness tools plus their file-system activities. |
| `worker.py` | Worker entrypoint that registers the workflow, provider activity, and generic tool/guard routers. |
| `start_workflow.py` | Small CLI client for starting the workflow. |
| `env.py` | Tiny `.env` loader used only by this example. |

## Run It

Start Temporal locally:

```bash
temporal server start-dev --ip 0.0.0.0
```

Set an Anthropic key, or put it in `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
TEMPORAL_ENDPOINT=localhost:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TLS=0
```

Start the worker:

```bash
uv run python -m basic_file_agent
```

In another terminal, start a workflow:

```bash
uv run python -m basic_file_agent.start_workflow \
  "Create notes/hello.md with a short note explaining Temporal workflows."
```

By default, files are read from and written to `./basic_file_agent_workspace`.
Override that with `BASIC_FILE_AGENT_WORKSPACE`.

## What It Intentionally Leaves Out

This example disables required tool guards so the mutating `write_file` tool can
stay small. Production-style agents should attach pre-guards to mutating,
admin, or external MCP tools.

This example also disables continue-as-new handling. A long-running or
multi-turn workflow should carry `AgentResult.continuation_state` into a new run
when the agent asks for it.
