# Building Agents On Temporal: Production Readiness Notes

This document is a field note from building this repo. It is not a generic
Temporal tutorial and it is not a claim that every agent needs this exact
architecture. It is a list of the architectural decisions that became important
while this harness grew from a simple provider call into a durable chat agent
with tools, approvals, attachments, provider adapters, streaming, demo
workspaces, retention, and deployment versioning.

The short version: Temporal is a strong fit for agent orchestration, but it will
make implicit choices durable, sometimes painfully so! Before you build, decide what state is
authoritative, what can be replayed, what can be retried, what can be dropped,
and how the system moves forward when a workflow has lived longer than your
first design assumed.

## What The Repo History Taught

The git history here has a recurring pattern:

1. Start with a workflow-shaped agent loop.
2. Add tools and guards.
3. Add UI streaming for visibility.
4. Discover that visibility is not durable state.
5. Add attachments, artifacts, and external payload storage.
6. Discover that storage lifecycle and workflow retention are now coupled.
7. Add continue-as-new.
8. Discover that continue-as-new must preserve exactly the state the agent can
   still use.
9. Add multiple providers.
10. Discover that provider details belong at the adapter boundary, not in the
    agent loop.
11. Add worker versioning.
12. Discover that temporary task queues and local dev need a versioning story.
13. Add heartbeats, idempotency, and retry classification.
14. Discover that tool failures should often be model-visible, while platform
    failures should still fail loudly.

Most of the production-readiness work was not about calling a model. It was
about boundaries.

## The Boundary Decisions To Make First

Before you write much code, answer these questions.

### Workflow Topology

- Is one agent conversation one workflow, or is an entire user/session/project
  one workflow?
- Do you need a parent entity workflow that owns child agent workflows?
- Which operations should be signals, which should be updates, and which should
  be queries?
- What happens when a user, project, or workspace is deleted?
- Which workflows should outlive their parent, and which should close when the
  parent closes?

This repo ended up with:

- `SimpleChatWorkflow`: one chat conversation;
- `UserChatsWorkflow`: one user registry/entity workflow;
- `DemoWorkspaceWorkflow`: one temporary workspace controller;
- `SubagentWorkflow`: child agent workflows for delegated work.

That split matters. Chat state and agent turns grow differently from registry
state. Workspace provisioning has long activities and cleanup. Subagents need
approval delegation. Putting all of that in one workflow would have made
continue-as-new, cancellation, and queries harder to reason about.

### Durable State Versus Visibility

Decide what the source of truth is.

In this repo, the source of truth for the conversation is workflow state and the
agent context manager. Provider/tool streams are sideband visibility. Partial
streamed text is useful for UX, but it is not committed conversation state until
the provider activity completes and the workflow records the assistant message.

That drove the current design:

- Provider/tool deltas are best-effort stream events.
- The final turn settlement is emitted by a Temporal activity after transcript
  state is committed.
- The UI reconciles against workflow snapshot/delta queries, not against an
  unbounded browser-side transcript.

If you skip this distinction, you will eventually debug duplicated streamed
text, missing final messages, reconnect races, and UI state that disagrees with
workflow state.

### Harness Versus Application

A reusable harness should own generic rules:

- provider-neutral agent loop;
- context recording, windowing, compaction, and restoration;
- tool and guard registration;
- generic activity routing;
- interrupt and steering semantics;
- provider stream event names;
- continue-as-new handoff shape.

The application should own product-specific state:

- auth and OAuth;
- user/session registry;
- tool availability;
- storage backends;
- attachment/artifact semantics;
- UI transcript rendering;
- deployment-specific stream transport.

This repo moved from a Claude-specific harness to `agent_harness` plus provider
adapters because the overlap between Claude, Gemini, and ChatGPT belonged in the
generic loop. The provider-specific pieces are request/response conversion,
SDK calls, vendor stream parsing, vendor stop reasons, and vendor context-limit
errors.

## Event History And Continue-As-New

Agent workflows grow quickly. A single user turn can include:

