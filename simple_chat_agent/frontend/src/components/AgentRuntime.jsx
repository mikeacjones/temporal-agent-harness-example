import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  AGENT_TOOL_INPUT_EVENT_PREFIX,
  AgentStreamEventKind,
} from "../state/streamEvents.js";

export function AgentRuntime({ timeline, agents, inputText = "" }) {
  const mainAgent = agents.find((agent) => agent.kind === "main") || agents[0] || null;
  const [activeAgentId, setActiveAgentId] = useState(mainAgent?.id || null);
  const activeAgent =
    agents.find((agent) => agent.id === activeAgentId) || mainAgent || agents[0] || null;
  const frames = useMemo(
    () => buildRuntimeFrames(timeline, activeAgent),
    [timeline, activeAgent],
  );
  const [cursor, setCursor] = useState(() => Math.max(0, frames.length - 1));
  const [following, setFollowing] = useState(true);
  const [playing, setPlaying] = useState(false);
  const [selectedCard, setSelectedCard] = useState(null);
  const toolGridRef = useRef(null);
  const thoughtStreamRef = useRef(null);
  const thoughtContentRef = useRef(null);
  const lastAgentIdRef = useRef(activeAgent?.id || null);
  const finalFrame = Math.max(0, frames.length - 1);
  const visibleCursor = following ? finalFrame : Math.min(cursor, finalFrame);
  const projectedModel = useMemo(
    () => projectRuntimeFrame(timeline, activeAgent, frames, visibleCursor),
    [timeline, activeAgent, frames, visibleCursor],
  );
  const model = useMemo(
    () => projectSubagentTools(projectedModel, agents, activeAgent, following),
    [activeAgent, agents, following, projectedModel],
  );
  const thoughtText = String(model.currentSegment?.thinking || "").trim();
  const showThoughtStream =
    Boolean(thoughtText) && ["active", "thinking"].includes(model.modelStatus);
  const layoutVersion = model.tools
    .map((tool) => `${tool.id}:${tool.status}`)
    .join("|");

  useFluidToolLayout(toolGridRef, layoutVersion);

  useEffect(() => {
    if (lastAgentIdRef.current === activeAgent?.id) return;
    lastAgentIdRef.current = activeAgent?.id || null;
    setCursor(finalFrame);
    setFollowing(true);
    setPlaying(false);
    setSelectedCard(null);
  }, [activeAgent?.id, finalFrame]);

  useEffect(() => {
    if (!following) return;
    setCursor(finalFrame);
  }, [finalFrame, following]);

  useLiveThoughtAutoscroll(
    thoughtStreamRef,
    thoughtContentRef,
    following && showThoughtStream,
    activeAgent?.id,
  );

  useEffect(() => {
    if (!playing) return undefined;
    if (visibleCursor >= finalFrame) {
      setPlaying(false);
      return undefined;
    }
    const timer = window.setTimeout(() => {
      setCursor((current) => Math.min(finalFrame, current + 1));
    }, 620);
    return () => window.clearTimeout(timer);
  }, [finalFrame, playing, visibleCursor]);

  if (!activeAgent || !frames.length) {
    return <div className="runtime-empty">Waiting for agent activity…</div>;
  }

  const inspected = inspectedRuntimeCard(selectedCard, model);
  const isLive = !["complete", "interrupted"].includes(timeline.status);
  const showIngress = activeAgent.kind === "main" && visibleCursor <= 1;

  function seek(nextCursor) {
    const next = Math.max(0, Math.min(finalFrame, Number(nextCursor)));
    setCursor(next);
    setFollowing(next === finalFrame);
    setPlaying(false);
  }

  function selectAgent(agentId) {
    setActiveAgentId(agentId);
    setCursor(Number.MAX_SAFE_INTEGER);
    setFollowing(true);
    setPlaying(false);
    setSelectedCard(null);
  }

  return (
    <div className="runtime-visualizer">
      {agents.length > 1 ? (
        <div className="runtime-agent-switcher" aria-label="Select agent runtime">
          {agents.map((agent) => (
            <button
              key={agent.id}
              type="button"
              className={agent.id === activeAgent.id ? "active" : ""}
              onClick={() => selectAgent(agent.id)}
            >
              <span className={`runtime-dot ${runtimeStatus(agent.status)}`} />
              <span>{agent.kind === "main" ? "Main agent" : compactText(agent.label, 42)}</span>
              <small>{runtimeStatus(agent.status)}</small>
            </button>
          ))}
        </div>
      ) : null}

      <div className={`runtime-stage${showIngress ? " has-ingress" : ""}`}>
        <div className="runtime-grid" aria-hidden="true" />
        <div className="runtime-stage-label">
          <span>Agent runtime</span>
          <i />
        </div>

        {showIngress ? (
          <button
            type="button"
            className="runtime-ingress-card"
            onClick={() => setSelectedCard({ type: "ingress", id: "ingress" })}
          >
            <strong>Message in</strong>
            <span><i /> Received</span>
            <p>{compactText(inputText || "Conversation turn accepted", 96)}</p>
          </button>
        ) : null}

        <section className={`runtime-agent-shell ${runtimeStatus(model.agentStatus)}`}>
          <header className="runtime-agent-header">
            <div className="runtime-agent-title">
              <span className="runtime-agent-glyph" />
              <strong>{activeAgent.kind === "main" ? "Agent" : "Subagent"}</strong>
              {activeAgent.kind === "subagent" ? (
                <small title={activeAgent.label}>{compactText(activeAgent.label, 48)}</small>
              ) : null}
            </div>
            <div className={`runtime-run-pill ${runtimeStatus(model.agentStatus)}`}>
              <i />
              <span>{runtimeStatusLabel(model.agentStatus)}</span>
              <b>·</b>
              <span>Turn {model.turn || "—"}</span>
            </div>
          </header>

          <div className="runtime-agent-body">
            <div className="runtime-signal-line" aria-hidden="true"><i /></div>
            <button
              type="button"
              className={`runtime-model-card ${model.modelStatus}`}
              onClick={() => setSelectedCard({ type: "model", id: "model" })}
              aria-label={`Model ${model.modelStatus}`}
            >
              <span className="runtime-model-icon"><i /></span>
              <strong>Model</strong>
              <small>{modelStatusLabel(model.modelStatus)}</small>
            </button>

            <div ref={toolGridRef} className="runtime-tool-bay">
              {showThoughtStream ? (
                <div className="runtime-thought-stream">
                  <div className="runtime-thought-heading">
                    <span><i /> Thinking stream</span>
                    <small>Following live</small>
                  </div>
                  <p ref={thoughtStreamRef} aria-live="polite">
                    <span ref={thoughtContentRef}>{thoughtText}</span>
                  </p>
                </div>
              ) : null}
              {model.tools.length ? (
                model.tools.map((tool) => (
                  <button
                    key={tool.id}
                    type="button"
                    data-runtime-card={tool.id}
                    className={`runtime-tool-card ${tool.kind || "tool"} ${tool.status}${
                      selectedCard?.type === "tool" && selectedCard.id === tool.id
                        ? " selected"
                        : ""
                    }`}
                    onClick={() => setSelectedCard({ type: "tool", id: tool.id })}
                  >
                    <div className="runtime-tool-heading">
                      <strong title={tool.name}>{compactText(tool.name, 24)}</strong>
                      <small>#{shortId(tool.id)}</small>
                    </div>
                    <span className="runtime-tool-status"><i /> {toolStatusLabel(tool.status)}</span>
                    {tool.preview ? <p>{compactText(tool.preview, 88)}</p> : null}
                  </button>
                ))
              ) : !showThoughtStream ? (
                <div className="runtime-tool-empty">
                  {model.modelStatus === "complete"
                    ? "Run complete"
                    : model.modelStatus === "failed"
                      ? "Run stopped"
                      : "Tools appear here when requested"}
                </div>
              ) : null}
            </div>
          </div>
        </section>
      </div>

      <div className="runtime-transport">
        <div className="runtime-transport-buttons">
          <button type="button" onClick={() => seek(visibleCursor - 1)} disabled={!visibleCursor} aria-label="Previous event">‹</button>
          <button
            type="button"
            onClick={() => {
              if (visibleCursor >= finalFrame) setCursor(0);
              setFollowing(false);
              setPlaying((current) => !current);
            }}
            aria-label={playing ? "Pause replay" : "Play replay"}
          >
            {playing ? "Ⅱ" : "▶"}
          </button>
          <button type="button" onClick={() => seek(visibleCursor + 1)} disabled={visibleCursor >= finalFrame} aria-label="Next event">›</button>
        </div>
        <div className="runtime-scrubber">
          <input
            type="range"
            min="0"
            max={finalFrame}
            value={visibleCursor}
            onChange={(event) => seek(event.currentTarget.value)}
            aria-label="Runtime replay position"
          />
          <div className="runtime-frame-marks" aria-hidden="true">
            {frames.map((frame, index) => (
              <i
                key={frame.id}
                className={`${frame.tone}${index <= visibleCursor ? " passed" : ""}`}
                style={{ left: `${finalFrame ? (index / finalFrame) * 100 : 0}%` }}
              />
            ))}
          </div>
        </div>
        <div className="runtime-transport-state">
          <strong>{playing ? "Playing" : following && isLive ? "Live" : "Paused"}</strong>
          <span>· click a card</span>
          <small>{visibleCursor + 1} / {frames.length} · t{model.turn || "—"}</small>
          {!following ? (
            <button type="button" onClick={() => { setCursor(finalFrame); setFollowing(true); setPlaying(false); }}>
              {isLive ? "Jump live" : "Latest"}
            </button>
          ) : null}
        </div>
      </div>

      {inspected ? (
        <RuntimeInspector card={inspected} onClose={() => setSelectedCard(null)} />
      ) : null}
    </div>
  );
}

