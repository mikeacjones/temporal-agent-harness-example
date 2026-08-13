import assert from "node:assert/strict";
import test from "node:test";

import { createServer } from "vite";

test("the runtime projects requested, running, failed, recovered, and completed tools", async (t) => {
  const server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  t.after(() => server.close());
  const {
    buildRuntimeFrames,
    projectRuntimeFrame,
    projectSubagentTools,
  } = await server.ssrLoadModule("/src/components/AgentRuntime.jsx");

  const agent = {
    id: "main",
    kind: "main",
    label: "Main agent",
    status: "running",
    segments: [
      {
        id: "agent:main:1",
        type: "agent",
        sequence: 1,
        status: "complete",
        terminal: false,
        stopReason: "tool_use",
      },
      {
        id: "tools:main:1",
        type: "tools",
        afterSequence: 1,
        status: "complete",
        events: [
          {
            kind: "agent_tool_input_complete",
            payload: {
              tool_use_id: "tool-1",
              tool_name: "file_search",
              sequence: 1,
              input: { query: "Temporal agents" },
            },
          },
          {
            kind: "harness_tool_start",
            payload: {
              operation_id: "tool-1",
              tool_name: "file_search",
              llm_sequence: 1,
              status: "running",
            },
          },
          {
            kind: "harness_tool_guard_start",
            payload: {
              operation_id: "tool-1:guard:pre:0:policy",
              parent_operation_id: "tool-1",
              tool_name: "file_search",
              guard_name: "policy",
              timing: "pre",
              status: "running",
            },
          },
          {
            kind: "harness_tool_guard_complete",
            payload: {
              operation_id: "tool-1:guard:pre:0:policy",
              parent_operation_id: "tool-1",
              tool_name: "file_search",
              guard_name: "policy",
              timing: "pre",
              status: "passed",
            },
          },
          {
            kind: "harness_tool_activity_failed",
            payload: {
              operation_id: "tool-1:activity:run",
              parent_operation_id: "tool-1",
              tool_name: "file_search",
              activity_attempt: 1,
              error: { message: "temporary failure" },
            },
          },
          {
            kind: "harness_tool_activity_start",
            payload: {
              operation_id: "tool-1:activity:run",
              parent_operation_id: "tool-1",
              tool_name: "file_search",
              activity_attempt: 2,
              status: "running",
            },
          },
          {
            kind: "harness_tool_complete",
            payload: {
              operation_id: "tool-1",
              tool_name: "file_search",
              llm_sequence: 1,
              status: "complete",
            },
          },
        ],
      },
      {
        id: "agent:main:2",
        type: "agent",
        sequence: 2,
        status: "streaming",
        thinking: "I should combine the tool results before answering.",
        text: "Synthesizing the result",
      },
    ],
  };
  const timeline = { status: "streaming", activeSequence: 2 };
  const frames = buildRuntimeFrames(timeline, agent);

  function atEvent(kind) {
    const index = frames.findIndex(
      (frame) => frame.kind === "tool-event" && frame.event.kind === kind,
    );
    assert.notEqual(index, -1);
    return projectRuntimeFrame(timeline, agent, frames, index);
  }

  assert.equal(atEvent("agent_tool_input_complete").tools[0].status, "requested");
  assert.equal(atEvent("harness_tool_start").tools[0].status, "running");
  const guardedTool = atEvent("harness_tool_guard_start").tools[0];
  assert.equal(guardedTool.id, "tool-1");
  assert.equal(guardedTool.status, "running");
  assert.deepEqual(
    guardedTool.guards.map((guard) => ({
      id: guard.id,
      name: guard.name,
      timing: guard.timing,
      status: guard.status,
    })),
    [{
      id: "tool-1:guard:pre:0:policy",
      name: "policy",
      timing: "pre",
      status: "running",
    }],
  );
  assert.equal(
    atEvent("harness_tool_guard_complete").tools[0].guards[0].status,
    "done",
  );
  assert.equal(atEvent("harness_tool_activity_failed").tools[0].status, "failed");
  assert.equal(atEvent("harness_tool_activity_start").tools[0].status, "running");
  assert.equal(atEvent("harness_tool_complete").tools[0].status, "done");

  const current = projectRuntimeFrame(timeline, agent, frames, frames.length - 1);
  assert.equal(current.turn, 2);
  assert.equal(current.modelStatus, "active");
  assert.match(current.currentSegment.thinking, /combine the tool results/);
  assert.deepEqual(
    current.tools.map((tool) => ({ id: tool.id, name: tool.name, status: tool.status })),
    [
      { id: "tool-1", name: "file_search", status: "done" },
    ],
  );

  const child = {
    id: "child-1",
    parentId: "main",
    parentToolCallId: "subagent-tool-1",
    kind: "subagent",
    label: "Research shark population changes",
    status: "running",
    turnCount: 2,
    toolCount: 1,
  };
  const withSubagent = projectSubagentTools(current, [agent, child], agent);
  assert.deepEqual(
    withSubagent.tools.map((tool) => ({
      id: tool.id,
      name: tool.name,
      status: tool.status,
      preview: tool.preview,
    })),
    [
      {
        id: "tool-1",
        name: "file_search",
        status: "done",
        preview: '{"query":"Temporal agents"}',
      },
      {
        id: "subagent-tool-1",
        name: "create_subagent",
        status: "running",
        preview: "Research shark population changes",
      },
    ],
  );
  const completedChild = { ...child, status: "complete" };
  assert.deepEqual(
    projectSubagentTools(current, [agent, completedChild], agent).tools.map(
      (tool) => tool.id,
    ),
    ["tool-1"],
  );
  assert.equal(projectSubagentTools(current, [agent, child], agent, false), current);
});

