from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from agent_harness.agent import Agent, ToolExecutionResult
from agent_harness.messages import text_message, tool_use_block
from agent_harness.providers.interface import ProviderResponse
from basic_file_agent.start_workflow import _parse_args
from simple_chat_agent.api.schemas import CreateSessionRequest
from simple_chat_agent.worker.workflow import SimpleChatInput


def _tool_response(turn: int) -> ProviderResponse:
    return ProviderResponse(
        id=f"response-{turn}",
        model="test-model",
        message={
            "role": "assistant",
            "content": [
                tool_use_block(
                    tool_use_id=f"tool-{turn}",
                    name="research",
                    input={"turn": turn},
                )
            ],
        },
        stop_reason="tool_use",
        stop_sequence=None,
        usage={},
    )


def _final_response() -> ProviderResponse:
    return ProviderResponse(
        id="response-final",
        model="test-model",
        message=text_message("assistant", "Research complete."),
        stop_reason="end_turn",
        stop_sequence=None,
        usage={},
    )


class AgentTurnLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_workflow_histories_continue_past_legacy_turn_limit(self) -> None:
        tools = Mock()
        tools.tool_schemas.return_value = []
        provider = Mock()
        provider.response_with_visible_refusal.side_effect = lambda response: response
        provider.response_message.side_effect = lambda response: response.message
        agent = Agent(
            "Research thoroughly.",
            tools,
            provider=provider,
            model="test-model",
        )
        agent._call_provider = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                *[_tool_response(turn) for turn in range(1, 26)],
                _final_response(),
            ]
        )
        agent._execute_requested_tools = AsyncMock(  # type: ignore[method-assign]
            return_value=ToolExecutionResult(tool_results=[])
        )
        agent._should_return_continue_as_new = Mock(  # type: ignore[method-assign]
            return_value=False
        )

        with patch("agent_harness.agent.workflow.patched", return_value=True):
            result = await agent.run("Investigate this.", max_turns=20)

        self.assertEqual(result.message, text_message("assistant", "Research complete."))
        self.assertEqual(result.turns, 26)
        self.assertEqual(agent._call_provider.await_count, 26)

    async def test_old_workflow_histories_replay_original_turn_limit(self) -> None:
        tools = Mock()
        tools.tool_schemas.return_value = []
        provider = Mock()
        provider.response_with_visible_refusal.side_effect = lambda response: response
        provider.response_message.side_effect = lambda response: response.message
        provider.stop_reason_for_max_turns.return_value = "max_tokens"
        agent = Agent(
            "Research thoroughly.",
            tools,
            provider=provider,
            model="test-model",
        )
        agent._call_provider = AsyncMock(  # type: ignore[method-assign]
            side_effect=[_tool_response(1), _tool_response(2)]
        )
        agent._execute_requested_tools = AsyncMock(  # type: ignore[method-assign]
            return_value=ToolExecutionResult(tool_results=[])
        )
        agent._should_return_continue_as_new = Mock(  # type: ignore[method-assign]
            return_value=False
        )

        with patch("agent_harness.agent.workflow.patched", return_value=False):
            result = await agent.run("Investigate this.", max_turns=2)

        self.assertEqual(
            result.message,
            text_message("assistant", "Stopped after reaching max_turns=2."),
        )
        self.assertEqual(result.stop_reason, "max_tokens")
        self.assertEqual(result.turns, 2)


class DefaultAgentBehaviorTests(unittest.TestCase):
    def test_api_and_workflow_use_same_deep_research_prompt(self) -> None:
        api_prompt = CreateSessionRequest().system_prompt

        self.assertEqual(api_prompt, SimpleChatInput().system_prompt)
        self.assertIn("rigorous deep-research agent", api_prompt)
        self.assertIn("multiple independent primary sources", api_prompt)
        self.assertIn("do not stop at the first plausible result", api_prompt)
        self.assertIn("dispatch those lines concurrently", api_prompt)
        self.assertIn(
            "ten independent report topics may warrant ten subagents",
            api_prompt,
        )
        self.assertIn("ask focused clarifying questions", api_prompt)
        self.assertIn(
            "high-quality, self-contained HTML artifact as the primary deliverable",
            api_prompt,
        )
        self.assertIn("semantic, accessible, responsive HTML", api_prompt)

    def test_session_api_no_longer_exposes_max_turns(self) -> None:
        properties = CreateSessionRequest.model_json_schema()["properties"]

        self.assertNotIn("max_turns", properties)

    def test_basic_file_agent_cli_no_longer_exposes_max_turns(self) -> None:
        with patch(
            "sys.argv",
            ["basic-file-agent", "Investigate these files thoroughly."],
        ):
            args = _parse_args()

        self.assertFalse(hasattr(args, "max_turns"))


if __name__ == "__main__":
    unittest.main()
