from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import unittest
from unittest.mock import patch

from agent_harness.activity_router import (
    RoutedActivityContext,
    ToolActivityContext,
    call_activity,
)
from agent_harness.streaming import StreamContext


@dataclass
class _ActivityInfo:
    activity_id: str = "activity-1"
    attempt: int = 1
    heartbeat_timeout: timedelta | None = None


async def routed_callback(
    value: str,
    *,
    activity_ctx: RoutedActivityContext,
) -> dict[str, object]:
    return {
        "value": value,
        "route_kind": activity_ctx.route_kind,
        "route_name": activity_ctx.route_name,
        "tool_name": activity_ctx.tool_name,
    }


async def legacy_callback(
    *,
    activity_ctx: ToolActivityContext,
) -> str | None:
    return activity_ctx.route_name


class ActivityRouterTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_activity_context_is_compatibility_alias(self) -> None:
        self.assertIs(ToolActivityContext, RoutedActivityContext)

    async def test_call_activity_injects_routed_activity_context(self) -> None:
        with patch(
            "agent_harness.activity_router.temporal_activity.info",
            return_value=_ActivityInfo(),
        ):
            activity_ctx = RoutedActivityContext(
                route_kind="guard",
                route_name="approval",
                step="check",
                stream_id="stream-1",
            )

        result = await call_activity(
            routed_callback,
            {"value": "ready"},
            StreamContext(stream_id="stream-1"),
            activity_context=activity_ctx,
        )

        self.assertEqual(
            result,
            {
                "value": "ready",
                "route_kind": "guard",
                "route_name": "approval",
                "tool_name": "approval",
            },
        )

    async def test_call_activity_accepts_legacy_tool_context_annotation(self) -> None:
        with patch(
            "agent_harness.activity_router.temporal_activity.info",
            return_value=_ActivityInfo(),
        ):
            activity_ctx = RoutedActivityContext(
                route_kind="tool",
                route_name="read_file",
                step=None,
                stream_id=None,
            )

        result = await call_activity(
            legacy_callback,
            {},
            StreamContext(stream_id=None),
            activity_context=activity_ctx,
        )

        self.assertEqual(result, "read_file")

    def test_tool_heartbeat_preserves_legacy_source(self) -> None:
        with patch(
            "agent_harness.activity_router.temporal_activity.info",
            return_value=_ActivityInfo(heartbeat_timeout=timedelta(seconds=30)),
        ), patch("agent_harness.activity_router.temporal_activity.heartbeat") as heartbeat:
            activity_ctx = RoutedActivityContext(
                route_kind="tool",
                route_name="read_file",
                step="read",
                stream_id="stream-1",
            )
            sent = activity_ctx.heartbeat({"phase": "read"}, force=True)

        self.assertTrue(sent)
        payload = heartbeat.call_args.args[0]
        self.assertEqual(payload["source"], "agent_harness.tool_activity")
        self.assertEqual(payload["route_kind"], "tool")
        self.assertEqual(payload["route_name"], "read_file")


if __name__ == "__main__":
    unittest.main()
