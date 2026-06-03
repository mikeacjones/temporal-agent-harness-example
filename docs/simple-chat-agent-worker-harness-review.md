# Simple Chat Worker And Harness Review

This tracks the current design review findings for `simple_chat_agent/worker` and
`agent_harness`, plus the decisions made while triaging them.

## Current Decisions

- `simple_chat_agent` intentionally exposes Claude only for now. The Gemini and
  ChatGPT providers are harness examples and future extension points, not an app
  requirement yet.
- Mutating-tool idempotency is a tool-author responsibility. The harness cannot
  generically know what external side effect a tool performs or how that system
  deduplicates writes.
- Provider calls and tool calls should remain regular Temporal activities unless
  a specific use case needs local activities.

## Work Items

1. Approval waits must not pin workflows indefinitely.
   - Status: implemented in this branch with a one-hour approval expiry.
   - Direct mutating-tool approvals should expire.
   - Delegated subagent approvals should also expire and clear the parent
     pending approval.
   - Interrupt/delete should unblock local approval waits.

2. Tool-author idempotency needs documentation and examples.
   - Status: implemented in harness docs with a stable `ToolContext`
     idempotency-key helper; `create_artifact` uses it for app-owned writes.
   - Mutating tools should accept or derive a stable idempotency key from the
     workflow/tool-use context when the backing system supports it.
   - If the external system has no idempotency mechanism, the tool should make
     retry behavior explicit in its docs and activity options.

3. Transcript queries need byte-oriented bounds.
   - Status: implemented; `state()` is transcript-free, full-transcript query
     removed, and snapshot/page/delta queries now enforce byte budgets.
   - `state()` should stay transcript-free.
   - Paged transcript queries should enforce a response byte budget, not only a
     message-count budget.
   - The legacy full-transcript query should be removed or made internal-only.

4. Exception handling should distinguish user-facing failures from bugs.
   - Expected provider/tool failures can stay LLM-visible.
   - Serialization, schema, invariant, and programmer errors should fail loudly.

5. User registry fan-out should be revisited if chat counts grow.
   - Broadcasting MCP/tool connection changes to every active chat is acceptable
     for this demo but should not become the long-term scaling shape.

6. Replay and workflow tests should cover the durable behavior.
   - Continue-as-new snapshot shape.
   - Approval expiry and interrupt behavior.
   - Provider request/response serialization.
   - Transcript query payload bounds.
