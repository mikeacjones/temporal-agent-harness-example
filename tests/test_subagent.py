from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from temporalio.common import (
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
)

from simple_chat_agent.worker.tools.subagent import (
    SubagentProvider,
    SubagentResponse,
)


class SubagentSearchAttributeTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_workflow_inherits_parent_search_attributes(self) -> None:
        user_email = SearchAttributeKey.for_keyword("UserEmail")
        parent_search_attributes = TypedSearchAttributes(
            [SearchAttributePair(user_email, "researcher@example.com")]
        )
        workflow_info = Mock(
            workflow_id="simple-chat-parent",
            typed_search_attributes=parent_search_attributes,
        )
        ctx = Mock(stream_id="simple-chat-parent")
        ctx.tool_names.return_value = ["research"]
        provider = SubagentProvider(
            default_model=lambda: "claude-sonnet-4-5",
            user_ref=lambda: "user-123",
            conversation_id=lambda: "simple-chat-parent",
            github_connection_id=lambda: None,
        )
        child_result = SubagentResponse(
            text="Research complete.",
            stop_reason="end_turn",
            turns=3,
            model="claude-sonnet-4-5",
            tool_names=["research"],
            denied_tool_names=[],
        )

        with (
            patch(
                "simple_chat_agent.worker.tools.subagent.workflow.info",
                return_value=workflow_info,
            ),
            patch(
                "simple_chat_agent.worker.tools.subagent.workflow.uuid4",
                return_value="child-id",
            ),
            patch(
                "simple_chat_agent.worker.tools.subagent.workflow.patched",
                return_value=True,
            ) as patched,
            patch(
                "simple_chat_agent.worker.tools.subagent.workflow."
                "execute_child_workflow",
                new=AsyncMock(return_value=child_result),
            ) as execute_child,
        ):
            result = await provider.create_subagent(
                ctx,
                system_prompt="Research rigorously.",
                task="Investigate the question.",
                tool_names=["research"],
            )

        patched.assert_called_once_with("subagent-inherit-search-attributes-v1")
        self.assertIs(
            execute_child.await_args.kwargs["search_attributes"],
            parent_search_attributes,
        )
        self.assertFalse(result.error)


if __name__ == "__main__":
    unittest.main()
