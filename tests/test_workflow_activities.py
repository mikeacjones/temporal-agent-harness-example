from __future__ import annotations

import unittest

from agent_harness.workflow_activities import record_routed_activity_call


class WorkflowActivityTests(unittest.TestCase):
    def test_single_unstepped_activity_is_allowed(self) -> None:
        activity_count, used_unstepped = record_routed_activity_call(
            owner="Tool demo",
            step=None,
            activity_count=0,
            used_unstepped_activity=False,
        )

        self.assertEqual(activity_count, 1)
        self.assertTrue(used_unstepped)

    def test_multiple_unstepped_activities_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Tool demo called multiple activities; pass step=...",
        ):
            record_routed_activity_call(
                owner="Tool demo",
                step=None,
                activity_count=1,
                used_unstepped_activity=True,
            )

    def test_mixing_unstepped_and_stepped_activities_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Guard demo mixed an unstepped activity with stepped activities",
        ):
            record_routed_activity_call(
                owner="Guard demo",
                step="write",
                activity_count=1,
                used_unstepped_activity=True,
            )

    def test_multiple_stepped_activities_are_allowed(self) -> None:
        activity_count, used_unstepped = record_routed_activity_call(
            owner="LLM guard demo",
            step="first",
            activity_count=0,
            used_unstepped_activity=False,
        )
        activity_count, used_unstepped = record_routed_activity_call(
            owner="LLM guard demo",
            step="second",
            activity_count=activity_count,
            used_unstepped_activity=used_unstepped,
        )

        self.assertEqual(activity_count, 2)
        self.assertFalse(used_unstepped)


if __name__ == "__main__":
    unittest.main()
