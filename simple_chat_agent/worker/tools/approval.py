from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from agent_harness.guards import GuardContext, GuardResult
from agent_harness.tool_types import ToolType
from agent_harness.tools import guard

ApprovalDecision = Literal["allow", "always_allow", "deny"]
ApprovalRequest = Callable[[str, dict[str, Any]], Awaitable[ApprovalDecision]]


@dataclass
class ChildToolApprovalRequest:
    child_workflow_id: str
    child_approval_id: str
    tool_name: str
    tool_args: dict


class MutatingToolApprovalProvider:
    def __init__(
        self,
        request_mutating_tool_approval: ApprovalRequest | None = None,
    ) -> None:
        self._request_mutating_tool_approval = request_mutating_tool_approval

    @guard(
        name="mutating_tool_approval",
        fulfills=(ToolType.MUTATING, ToolType.MCP),
    )
    async def require_mutating_approval(self, ctx: GuardContext) -> GuardResult:
        if self._request_mutating_tool_approval is None:
            return GuardResult(
                passed=False,
                reason="No approval handler is configured for tools that require approval.",
                llm_payload={
                    "error": "Approval unavailable",
                    "tool": ctx.tool_name,
                    "reason": (
                        "This agent is not configured to request user approval "
                        "for this action."
                    ),
                },
            )

        decision = await self._request_mutating_tool_approval(
            ctx.tool_name,
            ctx.tool_args,
        )
        if decision in ("allow", "always_allow"):
            return GuardResult(passed=True)

        return GuardResult(
            passed=False,
            reason="User denied the tool call.",
            llm_payload={
                "error": "Approval denied",
                "tool": ctx.tool_name,
                "reason": "The user denied this action.",
            },
        )