test("the final response retains create_artifact without reviving old subagents", async (t) => {
  const server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  t.after(() => server.close());
  const {
    buildRuntimeFrames,
    projectRuntimeFrame,
    projectSubagentTools,
  } = await server.ssrLoadModule("/src/components/AgentRuntime.jsx");
  const agent = {
    id: "main",
    kind: "main",
    status: "complete",
    segments: [
      {
        id: "agent:main:4",
        type: "agent",
        sequence: 4,
        status: "complete",
        terminal: false,
        stopReason: "tool_use",
      },
      {
        id: "tools:main:4",
        type: "tools",
        afterSequence: 4,
        status: "complete",
        events: [
          {
            kind: "agent_tool_input_complete",
            payload: {
              tool_use_id: "artifact-tool-1",
              tool_name: "create_artifact",
              sequence: 4,
              input: { filename: "report.html" },
            },
          },
          {
            kind: "harness_tool_complete",
            payload: {
              operation_id: "artifact-tool-1",
              tool_name: "create_artifact",
              llm_sequence: 4,
              status: "complete",
            },
          },
        ],
      },
      {
        id: "agent:main:5",
        type: "agent",
        sequence: 5,
        status: "complete",
        terminal: true,
        stopReason: "end_turn",
        text: "Your report is ready.",
      },
    ],
  };
  const timeline = { status: "complete", activeSequence: 5 };
  const frames = buildRuntimeFrames(timeline, agent);
  const finalModel = projectRuntimeFrame(timeline, agent, frames, frames.length - 1);
  const completedChild = {
    id: "child-1",
    parentId: "main",
    parentToolCallId: "old-subagent-tool",
    kind: "subagent",
    label: "Old completed research",
    status: "complete",
    turnCount: 3,
    toolCount: 2,
  };
  const projected = projectSubagentTools(
    finalModel,
    [agent, completedChild],
    agent,
  );

  assert.equal(projected.turn, 5);
  assert.equal(projected.modelStatus, "complete");
  assert.deepEqual(
    projected.tools.map((tool) => ({ id: tool.id, name: tool.name, status: tool.status })),
    [{ id: "artifact-tool-1", name: "create_artifact", status: "done" }],
  );
});

test("model streams transition from thinking to response and pin to newest content", async (t) => {
  const server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  t.after(() => server.close());
  const { modelStreamForProjection, scrollStreamToBottom } = await server.ssrLoadModule(
    "/src/components/AgentRuntime.jsx",
  );
  assert.deepEqual(
    modelStreamForProjection({ currentSegment: { thinking: "Considering tools", text: "" } }),
    { kind: "thinking", label: "Thinking stream", text: "Considering tools" },
  );
  assert.deepEqual(
    modelStreamForProjection({
      currentSegment: { thinking: "Considering tools", text: "Here is the answer" },
    }),
    { kind: "response", label: "Response stream", text: "Here is the answer" },
  );
  const viewport = {
    clientHeight: 132,
    scrollHeight: 412,
    scrollTop: 0,
  };

  scrollStreamToBottom(viewport);

  assert.equal(viewport.scrollTop, 280);
});

