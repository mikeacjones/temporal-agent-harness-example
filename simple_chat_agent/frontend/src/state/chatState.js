export function updateWorkflowStateInState(previous, nextWorkflowState) {
  const normalized = normalizeWorkflowState(nextWorkflowState, previous.workflowState);
  const previousAssistantCount = previous.workflowState
    ? previous.workflowState.transcript.filter((message) => message.role === "assistant").length
    : 0;
  const nextAssistantCount = normalized.transcript.filter(
    (message) => message.role === "assistant",
  ).length;
  const resolvingApprovals = new Set(previous.resolvingApprovals);
  const pendingApprovalIds = new Set(
    (normalized.pending_approvals || []).map((approval) => approval.approval_id),
  );
  for (const approvalId of resolvingApprovals) {
    if (!pendingApprovalIds.has(approvalId)) resolvingApprovals.delete(approvalId);
  }
  let next = {
    ...previous,
    workflowState: normalized,
    workflowStateProjectionRevision: Math.max(
      previous.workflowStateProjectionRevision || 0,
      Number(normalized.state_revision || 0),
    ),
    workflowTranscriptProjectionRevision: Math.max(
      previous.workflowTranscriptProjectionRevision || 0,
      Number(normalized.transcript_revision || 0),
    ),
    localPending: previous.localPending.filter(
      (pending) => !isAcknowledged(pending, normalized),
    ),
    resolvingApprovals,
    statusNotice: "",
  };
  if (nextAssistantCount > previousAssistantCount) next = markStreamCommittedInState(next);
  if (!hasLiveWorkflowActivity(next, normalized)) next = markStreamCommittedInState(next);
  return next;
}

function normalizeWorkflowState(nextWorkflowState, previousWorkflowState = null) {
  return {
    ...nextWorkflowState,
    transcript: nextWorkflowState.transcript || [],
    pending_approvals: nextWorkflowState.pending_approvals || [],
    queued_message_indices: nextWorkflowState.queued_message_indices || [],
    artifacts: nextWorkflowState.artifacts || previousWorkflowState?.artifacts || [],
  };
}

export function createPendingMessage(label, content, phase, state) {
  return {
    id: crypto.randomUUID(),
    label,
    content,
    phase,
    transcriptIndex: state.workflowState?.transcript?.length || 0,
  };
}

export function visibleMessageItems(transcript, localPending) {
  const pendingByIndex = new Map();
  for (const pending of localPending) {
    if (isPendingAcknowledgedByTranscript(pending, transcript)) continue;
    const index = pendingTranscriptIndex(pending, transcript.length);
    const pendingAtIndex = pendingByIndex.get(index) || [];
    pendingAtIndex.push(pending);
    pendingByIndex.set(index, pendingAtIndex);
  }

  const items = [];
  for (let index = 0; index <= transcript.length; index += 1) {
    for (const pending of pendingByIndex.get(index) || []) {
      items.push({ kind: "pending", pending });
    }
    if (index < transcript.length) {
      items.push({ kind: "transcript", message: transcript[index], index });
    }
  }
  return items;
}

function pendingTranscriptIndex(pending, transcriptLength) {
  const index = Number(pending.transcriptIndex);
  if (!Number.isFinite(index)) return transcriptLength;
  return Math.max(0, Math.min(transcriptLength, index));
}