function useLiveThoughtAutoscroll(viewportRef, contentRef, enabled, streamId) {
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const content = contentRef.current;
    if (!enabled || !viewport || !content) return undefined;

    let animationFrame = null;
    const scheduleScroll = () => {
      if (animationFrame !== null) window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        animationFrame = null;
        scrollStreamToBottom(viewport);
      });
    };

    scrollStreamToBottom(viewport);
    scheduleScroll();

    const resizeObserver = typeof ResizeObserver === "function"
      ? new ResizeObserver(scheduleScroll)
      : null;
    resizeObserver?.observe(content);

    const mutationObserver = typeof MutationObserver === "function"
      ? new MutationObserver(scheduleScroll)
      : null;
    mutationObserver?.observe(content, {
      characterData: true,
      childList: true,
      subtree: true,
    });

    return () => {
      if (animationFrame !== null) window.cancelAnimationFrame(animationFrame);
      resizeObserver?.disconnect();
      mutationObserver?.disconnect();
    };
  }, [contentRef, enabled, streamId, viewportRef]);
}

export function scrollStreamToBottom(viewport) {
  if (!viewport) return;
  viewport.scrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
}

function RuntimeInspector({ card, onClose }) {
  const detail = card.detail && typeof card.detail === "object"
    ? JSON.stringify(card.detail, null, 2)
    : String(card.detail || "No additional details were retained for this event.");
  return (
    <aside className={`runtime-inspector ${card.status || "waiting"}`}>
      <div>
        <span>{card.kind}</span>
        <strong>{card.label}</strong>
        {card.summary ? <p>{card.summary}</p> : null}
      </div>
      <pre>{detail.length > 12_000 ? `${detail.slice(0, 12_000)}\n…` : detail}</pre>
      <button type="button" onClick={onClose}>Close details</button>
    </aside>
  );
}

