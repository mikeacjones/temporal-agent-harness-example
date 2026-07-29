import { useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { CodeBlock } from "./MarkdownContent.jsx";
import { AgentStreamEventKind } from "../state/streamEvents.js";

const nodeTypes = { harness: HarnessNode };

export function HarnessFlow({
  timeline,
  agents,
  activeAgentId,
  onSelectAgent,
}) {
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const model = useMemo(
    () => buildHarnessFlowModel(timeline, agents, activeAgentId),
    [timeline, agents, activeAgentId],
  );
  const selectedNode =
    model.nodes.find((node) => node.id === selectedNodeId) || null;
  const activeAgent =
    agents.find((agent) => agent.id === activeAgentId) ||
    agents.find((agent) => agent.kind === "main") ||
    null;
  const parentAgent =
    activeAgent?.kind === "subagent"
      ? agents.find((agent) => agent.id === activeAgent.parentId) ||
        agents.find((agent) => agent.kind === "main")
      : null;

  return (
    <div className="harness-flow-shell">
      <div className="harness-flow-toolbar">
        <div className="harness-flow-breadcrumbs">
          {parentAgent ? (
            <>
              <button type="button" onClick={() => onSelectAgent(parentAgent.id)}>
                Main agent
              </button>
              <span>›</span>
            </>
          ) : null}
          <strong>
            {activeAgent?.kind === "subagent"
              ? compactText(activeAgent.label, 70)
              : "Main agent"}
          </strong>
        </div>
        <span>{model.nodes.length} harness operations</span>
      </div>
      <div className="harness-flow-workspace">
        <div className="harness-flow-canvas">
          <ReactFlow
            nodes={model.nodes}
            edges={model.edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.18, maxZoom: 1 }}
            minZoom={0.2}
            maxZoom={1.75}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable
            onNodeClick={(_event, node) => {
              if (node.data.childAgentId) {
                onSelectAgent(node.data.childAgentId);
                setSelectedNodeId(null);
                return;
              }
              setSelectedNodeId(node.id);
            }}
          >
            <Background color="#334155" gap={24} size={1} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
        {selectedNode ? (
          <aside className="harness-node-inspector">
            <div className="harness-node-inspector-heading">
              <div>
                <span>{nodeKindLabel(selectedNode.data.kind)}</span>
                <strong>{selectedNode.data.label}</strong>
              </div>
              <button type="button" onClick={() => setSelectedNodeId(null)}>
                Close
              </button>
            </div>
            <div className="harness-node-inspector-status">
              <span className={`harness-status ${selectedNode.data.status}`}>
                {selectedNode.data.status}
              </span>
              {selectedNode.data.attempt ? (
                <span>Attempt {selectedNode.data.attempt}</span>
              ) : null}
              {selectedNode.data.duration ? (
                <span>{selectedNode.data.duration}</span>
              ) : null}
            </div>
            {selectedNode.data.error ? (
              <div className="harness-node-error">
                <strong>{selectedNode.data.error.type || "Error"}</strong>
                <span>{selectedNode.data.error.message || "The operation failed."}</span>
              </div>
            ) : null}
            <CodeBlock
              source={JSON.stringify(selectedNode.data.detail || {}, null, 2)}
              languageHint="json"
              compact
              wrapLongLines
            />
          </aside>
        ) : (
          <aside className="harness-node-inspector empty">
            Select a node to inspect its status, attempts, inputs, and errors.
          </aside>
        )}
      </div>
    </div>
  );
}

