from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable, cast, get_type_hints

from anthropic.types import ToolParam
from pydantic import create_model
from temporalio import activity as temporal_activity
from temporalio import workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent

from .activity_options import (
    DEFAULT_ACTIVITY_OPTIONS,
    ActivityOptions,
    activity_options_with_overrides,
)
from .activity_router import ActivityFn, call_activity, function_ref, resolve_function_ref
from .guards import (
    GuardActivityRequest,
    GuardContext,
    GuardDef,
    GuardFn,
    GuardSet,
    GuardPolicy,
    GuardResult,
    GuardTiming,
    run_guard_activity,
)
from .streaming import StreamContext
from .tool_types import ToolType

ToolFn = Callable[..., Awaitable["ToolResult"]]
RUN_TOOL_ACTIVITY_NAME = "claude_harness.run_tool_activity"


@dataclass
class ToolContext:
    tool_name: str
    _tools: "ToolSet"
    stream_id: str | None = None
    activity_options: ActivityOptions = DEFAULT_ACTIVITY_OPTIONS
    _activity_count: int = field(default=0, init=False)
    _used_unstepped_activity: bool = field(default=False, init=False)

    def tool_names(self) -> list[str]:
        return self._tools.tool_names()

    def tool_schemas(self, names: Iterable[str] | None = None) -> list[ToolParam]:
        return self._tools.tool_schemas(names)

    async def activity(
        self,
        fn: ActivityFn,
        *,
        step: str | None = None,
        args: dict[str, Any] | None = None,
        activity_options: ActivityOptions | None = None,
        task_queue: str | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        retry_policy: RetryPolicy | None = None,
        cancellation_type: ActivityCancellationType | None = None,
        activity_id: str | None = None,
        versioning_intent: VersioningIntent | None = None,
        priority: Priority | None = None,
    ) -> Any:
        summary = self.tool_name if step is None else f"{self.tool_name}:{step}"
        options = activity_options_with_overrides(
            self.activity_options,
            activity_options=activity_options,
            task_queue=task_queue,
            schedule_to_close_timeout=schedule_to_close_timeout,
            schedule_to_start_timeout=schedule_to_start_timeout,
            start_to_close_timeout=start_to_close_timeout,
            heartbeat_timeout=heartbeat_timeout,
            retry_policy=retry_policy,
            cancellation_type=cancellation_type,
            versioning_intent=versioning_intent,
            priority=priority,
        )
        activity_kwargs = options.to_execute_activity_kwargs()
        if activity_id is not None:
            activity_kwargs["activity_id"] = activity_id

        self._record_activity_call(step)

        return await workflow.execute_activity(
            RUN_TOOL_ACTIVITY_NAME,
            ToolActivityRequest(
                function_ref=function_ref(fn),
                args=args or {},
                tool_name=self.tool_name,
                step=step,
                stream_id=self.stream_id,
            ),
            summary=summary,
            **activity_kwargs,
        )

    def _record_activity_call(self, step: str | None) -> None:
        if step is None and self._activity_count > 0:
            raise ValueError(
                f"Tool {self.tool_name} called multiple activities; pass step=..."
            )
        if step is not None and self._used_unstepped_activity:
            raise ValueError(
                f"Tool {self.tool_name} mixed an unstepped activity with stepped "
                "activities"
            )

        self._activity_count += 1
        if step is None:
            self._used_unstepped_activity = True


@dataclass
class ToolResult:
    payload: dict[str, Any]
    error: bool


@dataclass
class ToolActivityRequest:
    function_ref: str
    args: dict[str, Any]
    tool_name: str | None = None
    step: str | None = None
    stream_id: str | None = None


@dataclass
class ToolDef:
    schema: ToolParam
    tool_type: ToolType
    fn: ToolFn
    pre_guards: list[GuardDef]
    post_guards: list[GuardDef]