export function buildRuntimeFrames(_timeline, agent) {
  if (!agent) return [];
  const frames = [
    { id: `${agent.id}:ingress`, kind: "ingress", tone: "ingress", turn: 0 },
  ];
  let ordinal = 0;
  for (const segment of agent.segments || []) {
    const turn = Number(segment.sequence ?? segment.afterSequence ?? 0);
    if (segment.type === "agent") {
      if (String(segment.thinking || "").trim()) {
        frames.push({
          id: `${segment.id}:thinking`,
          kind: "model",
          tone: "model",
          turn,
          modelStatus: "thinking",
          segment,
        });
      }
      frames.push({
        id: `${segment.id}:model`,
        kind: "model",
        tone: segment.status === "failed" ? "failed" : "model",
        turn,
        modelStatus: modelStatusForSegment(segment),
        segment,
      });
      continue;
    }
    for (const event of segment.events || []) {
      frames.push({
        id: `${segment.id}:event:${ordinal}`,
        kind: "tool-event",
        tone: eventTone(event),
        turn,
        event,
        ordinal,
      });
      ordinal += 1;
    }
    if (segment.status === "complete") {
      frames.push({
        id: `${segment.id}:settled`,
        kind: "tools-settled",
        tone: "done",
        turn,
        segment,
      });
    }
  }
  return frames;
}

