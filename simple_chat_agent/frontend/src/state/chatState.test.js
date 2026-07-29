import assert from "node:assert/strict";
import test from "node:test";

import {
  handleStreamEventInState,
  streamEventNeedsSettledTranscriptDelta,
  streamEventNeedsWorkflowStateRefresh,
} from "./chatState.js";

function initialState() {
  return {
    workflowState: {
      status: "responding",
      transcript: [],
      transcript_offset: 0,
      transcript_total: 0,
      pending_approvals: [],
      queued_message_indices: [],
      artifacts: [],
    },
    workflowStateProjectionRevision: 0,
    workflowTranscriptProjectionRevision: 0,
    streamTurn: null,
    currentAgentSequence: null,
    ignoreAgentUntilStart: false,
    localPending: [],
    resolvingApprovals: new Set(),
    turnTraces: {},
  };
}

function event(kind, agent, payload = {}, extra = {}) {
  return {
    stream_id: "chat-1",
    tool_name: "agent",
    step: null,
    kind,
    payload,
    sequence: 1,
    agent,
    tool_call_id: null,
    ...extra,
  };
}

test("concurrent subagents with colliding turn numbers retain separate traces", () => {
  const main = { id: "chat-1", parent_id: null, kind: "main", label: "Main agent" };
  const sharks = {
    id: "chat-1-subagent-sharks",
    parent_id: "chat-1",
    kind: "subagent",
    label: "Research shark population changes",
  };
  const policy = {
    id: "chat-1-subagent-policy",
    parent_id: "chat-1",
    kind: "subagent",
    label: "Research shark conservation policy",
  };

  let state = initialState();
  state = handleStreamEventInState(
    state,
    event(AgentStart, main, { sequence: 1, provider: "claude" }),
  );
  state = handleStreamEventInState(
    state,
    event(AgentComplete, main, {
      sequence: 1,
      provider: "claude",
      stop_reason: "tool_use",
      text: "I will delegate this research.",
    }),
  );
  state = handleStreamEventInState(
    state,
    event(AgentStart, sharks, { sequence: 1, provider: "claude" }),
  );
  state = handleStreamEventInState(
    state,
    event(AgentStart, policy, { sequence: 1, provider: "claude" }),
  );
  state = handleStreamEventInState(
    state,
    event(AgentTextDelta, sharks, {
      sequence: 1,
      provider: "claude",
      text: "Population findings",
    }),
  );
  state = handleStreamEventInState(
    state,
    event(AgentTextDelta, policy, {
      sequence: 1,
      provider: "claude",
      text: "Policy findings",
    }),
  );
  state = handleStreamEventInState(
    state,
    event(AgentComplete, sharks, {
      sequence: 1,
      provider: "claude",
      stop_reason: "end_turn",
      text: "Population findings complete.",
    }),
  );

  assert.notEqual(state.streamTurn.status, "complete");
  const childSegments = state.streamTurn.segments.filter(
    (segment) => segment.type === "agent" && segment.agentKind === "subagent",
  );
  assert.equal(childSegments.length, 2);
  assert.equal(
    childSegments.find((segment) => segment.agentId === sharks.id).text,
    "Population findings complete.",
  );
  assert.equal(
    childSegments.find((segment) => segment.agentId === policy.id).text,
    "Policy findings",
  );

  state = handleStreamEventInState(
    state,
    event(AgentStart, main, { sequence: 2, provider: "claude" }),
  );
  state = handleStreamEventInState(
    state,
    event(AgentTextDelta, main, {
      sequence: 2,
      provider: "claude",
      text: "Synthesizing delegated findings",
    }),
  );

  const resumedMain = state.streamTurn.segments.find(
    (segment) =>
      segment.type === "agent" && segment.agentKind === "main" && segment.sequence === 2,
  );
  assert.equal(resumedMain.text, "Synthesizing delegated findings");
});

test("only main-agent completions trigger workflow reconciliation boundaries", () => {
  const main = { id: "chat-1", parent_id: null, kind: "main", label: "Main agent" };
  const child = {
    id: "chat-1-subagent",
    parent_id: "chat-1",
    kind: "subagent",
    label: "Research one topic",
  };
  const mainToolUse = event(AgentComplete, main, { stop_reason: "tool_use" });
  const childToolUse = event(AgentComplete, child, { stop_reason: "tool_use" });
  const mainDone = event(AgentComplete, main, { stop_reason: "end_turn" });
  const childDone = event(AgentComplete, child, { stop_reason: "end_turn" });

  assert.equal(streamEventNeedsWorkflowStateRefresh(mainToolUse), true);
  assert.equal(streamEventNeedsWorkflowStateRefresh(childToolUse), false);
  assert.equal(streamEventNeedsSettledTranscriptDelta(mainDone), true);
  assert.equal(streamEventNeedsSettledTranscriptDelta(childDone), false);
});

const AgentStart = "agent_start";
const AgentTextDelta = "agent_text_delta";
const AgentComplete = "agent_complete";
