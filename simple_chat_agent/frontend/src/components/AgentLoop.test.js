import assert from "node:assert/strict";
import test from "node:test";

import { createServer } from "vite";

test("the agent loop stays fixed while retaining retries, failures, and subagents", async (t) => {
  const server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  t.after(() => server.close());
  const { buildHarnessLoopModel } = await server.ssrLoadModule(
    "/src/components/AgentLoop.jsx",
  );

  const main = {
    id: "main",
    kind: "main",
    label: "Main agent",
    status: "running",
    segments: [
      {
        type: "agent",
        sequence: 1,
        attempt: 1001,
        activityAttempt: 1,
        streamAttempt: 1,
        status: "complete",
        provider: "anthropic",
        model: "claude",
      },
      {
        type: "tools",
        events: [
          {
            kind: "harness_llm_guard_complete",
            payload: {
              operation_id: "main:llm:1:guard:pre:0:input",
              parent_operation_id: "main:llm:1",
              guard_name: "input",
              timing: "pre",
              llm_sequence: 1,
              status: "passed",
            },
          },
          {
            kind: "harness_llm_guard_complete",
            payload: {
              operation_id: "main:llm:1:guard:post:0:good_place",
              parent_operation_id: "main:llm:1",
              guard_name: "good_place",
              timing: "post",
              llm_sequence: 1,
              status: "passed",
            },
          },
          {
            kind: "agent_tool_input_complete",
            payload: {
              tool_use_id: "tool-1",
              tool_name: "create_subagent",
              sequence: 1,
              input: { task: "Research sharks" },
            },
          },
          {
            kind: "harness_tool_start",
            payload: {
              operation_id: "tool-1",
              parent_operation_id: "main:llm:1",
              tool_name: "create_subagent",
              llm_sequence: 1,
              status: "running",
            },
          },
          {
            kind: "harness_tool_guard_complete",
            payload: {
              operation_id: "tool-1:guard:pre:0:approval",
              parent_operation_id: "tool-1",
              guard_name: "approval",
              timing: "pre",
              tool_name: "create_subagent",
              llm_sequence: 1,
              status: "passed",
            },
          },
          {
            kind: "harness_tool_activity_failed",
            payload: {
              operation_id: "tool-1:activity:run",
              parent_operation_id: "tool-1",
              activity_name: "run",
              tool_name: "create_subagent",
              activity_attempt: 1,
              llm_sequence: 1,
              status: "failed",
              error: { message: "temporary failure" },
            },
          },
          {
            kind: "harness_tool_activity_complete",
            payload: {
              operation_id: "tool-1:activity:run",
              parent_operation_id: "tool-1",
              activity_name: "run",
              tool_name: "create_subagent",
              activity_attempt: 2,
              llm_sequence: 1,
              status: "complete",
            },
          },
          {
            kind: "harness_tool_guard_complete",
            payload: {
              operation_id: "tool-1:guard:post:0:result",
              parent_operation_id: "tool-1",
              guard_name: "result",
              timing: "post",
              tool_name: "create_subagent",
              llm_sequence: 1,
              status: "passed",
            },
          },
          {
            kind: "harness_tool_complete",
            payload: {
              operation_id: "tool-1",
              parent_operation_id: "main:llm:1",
              tool_name: "create_subagent",
              llm_sequence: 1,
              status: "complete",
            },
          },
        ],
      },
      {
        type: "agent",
        sequence: 2,
        attempt: 2001,
        activityAttempt: 1,
        streamAttempt: 1,
        status: "streaming",
        provider: "anthropic",
        model: "claude",
      },
    ],
  };
  const child = {
    id: "child-1",
    kind: "subagent",
    parentId: "main",
    parentToolCallId: "tool-1",
    label: "Research sharks",
    status: "complete",
    segments: [],
  };

  const model = buildHarnessLoopModel({}, [main, child], "main");

  assert.equal(model.stages.length, 9);
  assert.equal(model.currentStageId, "llm");
  assert.equal(model.iteration, 2);
  assert.deepEqual(
    model.stages.map((stage) => stage.id),
    [
      "request",
      "inputGuard",
      "llm",
      "outputGuard",
      "decision",
      "preToolGuard",
      "tool",
      "postToolGuard",
      "response",
    ],
  );
  assert.equal(
    model.stages.find((stage) => stage.id === "tool").failureCount,
    1,
  );
  assert.equal(
    model.stages.find((stage) => stage.id === "tool").retryCount,
    1,
  );
  assert.equal(
    model.stages.find((stage) => stage.id === "decision").latestOperation
      .childAgentId,
    "child-1",
  );
});