export function projectRuntimeFrame(timeline, agent, frames, cursor) {
  const tools = new Map();
  let toolTurn = null;
  let turn = Number(agent?.segments?.[0]?.sequence || timeline?.activeSequence || 0);
  let modelStatus = "dormant";
  let currentSegment = null;
  const visibleFrames = frames.slice(0, Math.max(0, cursor) + 1);

  for (const frame of visibleFrames) {
    if (frame.kind === "model") {
      const nextTurn = frame.turn || turn;
      turn = nextTurn;
      modelStatus = frame.modelStatus;
      currentSegment = frame.segment;
      continue;
    }
    if (frame.kind === "tool-event") {
      const nextToolTurn = frame.turn || turn;
      if (toolTurn !== null && nextToolTurn !== toolTurn) tools.clear();
      toolTurn = nextToolTurn;
      turn = nextToolTurn;
      const update = runtimeToolUpdate(frame.event, frame.ordinal, tools);
      if (update) tools.set(update.id, update);
      modelStatus = "dormant";
      continue;
    }
    if (frame.kind === "tools-settled") {
      const nextToolTurn = frame.turn || turn;
      if (toolTurn !== null && nextToolTurn !== toolTurn) tools.clear();
      toolTurn = nextToolTurn;
      turn = nextToolTurn;
      for (const [id, tool] of tools) {
        if (["requested", "running", "waiting"].includes(tool.status)) {
          tools.set(id, { ...tool, status: "done" });
        }
      }
      modelStatus = "dormant";
    }
  }

  return {
    turn,
    modelStatus,
    currentSegment,
    tools: [...tools.values()],
    agentStatus:
      cursor >= frames.length - 1
        ? runtimeStatus(agent?.status || timeline?.status)
        : modelStatus === "failed"
          ? "error"
          : "running",
  };
}

export function projectSubagentTools(model, agents, activeAgent, enabled = true) {
  if (!enabled || activeAgent?.kind !== "main") return model;
  const tools = new Map(model.tools.map((tool) => [tool.id, tool]));
  const subagents = agents.filter(
    (agent) => agent.kind === "subagent" && agent.parentId === activeAgent.id,
  );

  for (const subagent of subagents) {
    const id = String(subagent.parentToolCallId || `subagent:${subagent.id}`);
    const existing = tools.get(id);
    const status = subagentToolStatus(subagent.status);
    if (!existing && !["requested", "running", "waiting"].includes(status)) {
      continue;
    }
    tools.set(id, {
      ...existing,
      id,
      name: "create_subagent",
      status,
      preview: subagent.label,
      detail: {
        ...(existing?.detail || {}),
        tool_call_id: id,
        tool_name: "create_subagent",
        latest_event: "subagent_status",
        child_agent_id: subagent.id,
        child_agent_status: subagent.status,
        child_task: subagent.label,
        child_turns: subagent.turnCount || 0,
        child_tools: subagent.toolCount || 0,
      },
    });
  }

  return { ...model, tools: [...tools.values()] };
}

