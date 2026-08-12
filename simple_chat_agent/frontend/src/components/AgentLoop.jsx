import { useMemo, useState } from "react";

import { CodeBlock } from "./MarkdownContent.jsx";
import { AgentStreamEventKind } from "../state/streamEvents.js";

const FIXED_STAGES = [
  {
    id: "request",
    label: "Request",
    shortLabel: "Input",
    description: "The user message or delegated task enters the agent loop.",
  },
  {
    id: "inputGuard",
    label: "Input guards",
    shortLabel: "LLM guard",
    description: "Pre-model guards inspect or transform the request and conversation state.",
  },
  {
    id: "llm",
    label: "LLM call",
    shortLabel: "Model",
    description: "The model reasons, emits text, or asks the harness to run a tool.",
  },
  {
    id: "outputGuard",
    label: "Output guards",
    shortLabel: "LLM guard",
    description: "Post-model guards inspect or transform the model response before it is used.",
  },
  {
    id: "decision",
    label: "Route response",
    shortLabel: "Decision",
    description: "The harness either emits the response or routes each requested tool call.",
  },
  {
    id: "preToolGuard",
    label: "Tool guards",
    shortLabel: "Pre-tool",
    description: "Pre-tool policy and approval guards decide whether the tool may execute.",
  },
  {
    id: "tool",
    label: "Tool execution",
    shortLabel: "Tool",
    description: "The selected tool and any routed Temporal Activities execute here.",
  },
  {
    id: "postToolGuard",
    label: "Result guards",
    shortLabel: "Post-tool",
    description: "Post-tool guards inspect the result before it returns to the model loop.",
  },
  {
    id: "response",
    label: "Response",
    shortLabel: "Output",
    description: "The final guarded response is emitted to the user.",
  },
];

