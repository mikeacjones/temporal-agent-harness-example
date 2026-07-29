import { useEffect, useMemo, useRef, useState } from "react";

import { CodeBlock, MarkdownContent } from "./MarkdownContent.jsx";
import { inferCodeLanguage } from "../utils/code.js";
import {
  AGENT_TOOL_INPUT_EVENT_PREFIX,
  AgentStreamEventKind,
} from "../state/streamEvents.js";
import { HarnessFlow } from "./HarnessFlow.jsx";

export function StreamPanel({ turn, collapsed, onToggle, embedded = false }) {
  const [view, setView] = useState("flow");
  const [selectedAgentId, setSelectedAgentId] = useState(null);
  const [flowAgentId, setFlowAgentId] = useState(null);
  const [expandedAgentIds, setExpandedAgentIds] = useState(() => new Set());
  const [following, setFollowing] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  const bodyRef = useRef(null);
  const timeline = useMemo(() => normalizeStreamTimeline(turn), [turn]);
  const agents = useMemo(() => streamAgents(timeline), [timeline]);
  const mainAgent = agents.find((agent) => agent.kind === "main") || null;
  const subagents = agents.filter((agent) => agent.kind === "subagent");
  const selectedAgent =
    agents.find((agent) => agent.id === selectedAgentId) || subagents[0] || null;
  const activeFlowAgentId =
    agents.find((agent) => agent.id === flowAgentId)?.id || mainAgent?.id || null;
  const activityVersion = timeline.segments.reduce(
    (total, segment) =>
      total +
      String(segment.text || "").length +
      String(segment.thinking || "").length +
      (segment.events || []).reduce(
        (eventTotal, event) => eventTotal + JSON.stringify(event).length,
        0,
      ),
    0,
  );

  useEffect(() => {
    if (timeline.status === "complete" || timeline.status === "interrupted") return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [timeline.status]);

  useEffect(() => {
    if (!following || collapsed || !bodyRef.current) return;
    window.requestAnimationFrame(() => {
      if (!bodyRef.current) return;
      bodyRef.current.scrollTo({
        top: view === "all" ? bodyRef.current.scrollHeight : 0,
        behavior: "smooth",
      });
    });
  }, [activityVersion, collapsed, following, view]);

  if (!turn || !timeline.segments.length) return null;

  return (
    <section
      className={`stream-panel ${timeline.status}${collapsed ? " collapsed" : ""}${
        embedded ? " embedded" : ""
      }`}
    >
      <div className="stream-panel-header">
        <div className="stream-panel-title">
          Agent orchestration
          <span className="stream-panel-status">{streamPanelStatus(timeline, agents)}</span>
        </div>
        <div className="stream-panel-actions">
          {!collapsed ? (
            <button
              type="button"
              className={`stream-follow-toggle${following ? " active" : ""}`}
              onClick={() => {
                setFollowing(true);
                if (!bodyRef.current) return;
                bodyRef.current.scrollTo({
                  top: view === "all" ? bodyRef.current.scrollHeight : 0,
                  behavior: "smooth",
                });
              }}
            >
              {following ? "Following live" : "Jump to current"}
            </button>
          ) : null}
          {onToggle ? (
            <button type="button" className="stream-panel-toggle" onClick={onToggle}>
              {collapsed ? "Expand" : "Collapse"}
            </button>
          ) : null}
        </div>
      </div>
      <div className="stream-panel-body">
        {collapsed ? (
          <div className="stream-preview">{streamPanelPreview(timeline, agents)}</div>
        ) : (
          <>
            <CurrentAgentLane
              mainAgent={mainAgent}
              subagents={subagents}
              now={now}
              following={following}
            />
            <div className="stream-view-tabs" role="tablist" aria-label="Stream views">
              {[
                ["flow", "Harness flow"],
                ["overview", "Overview"],
                ["main", "Main agent"],
                ["subagents", `Subagents (${subagents.length})`],
                ["all", "All events"],
              ].map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={view === id}
                  disabled={id === "subagents" && !subagents.length}
                  className={view === id ? "active" : ""}
                  onClick={() => setView(id)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div
              ref={bodyRef}
              className="stream-orchestration-scroll"
              onScroll={(event) => {
                const element = event.currentTarget;
                const atCurrent =
                  view === "all"
                    ? element.scrollHeight - element.scrollTop - element.clientHeight < 32
                    : element.scrollTop < 32;
                setFollowing(atCurrent);
              }}
            >
              {view === "overview" ? (
                <StreamOverview
                  mainAgent={mainAgent}
                  subagents={subagents}
                  now={now}
                  expandedAgentIds={expandedAgentIds}
                  onToggleAgent={(agentId) => {
                    setExpandedAgentIds((previous) => {
                      const next = new Set(previous);
                      if (next.has(agentId)) next.delete(agentId);
                      else next.add(agentId);
                      return next;
                    });
                  }}
                  onInspectAgent={(agentId) => {
                    setSelectedAgentId(agentId);
                    setView("subagents");
                  }}
                />
              ) : null}
              {view === "flow" ? (
                <HarnessFlow
                  timeline={timeline}
                  agents={agents}
                  activeAgentId={activeFlowAgentId}
                  onSelectAgent={(agentId) => setFlowAgentId(agentId)}
                />
              ) : null}
              {view === "main" ? (
                mainAgent ? (
                  <AgentDetail agent={mainAgent} now={now} />
                ) : (
                  <div className="stream-empty-state">Waiting for main-agent activity…</div>
                )
              ) : null}
              {view === "subagents" ? (
                <div className="stream-subagent-browser">
                  <div className="stream-subagent-selector" aria-label="Select subagent">
                    {subagents.map((agent) => (
                      <button
                        key={agent.id}
                        type="button"
                        className={selectedAgent?.id === agent.id ? "active" : ""}
                        onClick={() => setSelectedAgentId(agent.id)}
                      >
                        <AgentStatusDot status={agent.status} />
                        <span>{agentDisplayLabel(agent)}</span>
                      </button>
                    ))}
                  </div>
                  {selectedAgent ? (
                    <AgentDetail agent={selectedAgent} now={now} />
                  ) : (
                    <div className="stream-empty-state">No subagents have started.</div>
                  )}
                </div>
              ) : null}
              {view === "all" ? <RawEventTimeline timeline={timeline} /> : null}
            </div>
          </>
        )}
      </div>
    </section>
  );
}

function CurrentAgentLane({ mainAgent, subagents, now, following }) {
  const completeCount = subagents.filter((agent) => agent.status === "complete").length;
  const activeCount = subagents.filter(
    (agent) => agent.status === "running" || agent.status === "waiting",
  ).length;
  return (
    <div className="stream-current-agent">
      <div className="stream-current-agent-heading">
        <div>
          <span className="stream-current-kicker">Current</span>
          <strong>Main agent</strong>
        </div>
        <span className={`stream-agent-status ${mainAgent?.status || "queued"}`}>
          {mainAgent?.status || "starting"}
        </span>
      </div>
      <div className="stream-current-action">
        {mainAgent?.latestAction || "Waiting for the main agent to start…"}
      </div>
      <div className="stream-current-meta">
        {subagents.length ? (
          <span>
            {completeCount}/{subagents.length} subagents complete
            {activeCount ? ` · ${activeCount} active` : ""}
          </span>
        ) : (
          <span>No delegated workstreams yet</span>
        )}
        {mainAgent?.startedAt ? (
          <span>{formatDuration(mainAgent.startedAt, mainAgent.completedAt, now)}</span>
        ) : null}
        <span>{following ? "Live updates on" : "Live updates paused"}</span>
      </div>
    </div>
  );
}

function StreamOverview({
  mainAgent,
  subagents,
  now,
  expandedAgentIds,
  onToggleAgent,
  onInspectAgent,
}) {
  const completeCount = subagents.filter((agent) => agent.status === "complete").length;
  return (
    <div className="stream-overview">
      <div className="stream-overview-section">
        <div className="stream-section-heading">
          <div>
            <span>Delegated work</span>
            <small>
              {subagents.length
                ? `${completeCount} of ${subagents.length} complete`
                : "No subagents started"}
            </small>
          </div>
        </div>
        {subagents.length ? (
          <div className="stream-subagent-grid">
            {subagents.map((agent) => (
              <SubagentCard
                key={agent.id}
                agent={agent}
                now={now}
                expanded={expandedAgentIds.has(agent.id)}
                onToggle={() => onToggleAgent(agent.id)}
                onInspect={() => onInspectAgent(agent.id)}
              />
            ))}
          </div>
        ) : (
          <div className="stream-empty-state">
            Independent workstreams will appear here as soon as they are delegated.
          </div>
        )}
      </div>
      <div className="stream-overview-section">
        <div className="stream-section-heading">
          <div>
            <span>Main-agent milestones</span>
            <small>Orchestration and synthesis only</small>
          </div>
        </div>
        <div className="stream-milestones">
          <div className={mainAgent ? "complete" : "pending"}>
            <AgentStatusDot status={mainAgent ? "complete" : "queued"} />
            <span>Planned the response</span>
          </div>
          <div className={subagents.length ? "complete" : "pending"}>
            <AgentStatusDot status={subagents.length ? "complete" : "queued"} />
            <span>
              {subagents.length
                ? `Delegated ${subagents.length} workstream${
                    subagents.length === 1 ? "" : "s"
                  }`
                : "No delegation required yet"}
            </span>
          </div>
          {subagents.length ? (
            <div className={completeCount === subagents.length ? "complete" : "active"}>
              <AgentStatusDot
                status={completeCount === subagents.length ? "complete" : "running"}
              />
              <span>
                Collected {completeCount} of {subagents.length} subagent results
              </span>
            </div>
          ) : null}
          <div className={mainAgent?.status === "complete" ? "complete" : "active"}>
            <AgentStatusDot status={mainAgent?.status || "queued"} />
            <span>{mainAgent?.latestAction || "Waiting to synthesize"}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function SubagentCard({ agent, now, expanded, onToggle, onInspect }) {
  return (
    <article className={`stream-subagent-card ${agent.status}`}>
      <button
        type="button"
        className="stream-subagent-card-toggle"
        aria-expanded={expanded}
        onClick={onToggle}
      >
        <div className="stream-subagent-card-heading">
          <AgentStatusDot status={agent.status} />
          <strong title={agent.label}>{agentDisplayLabel(agent)}</strong>
          <span className={`stream-agent-status ${agent.status}`}>{agent.status}</span>
        </div>
        <div className="stream-subagent-action">{agent.latestAction}</div>
        <div className="stream-subagent-meta">
          <span>
            {agent.turnCount} turn{agent.turnCount === 1 ? "" : "s"}
          </span>
          <span>
            {agent.toolCount} tool{agent.toolCount === 1 ? "" : "s"}
          </span>
          {agent.startedAt ? (
            <span>{formatDuration(agent.startedAt, agent.completedAt, now)}</span>
          ) : null}
          <span>{expanded ? "Hide details" : "Show details"}</span>
        </div>
      </button>
      {expanded ? (
        <div className="stream-subagent-card-details">
          <div className="stream-subagent-task">{agent.label}</div>
          <AgentActivityList agent={agent} condensed />
          <button type="button" className="stream-inspect-agent" onClick={onInspect}>
            Open full subagent trace
          </button>
        </div>
      ) : null}
    </article>
  );
}

function AgentDetail({ agent, now }) {
  return (
    <section className="stream-agent-detail">
      <div className="stream-agent-detail-header">
        <div>
          <span>{agent.kind === "main" ? "Primary orchestration" : "Delegated workstream"}</span>
          <h3>{agent.kind === "main" ? "Main agent" : agentDisplayLabel(agent)}</h3>
        </div>
        <span className={`stream-agent-status ${agent.status}`}>{agent.status}</span>
      </div>
      {agent.kind === "subagent" ? (
        <div className="stream-agent-task-full">{agent.label}</div>
      ) : null}
      <div className="stream-agent-detail-meta">
        <span>
          {agent.turnCount} agent turn{agent.turnCount === 1 ? "" : "s"}
        </span>
        <span>
          {agent.toolCount} tool call{agent.toolCount === 1 ? "" : "s"}
        </span>
        {agent.startedAt ? (
          <span>{formatDuration(agent.startedAt, agent.completedAt, now)}</span>
        ) : null}
      </div>
      <AgentActivityList agent={agent} />
    </section>
  );
}

function AgentActivityList({ agent, condensed = false }) {
  const segments = condensed ? agent.segments.slice(-3) : agent.segments;
  return (
    <div className="stream-agent-activity-list">
      {segments.map((segment) =>
        segment.type === "agent" ? (
          <AgentStreamSegment key={segment.id} segment={segment} showAgent={false} />
        ) : (
          <ToolStreamSegment
            key={segment.id}
            segment={segment}
            showAgent={false}
            condensed={condensed}
          />
        ),
      )}
    </div>
  );
}

function RawEventTimeline({ timeline }) {
  return (
    <div className="stream-raw-view">
      <div className="stream-debug-notice">
        Debug view · every retained event grouped by agent turn, with explicit attribution.
      </div>
      <div className="stream-timeline">
        {timeline.segments.map((segment) =>
          segment.type === "agent" ? (
            <AgentStreamSegment key={segment.id} segment={segment} showAgent />
          ) : (
            <ToolStreamSegment
              key={segment.id}
              segment={segment}
              showAgent
              rollup={false}
            />
          ),
        )}
      </div>
    </div>
  );
}

function AgentStreamSegment({ segment, showAgent = true }) {
  const complete = segment.status === "complete";
  const failed = segment.status === "failed";
  const retrying = segment.status === "retrying";
  const text = String(segment.text || "").trim();
  const thinking = String(segment.thinking || "").trim();
  const refusalDetails = refusalDetailsText(segment.stopDetails);
  return (
    <div
      className={`stream-agent-segment ${
        failed ? "failed" : retrying ? "retrying" : complete ? "complete" : "streaming"
      }`}
    >
      <div className="stream-finished-title">
        {showAgent ? (
          <span className={`stream-agent-origin ${segment.agentKind || "main"}`}>
            {segment.agentKind === "subagent"
              ? agentDisplayLabel({
                  id: segment.agentId,
                  label: segment.agentLabel,
                  kind: "subagent",
                })
              : "Main agent"}
          </span>
        ) : null}
        Agent turn {segment.sequence ?? "—"}{" "}
        {failed ? "failed" : retrying ? "retrying" : complete ? "complete" : "streaming"}
        {segment.activityAttempt > 1 || segment.streamAttempt > 1
          ? ` · attempt ${segment.activityAttempt || segment.streamAttempt}`
          : ""}
        {complete && segment.stopReason ? ` · ${segment.stopReason}` : ""}
      </div>
      {thinking ? <div className="stream-thinking">{thinking}</div> : null}
      {refusalDetails ? <div className="stream-refusal-details">{refusalDetails}</div> : null}
      {failed && segment.error ? (
        <div className="stream-refusal-details">
          {segment.error.type || "Error"}: {segment.error.message || "The model call failed."}
        </div>
      ) : null}
      {text ? (
        complete ? (
          <MarkdownContent content={text} />
        ) : (
          <div className="stream-text">{text}</div>
        )
      ) : (
        <div className="stream-preview">
          {failed
            ? "This attempt failed. Temporal may retry it."
            : retrying
              ? "Retrying the model call…"
              : complete
            ? `Completed without text (${segment.stopReason || "unknown"}).`
            : "Waiting for streamed tokens…"}
        </div>
      )}
    </div>
  );
}

function ToolStreamSegment({
  segment,
  showAgent = true,
  condensed = false,
  rollup = true,
}) {
  const complete = segment.status === "complete";
  return (
    <div className={`stream-tool-segment ${complete ? "complete" : "streaming"}`}>
      <div className="stream-finished-title">
        {showAgent ? (
          <span className={`stream-agent-origin ${segment.agentKind || "main"}`}>
            {segment.agentKind === "subagent"
              ? agentDisplayLabel({
                  id: segment.agentId,
                  label: segment.agentLabel,
                  kind: "subagent",
                })
              : "Main agent"}
          </span>
        ) : null}
        Tool activity after turn {segment.afterSequence ?? "—"}{" "}
        {complete ? "complete" : "streaming"}
      </div>
      {segment.events?.length ? (
        <StreamToolList
          events={condensed ? segment.events.slice(-12) : segment.events}
          active={!complete}
          rollup={rollup}
        />
      ) : (
        <div className="stream-preview">Waiting for tool activity…</div>
      )}
    </div>
  );
}

function StreamToolList({ events, active, rollup = true }) {
  const groups = rollup
    ? rollupToolEvents(events)
    : events.map((event, index) => ({
        id: streamToolEventKey(event, index),
        label: streamToolLabel(event),
        events: [event],
        callCount: 1,
      }));
  return (
    <div className="stream-tool-list">
      {groups.map((group) => {
        const event = group.events[group.events.length - 1];
        const status = event.payload?.status || sandboxProgressStatus(event.payload);
        return (
          <details
            key={group.id}
            className={`stream-tool-event${
              event.kind?.startsWith(AGENT_TOOL_INPUT_EVENT_PREFIX)
                ? " input-streaming"
                : ""
            }`}
          >
            <summary>
              <span className="stream-tool-name">
                {group.label}
                {group.callCount > 1 ? (
                  <span className="stream-tool-count">× {group.callCount} calls</span>
                ) : null}
              </span>
              <span className="stream-tool-summary">
                {streamToolSummary(event)}
                {status ? <span className="stream-tool-status">{status}</span> : null}
              </span>
              <span className="stream-tool-expand">Raw</span>
            </summary>
            <div className="stream-tool-payload">
              {group.events.map((rawEvent, index) => {
                const payloadText = streamToolPayloadText(rawEvent);
                return (
                  <div
                    key={streamToolEventKey(rawEvent, index)}
                    className="stream-tool-raw-event"
                  >
                    {group.events.length > 1 ? (
                      <div className="stream-tool-raw-kind">{rawEvent.kind}</div>
                    ) : null}
                    <CodeBlock
                      source={payloadText}
                      languageHint={streamToolLanguage(rawEvent, payloadText)}
                      compact
                      highlight={!active}
                      wrapLongLines
                    />
                  </div>
                );
              })}
            </div>
          </details>
        );
      })}
    </div>
  );
}

function streamAgents(timeline) {
  const agents = new Map();
  for (const segment of timeline.segments) {
    const kind = segment.agentKind === "subagent" ? "subagent" : "main";
    const id = String(segment.agentId || (kind === "main" ? "main" : "subagent:unknown"));
    if (!agents.has(id)) {
      agents.set(id, {
        id,
        parentId: segment.parentAgentId || null,
        parentToolCallId: segment.parentToolCallId || null,
        kind,
        label: String(segment.agentLabel || (kind === "main" ? "Main agent" : "Subagent")),
        segments: [],
        events: [],
        startedAt: segment.startedAt || null,
        completedAt: null,
      });
    }
    const agent = agents.get(id);
    agent.segments.push(segment);
    agent.events.push(...(segment.events || []));
    if (!agent.startedAt && segment.startedAt) agent.startedAt = segment.startedAt;
    if (segment.completedAt) agent.completedAt = segment.completedAt;
  }

  const startedTasks = new Set(
    [...agents.values()]
      .filter((agent) => agent.kind === "subagent")
      .map((agent) => agent.label),
  );
  for (const segment of timeline.segments) {
    if ((segment.agentKind || "main") !== "main" || segment.type !== "tools") continue;
    for (const event of segment.events || []) {
      if (event.kind !== AgentStreamEventKind.AGENT_TOOL_INPUT_COMPLETE) continue;
      if (event.payload?.tool_name !== "create_subagent") continue;
      const task = String(event.payload?.input?.task || "").trim();
      if (!task || startedTasks.has(task)) continue;
      const id = `queued:${event.payload?.tool_use_id || agents.size}`;
      agents.set(id, {
        id,
        parentId: segment.agentId || null,
        parentToolCallId: event.payload?.tool_use_id || event.tool_call_id || null,
        kind: "subagent",
        label: task,
        segments: [],
        events: [event],
        startedAt: segment.startedAt || null,
        completedAt: null,
        queued: true,
      });
      startedTasks.add(task);
    }
  }

  return [...agents.values()].map((agent) => {
    const agentSegments = agent.segments.filter((segment) => segment.type === "agent");
    const latestAgentSegment = agentSegments[agentSegments.length - 1] || null;
    const hasLiveSegment = agent.segments.some((segment) =>
      ["streaming", "retrying"].includes(segment.status),
    );
    let status = "waiting";
    if (agent.queued) status = "queued";
    else if (
      ["interrupted", "failed"].includes(latestAgentSegment?.status) ||
      ["error", "failed", "cancelled", "refusal"].includes(
        String(latestAgentSegment?.stopReason || "").toLowerCase(),
      )
    ) {
      status = "error";
    } else if (latestAgentSegment?.terminal) status = "complete";
    else if (latestAgentSegment?.status === "retrying") status = "retrying";
    else if (hasLiveSegment) status = "running";

    const toolCalls = new Set();
    agent.events.forEach((event, index) => {
      toolCalls.add(
        event.tool_call_id ||
          event.payload?.tool_use_id ||
          `${event.kind}:${event.tool_name || ""}:${event.step || ""}:${index}`,
      );
    });

    let latestAction = "Waiting for activity…";
    const latestSegment = agent.segments[agent.segments.length - 1] || null;
    const latestEvent = agent.events[agent.events.length - 1] || null;
    if (status === "queued") latestAction = "Queued for delegation";
    else if (status === "complete") {
      const finalText = String(latestAgentSegment?.text || "").replace(/\s+/g, " ").trim();
      latestAction = finalText
        ? `Finished · ${finalText.slice(0, 150)}${finalText.length > 150 ? "…" : ""}`
        : "Finished and returned findings";
    } else if (
      latestSegment?.type === "agent" &&
      ["streaming", "retrying"].includes(latestSegment.status)
    ) {
      const text = String(latestSegment.text || "").replace(/\s+/g, " ").trim();
      const thinking = String(latestSegment.thinking || "").replace(/\s+/g, " ").trim();
      latestAction = latestSegment.status === "retrying"
        ? `Retrying model call · attempt ${latestSegment.activityAttempt || latestSegment.streamAttempt || "—"}`
        : text
        ? `Drafting · ${text.slice(-150)}`
        : thinking
          ? `Reasoning · ${thinking.slice(-150)}`
          : "Starting the next agent turn";
    } else if (latestEvent) {
      latestAction = streamToolSummary(latestEvent);
    }

    return {
      ...agent,
      completedAt:
        status === "complete" || status === "error" ? agent.completedAt : null,
      status,
      latestAction,
      turnCount: agentSegments.length,
      toolCount: toolCalls.size,
    };
  });
}

function rollupToolEvents(events) {
  const groups = new Map();
  events.forEach((event, index) => {
    const label = streamToolLabel(event);
    if (!groups.has(label)) {
      groups.set(label, {
        id: `${label}:${index}`,
        label,
        events: [],
        callIds: new Set(),
      });
    }
    const group = groups.get(label);
    group.events.push(event);
    group.callIds.add(
      event.tool_call_id ||
        event.payload?.tool_use_id ||
        `${event.kind}:${event.sequence ?? index}`,
    );
  });
  return [...groups.values()].map((group) => ({
    id: group.id,
    label: group.label,
    events: group.events,
    callCount: group.callIds.size,
  }));
}

function streamToolSummary(event) {
  const payload = event.payload || {};
  const input = payload.input && typeof payload.input === "object" ? payload.input : {};
  const toolName = String(payload.tool_name || event.tool_name || "tool");
  const query = String(input.query || payload.query || "").trim();
  const url = String(input.url || payload.url || "").trim();
  const task = String(input.task || payload.task || "").trim();
  const path = String(input.path || input.filename || payload.path || "").trim();
  if (toolName === "create_subagent" && task) {
    return `Delegating · ${task.slice(0, 180)}${task.length > 180 ? "…" : ""}`;
  }
  if (query) return `Searching · ${query.slice(0, 180)}${query.length > 180 ? "…" : ""}`;
  if (url) return `Fetching · ${url.slice(0, 180)}${url.length > 180 ? "…" : ""}`;
  if (path) return `Working with · ${path}`;
  if (event.kind === "python_sandbox_stdout") return "Python produced output";
  if (event.kind === "python_sandbox_stderr") return "Python produced diagnostic output";
  if (event.kind?.startsWith("python_sandbox")) return "Running Python sandbox";
  if (event.kind?.startsWith("artifact_create")) return "Creating an artifact";
  if (event.kind?.startsWith(AGENT_TOOL_INPUT_EVENT_PREFIX)) {
    return `${humanizeEventName(toolName)} input ${
      event.kind === AgentStreamEventKind.AGENT_TOOL_INPUT_COMPLETE
        ? "ready"
        : "streaming"
    }`;
  }
  return humanizeEventName(event.kind || toolName);
}

function agentDisplayLabel(agent) {
  if (agent.kind === "main") return "Main agent";
  const label = String(agent.label || "Subagent").replace(/\s+/g, " ").trim();
  if (label.length <= 72) return label;
  return `${label.slice(0, 69)}…`;
}

function AgentStatusDot({ status }) {
  return <span className={`stream-agent-status-dot ${status}`} aria-hidden="true" />;
}

function formatDuration(startedAt, completedAt, now) {
  const start = Date.parse(startedAt);
  const end = completedAt ? Date.parse(completedAt) : now;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "";
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return `${minutes}m ${remainder}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function humanizeEventName(value) {
  return String(value || "activity")
    .replace(/^agent_/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function streamToolPayloadText(event) {
  const payload = event.payload || {};
  if (event.kind?.startsWith(AGENT_TOOL_INPUT_EVENT_PREFIX)) {
    if (event.kind === AgentStreamEventKind.AGENT_TOOL_INPUT_COMPLETE) {
      return truncateStreamText(
        formatStreamValue(payload.input ?? payload.input_partial ?? payload.input_preview),
      );
    }
    return truncateStreamText(String(payload.input_partial || payload.partial_json || ""));
  }

  if (
    typeof payload.text === "string" &&
    (event.kind?.includes("stdout") || event.kind?.includes("stderr"))
  ) {
    return truncateStreamText(payload.text);
  }

  return truncateStreamText(formatStreamValue(payload));
}

function streamToolLanguage(event, payloadText) {
  if (event.kind === AgentStreamEventKind.AGENT_TOOL_INPUT_COMPLETE) return "json";
  if (event.kind?.includes("stdout") || event.kind?.includes("stderr")) {
    return inferCodeLanguage(payloadText) || "text";
  }
  return inferCodeLanguage(payloadText) || "json";
}

function streamPanelStatus(turn, agents) {
  const count = streamToolEvents(turn).length;
  const toolText = count === 1 ? "1 tool event" : `${count} tool events`;
  const subagentCount = agents.filter((agent) => agent.kind === "subagent").length;
  const agentText =
    subagentCount === 1 ? "1 subagent" : `${subagentCount} subagents`;
  if (turn.status === "interrupted") return `interrupted · ${agentText} · ${toolText}`;
  if (turn.status === "complete") return `complete · ${agentText} · ${toolText}`;
  return `live · ${agentText} · ${toolText}`;
}

function refusalDetailsText(stopDetails) {
  if (!stopDetails || typeof stopDetails !== "object") return "";
  const parts = [];
  if (typeof stopDetails.category === "string" && stopDetails.category) {
    parts.push(`Category: ${stopDetails.category}`);
  }
  if (typeof stopDetails.explanation === "string" && stopDetails.explanation) {
    parts.push(stopDetails.explanation);
  }
  return parts.join(" | ");
}

function streamPanelPreview(turn, agents) {
  const mainAgent = agents.find((agent) => agent.kind === "main");
  if (mainAgent?.latestAction) return mainAgent.latestAction;
  const activeSubagent = agents.find(
    (agent) => agent.kind === "subagent" && agent.status === "running",
  );
  if (activeSubagent) {
    return `${agentDisplayLabel(activeSubagent)} · ${activeSubagent.latestAction}`;
  }
  return streamPanelStatus(turn, agents);
}

function normalizeStreamTimeline(turn) {
  if (!turn) return { status: "waiting", segments: [] };
  if (turn.segments) {
    return {
      ...turn,
      segments: turn.segments.map((segment) => ({
        ...segment,
        agentId: segment.agentId || "main",
        parentAgentId: segment.parentAgentId || null,
        agentKind: segment.agentKind === "subagent" ? "subagent" : "main",
        agentLabel:
          segment.agentLabel ||
          (segment.agentKind === "subagent" ? "Subagent" : "Main agent"),
      })),
    };
  }

  const segments = [];
  for (const finishedTurn of turn.finishedTurns || []) {
    segments.push({
      id: `agent:main:${finishedTurn.sequence ?? "unknown"}:${segments.length}`,
      type: "agent",
      agentId: "main",
      parentAgentId: null,
      agentKind: "main",
      agentLabel: "Main agent",
      sequence: finishedTurn.sequence ?? null,
      status: "complete",
      terminal: finishedTurn.stopReason !== "tool_use",
      text: finishedTurn.text || "",
      thinking: finishedTurn.thinking || "",
      stopReason: finishedTurn.stopReason || "unknown",
      stopDetails: finishedTurn.stopDetails || null,
      usage: finishedTurn.usage || null,
      completedAt: finishedTurn.completedAt || null,
    });
    if (finishedTurn.events?.length) {
      segments.push({
        id: `tools:main:${finishedTurn.sequence ?? "unknown"}:${segments.length}`,
        type: "tools",
        agentId: "main",
        parentAgentId: null,
        agentKind: "main",
        agentLabel: "Main agent",
        afterSequence: finishedTurn.sequence ?? null,
        status: "complete",
        events: finishedTurn.events,
        completedAt: finishedTurn.completedAt || null,
      });
    }
  }
  if (turn.text || turn.thinking) {
    segments.push({
      id: `agent:main:${turn.activeSequence ?? "unknown"}:${segments.length}`,
      type: "agent",
      agentId: "main",
      parentAgentId: null,
      agentKind: "main",
      agentLabel: "Main agent",
      sequence: turn.activeSequence ?? null,
      status: turn.status === "complete" ? "complete" : "streaming",
      text: turn.text || "",
      thinking: turn.thinking || "",
      stopReason: null,
      usage: null,
      completedAt: null,
    });
  }
  if (turn.currentEvents?.length) {
    segments.push({
      id: `tools:main:${turn.activeSequence ?? "unknown"}:${segments.length}`,
      type: "tools",
      agentId: "main",
      parentAgentId: null,
      agentKind: "main",
      agentLabel: "Main agent",
      afterSequence: turn.activeSequence ?? null,
      status: turn.status === "complete" ? "complete" : "streaming",
      events: turn.currentEvents,
      completedAt: null,
    });
  }
  return { ...turn, segments };
}

function streamToolEvents(turn) {
  return turn.segments.flatMap((segment) => segment.events || []);
}

function streamToolEventKey(event, index) {
  if (event.streamToolInputKey) return `input:${event.streamToolInputKey}:${index}`;
  if (event.tool_call_id) return `tool-call:${event.tool_call_id}:${event.kind}:${index}`;
  if (event.payload?.tool_use_id) {
    return `tool-use:${event.payload.tool_use_id}:${event.kind}:${index}`;
  }
  const sequence = event.sequence ?? event.payload?.sequence ?? index;
  return `${event.agent?.id || "main"}:${event.kind}:${event.tool_name || ""}:${
    event.step || ""
  }:${sequence}:${index}`;
}

function streamToolLabel(event) {
  if (event.kind === "python_sandbox_stdout") return "python_sandbox output";
  if (event.kind === "python_sandbox_stderr") return "python_sandbox diagnostics";
  if (event.kind === "python_sandbox_progress") return "python_sandbox progress";
  const payloadToolName = event.payload?.tool_name;
  const name = payloadToolName || event.tool_name || "stream";
  return event.step ? `${name}:${event.step}` : name;
}

function sandboxProgressStatus(payload = {}) {
  const elapsed = Number(payload.elapsed_seconds);
  const timeout = Number(payload.timeout_seconds);
  if (!Number.isFinite(elapsed) || !Number.isFinite(timeout) || timeout <= 0) {
    return "";
  }
  return `${elapsed}s / ${timeout}s`;
}

function formatStreamValue(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return String(value);
  }
}

function truncateStreamText(value) {
  const text = String(value || "");
  if (text.length <= 40_000) return text;
  return text.slice(-40_000);
}