- a provider activity;
- multiple tool activities;
- guard activities;
- approval waits;
- settlement activities;
- transcript state updates;
- sideband stream events outside history;
- claim-checked payload references inside history.

Plan continue-as-new from the beginning.

Important choices:

- Continue as new on both time-based checkpoints and Temporal server
  suggestions.
- Check continue-as-new both inside the agent loop and between user turns.
- Handle target worker deployment version changes as a checkpoint trigger.
- Do not wait for a long idle timeout if the server has already suggested
  continue-as-new.
- Catch `asyncio.TimeoutError` around `workflow.wait_condition(..., timeout=...)`
  when timeout is an expected path.
- Continue as new only after settling the turn state you need the UI to see.
- If you continue as new mid-agent-run, carry hot resume state separately from
  idle compacted context.

This repo has two continuation state concepts in `SimpleChatInput`:

- `agent_state`: hot resume state when continue-as-new happens mid-turn;
- `agent_context_state`: compacted idle context between turns.

That distinction avoids treating a resumed agent loop as a new user message.

## Context Is Not The UI Transcript

The context manager is what the agent can see. The UI transcript is what the
user should see. Those are related, but they are not the same.

The context manager may contain:

- user messages;
- assistant messages;
- tool-use blocks;
- tool-result blocks;
- guard-rewritten messages;
- steering and interrupt control messages;
- attachment manifests;
- compaction markers.

The UI usually wants:

- user messages;
- final assistant responses;
- approval state;
- maybe a detail drawer for tools/streams.

If you reuse the raw model context as the UI transcript, tool internals and
control messages leak into product UX. If you keep a separate unbounded UI
transcript, queries can exceed Temporal payload limits and continue-as-new can
become unclear.

The compromise here is a rendered transcript derived from context, with bounded
page and delta queries. When context compaction drops old model-visible history,
the context manager inserts a marker so the UI can show that older conversation
was compacted.

## Query Size Limits Are Product Requirements

Temporal queries are not a bulk data API. Plan query payloads as part of the UX
contract.

In this repo:

- `state()` is transcript-free;
- `snapshot()` returns current state plus a bounded transcript page;
- `transcript_page()` paginates older messages;
- `transcript_deltas_since()` returns a bounded delta buffer;
- queries expose `needs_snapshot` when deltas are too old or too large;
- query responses enforce byte budgets and truncate single large messages.

If you build a chat UI, design pagination and reconciliation early. Otherwise a
successful long conversation can break your API with a large query response.

## External Payload Storage Is A Lifecycle Contract

Claim-checking large Temporal payloads to S3 or local storage solves one
problem and creates another: replay now depends on external objects.

You need one coherent plan for:

- worker data converter;
- API data converter;
- codec server data converter;
- Temporal Web decoding;
- replay tooling;
- object lifecycle;
- deletion/purge;
- continue-as-new cadence;
- namespace retention.

This repo originally tried broad S3 expiration. That is attractive, but it is
unsafe unless no workflow run can need those payloads after expiration. The
safer shape is:

- keep workflow runs shorter than the external payload lifecycle;
- continue as new with compacted state before payloads expire;
- purge workflow payload prefixes when workflows are intentionally deleted;
- keep codec/API/worker storage configuration identical.

Do not set a storage lifecycle policy until you can explain how old workflow
histories will replay after objects expire.

## Activities, Retries, And Idempotency

Temporal retries activities. That is a feature, but it means every activity
that mutates an external system needs an idempotency story.

Plan for each tool:

- Is this read-only, mutating, admin, or unknown third-party behavior?
- Can the downstream system enforce an idempotency key?
- If not, can the tool write and later search for a stable marker?
- If neither is possible, should retries be limited?
- If the tool fails, should the workflow fail, or should the model receive an
  error result?

This repo's current stance:

- Tool authors own idempotency.
- The harness can generate stable `ctx.idempotency_key(...)` values.
- The GitHub issue tool uses a hidden marker because GitHub issue creation does
  not expose a native idempotency key.
- The Python sandbox uses conservative retries because arbitrary user code
  cannot be promised semantically idempotent.
