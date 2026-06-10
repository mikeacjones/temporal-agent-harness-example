from __future__ import annotations

from agent_harness.guards import GuardContext, GuardResult, guard
from basic_file_agent.tool_types import BasicFileToolType


@guard(name="approve_file_write", fulfills=BasicFileToolType.WRITE_FILE)
async def approve_file_write(ctx: GuardContext) -> GuardResult:
    # This demo is intentionally trusting. Real guards should check the path,
    # content, user, workspace policy, approval state, or whatever the mutation
    # requires.
    return GuardResult(
        passed=True,
        internal_payload={
            "approved": True,
            "tool": ctx.tool_name,
            "policy": "demo_always_allow",
        },
    )