export function handleStreamEventInState(previous, event) {
  const projectionResult = applyWorkflowProjectionEventInState(previous, event);
  if (projectionResult.handled) return projectionResult.state;

  const artifactResult = applyArtifactStreamEventInState(previous, event);
  if (artifactResult.handled) return artifactResult.state;

  if (!hasLiveWorkflowActivity(previous)) return previous;

  const next = {
    ...previous,
    streamTurn: cloneStreamTurn(previous.streamTurn),
  };
  const sequence = event.payload?.sequence ?? null;

  if (event.kind === "claude_start") {
    next.currentClaudeSequence = sequence;
    next.ignoreClaudeUntilStart = false;
    if (isOpenStreamTurn(next.streamTurn)) {
      registerStreamSequence(next.streamTurn, sequence);
      next.streamTurn.status = "streaming";
      next.streamTurn.activeSequence = sequence;
    }
  } else if (event.kind === "claude_text_delta" && event.payload?.text) {
    if (next.ignoreClaudeUntilStart || sequence !== next.currentClaudeSequence) return previous;
    const turn = ensureStreamTurn(next, sequence);
    turn.status = "streaming";
    turn.text += event.payload.text;
  } else if (event.kind === "claude_thinking_start") {
    if (next.ignoreClaudeUntilStart || sequence !== next.currentClaudeSequence) return previous;
    const turn = ensureStreamTurn(next, sequence);
    turn.status = "streaming";
  } else if (event.kind === "claude_thinking_delta" && event.payload?.thinking) {
    if (next.ignoreClaudeUntilStart || sequence !== next.currentClaudeSequence) return previous;
    const turn = ensureStreamTurn(next, sequence);
    turn.status = "streaming";
    turn.thinking += event.payload.thinking;
  } else if (event.kind === "claude_cancelled") {
    if (sequence === next.currentClaudeSequence) {
      return {
        ...markStreamInterruptedInState(next),
        ignoreClaudeUntilStart: true,
      };
    }
  } else if (event.kind === "claude_complete") {
    const terminal = isTerminalClaudeStop(event.payload || {});
    if (!terminal && sequence !== next.currentClaudeSequence) return previous;
    const turn = streamTurnForSequence(next.streamTurn, sequence) || ensureStreamTurn(next, sequence);
    if (turn) {
      finishStreamClaudeTurn(turn, event.payload || {});
      turn.status = terminal ? "complete" : turn.currentEvents.length ? "tooling" : "waiting";
      turn.lastClaudeCompletedAt = new Date().toISOString();
    }
    if (terminal) {
      return next;
    }
  } else if (isClaudeToolEvent(event)) {
    const turn = ensureStreamTurn(next, next.currentClaudeSequence);
    appendStreamToolEvent(turn, event);
    if (turn.status !== "complete" && turn.status !== "interrupted") {
      turn.status = "tooling";
    }
  } else if (!event.kind?.startsWith("claude_")) {
    const turn = ensureStreamTurn(next, next.currentClaudeSequence);
    appendStreamToolEvent(turn, event);
    if (turn.status !== "complete" && turn.status !== "interrupted") {
      turn.status = "tooling";
    }
  }
  return next;
}

function applyWorkflowProjectionEventInState(previous, event) {
  if (!previous.workflowState) return { handled: false, state: previous };
  const payload = event.payload || {};

  if (event.kind === "workflow_state") {
    const revision = Number(payload.revision || 0);
    if (revision && revision <= previous.workflowStateProjectionRevision) {
      return { handled: true, state: previous };
    }
    const { revision: _revision, transcript_revision: _transcriptRevision, ...patch } = payload;
    return {
      handled: true,
      state: updateWorkflowStateInState(previous, {
        ...previous.workflowState,
        ...patch,
        state_revision: revision || previous.workflowState.state_revision || 0,
        transcript_revision: previous.workflowState.transcript_revision || 0,
        transcript: previous.workflowState.transcript || [],
        artifacts: previous.workflowState.artifacts || [],
      }),
    };
  }

  if (event.kind === "workflow_transcript") {
    const revision = Number(payload.revision || 0);
    if (revision && revision <= previous.workflowTranscriptProjectionRevision) {
      return { handled: true, state: previous };
    }
    return {
      handled: true,
      state: updateWorkflowStateInState(previous, {
        ...previous.workflowState,
        transcript: payload.transcript || [],
        transcript_revision: revision || previous.workflowState.transcript_revision || 0,
        artifacts: previous.workflowState.artifacts || [],
      }),
    };
  }

  return { handled: false, state: previous };
}

function applyArtifactStreamEventInState(previous, event) {
  if (event.kind !== "artifact_create_complete" || !previous.workflowState) {
    return { handled: false, state: previous };
  }
  const artifact = event.payload || {};
  if (!artifact.artifact_id) return { handled: true, state: previous };
  const artifacts = previous.workflowState.artifacts || [];
  if (artifacts.some((existing) => existing.artifact_id === artifact.artifact_id)) {
    return { handled: true, state: previous };
  }
  return {
    handled: true,
    state: {
      ...previous,
      workflowState: {
        ...previous.workflowState,
        artifacts: [...artifacts, artifact],
      },
    },
  };
}