- Expected tool failures are returned to the model as `ToolResult(...,
  error=True)`.
- Platform failures, cancellation, and invariant violations should still fail
  loudly.

That split is important. A flaky web search should not necessarily break the
whole chat. A corrupted data converter should.

## Heartbeats And Cancellation

Cancellation is not magic for long-running activities. Activities need heartbeat
timeouts and either automatic or meaningful progress heartbeats so cancellation
can be delivered promptly.

This matters for:

- streaming provider calls;
- sandbox execution;
- Kubernetes provisioning;
- long HTTP/MCP calls;
- tools that may hang on third-party APIs.

The harness now has:

- provider activity heartbeats during streaming calls;
- generic tool activity auto-heartbeats when `heartbeat_timeout` is set;
- `ToolActivityContext.heartbeat(...)` for meaningful progress details;
- interrupt handling that cancels active provider/tool work;
- tool-result contract preservation when interruption cancels a tool.

Do not use heartbeat details as your UI stream. Heartbeats are for Temporal
liveness and cancellation. Use explicit stream events for user-facing progress.

## Streaming Needs A Reconciliation Plan

Streaming is one of the easiest places to accidentally build a second source of
truth.

Decide:

- Is streaming long-lived per chat or scoped to one user turn?
- What event tells the browser the turn settled?
- How does the browser reconnect?
- How does it recover if stream state is lost?
- Are stream events durable, best-effort, or mixed?
- What happens when an activity retries and re-emits deltas from token zero?

This repo landed on:

- generic `agent_*` provider stream events;
- provider stream attempts so consumers can reset duplicate partials;
- turn-scoped SSE for message submission;
- durable `turn_settled` emitted by activity after workflow state commits;
- snapshot/delta reconciliation when stream replay is unavailable;
- API-managed stream buffer in deployment and JSONL files in local dev.

The key principle: streamed deltas improve responsiveness, but final rendering
comes from durable workflow state.

## Provider Adapters Should Stay Boring

Provider adapters are not mini agent runtimes.

They should:

- convert generic messages to vendor requests;
- convert vendor responses back to generic messages;
- call the vendor SDK inside an activity;
- translate vendor stream events into generic event kinds;
- disable SDK retries when Temporal owns retry behavior;
- classify retryable versus non-retryable provider errors;
- map provider context-window errors into a generic error type;
- heartbeat while streaming;
- honor cancellation.

They should not:

- own application auth;
- decide which business tools exist;
- mutate workflow state;
- know how the UI renders a transcript;
- contain product-specific storage logic.

Context overflow deserves special handling. Character-based token estimates are
imperfect. This harness keeps conservative token budgets and lets provider
activities report a generic context-window error so the agent can compact more
aggressively and retry once.

## Guards And Approvals Are Workflow Policy

Approvals are not just UI modals. They are durable workflow policy.

The early direction in this repo was to keep confirmation logic near tools. The
better shape was to make confirmation a pre-guard fulfilled by tool type. That
keeps the tool focused on its capability and lets the harness enforce policy
consistently.

Plan:

- Which tool categories require approval?
- Can a user "always allow" a tool for identical arguments?
- How long does an approval stay pending?
- How do child workflows/subagents request parent approval?
- What is visible in the UI while an approval blocks the agent?
- What happens if the user closes the browser?

For unknown third-party MCP tools, this repo treats them as approval-required by
default because the application cannot know whether they mutate state.

## Entity Workflows Need Bounds Too

It is easy to focus on chat workflow history and forget entity workflows.

Registry workflows can grow through:

- every chat create/touch/delete;
- MCP server changes;
- workspace status changes;
- carried-forward state across continue-as-new.

This repo added a 15-day run TTL for user registries. If touched, the registry
continues as new. If untouched, it closes itself and its tracked chats. That is
appropriate for a demo; a production system might instead shard registries,
archive records externally, or enforce hard caps.

Make the policy explicit:

- maximum chats per user/project;
- maximum MCP/tool connections;
- retention behavior for inactive users;
- archival versus deletion;
- expected update rate;
- size of the continue-as-new input.

## Worker Versioning And Deployment