function runtimeToolUpdate(event, ordinal, tools) {
  const kind = String(event?.kind || "");
  if (!kind || kind.startsWith("harness_llm_")) return null;
  const payload = event.payload || {};
  const isInput = kind.startsWith(AGENT_TOOL_INPUT_EVENT_PREFIX);
  const isToolLifecycle = /^harness_tool_(start|complete|failed)$/.test(kind);
  const isToolGuard = kind.startsWith("harness_tool_guard_");
  const isToolActivity = kind.startsWith("harness_tool_activity_");
  const isToolDetail = isToolGuard || isToolActivity;
  const isArtifact = kind.startsWith("artifact_create");
  const isPython = kind.startsWith("python_sandbox_");
  const rawId =
    payload.tool_use_id ||
    (isToolGuard ? payload.operation_id : null) ||
    event.tool_call_id ||
    (isToolActivity ? payload.parent_operation_id : payload.operation_id) ||
    payload.run_id ||
    `${payload.tool_name || event.tool_name || kind}:${payload.llm_sequence ?? payload.sequence ?? ordinal}`;
  const id = String(rawId);
  const existing = tools.get(id);
  if (isToolActivity && !existing && !payload.tool_name) return null;
  if (!isInput && !isToolLifecycle && !isToolDetail && !isArtifact && !isPython && !event.tool_name && !payload.tool_name) {
    return null;
  }
  const name = String(
    (isToolGuard && payload.guard_name
      ? `guard · ${payload.guard_name}`
      : payload.tool_name) ||
      event.tool_name ||
      existing?.name ||
      (isArtifact ? "create_artifact" : isPython ? "python_sandbox" : "tool"),
  );
  const nextStatus = toolStatusForEvent(kind, payload, existing?.status);
  const input = payload.input ?? payload.input_partial ?? payload.input_preview;
  const guardPreview = isToolGuard
    ? [payload.timing ? `${payload.timing} guard` : "tool guard", payload.tool_name]
      .filter(Boolean)
      .join(" · ")
    : "";
  const preview = input === undefined
    ? existing?.preview || guardPreview
    : compactValue(input);
  const events = [...(existing?.events || []), event];
  return {
    id,
    name,
    kind: isToolGuard ? "guard" : existing?.kind || "tool",
    status: nextStatus,
    preview,
    events,
    detail: {
      tool_call_id: id,
      tool_name: name,
      parent_tool_call_id: isToolGuard ? payload.parent_operation_id || null : null,
      guard_name: isToolGuard ? payload.guard_name || null : null,
      guard_timing: isToolGuard ? payload.timing || null : null,
      input: input ?? existing?.detail?.input,
      latest_event: kind,
      latest_payload: payload,
      event_count: events.length,
    },
  };
}

function toolStatusForEvent(kind, payload, previous = "requested") {
  if (kind === AgentStreamEventKind.AGENT_TOOL_INPUT_START ||
      kind === AgentStreamEventKind.AGENT_TOOL_INPUT_DELTA ||
      kind === AgentStreamEventKind.AGENT_TOOL_INPUT_COMPLETE) return "requested";
  if (kind.startsWith("harness_tool_guard_")) {
    if (kind.endsWith("_failed") || kind.endsWith("_rejected")) return "failed";
    if (kind.endsWith("_complete")) return "done";
    if (kind.endsWith("_start") && String(payload.guard_name || "").includes("approval")) {
      return "waiting";
    }
    if (kind.endsWith("_start")) return "running";
    return normalizeToolStatus(payload.status, previous);
  }
  if (kind === "harness_tool_start" || kind.endsWith("_activity_start")) return "running";
  if (kind === "harness_tool_complete" || kind === "artifact_create_complete") return "done";
  if (kind.endsWith("_failed") || kind.endsWith("_rejected")) return "failed";
  if (kind.endsWith("_activity_complete")) return "running";
  if (kind.startsWith("python_sandbox_")) return "running";
  return normalizeToolStatus(payload.status, previous);
}

function normalizeToolStatus(value, fallback) {
  const status = String(value || "").toLowerCase();
  if (["done", "complete", "completed", "passed", "success"].includes(status)) return "done";
  if (["failed", "error", "rejected", "cancelled", "blocked"].includes(status)) return "failed";
  if (["waiting", "approval", "pending_approval"].includes(status)) return "waiting";
  if (["running", "streaming", "retrying", "active"].includes(status)) return "running";
  return fallback || "requested";
}

