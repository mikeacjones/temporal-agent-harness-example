from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from temporalio.common import (
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
)

from agent_harness.tools import ToolSet
from simple_chat_agent.worker.tools.subagent import (
    CREATE_SUBAGENT_TOOL,
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
        ctx = Mock(
            stream_id="simple-chat-parent",
            tool_call_id="create-subagent-call-1",
        )
        ctx.tool_names.return_value = ["research"]
        provider = SubagentProvider(
            default_model=lambda: "claude-sonnet-4-5",
            user_ref=lambda: "user-123",
            conversation_id=lambda: "simple-chat-parent",
            github_connection_id=lambda: None,
            reference_time=lambda: "2026-07-29T18:42:00Z",
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
                "simple_chat_agent.worker.tools.subagent.workflow."
                "execute_child_workflow",
                new=AsyncMock(return_value=child_result),
            ) as execute_child,
        ):
            result = await provider.create_subagent(
                ctx,
                task="Investigate the question.",
            )

        self.assertIs(
            execute_child.await_args.kwargs["search_attributes"],
            parent_search_attributes,
        )
        child_request = execute_child.await_args.args[1]
        self.assertEqual(child_request.tool_names, ["research"])
        self.assertEqual(child_request.reference_time, "2026-07-29T18:42:00Z")
        self.assertEqual(
            child_request.parent_tool_call_id,
            "create-subagent-call-1",
        )
        self.assertIn("focused research subagent", child_request.system_prompt)
        self.assertFalse(result.error)

    def test_tool_schema_invites_parallel_delegation_with_one_required_input(
        self,
    ) -> None:
        provider = SubagentProvider(
            default_model=lambda: "claude-sonnet-4-5",
            user_ref=lambda: "user-123",
            conversation_id=lambda: "simple-chat-parent",
            github_connection_id=lambda: None,
        )
        tools = ToolSet(providers=[provider])
        schema = next(
            tool
            for tool in tools.tool_schemas()
            if tool["name"] == CREATE_SUBAGENT_TOOL
        )

        self.assertEqual(schema["input_schema"]["required"], ["task"])
        self.assertIn("Proactively delegate", schema["description"])
        self.assertIn("same assistant turn", schema["description"])
        self.assertIn("those calls run concurrently", schema["description"])


if __name__ == "__main__":
    unittest.main()
