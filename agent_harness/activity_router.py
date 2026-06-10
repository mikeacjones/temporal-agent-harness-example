import asyncio
import contextlib
import importlib
import time
from typing import Any, Callable, cast

from temporalio import activity as temporal_activity
from temporalio.exceptions import ApplicationError, CancelledError

from .invocation import bind_keyword_arguments, maybe_await
from .streaming import StreamContext

ActivityFn = Callable[..., Any]


class RoutedActivityContext:
    """Activity-runtime context for functions called through ``ctx.activity``."""

    def __init__(
        self,
        *,
        route_name: str | None = None,
        route_kind: str = "tool",
        tool_name: str | None = None,
        step: str | None,
        stream_id: str | None,
    ) -> None:
        info = temporal_activity.info()
        self.route_name = route_name if route_name is not None else tool_name
        self.route_kind = route_kind
        self.tool_name = self.route_name
        self.step = step
        self.stream_id = stream_id
        self.activity_id = info.activity_id
        self.attempt = info.attempt
        self.heartbeat_timeout = info.heartbeat_timeout
        self._started_at = time.monotonic()
        self._latest_details: Any = None
        self._last_sent_at: float | None = None
        self._manual_min_interval_seconds = _manual_heartbeat_min_interval(
            info.heartbeat_timeout.total_seconds()
            if info.heartbeat_timeout is not None
            else None
        )

    @property
    def heartbeat_enabled(self) -> bool:
        return self.heartbeat_timeout is not None

    def heartbeat(self, details: Any | None = None, *, force: bool = False) -> bool:
        """Record a progress heartbeat when this activity has a heartbeat timeout.

        Returns True when a heartbeat was sent. Frequent calls are coalesced so
        tool authors can call this at logical progress points without creating a
        high-volume heartbeat stream.
        """

        self._latest_details = details
        if not self.heartbeat_enabled:
            return False

        now = time.monotonic()
        if (
            not force
            and self._last_sent_at is not None
            and now - self._last_sent_at < self._manual_min_interval_seconds
        ):
            return False

        self._send("manual")
        return True

    def _send(self, reason: str) -> None:
        payload = {
            "source": self._heartbeat_source(),
            "reason": reason,
            "route_kind": self.route_kind,
            "route_name": self.route_name,
            "tool_name": self.tool_name,
            "step": self.step,
            "stream_id": self.stream_id,
            "activity_id": self.activity_id,
            "attempt": self.attempt,
            "elapsed_seconds": round(time.monotonic() - self._started_at, 3),
            "details": self._latest_details,
        }
        temporal_activity.heartbeat(payload)
        self._last_sent_at = time.monotonic()

    def _heartbeat_source(self) -> str:
        if self.route_kind == "tool":
            return "agent_harness.tool_activity"
        if self.route_kind == "guard":
            return "agent_harness.guard_activity"
        return "agent_harness.routed_activity"


ToolActivityContext = RoutedActivityContext


async def call_activity(
    fn: ActivityFn,
    args: dict[str, Any],
    stream: StreamContext,
    *,
    activity_context: RoutedActivityContext | None = None,
) -> Any:
    heartbeat_task = _start_auto_heartbeat(activity_context)
    try:
        return await maybe_await(
            fn(**_kwargs_for_activity(fn, stream, args, activity_context))
        )
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task


def function_ref(fn: ActivityFn) -> str:
    module_name = getattr(fn, "__module__", None)
    qualname = getattr(fn, "__qualname__", None)
    if not module_name or not qualname or "<locals>" in qualname:
        raise ValueError(
            f"Activity function {fn} must be an importable module-level function"
        )

    return f"{module_name}:{qualname}"


def resolve_function_ref(function_ref: str) -> ActivityFn:
    module_name, separator, qualname = function_ref.partition(":")
    if not separator or not module_name or not qualname:
        raise ApplicationError(
            f"Invalid tool activity function reference: {function_ref}",
            type="InvalidToolActivityFunctionRef",
            non_retryable=True,
        )

    try:
        obj: Any = importlib.import_module(module_name)
        for attr in qualname.split("."):
            obj = getattr(obj, attr)
    except (ImportError, AttributeError) as err:
        raise ApplicationError(
            f"Unable to resolve tool activity function: {function_ref}",
            type="UnknownToolActivityFunction",
            non_retryable=True,
        ) from err

    if not callable(obj):
        raise ApplicationError(
            f"Tool activity function reference is not callable: {function_ref}",
            type="InvalidToolActivityFunctionRef",
            non_retryable=True,
        )

    return cast(ActivityFn, obj)


def _kwargs_for_activity(
    fn: ActivityFn,
    stream: StreamContext,
    args: dict[str, Any],
    activity_context: RoutedActivityContext | None = None,
) -> dict[str, Any]:
    special_values: dict[Any, Any] = {StreamContext: stream}
    if activity_context is not None:
        special_values[RoutedActivityContext] = activity_context

    return bind_keyword_arguments(
        fn,
        args,
        special_values=special_values,
        missing_special_errors={
            RoutedActivityContext: (
                "{function}.{parameter} requires a routed activity context"
            ),
        },
        argument_label="activity",
    )


def _start_auto_heartbeat(
    activity_context: RoutedActivityContext | None,
) -> asyncio.Task[None] | None:
    if activity_context is None or activity_context.heartbeat_timeout is None:
        return None

    interval_seconds = _auto_heartbeat_interval(
        activity_context.heartbeat_timeout.total_seconds()
    )
    if interval_seconds is None:
        return None

    current_task = asyncio.current_task()
    if current_task is None:
        return None

    return asyncio.create_task(
        _auto_heartbeat_loop(activity_context, interval_seconds, current_task)
    )


async def _auto_heartbeat_loop(
    activity_context: RoutedActivityContext,
    interval_seconds: float,
    activity_task: asyncio.Task[Any],
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            if (
                activity_context._last_sent_at is not None
                and time.monotonic() - activity_context._last_sent_at
                < interval_seconds
            ):
                continue
            activity_context._send("timer")
        except (asyncio.CancelledError, CancelledError):
            activity_task.cancel()
            raise
        except Exception:
            continue


def _auto_heartbeat_interval(timeout_seconds: float) -> float | None:
    if timeout_seconds <= 0:
        return None
    return max(1.0, min(30.0, timeout_seconds / 2))


def _manual_heartbeat_min_interval(timeout_seconds: float | None) -> float:
    if timeout_seconds is None or timeout_seconds <= 0:
        return 0.0
    return max(1.0, min(5.0, timeout_seconds / 2))
