from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent

from .activity_options import ActivityOptions, activity_options_with_overrides


def record_routed_activity_call(
    *,
    owner: str,
    step: str | None,
    activity_count: int,
    used_unstepped_activity: bool,
) -> tuple[int, bool]:
    """Validate readable summary usage for a workflow-side routed activity."""

    if step is None and activity_count > 0:
        raise ValueError(f"{owner} called multiple activities; pass step=...")
    if step is not None and used_unstepped_activity:
        raise ValueError(
            f"{owner} mixed an unstepped activity with stepped activities"
        )

    activity_count += 1
    if step is None:
        used_unstepped_activity = True
    return activity_count, used_unstepped_activity


async def execute_routed_activity(
    *,
    activity_name: str,
    request: Any,
    summary_base: str,
    step: str | None,
    defaults: ActivityOptions,
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
    summary = summary_base if step is None else f"{summary_base}:{step}"
    options = activity_options_with_overrides(
        defaults,
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

    return await workflow.execute_activity(
        activity_name,
        request,
        summary=summary,
        **activity_kwargs,
    )
