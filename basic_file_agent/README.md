# Basic File Agent

`basic_file_agent` is the smallest runnable example in this repo. It uses
`agent_harness` without the chat UI, user registry, OAuth, attachments,
approvals, artifact storage, demo workspace controller, or sideband event
stream.

The workflow receives one prompt plus system instructions, lets the model call
`read_file` and `write_file`, and returns the final assistant message as the
workflow result.

Read it in this order when using it as a template: `tool_types.py`,
`guards.py`, `tools.py`, `workflow.py`, then `worker.py`. That shows the app's
category vocabulary, policy, side-effect boundaries, workflow construction, and
worker registration without the larger chat application's product code.

## Files

| File | Purpose |
| --- | --- |
| `workflow.py` | Temporal workflow and request/result dataclasses. |
| `tool_types.py` | App-owned tool category vocabulary for this example. |
| `guards.py` | Demo write-file approval guard. |
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

This example defines custom `READ_FILE` and `WRITE_FILE` tool categories in
`tool_types.py`. Its workflow uses
`GuardPolicy.require_pre(BasicFileToolType.WRITE_FILE)`, and `write_file`
attaches `approve_file_write`. The workflow registers both tools with
`ToolSet(..., tools=[read_file, write_file])`.

The demo guard always approves so the example stays runnable without a UI or
human approval workflow. Production-style guards should check the path, content,
user, workspace policy, approval state, or whatever the mutation requires.

This example also disables continue-as-new handling. A long-running or
multi-turn workflow should carry `AgentResult.continuation_state` into a new run
when the agent asks for it.