Temporal workers replay old workflows. Deployment is part of correctness.

This repo uses worker versioning by default with pinned behavior. Continue-as-new
uses auto-upgrade so a workflow can adopt the latest compatible worker when it
checkpoints into a new run.

Important planning points:

- What is your build id?
- What is your worker deployment name?
- Are workflows pinned or auto-upgrading?
- How do continue-as-new runs choose their initial versioning behavior?
- Do temporary task queues need versioning disabled or separate deployments?
- How do local dev and test stacks avoid colliding with production task queues
  and workflow ids?

Worker versioning does not remove the need for replay compatibility. It gives
you a deployment control plane. You still need a story for incompatible command
sequence changes.

## Signals, Updates, And HTTP

Use Temporal interaction primitives deliberately.

- Query: read current state, return quickly, do not mutate.
- Signal: fire-and-forget mutation, good for chat input and interrupts.
- Update: mutation that returns a result, good for create/delete operations
  where the API needs an immediate durable answer.

This repo uses updates for operations such as creating chats and managing demo
workspaces, and signals for chat messages, steering, interrupts, and approvals.

Be careful with long synchronous updates. They are legal, but they couple the
HTTP caller to the full workflow operation. If you want the request to return
quickly, make the update kick off durable work and let the UI poll/query state.
If you intentionally keep the request open, document the timeout and retry
contract.

## Attachments And Artifacts Are Not Workflow Bytes

Do not put uploaded files, generated artifacts, or large fetched content directly
in workflow history.

This repo stores attachment/artifact bytes in application storage and passes
stable references into workflows. The agent sees attachment manifests; tools can
read supported attachment content by reference.

Plan:

- maximum upload size;
- content type detection;
- text extraction policy;
- binary/image support;
- provider-native file/image mapping;
- artifact retention;
- authorization checks for download/view routes;
- how expired attachments appear to the agent and user.

If a provider supports native images or files, that mapping belongs in the
provider adapter. The harness should still see stable attachment references.

## Replay Testing Is Not Optional

Every time workflow code changes, ask whether the command sequence changed.

High-risk areas:

- continue-as-new placement;
- changing activity order;
- adding/removing timers;
- changing child workflow starts;
- interrupt/cancel flow;
- approval flow;
- registry/workspace lifecycle;
- query shape changes that rely on carried state.

This repo includes a replay entrypoint because exported histories are the best
way to catch non-determinism before deployment. Replay tests should include more
than happy paths:

- interrupt mid-provider call;
- interrupt mid-tool;
- approval allow/deny/timeout;
- continue-as-new mid-agent-run;
- continue-as-new while idle;
- target worker version changed;
- activity timeout and retry exhaustion;
- missing external payloads;
- query size limits.

## Pre-Build Checklist

Before building your own harness or agent framework on Temporal, write down:

- Workflow topology: conversation, user, workspace, subagent boundaries.
- State authority: what is workflow state, external storage, sideband stream, or
  browser state.
- Context strategy: model-visible history, UI transcript rendering, compaction,
  markers, and continuation snapshots.
- History strategy: continue-as-new triggers, TTLs, server suggestions, target
  version changes, and carried state size.
- Storage strategy: claim-check threshold, bucket lifecycle, codec server,
  purge, replay, and retention.
- Tool strategy: categories, approvals, idempotency, retries, heartbeats,
  cancellation, and model-visible errors.
- Provider strategy: adapter contract, SDK retries, error classification,
  stream translation, token/context overflow recovery, and cancellation.
- Streaming strategy: best-effort versus durable events, replay cursors,
  reconnects, settlement, and snapshot reconciliation.
- Query strategy: pagination, deltas, byte budgets, and fallback snapshots.
- Deployment strategy: worker versioning, task queues, build ids, local dev,
  testing stacks, and temporary workspaces.
- Testing strategy: replay histories, time skipping, cancellation, activity
  failures, and long-running workflow upgrades.

If you cannot answer one of these, Temporal will not hide that gap. It will make
the gap durable, replayable, and visible in history. That is useful, but only if
you design for it.
