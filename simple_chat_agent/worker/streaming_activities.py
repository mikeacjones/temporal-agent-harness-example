from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import activity

from simple_chat_agent.common.streaming import emit_durable_stream_event


@dataclass
class EmitTurnSettledRequest:
    stream_id: str
    workflow_id: str
    idempotency_key: str
    result: dict[str, Any]


@activity.defn(name="simple_chat_agent.emit_turn_settled")
async def emit_turn_settled(request: EmitTurnSettledRequest) -> None:
    await emit_durable_stream_event(
        request.stream_id,
        "turn_settled",
        {
            "workflow_id": request.workflow_id,
            "result": request.result,
        },
        idempotency_key=request.idempotency_key,
    )
