from __future__ import annotations

from dataclasses import dataclass
import unittest

from agent_harness.invocation import bind_keyword_arguments, maybe_await


@dataclass
class ExampleContext:
    user_id: str


def callback(ctx: ExampleContext, name: str, limit: int = 5) -> tuple[str, str, int]:
    return ctx.user_id, name, limit


async def async_callback(value: str) -> str:
    return value.upper()


class InvocationTests(unittest.IsolatedAsyncioTestCase):
    def test_injects_special_values_by_annotation(self) -> None:
        ctx = ExampleContext(user_id="user-1")

        kwargs = bind_keyword_arguments(
            callback,
            {"name": "Ada"},
            special_values={ExampleContext: ctx},
            argument_label="example",
        )

        self.assertEqual(kwargs, {"ctx": ctx, "name": "Ada"})
        self.assertEqual(callback(**kwargs), ("user-1", "Ada", 5))

    def test_rejects_unexpected_arguments(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "Unexpected example argument\\(s\\) for callback: extra",
        ):
            bind_keyword_arguments(
                callback,
                {"name": "Ada", "extra": True},
                special_values={ExampleContext: ExampleContext("user-1")},
                argument_label="example",
            )

    def test_reports_missing_required_arguments(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "Missing required example argument callback.name",
        ):
            bind_keyword_arguments(
                callback,
                special_values={ExampleContext: ExampleContext("user-1")},
                argument_label="example",
            )

    def test_reserved_special_annotation_rejects_caller_supplied_arg(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "callback.ctx requires a real context",
        ):
            bind_keyword_arguments(
                callback,
                {"ctx": ExampleContext("fake"), "name": "Ada"},
                missing_special_errors={
                    ExampleContext: "{function}.{parameter} requires a real context",
                },
                argument_label="example",
            )

    async def test_maybe_await_accepts_sync_and_async_values(self) -> None:
        self.assertEqual(await maybe_await("ready"), "ready")
        self.assertEqual(await maybe_await(async_callback("ready")), "READY")


if __name__ == "__main__":
    unittest.main()