function ensureStreamTurn(state, sequence) {
  if (!isOpenStreamTurn(state.streamTurn)) {
    state.streamTurn = createStreamTurn(sequence);
  } else {
    registerStreamSequence(state.streamTurn, sequence);
  }
  return state.streamTurn;
}

function streamTurnForSequence(turn, sequence) {
  if (!isOpenStreamTurn(turn)) return null;
  if (sequence === null) return turn;
  return turn.sequences.includes(sequence) ? turn : null;
}

function isOpenStreamTurn(turn) {
  return Boolean(turn && turn.status !== "complete" && turn.status !== "interrupted");
}

function registerStreamSequence(turn, sequence) {
  if (sequence !== null && !turn.sequences.includes(sequence)) {
    turn.sequences.push(sequence);
  }
}

function createStreamTurn(sequence) {
  return {
    sequence,
    sequences: sequence === null ? [] : [sequence],
    activeSequence: sequence,
    status: "streaming",
    text: "",
    thinking: "",
    finishedTurns: [],
    currentEvents: [],
    startedAt: new Date().toISOString(),
    completedAt: null,
    lastClaudeCompletedAt: null,
    interrupted: false,
  };
}

function cloneStreamTurn(turn) {
  if (!turn) return null;
  return {
    ...turn,
    sequences: [...turn.sequences],
    finishedTurns: turn.finishedTurns.map((finishedTurn) => ({
      ...finishedTurn,
      events: [...(finishedTurn.events || [])],
    })),
    currentEvents: [...turn.currentEvents],
  };
}

function finishStreamClaudeTurn(turn, payload) {
  const text = String(payload.text || turn.text || "").trim();
  const stopReason = payload.stop_reason || "unknown";
  const sequence = payload.sequence ?? turn.activeSequence;
  turn.finishedTurns.push({
    sequence,
    text,
    thinking: String(turn.thinking || "").trim(),
    stopReason,
    usage: payload.usage || null,
    events: turn.currentEvents,
    completedAt: new Date().toISOString(),
  });
  turn.finishedTurns = turn.finishedTurns.slice(-12);
  turn.text = "";
  turn.thinking = "";
  turn.currentEvents = [];
}

function appendStreamToolEvent(turn, event) {
  const finishedTurn = latestFinishedToolUseTurn(turn);
  if (finishedTurn) {
    finishedTurn.events = mergeStreamToolEvent(finishedTurn.events || [], event);
    return;
  }
  turn.currentEvents = mergeStreamToolEvent(turn.currentEvents, event);
}

function isClaudeToolEvent(event) {
  return event.kind?.startsWith("claude_tool_input_");
}

function mergeStreamToolEvent(events, event) {
  if (!event.kind?.startsWith("claude_tool_input_")) {
    return [...events, event].slice(-5);
  }

  const key = streamToolInputKey(event);
  const nextEvents = [...events];
  const existingIndex = nextEvents.findIndex(
    (candidate) =>
      candidate.kind?.startsWith("claude_tool_input_") &&
      streamToolInputKey(candidate) === key,
  );
  const existing = existingIndex >= 0 ? nextEvents[existingIndex] : null;
  const merged = mergeToolInputEvent(existing, event, key);
  if (existingIndex >= 0) {
    nextEvents[existingIndex] = merged;
  } else {
    nextEvents.push(merged);
  }
  return nextEvents.slice(-5);
}

function mergeToolInputEvent(existing, event, key) {
  const existingPayload = existing?.payload || {};
  const payload = event.payload || {};
  const nextPayload = { ...existingPayload, ...payload };
  const existingPartial = String(existingPayload.input_partial || "");

  if (event.kind === "claude_tool_input_delta") {
    nextPayload.input_partial = existingPartial + String(payload.partial_json || "");
    nextPayload.status = "streaming input";
  } else if (event.kind === "claude_tool_input_complete") {
    nextPayload.input_partial = existingPartial;
    nextPayload.status = "input complete";
  } else {
    nextPayload.input_partial = existingPartial;
    nextPayload.status = "building input";
  }

  return {
    ...(existing || event),
    kind: event.kind,
    payload: nextPayload,
    streamToolInputKey: key,
  };
}

