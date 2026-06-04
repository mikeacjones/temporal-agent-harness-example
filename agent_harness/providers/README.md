# Provider Implementations

Providers adapt a model vendor's API to the provider-neutral agent loop in
`agent_harness.agent.Agent`. The agent owns orchestration: context management,
tool execution, steering, interrupts, guards, continue-as-new, and persistence.
A provider owns the vendor-specific request shape, response shape, raw streaming
translation, stop reasons, and SDK call.

This split lets an app keep the same workflow, tool, context, approval, and
attachment behavior while swapping from one model vendor to another.

## What Belongs In This Folder

Provider modules are adapter code. They translate between the harness's generic
agent concepts and one vendor's API.

Provider modules should contain:

- vendor request and response dataclasses;
- an `AgentProvider` implementation;
- pure message/request/response conversion helpers;
- the Temporal activity that calls the vendor SDK;
- vendor stream-event translation into `AgentStreamWriter`;
- a small convenience `Agent` subclass when useful for app construction.

Provider modules should not contain product auth, user state, API routes,
artifact storage, tool implementations, or UI behavior. Those belong in the
application.

Current provider modules:

| File | Status |
| --- | --- |
| `claude.py` | Primary implementation used by `simple_chat_agent`. |
| `gemini.py` | Initial Gemini provider implementation. |
| `chatgpt.py` | Initial ChatGPT provider implementation. |

## Required Shape

Implement `AgentProvider` from `interface.py`.

Required provider responsibilities:

- `name`: short stable provider name used in activity summaries and stream
  labels.
- `activity`: the Temporal activity function that calls the vendor API.
- `activity_options`: default options for the provider activity.
- `estimate_request_tokens`: approximate fixed request overhead for the system
  prompt and tool schema. The generic context manager uses this to fit history
  into the model context window.
- `create_request`: convert generic `AgentMessage` history plus tool schemas
  into the provider-specific request dataclass.
- `request_chat_history` and `replace_request_chat_history`: expose and replace
  the request history as generic `AgentMessage` values. Guards and context
  windowing depend on this.
- `replace_request_stream_attempt`: update retry-attempt metadata before a
  provider activity retry so stream consumers can reset duplicate partials.
- `request_to_dict` / `request_from_dict`: lossless serialization for guard
  activities and workflow state.
- `response_to_dict` / `response_from_dict`: lossless serialization for guard
  activities and workflow state.
- `response_from_guard_execution`: build a provider-shaped response when a guard
  terminates or rewrites a call.
- `response_with_visible_refusal`: normalize provider refusals so the user sees
  a clear assistant message.
- `response_message`: convert the provider response back into an `AgentMessage`.
- `stop_reason_for_max_turns`: provider stop reason to use when the generic loop
  stops at `max_turns`.

Provider adapter methods run inside workflows, so they must be deterministic:
pure conversion, no network, no environment reads, no clocks, no random values.
The Temporal activity is where SDK calls, environment access, HTTP, retries, and
streaming belong.

## Recommended File Structure

A provider module normally contains:

- Provider-specific config dataclasses, such as thinking or output options.
- Provider request and response dataclasses.
- An `AgentProvider` implementation.
- An optional convenience `Agent` subclass that preconfigures the generic agent
  with the provider.
- Conversion helpers between `AgentMessage` and the vendor's message schema.
- A Temporal activity that calls the vendor SDK.
- Streaming translation helpers that map vendor events into `AgentStreamWriter`.

The Claude implementation in `claude.py` follows this pattern with
`ClaudeProvider`, `ClaudeRequest`, `ClaudeResponse`, `call_agent_api`, and the
`ClaudeAgent` convenience wrapper.

## Message Conversion

The generic harness message model lives in `agent_harness.messages`.
Provider code should translate between that model and the vendor schema at the
boundary only.

Keep these rules:

- Preserve text, tool-use, and tool-result blocks without changing their IDs.
- Map provider-native tool calls into generic tool-use blocks with `id`, `name`,
  and `input`.
- Map generic tool-result blocks back into the provider's required tool-result
  shape.
- Preserve provider-only blocks as provider blocks when useful, but do not make
  the generic agent loop understand provider-specific internals.
- Keep refusal, thinking, citations, and usage details provider-specific unless
  the generic loop needs them for control flow.

## Activities And Streaming

The provider activity should:

- Accept the provider request dataclass.
- Instantiate the vendor SDK/client inside the activity.
- Disable SDK retries when Temporal should own retries, or mark known bad
  requests as non-retryable `ApplicationError`s.
- Heartbeat during long streaming calls.
- Honor activity cancellation.
- Emit streaming events through `AgentStreamWriter`; provider code should call
  helper methods such as `text_delta`, `thinking_delta`,
  `tool_input_started`, and `agent_completed` instead of choosing raw event
  names.
- Return the provider response dataclass with enough data to reconstruct the
  assistant message and usage.

Streaming is intentionally sideband state. The workflow must not depend on
stream emission succeeding.
The harness owns the public agent stream event names via
`AgentStreamEventKind`, so application UIs can consume one `agent_*` protocol
across providers.

## Provider-Specific Features

Provider-specific options belong in provider config dataclasses and request
conversion helpers. Examples:

- Claude thinking mode and budget.
- Vendor-specific output controls.
- Vendor beta headers.
- Provider-specific refusal details.
- Native server-side tool formats.

Do not push those details into `Agent` unless the generic loop truly needs the
concept. For example, context windowing, tool execution, and approvals are
generic; Claude thinking is provider-specific.

## Context And Token Budgeting

The generic agent asks the provider for a request-overhead estimate through
`estimate_request_tokens(...)`. That estimate is used before context windowing
so the context manager can reserve room for the system prompt and tool schema.

Providers should also detect provider-specific context-limit failures in their
activity and return the generic `ContextWindowExceeded` error type when
possible. The generic agent can then compact more aggressively and retry once
without making every provider expose the same tokenizer.

If a provider has an accurate token-counting API, use it inside the provider
activity or provider-specific helper. Keep non-deterministic token-count API
calls out of workflow code.

## Attachments

Attachments are generic references in the harness. The context manager records
attachment manifests using `AttachmentRef`, and tools can read attachment
contents when needed.

If a provider supports native file inputs, add that support in the provider
conversion layer without making the generic agent loop provider-aware. The app
or tools should still own storage and retrieval. The provider should only decide
how available attachment references are represented in that provider's request.

## Implementation Checklist

1. Create `<provider>.py` under this folder.
2. Define provider request/response dataclasses that can be serialized by the
   Temporal data converter.
3. Implement `AgentProvider` with pure deterministic conversion methods.
4. Write message conversion helpers to and from `AgentMessage`.
5. Write the Temporal activity that calls the vendor SDK and returns the
   provider response.
6. Add streaming event translation and activity heartbeats if the provider
   streams.
7. Add an optional convenience `Agent` subclass if app code should construct the
   provider with a small provider-specific surface.
8. Export any public provider types intentionally.
9. Add smoke tests or replay coverage for request/response serialization,
   message conversion, tool-call round trips, refusals, and cancellation.

Keep provider modules narrow: the generic harness should remain provider
agnostic, and the provider should remain an adapter around the vendor API.
