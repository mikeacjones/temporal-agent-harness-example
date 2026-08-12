from __future__ import annotations

import unittest
from unittest.mock import patch

from temporalio.exceptions import ApplicationError

from agent_harness.streaming import StreamContext
from simple_chat_agent.worker.tools.research import (
    _raise_research_request_error,
    _search_web_activity,
)


class ResearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_primary_engine_failures_fall_back_to_bing(self) -> None:
        primary_response = {
            "results": [],
            "suggestions": [],
            "unresponsive_engines": [
                ["brave", "too many requests"],
                ["google", "CAPTCHA"],
            ],
        }
        bing_response = {
            "results": [
                {
                    "title": "Shark study",
                    "url": "https://example.com/study",
                    "content": "A recent study",
                    "engine": "bing",
                }
            ],
            "suggestions": [],
            "unresponsive_engines": [],
        }

        with (
            patch(
                "simple_chat_agent.worker.tools.research._env",
                return_value="http://searxng",
            ),
            patch(
                "simple_chat_agent.worker.tools.research._get_json",
                side_effect=[primary_response, bing_response],
            ) as get_json,
        ):
            result = await _search_web_activity(
                query="recent shark research",
                stream=StreamContext(stream_id=None),
            )

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["fallback_engine"], "bing")
        self.assertEqual(get_json.call_count, 2)
        self.assertNotIn("engines", get_json.call_args_list[0].args[1])
        self.assertEqual(get_json.call_args_list[1].args[1]["engines"], "bing")

    async def test_bing_failure_does_not_rapidly_retry_blocked_engines(self) -> None:
        primary_response = {
            "results": [],
            "suggestions": [],
            "unresponsive_engines": [["brave", "too many requests"]],
        }
        bing_response = {
            "results": [],
            "suggestions": [],
            "unresponsive_engines": [["bing", "CAPTCHA"]],
        }

        with (
            patch(
                "simple_chat_agent.worker.tools.research._env",
                return_value="http://searxng",
            ),
            patch(
                "simple_chat_agent.worker.tools.research._get_json",
                side_effect=[primary_response, bing_response],
            ) as get_json,
        ):
            with self.assertRaises(ApplicationError) as raised:
                await _search_web_activity(
                    query="recent shark research",
                    stream=StreamContext(stream_id=None),
                )

        self.assertEqual(raised.exception.type, "SearchEnginesUnavailable")
        self.assertTrue(raised.exception.non_retryable)
        self.assertEqual(get_json.call_count, 2)

    async def test_partial_searxng_results_do_not_fail(self) -> None:
        response = {
            "results": [
                {
                    "title": "Shark study",
                    "url": "https://example.com/study",
                    "content": "A recent study",
                    "engine": "brave",
                }
            ],
            "suggestions": [],
            "unresponsive_engines": [["google", "too many requests"]],
        }

        with (
            patch(
                "simple_chat_agent.worker.tools.research._env",
                return_value="http://searxng",
            ),
            patch(
                "simple_chat_agent.worker.tools.research._get_json",
                return_value=response,
            ),
        ):
            result = await _search_web_activity(
                query="recent shark research",
                stream=StreamContext(stream_id=None),
            )

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["engine_errors"], [["google", "too many requests"]])
        self.assertIsNone(result["fallback_engine"])

    def test_rate_limit_is_retryable(self) -> None:
        with self.assertRaises(ApplicationError) as raised:
            _raise_research_request_error(
                {"error": "HTTP 429: Too Many Requests"}
            )

        self.assertFalse(raised.exception.non_retryable)

    def test_authentication_failure_is_not_retryable(self) -> None:
        with self.assertRaises(ApplicationError) as raised:
            _raise_research_request_error(
                {"error": "HTTP 401: Unauthorized"}
            )

        self.assertTrue(raised.exception.non_retryable)


if __name__ == "__main__":
    unittest.main()