function streamToolInputKey(event) {
  return (
    event.streamToolInputKey ||
    event.payload?.tool_use_id ||
    `block:${event.payload?.content_block_index ?? "unknown"}`
  );
}

function latestFinishedToolUseTurn(turn) {
  const latest = turn.finishedTurns[turn.finishedTurns.length - 1];
  if (!latest || latest.stopReason !== "tool_use") return null;
  return latest;
}

function markStreamCommittedInState(state) {
  return {
    ...state,
    streamTurn: null,
    currentClaudeSequence: null,
    ignoreClaudeUntilStart: false,
  };
}

export function markStreamInterruptedInState(state) {
  return {
    ...state,
    streamTurn: null,
    currentClaudeSequence: null,
  };
}

function hasLiveWorkflowActivity(state, workflowState = state.workflowState) {
  if (!workflowState) return true;
  if (workflowState.status === "responding") return true;
  if (Number(workflowState.pending_messages || 0) > 0) return true;
  return state.localPending.length > 0;
}

function isTerminalClaudeStop(payload) {
  return payload.stop_reason && payload.stop_reason !== "tool_use";
}

function isAcknowledged(pending, workflowState) {
  return isPendingAcknowledgedByTranscript(pending, workflowState.transcript);
}

function isPendingAcknowledgedByTranscript(pending, transcript) {
  return transcript.some((message) => {
    if (message.role === "user" && message.content === pending.content) return true;
    if (message.role === "system" && message.content.includes(pending.content)) return true;
    return false;
  });
}

export function displayStatus(state) {
  if (state.statusNotice) return state.statusNotice;
  const workflowState = state.workflowState;
  const thinkingLabel = workflowState?.thinking?.enabled ? " | thinking" : "";
  const modelLabel = workflowState?.model ? ` | ${workflowState.model}${thinkingLabel}` : "";
  if (state.draftConversation) return "draft | workflow not started";
  if (workflowState) {
    const queued = workflowState.pending_messages
      ? `, queued: ${workflowState.pending_messages}`
      : "";
    return `${workflowState.status}${queued}${modelLabel}`;
  }
  return state.auth === "app" ? "starting..." : "connecting...";
}

export function selectedModelUsesAdaptiveThinking(state) {
  const model = state.agentSettings.model || "";
  return (state.config?.thinking?.adaptive_model_prefixes || []).some((prefix) =>
    model.startsWith(prefix),
  );
}

export function agentSettingsFromConfig(config) {
  const modelOptions = config.model_options || [];
  const savedModel = localStorage.getItem("simpleChatModel");
  const savedEffort = localStorage.getItem("simpleChatThinkingEffort");
  const effortOptions = config.thinking?.effort_options || ["medium"];
  return {
    model: savedModel && modelOptions.includes(savedModel) ? savedModel : config.default_model || "",
    thinkingEnabled: localStorage.getItem("simpleChatThinkingEnabled") === "true",
    thinkingBudgetTokens: Number(
      localStorage.getItem("simpleChatThinkingBudgetTokens") ||
        config.thinking?.budget_tokens ||
        4096,
    ),
    thinkingEffort:
      savedEffort && effortOptions.includes(savedEffort)
        ? savedEffort
        : config.thinking?.effort || "medium",
  };
}

export function saveAgentSettings(agentSettings) {
  localStorage.setItem("simpleChatModel", agentSettings.model);
  localStorage.setItem("simpleChatThinkingEnabled", String(agentSettings.thinkingEnabled));
  localStorage.setItem(
    "simpleChatThinkingBudgetTokens",
    String(agentSettings.thinkingBudgetTokens),
  );
  localStorage.setItem("simpleChatThinkingEffort", agentSettings.thinkingEffort);
}

export function temporalUiUrl(conversation) {
  if (conversation.temporal_ui_url) return conversation.temporal_ui_url;
  const workflow = encodeURIComponent(conversation.workflow_id);
  const run = encodeURIComponent(conversation.run_id || "");
  if (run) {
    return `http://localhost:8233/namespaces/default/workflows/${workflow}/${run}/history`;
  }
  return `http://localhost:8233/namespaces/default/workflows/${workflow}`;
}
