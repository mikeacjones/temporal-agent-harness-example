import assert from "node:assert/strict";
import test from "node:test";

import { latestBufferedStreamCursor } from "./streamBuffer.js";

test("the reconnect cursor advances only through events applied to the active workflow", () => {
  const pending = [
    { workflowId: "chat-1", cursor: "100-0" },
    { workflowId: "chat-2", cursor: "200-0" },
    { workflowId: "chat-1", cursor: "101-0" },
  ];

  assert.equal(latestBufferedStreamCursor(pending, "chat-1"), "101-0");
  assert.equal(latestBufferedStreamCursor(pending, "chat-2"), "200-0");
  assert.equal(latestBufferedStreamCursor(pending, "chat-3"), "");
});