function subagentToolStatus(status) {
  const normalized = runtimeStatus(status);
  if (normalized === "running") return "running";
  if (normalized === "complete") return "done";
  if (normalized === "error") return "failed";
  if (normalized === "waiting") return "waiting";
  return "requested";
}

function modelStatusForSegment(segment) {
  if (segment.status === "failed" || segment.status === "interrupted") return "failed";
  if (segment.status === "retrying") return "thinking";
  if (segment.status === "streaming") {
    return String(segment.thinking || "").trim() && !String(segment.text || "").trim()
      ? "thinking"
      : "active";
  }
  if (segment.terminal) return "complete";
  if (segment.stopReason === "tool_use") return "dormant";
  return segment.status === "complete" ? "complete" : "active";
}

function inspectedRuntimeCard(selected, model) {
  if (!selected) return null;
  if (selected.type === "ingress") {
    return { kind: "Ingress", label: "Message received", status: "received", detail: {} };
  }
  if (selected.type === "model") {
    const segment = model.currentSegment;
    return {
      kind: "Model",
      label: segment?.model || segment?.provider || "Agent model",
      status: model.modelStatus,
      summary: segment?.thinking ? compactText(segment.thinking, 220) : compactText(segment?.text, 220),
      detail: segment || {},
    };
  }
  const tool = model.tools.find((candidate) => candidate.id === selected.id);
  if (!tool) return null;
  return {
    kind: tool.kind === "guard" ? "Tool guard" : "Tool",
    label: tool.name,
    status: tool.status,
    summary: tool.preview,
    detail: tool.detail,
  };
}

function useFluidToolLayout(ref, version) {
  const positionsRef = useRef(new Map());
  useLayoutEffect(() => {
    const container = ref.current;
    if (!container) return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const next = new Map();
    for (const element of container.querySelectorAll("[data-runtime-card]")) {
      const id = element.getAttribute("data-runtime-card");
      const rect = element.getBoundingClientRect();
      next.set(id, rect);
      const previous = positionsRef.current.get(id);
      if (!previous || reduceMotion || typeof element.animate !== "function") continue;
      const deltaX = previous.left - rect.left;
      const deltaY = previous.top - rect.top;
      if (Math.abs(deltaX) < 1 && Math.abs(deltaY) < 1) continue;
      element.animate(
        [
          { transform: `translate(${deltaX}px, ${deltaY}px)` },
          { transform: "translate(0, 0)" },
        ],
        { duration: 360, easing: "cubic-bezier(.2,.8,.2,1)" },
      );
    }
    positionsRef.current = next;
  }, [ref, version]);
}

function eventTone(event) {
  const kind = String(event?.kind || "");
  if (kind.endsWith("_failed") || kind.endsWith("_rejected")) return "failed";
  if (kind.endsWith("_complete")) return "done";
  if (kind.startsWith(AGENT_TOOL_INPUT_EVENT_PREFIX)) return "requested";
  return "running";
}

function runtimeStatus(status) {
  const value = String(status || "waiting").toLowerCase();
  if (["streaming", "tooling", "running", "retrying", "active"].includes(value)) return "running";
  if (["complete", "completed", "done"].includes(value)) return "complete";
  if (["failed", "error", "cancelled", "interrupted"].includes(value)) return "error";
  return value === "queued" ? "queued" : "waiting";
}

function runtimeStatusLabel(status) {
  const value = runtimeStatus(status);
  return value === "complete" ? "Complete" : value === "error" ? "Stopped" : value === "waiting" ? "Waiting" : value === "queued" ? "Queued" : "Running";
}

function modelStatusLabel(status) {
  return ({ dormant: "Dormant", active: "Active", thinking: "Thinking", complete: "Complete", failed: "Stopped" })[status] || "Dormant";
}

function toolStatusLabel(status) {
  return ({ requested: "Requested", running: "Running", waiting: "Waiting", done: "Done", failed: "Failed" })[status] || "Requested";
}

function shortId(value) {
  const compact = String(value || "tool").replace(/[^a-zA-Z0-9]/g, "");
  return (compact.slice(-5) || "tool").toLowerCase();
}

function compactValue(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch (_error) {
    return String(value);
  }
}

function compactText(value, limit) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 1))}…`;
}