class ToolSet:
    def __init__(self, *, guard_policy: GuardPolicy | None = None) -> None:
        self._tool_registry: dict[str, ToolDef] = {}
        self._guards = GuardSet(guard_policy=guard_policy)

    def tool_names(self) -> list[str]:
        return list(self._tool_registry)

    def tool_schemas(self, names: Iterable[str] | None = None) -> list[ToolParam]:
        if names is None:
            return [t.schema for t in self._tool_registry.values()]
        return [self.get_tool(name).schema for name in names]

    def get_tool(self, name: str) -> ToolDef:
        return self._tool_registry[name]

    async def execute_tool(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        activity_options: ActivityOptions | None = None,
    ) -> ToolResult:
        tool = self.get_tool(name)
        self._guards.validate_tool_guards(
            tool_type=tool.tool_type,
            pre_guards=tool.pre_guards,
            post_guards=tool.post_guards,
        )
        tool_args = args or {}
        resolved_activity_options = activity_options or DEFAULT_ACTIVITY_OPTIONS

        pre_guard_failure = await self._guards.execute_guards(
            tool.pre_guards,
            GuardTiming.PRE,
            tool_name=name,
            tool_type=tool.tool_type,
            tool_args=tool_args,
            tool_result=None,
            stream_id=stream_id,
            activity_options=resolved_activity_options,
        )
        if pre_guard_failure is not None:
            return ToolResult(payload=pre_guard_failure.payload, error=True)

        ctx = ToolContext(
            tool_name=name,
            _tools=self,
            stream_id=stream_id,
            activity_options=resolved_activity_options,
        )
        tool_result = await _call_tool(tool.fn, ctx, tool_args)

        post_guard_failure = await self._guards.execute_guards(
            tool.post_guards,
            GuardTiming.POST,
            tool_name=name,
            tool_type=tool.tool_type,
            tool_args=tool_args,
            tool_result=tool_result,
            stream_id=stream_id,
            activity_options=resolved_activity_options,
        )
        if post_guard_failure is not None:
            return ToolResult(payload=post_guard_failure.payload, error=True)

        return tool_result

    def tool(
        self,
        *,
        name: str,
        description: str,
        tool_type: ToolType,
        pre_guards: list[GuardFn] | None = None,
        post_guards: list[GuardFn] | None = None,
    ):
        def decorator(
            fn: Callable[..., Awaitable[ToolResult]],
        ) -> Callable[..., Awaitable[ToolResult]]:
            if name in self._tool_registry:
                raise ValueError(f"Duplicate tool name: {name}")

            self._tool_registry[name] = ToolDef(
                schema=ToolParam(
                    name=name,
                    description=description,
                    input_schema=_input_schema_for_tool(fn),
                ),
                tool_type=tool_type,
                fn=fn,
                pre_guards=self._guards.defs_for(pre_guards or []),
                post_guards=self._guards.defs_for(post_guards or []),
            )
            return fn

        return decorator

    def guard(
        self,
        *,
        name: str,
        fulfills: ToolType | Iterable[ToolType],
    ):
        return self._guards.guard(name=name, fulfills=fulfills)


def _input_schema_for_tool(fn: ToolFn) -> dict[str, Any]:
    signature = inspect.signature(fn)
    type_hints = get_type_hints(fn)
    model_fields: dict[str, tuple[Any, Any]] = {}

    for name, parameter in signature.parameters.items():
        if name in ("self", "cls"):
            continue
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise TypeError(
                f"Tool {fn.__name__} cannot use positional-only, *args, or **kwargs"
            )

        annotation = type_hints.get(name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            raise TypeError(f"Tool parameter {fn.__name__}.{name} must be typed")
        if annotation is ToolContext:
            continue

        default = (
            ...
            if parameter.default is inspect.Parameter.empty
            else parameter.default
        )
        model_fields[name] = (annotation, default)

    field_definitions = cast(dict[str, Any | tuple[Any, Any]], model_fields)
    model = create_model(f"{fn.__name__}_ToolInput", **field_definitions)
    return model.model_json_schema()


@temporal_activity.defn(name=RUN_TOOL_ACTIVITY_NAME)
async def run_tool_activity(request: ToolActivityRequest) -> Any:
    fn = resolve_function_ref(request.function_ref)
    stream = StreamContext(
        stream_id=request.stream_id,
        tool_name=request.tool_name,
        step=request.step,
    )
    return await call_activity(fn, request.args, stream)


async def _call_tool(
    fn: ToolFn, ctx: ToolContext, args: dict[str, Any]
) -> ToolResult:
    kwargs = _kwargs_for_tool(fn, ctx, args)
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _kwargs_for_tool(
    fn: ToolFn, ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    signature = inspect.signature(fn)
    type_hints = get_type_hints(fn)
    kwargs: dict[str, Any] = {}
    consumed_args: set[str] = set()

    for name, parameter in signature.parameters.items():
        if name in ("self", "cls"):
            continue

        annotation = type_hints.get(name, parameter.annotation)
        if annotation is ToolContext:
            kwargs[name] = ctx
            continue

        if name in args:
            kwargs[name] = args[name]
            consumed_args.add(name)
        elif parameter.default is inspect.Parameter.empty:
            raise TypeError(f"Missing required tool argument {fn.__name__}.{name}")

    unexpected_args = set(args) - consumed_args
    if unexpected_args:
        names = ", ".join(sorted(unexpected_args))
        raise TypeError(f"Unexpected tool argument(s) for {fn.__name__}: {names}")

    return kwargs