function HarnessNode({ data }) {
  const usage = data.usage || {};
  const inputTokens = usage.input_tokens ?? usage.prompt_tokens;
  const outputTokens = usage.output_tokens ?? usage.completion_tokens;
  return (
    <div
      className={`harness-node ${data.kind} ${data.status}${
        data.childAgentId ? " clickable" : ""
      }`}
    >
      <Handle type="target" position={Position.Left} />
      <div className="harness-node-heading">
        <span>{nodeKindLabel(data.kind)}</span>
        <span className={`harness-status ${data.status}`}>{data.status}</span>
      </div>
      <strong title={data.fullLabel || data.label}>{data.label}</strong>
      {data.subtitle ? <small>{data.subtitle}</small> : null}
      {data.error ? (
        <div className="harness-node-error-brief">
          {compactText(data.error.message || data.error.type || "Call failed", 78)}
        </div>
      ) : null}
      {inputTokens !== undefined || outputTokens !== undefined ? (
        <div className="harness-node-metrics">
          <span>{inputTokens ?? "—"} in</span>
          <span>{outputTokens ?? "—"} out</span>
        </div>
      ) : null}
      {data.attempt && data.attempt > 1 ? (
        <div className="harness-node-attempt">Attempt {data.attempt}</div>
      ) : null}
      {data.childAgentId ? (
        <div className="harness-node-open">Open child agent →</div>
      ) : null}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

export function buildHarnessFlowModel(timeline, agents, activeAgentId) {
  const activeAgent =
    agents.find((agent) => agent.id === activeAgentId) ||
    agents.find((agent) => agent.kind === "main") ||
    agents[0];
  if (!activeAgent) return { nodes: [], edges: [] };

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

  operations.set("input", {
    id: "input",
    kind: "input",
    label: activeAgent.kind === "main" ? "User input" : "Delegated task",
    status: "complete",
    sequence: 0,
    detail: { agent: activeAgent.label },
  });

  for (const segment of agentSegments) {
    const baseOperationId = `${activeAgent.id}:llm:${segment.sequence ?? "unknown"}`;
    const attempt = Number(
      segment.activityAttempt > 1
        ? segment.activityAttempt
        : segment.streamAttempt || 1,
    );
    const id = `${baseOperationId}:attempt:${segment.attempt ?? attempt}`;
    operations.set(id, {
      id,
      baseOperationId,
      kind: "llm",
      label: segment.model || segment.provider || "Model call",
      fullLabel: segment.model || segment.provider || "Model call",
      subtitle: `Agent turn ${segment.sequence ?? "—"}`,
      status: normalizeStatus(segment.status),
      sequence: Number(segment.sequence || 0),
      attempt,
      attemptOrder: Number(segment.attempt || attempt),
      usage: segment.usage,
      error: segment.error,
      startedAt: segment.startedAt,
      completedAt: segment.completedAt,
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

  for (const event of events) {
    const payload = event.payload || {};
    if (event.kind === AgentStreamEventKind.AGENT_TOOL_INPUT_COMPLETE) {
      const operationId = payload.tool_use_id || event.tool_call_id;
      if (!operationId) continue;
      const existing = operations.get(operationId) || {};
      operations.set(operationId, {
        ...existing,
        id: operationId,
        kind: payload.tool_name === "create_subagent" ? "subagent" : "tool",
        label: payload.tool_name || event.tool_name || "Tool call",
        fullLabel: payload.tool_name || event.tool_name || "Tool call",
        subtitle:
          payload.tool_name === "create_subagent"
            ? compactText(payload.input?.task || "Delegated workstream", 82)
            : compactInput(payload.input),
        status: existing.status || "running",
        sequence: Number(payload.sequence || 0),
        parentOperationId: `${activeAgent.id}:llm:${payload.sequence ?? "unknown"}`,
        detail: { input: payload.input, input_preview: payload.input_preview },
      });
      continue;
    }

    if (event.kind?.startsWith("artifact_create")) {
      const operationId =
        payload.operation_id ||
        `${event.tool_call_id || "artifact"}:${payload.artifact_id || "create"}`;
      const existing = operations.get(operationId) || {};
      operations.set(operationId, {
        ...existing,
        id: operationId,
        kind: "artifact",
        label: payload.filename || payload.name || "HTML artifact",
        status: lifecycleStatus(event.kind, payload),
        sequence: Number(payload.llm_sequence || 0),
        parentOperationId: event.tool_call_id || existing.parentOperationId,
        error: payload.error || existing.error,
        detail: { ...(existing.detail || {}), ...payload },
      });
      continue;
    }

    if (!event.kind?.startsWith("harness_")) continue;
    const baseOperationId = payload.operation_id;
    if (!baseOperationId) continue;
    const workflowRetry = event.kind === "harness_llm_retry";
    const activityOperation =
      event.kind.includes("_activity_") && payload.activity_attempt;
    const operationId = workflowRetry
      ? `${baseOperationId}:workflow-attempt:${payload.failed_attempt || 1}`
      : activityOperation
        ? `${baseOperationId}:attempt:${payload.activity_attempt}`
        : baseOperationId;
    const existing = operations.get(operationId) || {};
    const kind = harnessEventNodeKind(event.kind, payload);
    operations.set(operationId, {
      ...existing,
      id: operationId,
      baseOperationId,
      kind,
      label: harnessEventLabel(kind, payload),
      fullLabel: harnessEventLabel(kind, payload),
      subtitle: harnessEventSubtitle(kind, payload),
      status: lifecycleStatus(event.kind, payload),
      sequence: Number(payload.llm_sequence || existing.sequence || 0),
      attempt: workflowRetry
        ? Number(payload.failed_attempt || 1)
        : activityOperation
          ? Number(payload.activity_attempt)
          : existing.attempt,
      parentOperationId:
        payload.parent_operation_id || existing.parentOperationId || null,
      timing: payload.timing || existing.timing,
      error: payload.error || existing.error,
      startedAt:
        event.kind.endsWith("_start")
          ? event.emitted_at || existing.startedAt
          : existing.startedAt,
      completedAt:
        event.kind.endsWith("_complete") || event.kind.endsWith("_failed")
          ? event.emitted_at || existing.completedAt
          : existing.completedAt,
      detail: {
        ...(existing.detail || {}),
        ...payload,
        events: [...(existing.detail?.events || []), event.kind],
      },
    });
  }

  for (const child of agents.filter((agent) => agent.kind === "subagent")) {
    if (!child.parentToolCallId) continue;
    const operation = operations.get(child.parentToolCallId);
    if (!operation) continue;
    operations.set(child.parentToolCallId, {
      ...operation,
      kind: "subagent",
      childAgentId: child.id,
      label: "Child agent",
      subtitle: compactText(child.label, 82),
      status: normalizeStatus(child.status),
      detail: {
        ...(operation.detail || {}),
        child_agent_id: child.id,
        task: child.label,
        child_status: child.status,
      },
    });
  }

  const blockedWithoutModelCall =
    !agentSegments.length &&
    [...operations.values()].some((operation) => operation.status === "blocked");
  operations.set("output", {
    id: "output",
    kind: "output",
    label: "Agent output",
    status:
      activeAgent.status === "complete"
        ? "complete"
        : activeAgent.status === "error"
          ? "failed"
          : blockedWithoutModelCall
            ? "blocked"
            : "waiting",
    sequence: Math.max(1, ...agentSegments.map((segment) => Number(segment.sequence || 0))) + 1,
    detail: { agent_status: activeAgent.status },
  });

  const llmByBase = new Map();
  for (const operation of operations.values()) {
    if (operation.kind !== "llm") continue;
    const attempts = llmByBase.get(operation.baseOperationId) || [];
    attempts.push(operation);
    llmByBase.set(operation.baseOperationId, attempts);
  }
  for (const attempts of llmByBase.values()) {
    attempts.sort(
      (left, right) =>
        Number(left.attemptOrder || left.attempt || 1) -
        Number(right.attemptOrder || right.attempt || 1),
    );
  }

  const semanticNodes = [...operations.values()];
  const edges = [];
  const edgeKeys = new Set();
  const connect = (source, target, options = {}) => {
    if (!source || !target || source === target) return;
    const key = `${source}->${target}:${options.label || ""}`;
    if (edgeKeys.has(key)) return;
    edgeKeys.add(key);
    edges.push({
      id: key,
      source,
      target,
      type: "smoothstep",
      animated: Boolean(options.animated),
      label: options.label,
      className: options.className || "",
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
    });
  };

  for (const attempts of llmByBase.values()) {
    for (let index = 1; index < attempts.length; index += 1) {
      connect(attempts[index - 1].id, attempts[index].id, {
        label: "retry",
        animated: attempts[index].status === "retrying",
        className: "retry",
      });
    }
  }

  const operationForBase = (baseId) => {
    const attempts = llmByBase.get(baseId);
    if (attempts?.length) return attempts[attempts.length - 1].id;
    return operations.has(baseId) ? baseId : null;
  };
  const sequences = [
    ...new Set(
      semanticNodes
        .filter((node) => !["input", "output"].includes(node.kind))
        .map((node) => Number(node.sequence || 0)),
    ),
  ]
    .filter((sequence) => sequence > 0)
    .sort((left, right) => left - right);
  let previousExits = ["input"];

  for (const sequence of sequences) {
    const llmBase = `${activeAgent.id}:llm:${sequence}`;
    const llmAttempts = llmByBase.get(llmBase) || [];
    const preLlmGuards = semanticNodes
      .filter(
        (node) =>
          node.kind === "llmGuard" &&
          node.sequence === sequence &&
          node.timing === "pre",
      )
      .sort(byOperationId);
    if (!llmAttempts.length) {
      const guardEntry = preLlmGuards[0] || postLlmGuards[0];
      if (!guardEntry) continue;
      previousExits.forEach((source) => connect(source, guardEntry.id));
      const guards = [...preLlmGuards, ...postLlmGuards];
      for (let index = 1; index < guards.length; index += 1) {
        connect(guards[index - 1].id, guards[index].id);
      }
      previousExits = [guards[guards.length - 1].id];
      continue;
    }
    const postLlmGuards = semanticNodes
      .filter(
        (node) =>
          node.kind === "llmGuard" &&
          node.sequence === sequence &&
          node.timing === "post",
      )
      .sort(byOperationId);
    const entry = preLlmGuards[0]?.id || llmAttempts[0].id;
    previousExits.forEach((source) => connect(source, entry));
    for (let index = 1; index < preLlmGuards.length; index += 1) {
      connect(preLlmGuards[index - 1].id, preLlmGuards[index].id);
    }
    if (preLlmGuards.length) {
      connect(preLlmGuards[preLlmGuards.length - 1].id, llmAttempts[0].id);
    }
    let llmExit = llmAttempts[llmAttempts.length - 1].id;
    for (const guard of postLlmGuards) {
      connect(llmExit, guard.id);
      llmExit = guard.id;
    }

    const tools = semanticNodes
      .filter(
        (node) =>
          (node.kind === "tool" || node.kind === "subagent") &&
          node.sequence === sequence,
      )
      .sort(byOperationId);
    if (!tools.length) {
      previousExits = [llmExit];
      continue;
    }
    previousExits = [];
    for (const tool of tools) {
      const preGuards = semanticNodes
        .filter(
          (node) =>
            node.kind === "toolGuard" &&
            node.parentOperationId === tool.id &&
            node.timing === "pre",
        )
        .sort(byOperationId);
      const postGuards = semanticNodes
        .filter(
          (node) =>
            node.kind === "toolGuard" &&
            node.parentOperationId === tool.id &&
            node.timing === "post",
        )
        .sort(byOperationId);
      let branch = llmExit;
      for (const guard of preGuards) {
        connect(branch, guard.id);
        branch = guard.id;
      }
      connect(branch, tool.id);
      branch = tool.id;
      for (const guard of postGuards) {
        connect(branch, guard.id);
        branch = guard.id;
      }
      previousExits.push(branch);
    }
  }
  previousExits.forEach((source) => connect(source, "output"));

  const childrenByParent = new Map();
  for (const operation of semanticNodes) {
    if (
      !["activity", "artifact"].includes(operation.kind) ||
      !operation.parentOperationId
    ) {
      continue;
    }
    const parentId = operationForBase(operation.parentOperationId);
    if (!parentId) continue;
    const children = childrenByParent.get(parentId) || [];
    children.push(operation);
    childrenByParent.set(parentId, children);
  }
  for (const [parentId, children] of childrenByParent) {
    children.sort(
      (left, right) =>
        Number(left.attempt || 1) - Number(right.attempt || 1) ||
        byOperationId(left, right),
    );
    let previous = parentId;
    for (const child of children) {
      connect(previous, child.id, {
        label: child.attempt > 1 ? "retry" : undefined,
        animated: child.status === "retrying",
        className: child.attempt > 1 ? "retry" : "",
      });
      previous = child.id;
    }
    for (const edge of edges) {
      if (edge.source !== parentId) continue;
      if (children.some((child) => child.id === edge.target)) continue;
      edge.source = previous;
    }
  }

  const topologicalLayer = new Map([["input", 0]]);
  for (let pass = 0; pass < semanticNodes.length + 2; pass += 1) {
    let changed = false;
    for (const edge of edges) {
      const sourceLayer = topologicalLayer.get(edge.source);
      if (sourceLayer === undefined) continue;
      const nextLayer = sourceLayer + 1;
      if ((topologicalLayer.get(edge.target) ?? -1) < nextLayer) {
        topologicalLayer.set(edge.target, nextLayer);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const rowsAtLayer = new Map();
  const nodes = semanticNodes.map((operation) => {
    const layer =
      topologicalLayer.get(operation.id) ??
      Math.max(1, Number(operation.sequence || 1) * 3);
    const row = rowsAtLayer.get(layer) || 0;
    rowsAtLayer.set(layer, row + 1);
    const duration = formatDuration(operation.startedAt, operation.completedAt);
    return {
      id: operation.id,
      type: "harness",
      position: {
        x: layer * 270,
        y: row * 190 + (operation.kind === "activity" ? 84 : 0),
      },
      data: { ...operation, duration },
    };
  });

  return { nodes, edges };
}

function harnessEventNodeKind(kind, payload) {
  if (kind.includes("llm_guard")) return "llmGuard";
  if (kind.includes("tool_guard")) {
    return String(payload.guard_name || "").toLowerCase().includes("approval")
      ? "approval"
      : "toolGuard";
  }
  if (kind.includes("_activity_")) return "activity";
  if (kind.includes("llm_retry")) return "llm";
  return payload.tool_name === "create_subagent" ? "subagent" : "tool";
}

function harnessEventLabel(kind, payload) {
  if (kind === "llmGuard" || kind === "toolGuard" || kind === "approval") {
    return payload.guard_name || "Guard";
  }
  if (kind === "activity") {
    return String(payload.activity_name || "Activity").split(":").pop().split(".").pop();
  }
  if (kind === "llm") return payload.model || payload.provider || "Model call";
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
  if (["block", "blocked", "terminate", "terminated", "refusal"].includes(normalized)) {
    return "blocked";
  }
  if (["passed", "complete", "completed", "done"].includes(normalized)) {
    return "complete";
  }
  if (normalized === "retrying") return "retrying";
  return "waiting";
}

function nodeKindLabel(kind) {
  return {
    input: "Input",
    llm: "LLM call",
    llmGuard: "LLM guard",
    toolGuard: "Tool guard",
    approval: "Approval",
    tool: "Tool",
    activity: "Activity",
    subagent: "Subagent",
    artifact: "Artifact",
    output: "Output",
  }[kind] || "Operation";
}

function compactInput(input) {
  if (!input || typeof input !== "object") return "";
  const value = input.query || input.url || input.path || input.filename;
  return compactText(value || JSON.stringify(input), 82);
}

function compactText(value, limit) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}…`;
}

function byOperationId(left, right) {
  return String(left.id).localeCompare(String(right.id));
}

function formatDuration(startedAt, completedAt) {
  const start = Date.parse(startedAt);
  const end = Date.parse(completedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const milliseconds = end - start;
  if (milliseconds < 1_000) return `${milliseconds}ms`;
  return `${(milliseconds / 1_000).toFixed(1)}s`;
}
