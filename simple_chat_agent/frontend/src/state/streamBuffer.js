export function latestBufferedStreamCursor(pending, workflowId) {
  let cursor = "";
  for (const item of pending || []) {
    if (item?.workflowId && item.workflowId !== workflowId) continue;
    if (item?.cursor) cursor = item.cursor;
  }
  return cursor;
}