export function AgentLoop({
  timeline,
  agents,
  activeAgentId,
  onSelectAgent,
}) {
  const [selectedStageId, setSelectedStageId] = useState(null);
  const [selectedOperationId, setSelectedOperationId] = useState(null);
  const model = useMemo(
    () => buildHarnessLoopModel(timeline, agents, activeAgentId),
    [timeline, agents, activeAgentId],
  );
  const activeAgent =
    agents.find((agent) => agent.id === activeAgentId) ||
    agents.find((agent) => agent.kind === "main") ||
    null;
  const inspectedStage =
    model.stages.find((stage) => stage.id === selectedStageId) ||
    model.stages.find((stage) => stage.id === model.currentStageId) ||
    model.stages[0] ||
    null;
  const inspectedOperation =
    inspectedStage?.operations.find(
      (operation) => operation.id === selectedOperationId,
    ) ||
    inspectedStage?.latestOperation ||
    null;
  const visibleOperations = inspectedStage
    ? [...inspectedStage.operations].sort(
        (left, right) => right.order - left.order,
      )
    : [];

  return (
    <div className="harness-flow-shell">
      <div className="harness-flow-toolbar">
        <div className="harness-flow-breadcrumbs">
          <strong>Agent loop</strong>
          <span>›</span>
          <span>
            {activeAgent?.kind === "subagent"
              ? compactText(activeAgent.label, 70)
              : "Main agent"}
          </span>
        </div>
        <span>
          {model.currentStage?.label || "Waiting"}
          {model.iteration ? ` · turn ${model.iteration}` : ""}
        </span>
      </div>

      <div className="harness-agent-picker" aria-label="Select agent loop">
        {agents.map((agent) => (
          <button
            key={agent.id}
            type="button"
            className={agent.id === activeAgent?.id ? "active" : ""}
            onClick={() => {
              onSelectAgent(agent.id);
              setSelectedStageId(null);
              setSelectedOperationId(null);
            }}
          >
            <span
              className={`stream-agent-status-dot ${agent.status || "waiting"}`}
              aria-hidden="true"
            />
            <span>
              {agent.kind === "main"
                ? "Main agent"
                : compactText(agent.label, 54)}
            </span>
            <small>{agent.status || "waiting"}</small>
          </button>
        ))}
      </div>

      <div className="harness-flow-workspace">
        <div className="harness-loop-scroll">
          <div className="harness-loop-diagram" aria-label="Agent harness stages">
            {model.stages.map((stage) => {
              const latest = stage.latestOperation;
              return (
                <button
                  key={stage.id}
                  type="button"
                  className={`harness-stage stage-${stage.id} ${stage.status}${
                    stage.id === model.currentStageId ? " current" : ""
                  }${stage.id === inspectedStage?.id ? " selected" : ""}`}
                  aria-current={
                    stage.id === model.currentStageId ? "step" : undefined
                  }
                  onClick={() => {
                    setSelectedStageId(stage.id);
                    setSelectedOperationId(null);
                  }}
                >
                  <div className="harness-stage-heading">
                    <span>{stage.shortLabel}</span>
                    <span className={`harness-status ${stage.status}`}>
                      {stage.id === model.currentStageId
                        ? "current"
                        : stage.status}
                    </span>
                  </div>
                  <strong>{stage.label}</strong>
                  <small>
                    {latest
                      ? compactText(latest.label || latest.subtitle, 72)
                      : stage.description}
                  </small>
                  <div className="harness-stage-footer">
                    <span>
                      {stage.operations.length
                        ? `${stage.operations.length} ${
                            stage.operations.length === 1
                              ? "operation"
                              : "operations"
                          }`
                        : "Waiting"}
                    </span>
                    {stage.retryCount ? (
                      <span className="retry">{stage.retryCount} retry</span>
                    ) : null}
                    {stage.failureCount ? (
                      <span className="failed">
                        {stage.failureCount} failed
                      </span>
                    ) : null}
                  </div>
                </button>
              );
            })}

            <div className="harness-connector connector-request" aria-hidden="true">
              →
            </div>
            <div className="harness-connector connector-input-guard" aria-hidden="true">
              →
            </div>
            <div className="harness-connector connector-llm" aria-hidden="true">
              →
            </div>
            <div className="harness-connector connector-output-guard" aria-hidden="true">
              →
            </div>
            <div className="harness-connector connector-response" aria-hidden="true">
              →
            </div>
            <div className="harness-branch-label branch-tool" aria-hidden="true">
              tool requested ↓
            </div>
            <div className="harness-connector connector-pre-tool" aria-hidden="true">
              ←
            </div>
            <div className="harness-connector connector-tool" aria-hidden="true">
              ←
            </div>
            <div className="harness-loop-return" aria-hidden="true">
              <span>↑</span>
              <strong>tool result starts the next model turn</strong>
            </div>
            <div className="harness-branch-label branch-final" aria-hidden="true">
              final response →
            </div>
          </div>
        </div>

        <aside className="harness-node-inspector">
          {inspectedStage ? (
            <>
              <div className="harness-node-inspector-heading">
                <div>
                  <span>{inspectedStage.shortLabel}</span>
                  <strong>{inspectedStage.label}</strong>
                </div>
                {selectedStageId ? (
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedStageId(null);
                      setSelectedOperationId(null);
                    }}
                  >
                    Follow current
                  </button>
                ) : null}
              </div>
              <p className="harness-stage-description">
                {inspectedStage.description}
              </p>
              <div className="harness-node-inspector-status">
                <span className={`harness-status ${inspectedStage.status}`}>
                  {inspectedStage.status}
                </span>
                <span>
                  {inspectedStage.operations.length} recorded{" "}
                  {inspectedStage.operations.length === 1
                    ? "operation"
                    : "operations"}
                </span>
              </div>

              {visibleOperations.length ? (
                <div className="harness-operation-list">
                  {visibleOperations.map((operation) => (
                    <button
                      key={operation.id}
                      type="button"
                      className={
                        operation.id === inspectedOperation?.id ? "active" : ""
                      }
                      onClick={() => setSelectedOperationId(operation.id)}
                    >
                      <span>
                        <strong>{operation.label}</strong>
                        <small>
                          {operation.sequence
                            ? `Turn ${operation.sequence}`
                            : "Harness"}
                          {operation.attempt
                            ? ` · attempt ${operation.attempt}`
                            : ""}
                        </small>
                      </span>
                      <span className={`harness-status ${operation.status}`}>
                        {operation.status}
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="harness-stage-empty">
                  No activity has reached this stage yet.
                </div>
              )}

              {inspectedOperation ? (
                <div className="harness-operation-detail">
                  <div className="harness-operation-detail-heading">
                    <div>
                      <span>{nodeKindLabel(inspectedOperation.kind)}</span>
                      <strong>{inspectedOperation.label}</strong>
                    </div>
                    {inspectedOperation.duration ? (
                      <small>{inspectedOperation.duration}</small>
                    ) : null}
                  </div>
                  {inspectedOperation.error ? (
                    <div className="harness-node-error">
                      <strong>
                        {inspectedOperation.error.type || "Operation failed"}
                      </strong>
                      <span>
                        {inspectedOperation.error.message ||
                          "The operation failed."}
                      </span>
                    </div>
                  ) : null}
                  {inspectedOperation.childAgentId ? (
                    <button
                      type="button"
                      className="harness-open-child"
                      onClick={() => {
                        onSelectAgent(inspectedOperation.childAgentId);
                        setSelectedStageId(null);
                        setSelectedOperationId(null);
                      }}
                    >
                      Open child agent loop →
                    </button>
                  ) : null}
                  <CodeBlock
                    source={JSON.stringify(
                      inspectedOperation.detail || {},
                      null,
                      2,
                    )}
                    languageHint="json"
                    compact
                    wrapLongLines
                  />
                </div>
              ) : null}
            </>
          ) : (
            <div className="harness-stage-empty">
              Waiting for the agent loop to start.
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

export function buildHarnessLoopModel(_timeline, agents, activeAgentId) {
  const activeAgent =
    agents.find((agent) => agent.id === activeAgentId) ||
    agents.find((agent) => agent.kind === "main") ||
    agents[0];
  if (!activeAgent) {
    return {
      stages: FIXED_STAGES.map((stage) => ({
        ...stage,
        status: "waiting",
        operations: [],
        latestOperation: null,
        retryCount: 0,
        failureCount: 0,
      })),
      currentStageId: "request",
      currentStage: FIXED_STAGES[0],
      iteration: 0,
      operations: [],
    };
  }

  const operations = new Map();
  const agentSegments = activeAgent.segments
    .filter((segment) => segment.type === "agent")
    .sort(
      (left, right) =>
        Number(left.sequence || 0) - Number(right.sequence || 0) ||
        Number(left.attempt || 0) - Number(right.attempt || 0),
    );
  const events = activeAgent.segments
    .filter((segment) => segment.type === "tools")
    .flatMap((segment) => segment.events || []);
  const setOperation = (id, operation) => {
    const existing = operations.get(id) || {};
    operations.set(id, {
      ...existing,
      ...operation,
      id,
      detail: {
        ...(existing.detail || {}),
        ...(operation.detail || {}),
      },
    });
  };

  setOperation("request", {
    stageId: "request",
    kind: "input",
    label: activeAgent.kind === "main" ? "User input" : "Delegated task",
    subtitle: activeAgent.label,
    status: activeAgent.queued ? "waiting" : "complete",
    sequence: 0,
    order: 0,
    detail: {
      agent_id: activeAgent.id,
      agent_kind: activeAgent.kind,
      task: activeAgent.label,
    },
  });

  for (const segment of agentSegments) {
    const sequence = Number(segment.sequence || 0);
    const attempt = Number(
      segment.activityAttempt > 1
        ? segment.activityAttempt
        : segment.streamAttempt || 1,
    );
    const id = `${activeAgent.id}:llm:${segment.sequence ?? "unknown"}:attempt:${
      segment.attempt ?? attempt
    }`;
    setOperation(id, {
      baseOperationId: `${activeAgent.id}:llm:${
        segment.sequence ?? "unknown"
      }`,
      stageId: "llm",
      kind: "llm",
      label: segment.model || segment.provider || "Model call",
      subtitle: `Agent turn ${segment.sequence ?? "—"}`,
      status: normalizeStatus(segment.status),
      sequence,
      attempt,
      usage: segment.usage,
      error: segment.error,
      startedAt: segment.startedAt,
      completedAt: segment.completedAt,
      order: sequence * 100_000 + 20_000 + attempt * 1_000,
      detail: {
        provider: segment.provider,
        model: segment.model,
        sequence: segment.sequence,
        stream_attempt: segment.streamAttempt,
        activity_attempt: segment.activityAttempt,
        stop_reason: segment.stopReason,
        stop_details: segment.stopDetails,
        usage: segment.usage,
        error: segment.error,
      },
    });
  }

  events.forEach((event, eventIndex) => {
    const payload = event.payload || {};
    const sequence = Number(payload.llm_sequence || payload.sequence || 0);

    if (event.kind === AgentStreamEventKind.AGENT_TOOL_INPUT_COMPLETE) {
      const toolCallId = payload.tool_use_id || event.tool_call_id;
      if (!toolCallId) return;
      setOperation(`decision:${toolCallId}`, {
        stageId: "decision",
        kind:
          payload.tool_name === "create_subagent" ? "subagent" : "toolDecision",
        label: payload.tool_name || event.tool_name || "Tool requested",
        subtitle:
          payload.tool_name === "create_subagent"
            ? compactText(payload.input?.task || "Delegated workstream", 82)
            : compactInput(payload.input),
        status: "complete",
        sequence,
        parentOperationId: `${activeAgent.id}:llm:${
          payload.sequence ?? "unknown"
        }`,
        order: sequence * 100_000 + 40_000 + eventIndex,
        detail: {
          tool_call_id: toolCallId,
          tool_name: payload.tool_name || event.tool_name,
          input: payload.input,
          input_preview: payload.input_preview,
        },
      });
      return;
    }

    if (event.kind?.startsWith("artifact_create")) {
      const operationId =
        payload.operation_id ||
        `${event.tool_call_id || "artifact"}:${
          payload.artifact_id || "create"
        }`;
      setOperation(operationId, {
        stageId: "tool",
        kind: "artifact",
        label: payload.filename || payload.name || "HTML artifact",
        status:
          event.kind === "artifact_create_rejected"
            ? "blocked"
            : lifecycleStatus(event.kind, payload),
        sequence,
        parentOperationId: event.tool_call_id || null,
        error: payload.error,
        order: sequence * 100_000 + 60_000 + eventIndex,
        detail: {
          ...payload,
          event_kind: event.kind,
        },
      });
      return;
    }

    if (!event.kind?.startsWith("harness_")) return;
    const baseOperationId = payload.operation_id;
    if (!baseOperationId) return;
    const parentOperation = operations.get(payload.parent_operation_id);
    const workflowRetry = event.kind === "harness_llm_retry";
    const activityOperation =
      event.kind.includes("_activity_") && payload.activity_attempt;
    const operationId = workflowRetry
      ? `${baseOperationId}:workflow-attempt:${
          payload.failed_attempt || 1
        }`
      : activityOperation
        ? `${baseOperationId}:attempt:${payload.activity_attempt}`
        : baseOperationId;

    let stageId = "tool";
    let kind = "tool";
    let phase = 60_000;
    if (event.kind.includes("llm_guard")) {
      stageId = payload.timing === "post" ? "outputGuard" : "inputGuard";
      kind = "llmGuard";
      phase = payload.timing === "post" ? 30_000 : 10_000;
    } else if (event.kind.includes("tool_guard")) {
      stageId =
        payload.timing === "post" ? "postToolGuard" : "preToolGuard";
      kind = String(payload.guard_name || "")
        .toLowerCase()
        .includes("approval")
        ? "approval"
        : "toolGuard";
      phase = payload.timing === "post" ? 70_000 : 50_000;
    } else if (event.kind.includes("_activity_")) {
      stageId =
        parentOperation?.stageId ||
        (String(payload.parent_operation_id || "").includes(":guard:post:")
          ? "postToolGuard"
          : payload.guard_name
            ? "preToolGuard"
            : "tool");
      kind = "activity";
      phase =
        stageId === "inputGuard"
          ? 10_000
          : stageId === "outputGuard"
            ? 30_000
            : stageId === "preToolGuard"
              ? 50_000
              : stageId === "postToolGuard"
                ? 70_000
                : 60_000;
    } else if (workflowRetry) {
      stageId = "llm";
      kind = "llm";
      phase = 20_000;
    }

    const resolvedSequence = Number(
      sequence || parentOperation?.sequence || 0,
    );
    const existing = operations.get(operationId);
    setOperation(operationId, {
      baseOperationId,
      stageId,
      kind,
      label: harnessEventLabel(kind, payload),
      subtitle: harnessEventSubtitle(kind, payload),
      status: lifecycleStatus(event.kind, payload),
      sequence: resolvedSequence,
      attempt: workflowRetry
        ? Number(payload.failed_attempt || 1)
        : activityOperation
          ? Number(payload.activity_attempt)
          : existing?.attempt,
      parentOperationId:
        payload.parent_operation_id || existing?.parentOperationId || null,
      timing: payload.timing || existing?.timing,
      error: payload.error || existing?.error,
      startedAt: event.kind.endsWith("_start")
        ? event.emitted_at || existing?.startedAt
        : existing?.startedAt,
      completedAt:
        event.kind.endsWith("_complete") || event.kind.endsWith("_failed")
          ? event.emitted_at || existing?.completedAt
          : existing?.completedAt,
      order: workflowRetry
        ? resolvedSequence * 100_000 +
          phase +
          Number(payload.next_attempt || payload.failed_attempt || 1) * 1_000 -
          1
        : resolvedSequence * 100_000 + phase + eventIndex,
      detail: {
        ...payload,
        events: [...(existing?.detail?.events || []), event.kind],
      },
    });
  });

  for (const child of agents.filter((agent) => agent.kind === "subagent")) {
    if (!child.parentToolCallId) continue;
    for (const operation of operations.values()) {
      if (
        operation.id !== child.parentToolCallId &&
        operation.id !== `decision:${child.parentToolCallId}` &&
        operation.parentOperationId !== child.parentToolCallId
      ) {
        continue;
      }
      setOperation(operation.id, {
        childAgentId: child.id,
        kind:
          operation.stageId === "decision" ? "subagent" : operation.kind,
        detail: {
          child_agent_id: child.id,
          child_task: child.label,
          child_status: child.status,
        },
      });
    }
  }

  const maxSequence = Math.max(
    0,
    ...agentSegments.map((segment) => Number(segment.sequence || 0)),
    ...events.map((event) =>
      Number(event.payload?.llm_sequence || event.payload?.sequence || 0),
    ),
  );
  if (activeAgent.status === "complete" || activeAgent.status === "error") {
    setOperation("response", {
      stageId: "response",
      kind: "output",
      label:
        activeAgent.status === "complete"
          ? "Final response emitted"
          : "Agent stopped with an error",
      status: activeAgent.status === "complete" ? "complete" : "failed",
      sequence: maxSequence,
      error:
        activeAgent.status === "error"
          ? { message: activeAgent.latestAction }
          : null,
      order: (maxSequence + 1) * 100_000 + 90_000,
      detail: {
        agent_id: activeAgent.id,
        agent_status: activeAgent.status,
        completed_at: activeAgent.completedAt,
      },
    });
  }

  const operationList = [...operations.values()]
    .map((operation) => ({
      ...operation,
      duration: formatDuration(
        operation.startedAt,
        operation.completedAt,
      ),
    }))
    .sort((left, right) => left.order - right.order);
  const latestOperation =
    operationList[operationList.length - 1] || operations.get("request");
  const currentStageId =
    activeAgent.status === "complete" || activeAgent.status === "error"
      ? "response"
      : latestOperation?.stageId || "request";
  const stages = FIXED_STAGES.map((stage) => {
    const stageOperations = operationList.filter(
      (operation) => operation.stageId === stage.id,
    );
    const latest =
      stageOperations[stageOperations.length - 1] || null;
    return {
      ...stage,
      status: latest?.status || "waiting",
      operations: stageOperations,
      latestOperation: latest,
      retryCount: stageOperations.filter(
        (operation) =>
          operation.status === "retrying" || Number(operation.attempt || 1) > 1,
      ).length,
      failureCount: stageOperations.filter(
        (operation) => operation.status === "failed",
      ).length,
    };
  });

  return {
    stages,
    currentStageId,
    currentStage: stages.find((stage) => stage.id === currentStageId),
    iteration: maxSequence,
    operations: operationList,
  };
}

function harnessEventLabel(kind, payload) {
  if (kind === "llmGuard" || kind === "toolGuard" || kind === "approval") {
    return payload.guard_name || "Guard";
  }
  if (kind === "activity") {
    return String(payload.activity_name || "Activity")
      .split(":")
      .pop()
      .split(".")
      .pop();
  }
  if (kind === "llm") return payload.model || payload.provider || "Model retry";
  return payload.tool_name || "Tool call";
}

function harnessEventSubtitle(kind, payload) {
  if (kind === "llmGuard" || kind === "toolGuard" || kind === "approval") {
    return `${payload.timing || "pre"} guard`;
  }
  if (kind === "activity") {
    return payload.guard_name
      ? `Guard activity · ${payload.guard_name}`
      : `Tool activity · ${payload.tool_name || "tool"}`;
  }
  if (kind === "llm") return "Provider retry";
  return payload.tool_type ? String(payload.tool_type) : "";
}

function lifecycleStatus(kind, payload) {
  if (kind.endsWith("_failed")) return "failed";
  if (kind.endsWith("_start")) {
    return payload.status === "retrying" ? "retrying" : "running";
  }
  return normalizeStatus(payload.status || "complete");
}

function normalizeStatus(status) {
  const normalized = String(status || "waiting").toLowerCase();
  if (["streaming", "running", "tooling"].includes(normalized)) return "running";
  if (["error", "failed", "cancelled", "interrupted"].includes(normalized)) {
    return "failed";
  }
  if (
    ["block", "blocked", "terminate", "terminated", "refusal"].includes(
      normalized,
    )
  ) {
    return "blocked";
  }
  if (["passed", "complete", "completed", "done"].includes(normalized)) {
    return "complete";
  }
  if (normalized === "retrying") return "retrying";
  return "waiting";
}

function nodeKindLabel(kind) {
  return (
    {
      input: "Input",
      llm: "LLM call",
      llmGuard: "LLM guard",
      toolGuard: "Tool guard",
      approval: "Approval",
      toolDecision: "Tool request",
      tool: "Tool",
      activity: "Activity",
      subagent: "Subagent",
      artifact: "Artifact",
      output: "Output",
    }[kind] || "Operation"
  );
}

function compactInput(input) {
  if (!input || typeof input !== "object") return "";
  const value = input.query || input.url || input.path || input.filename;
  return compactText(value || JSON.stringify(input), 82);
}

function compactText(value, limit) {
  const text = String(value || "")
    .replace(/\s+/g, " ")
    .trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}…`;
}

function formatDuration(startedAt, completedAt) {
  const start = Date.parse(startedAt);
  const end = Date.parse(completedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const milliseconds = end - start;
  if (milliseconds < 1_000) return `${milliseconds}ms`;
  return `${(milliseconds / 1_000).toFixed(1)}s`;
}