test("LLM guards are bundled with the model and replay precedes its tools", async (t) => {
  const server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  t.after(() => server.close());
  const { buildRuntimeFrames, projectRuntimeFrame } = await server.ssrLoadModule(
    "/src/components/AgentRuntime.jsx",
  );
  const preStart = {
    kind: "harness_llm_guard_start",
    payload: {
      operation_id: "chat:llm:1:guard:pre:policy",
      parent_operation_id: "chat:llm:1",
      guard_name: "prompt_policy",
      timing: "pre",
      status: "running",
      llm_sequence: 1,
    },
  };
  const preComplete = {
    ...preStart,
    kind: "harness_llm_guard_complete",
    payload: { ...preStart.payload, status: "passed" },
  };
  const postStart = {
    kind: "harness_llm_guard_start",
    payload: {
      operation_id: "chat:llm:1:guard:post:policy",
      parent_operation_id: "chat:llm:1",
      guard_name: "response_policy",
      timing: "post",
      status: "running",
      llm_sequence: 1,
    },
  };
  const postComplete = {
    ...postStart,
    kind: "harness_llm_guard_complete",
    payload: { ...postStart.payload, status: "passed" },
  };
  const agent = {
    id: "main",
    kind: "main",
    status: "running",
    segments: [
      {
        id: "tools:main:1",
        type: "tools",
        afterSequence: 1,
        status: "complete",
        events: [
          preStart,
          preComplete,
          {
            kind: "agent_tool_input_complete",
            payload: { tool_use_id: "tool-1", tool_name: "search_web", input: { query: "Temporal" } },
          },
          postStart,
          postComplete,
          {
            kind: "harness_tool_start",
            payload: { operation_id: "tool-1", tool_name: "search_web", status: "running" },
          },
          {
            kind: "search_start",
            tool_name: "search_web",
            tool_call_id: "tool-1",
            payload: { tool_name: "search_web" },
          },
          {
            kind: "harness_tool_complete",
            payload: { operation_id: "tool-1", tool_name: "search_web", status: "complete" },
          },
        ],
      },
      {
        id: "agent:main:1",
        type: "agent",
        sequence: 1,
        status: "complete",
        terminal: false,
        stopReason: "tool_use",
        thinking: "I should search.",
        text: "I’ll check that.",
      },
    ],
  };
  const timeline = { status: "tooling", activeSequence: 1 };
  const frames = buildRuntimeFrames(timeline, agent);

  assert.deepEqual(
    frames.map((frame) => frame.kind),
    [
      "ingress",
      "model-guard",
      "model-guard",
      "model",
      "model",
      "model-guard",
      "model-guard",
      "tool-event",
      "tool-event",
      "tool-event",
    ],
  );
  assert.equal(
    frames.some((frame) => frame.event?.kind === "search_start"),
    false,
  );
  const preGuardModel = projectRuntimeFrame(timeline, agent, frames, 1);
  assert.equal(preGuardModel.modelStatus, "guarding");
  assert.deepEqual(
    preGuardModel.modelGuards.map((guard) => ({ name: guard.name, status: guard.status })),
    [{ name: "prompt_policy", status: "running" }],
  );
  const responseFrame = frames.findIndex(
    (frame) => frame.kind === "model" && frame.modelStatus === "active",
  );
  assert.equal(projectRuntimeFrame(timeline, agent, frames, responseFrame).currentSegment.text, "I’ll check that.");
  const postGuardFrame = frames.findIndex(
    (frame) => frame.kind === "model-guard" && frame.event === postComplete,
  );
  assert.deepEqual(
    projectRuntimeFrame(timeline, agent, frames, postGuardFrame).modelGuards.map(
      (guard) => ({ name: guard.name, status: guard.status }),
    ),
    [
      { name: "prompt_policy", status: "done" },
      { name: "response_policy", status: "done" },
    ],
  );
});

test("a historical terminal turn projects a completed model", async (t) => {
  const server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  t.after(() => server.close());
  const { buildRuntimeFrames, projectRuntimeFrame } = await server.ssrLoadModule(
    "/src/components/AgentRuntime.jsx",
  );
  const agent = {
    id: "main",
    kind: "main",
    status: "complete",
    segments: [
      {
        id: "agent:main:1",
        type: "agent",
        sequence: 1,
        status: "complete",
        terminal: true,
        stopReason: "end_turn",
        text: "Done",
      },
    ],
  };
  const timeline = { status: "complete" };
  const frames = buildRuntimeFrames(timeline, agent);
  const model = projectRuntimeFrame(timeline, agent, frames, frames.length - 1);
  assert.equal(model.agentStatus, "complete");
  assert.equal(model.modelStatus, "complete");
  assert.equal(model.turn, 1);
});
