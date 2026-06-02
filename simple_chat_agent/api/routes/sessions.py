from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from temporalio.client import Client

from simple_chat_agent.api.anthropic_models import (
    clamp_output_tokens_for_model,
    get_anthropic_model_catalog,
    max_context_tokens_for_model,
)
from simple_chat_agent.api.artifacts import artifact_response
from simple_chat_agent.api.auth import AuthenticatedUser
from simple_chat_agent.api.schemas import (
    ApprovalDecisionRequest,
    CreateSessionRequest,
    MessageRequest,
    SteerRequest,
)
from simple_chat_agent.api.serialization import (
    artifact_dicts,
    conversation_title,
    record_timing,
    server_timing,
    set_transcript_headers,
    snapshot_to_dict,
    state_patch_to_dict,
    state_to_dict,
    transcript_delta_result_to_dict,
    transcript_page_to_dict,
)
from simple_chat_agent.api.streaming import StreamBroker
from simple_chat_agent.api.temporal_ui import temporal_ui_url
from simple_chat_agent.api.thinking import (
    default_model,
    good_place_enabled,
    thinking_config_from_request,
)
from simple_chat_agent.common.store import AppStore
from simple_chat_agent.worker.tools import (
    configured_research_tool_names,
    tool_names_for_connections,
)
from simple_chat_agent.worker.user_chats_workflow import (
    ChatRecord,
    CreateChatRequest,
    UserChatsWorkflow,
)
from simple_chat_agent.worker.workflow import SimpleChatWorkflow


@dataclass(frozen=True)
class SessionRouteDeps:
    client: Callable[[], Client]
    store: Callable[[], AppStore]
    stream_broker: Callable[[], StreamBroker]
    current_user: Callable[[Request], AuthenticatedUser]
    require_conversation_owner: Callable[..., Any]
    ensure_user_chats_workflow: Callable[..., Any]
    list_user_chats: Callable[..., Any]
    query_state: Callable[[str], Any]
    query_snapshot: Callable[..., Any]
    query_transcript_page: Callable[..., Any]
    query_transcript_deltas_since: Callable[..., Any]
    signal_workflow: Callable[..., Any]
    touch_conversation: Callable[..., Any]
    forget_conversation: Callable[..., Any]
    is_temporal_not_found: Callable[[BaseException], bool]
    github_connection_id_for_user: Callable[[AuthenticatedUser], str | None]
    register_demo_workspace_chat: Callable[..., Any]
    unregister_demo_workspace_chat: Callable[..., Any]
    touch_demo_workspace: Callable[..., Any]


def create_sessions_router(deps: SessionRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/conversations")
    async def conversations(request: Request) -> dict[str, Any]:
        user = deps.current_user(request)
        conversations = await deps.list_user_chats(user.user_id, user.username)
        return {
            "conversations": [
                {
                    **asdict(conversation),
                    "temporal_ui_url": temporal_ui_url(
                        namespace=deps.client().namespace,
                        workflow_id=conversation.workflow_id,
                        run_id=conversation.run_id,
                    ),
                }
                for conversation in conversations
            ]
        }

    @router.post("/api/sessions")
    async def create_session(
        request: Request,
        session_request: CreateSessionRequest,
    ) -> dict[str, str]:
        user = deps.current_user(request)
        client = deps.client()
        github_connection_id = deps.github_connection_id_for_user(user)
        registry = await deps.ensure_user_chats_workflow(user.user_id, user.username)
        mcp_servers = await registry.query(UserChatsWorkflow.list_mcp_servers)
        model_catalog = await asyncio.to_thread(get_anthropic_model_catalog)
        model = session_request.model or default_model(model_catalog)
        max_tokens = clamp_output_tokens_for_model(
            session_request.max_tokens,
            model_catalog,
            model,
        )
        max_context_tokens = max_context_tokens_for_model(model_catalog, model)
        conversation: ChatRecord = await registry.execute_update(
            UserChatsWorkflow.create_chat,
            CreateChatRequest(
                system_prompt=session_request.system_prompt,
                model=model,
                max_tokens=max_tokens,
                max_context_tokens=max_context_tokens,
                max_turns=session_request.max_turns,
                thinking=thinking_config_from_request(
                    session_request.thinking,
                    model=model,
                    max_tokens=max_tokens,
                ),
                initial_message=session_request.initial_message,
                available_tool_names=tool_names_for_connections(
                    github_connection_id=github_connection_id,
                    mcp_servers=mcp_servers,
                    research_tool_names=configured_research_tool_names(),
                ),
                github_connection_id=github_connection_id,
                mcp_servers=mcp_servers,
                good_place_censor=good_place_enabled(),
            ),
        )
        deps.stream_broker().clear(conversation.workflow_id)
        await deps.register_demo_workspace_chat(conversation)
        return {
            "workflow_id": conversation.workflow_id,
            "run_id": conversation.run_id,
            "temporal_ui_url": temporal_ui_url(
                namespace=client.namespace,
                workflow_id=conversation.workflow_id,
                run_id=conversation.run_id,
            ),
        }

    @router.get("/api/sessions/{workflow_id}/state")
    async def get_state(
        request: Request,
        workflow_id: str,
        response: Response,
    ) -> dict[str, Any]:
        user = await deps.require_conversation_owner(request, workflow_id)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Stream-Cursor"] = deps.stream_broker().cursor(workflow_id)
        try:
            state = await deps.query_state(workflow_id)
        except Exception as err:
            if not deps.is_temporal_not_found(err):
                raise
            await deps.forget_conversation(user.user_id, workflow_id, user.username)
            raise HTTPException(
                status_code=404,
                detail="Workflow execution not found. Start a new chat.",
            ) from err
        return state_to_dict(
            state,
            artifacts=deps.store().list_artifacts(
                user_id=user.user_id,
                workflow_id=workflow_id,
            ),
        )

    @router.get("/api/sessions/{workflow_id}/state/patch")
    async def get_state_patch(
        request: Request,
        workflow_id: str,
        response: Response,
        after_revision: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        user = await deps.require_conversation_owner(request, workflow_id)
        response.headers["Cache-Control"] = "no-store"
        try:
            state = await deps.query_state(workflow_id)
        except Exception as err:
            if not deps.is_temporal_not_found(err):
                raise
            await deps.forget_conversation(user.user_id, workflow_id, user.username)
            raise HTTPException(
                status_code=404,
                detail="Workflow execution not found. Start a new chat.",
            ) from err

        state_revision = int(state.state_revision or 0)
        response.headers["X-State-Revision"] = str(state_revision)
        if after_revision >= state_revision:
            return {
                "unchanged": True,
                "state_revision": state_revision,
            }
        return {
            "unchanged": False,
            "state": state_patch_to_dict(state),
        }

    @router.get("/api/sessions/{workflow_id}/snapshot")
    async def get_snapshot(
        request: Request,
        workflow_id: str,
        response: Response,
        limit: int = Query(default=60, ge=1, le=200),
    ) -> dict[str, Any]:
        timings: list[tuple[str, float]] = []
        started = time.perf_counter()
        user = await deps.require_conversation_owner(request, workflow_id)
        record_timing(timings, "owner", started)

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Stream-Cursor"] = deps.stream_broker().cursor(workflow_id)
        query_started = time.perf_counter()
        try:
            snapshot = await deps.query_snapshot(workflow_id, limit=limit)
        except Exception as err:
            if not deps.is_temporal_not_found(err):
                raise
            await deps.forget_conversation(user.user_id, workflow_id, user.username)
            raise HTTPException(
                status_code=404,
                detail="Workflow execution not found. Start a new chat.",
            ) from err
        record_timing(timings, "temporal", query_started)

        artifacts_started = time.perf_counter()
        artifacts = deps.store().list_artifacts(
            user_id=user.user_id,
            workflow_id=workflow_id,
        )
        record_timing(timings, "artifacts", artifacts_started)

        body = snapshot_to_dict(snapshot, artifacts=artifacts)
        set_transcript_headers(response, body)
        response.headers["Server-Timing"] = server_timing(timings)
        return body

    @router.get("/api/sessions/{workflow_id}/messages")
    async def get_messages(
        request: Request,
        workflow_id: str,
        response: Response,
        before: int | None = Query(default=None, ge=0),
        limit: int = Query(default=60, ge=1, le=200),
    ) -> dict[str, Any]:
        timings: list[tuple[str, float]] = []
        started = time.perf_counter()
        user = await deps.require_conversation_owner(request, workflow_id)
        record_timing(timings, "owner", started)
        response.headers["Cache-Control"] = "no-store"

        query_started = time.perf_counter()
        try:
            page = await deps.query_transcript_page(
                workflow_id,
                before=before,
                limit=limit,
            )
        except Exception as err:
            if not deps.is_temporal_not_found(err):
                raise
            await deps.forget_conversation(user.user_id, workflow_id, user.username)
            raise HTTPException(
                status_code=404,
                detail="Workflow execution not found. Start a new chat.",
            ) from err
        record_timing(timings, "temporal", query_started)

        body = transcript_page_to_dict(page)
        response.headers["Server-Timing"] = server_timing(timings)
        response.headers["X-Transcript-Start"] = str(body["start"])
        response.headers["X-Transcript-End"] = str(body["end"])
        response.headers["X-Transcript-Total"] = str(body["total"])
        return body

    @router.get("/api/sessions/{workflow_id}/messages/deltas")
    async def get_message_deltas(
        request: Request,
        workflow_id: str,
        response: Response,
        after_revision: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        timings: list[tuple[str, float]] = []
        started = time.perf_counter()
        user = await deps.require_conversation_owner(request, workflow_id)
        record_timing(timings, "owner", started)
        response.headers["Cache-Control"] = "no-store"

        query_started = time.perf_counter()
        try:
            result = await deps.query_transcript_deltas_since(
                workflow_id,
                after_revision=after_revision,
            )
        except Exception as err:
            if not deps.is_temporal_not_found(err):
                raise
            await deps.forget_conversation(user.user_id, workflow_id, user.username)
            raise HTTPException(
                status_code=404,
                detail="Workflow execution not found. Start a new chat.",
            ) from err
        record_timing(timings, "temporal", query_started)

        body = transcript_delta_result_to_dict(result)
        response.headers["Server-Timing"] = server_timing(timings)
        response.headers["X-Transcript-Revision"] = str(body["to_revision"])
        response.headers["X-Transcript-Total"] = str(body["transcript_length"])
        return body

    @router.post("/api/sessions/{workflow_id}/chat")
    async def chat(
        http_request: Request,
        workflow_id: str,
        request: MessageRequest,
    ) -> dict[str, str]:
        user = await deps.require_conversation_owner(http_request, workflow_id)
        await deps.signal_workflow(
            http_request,
            workflow_id,
            SimpleChatWorkflow.chat,
            request.message,
        )
        await deps.touch_conversation(
            user.user_id,
            workflow_id,
            title=conversation_title(request.message),
            user_email=user.username,
        )
        await deps.touch_demo_workspace()
        return {"status": "ok"}

    @router.post("/api/sessions/{workflow_id}/steer")
    async def steer(
        http_request: Request,
        workflow_id: str,
        request: SteerRequest,
    ) -> dict[str, str]:
        user = await deps.require_conversation_owner(http_request, workflow_id)
        await deps.signal_workflow(
            http_request,
            workflow_id,
            SimpleChatWorkflow.steer,
            args=[request.message, request.mode],
        )
        await deps.touch_conversation(
            user.user_id,
            workflow_id,
            user_email=user.username,
        )
        await deps.touch_demo_workspace()
        return {"status": "ok"}

    @router.post("/api/sessions/{workflow_id}/interrupt")
    async def interrupt(
        http_request: Request,
        workflow_id: str,
        request: MessageRequest,
    ) -> dict[str, str]:
        user = await deps.require_conversation_owner(http_request, workflow_id)
        await deps.signal_workflow(
            http_request,
            workflow_id,
            SimpleChatWorkflow.interrupt,
            request.message,
        )
        await deps.touch_conversation(
            user.user_id,
            workflow_id,
            user_email=user.username,
        )
        await deps.touch_demo_workspace()
        return {"status": "ok"}

    @router.post("/api/sessions/{workflow_id}/approvals/{approval_id}")
    async def resolve_approval(
        http_request: Request,
        workflow_id: str,
        approval_id: str,
        request: ApprovalDecisionRequest,
    ) -> dict[str, str]:
        user = await deps.require_conversation_owner(http_request, workflow_id)
        await deps.signal_workflow(
            http_request,
            workflow_id,
            SimpleChatWorkflow.resolve_approval,
            args=[approval_id, request.decision],
        )
        await deps.touch_conversation(
            user.user_id,
            workflow_id,
            user_email=user.username,
        )
        await deps.touch_demo_workspace()
        return {"status": "ok"}

    @router.get("/api/sessions/{workflow_id}/artifacts")
    async def list_session_artifacts(
        request: Request,
        workflow_id: str,
    ) -> dict[str, Any]:
        user = await deps.require_conversation_owner(request, workflow_id)
        return {
            "artifacts": artifact_dicts(
                deps.store().list_artifacts(
                    user_id=user.user_id,
                    workflow_id=workflow_id,
                )
            )
        }

    @router.get("/api/artifacts/{artifact_id}")
    async def view_artifact(request: Request, artifact_id: str) -> Response:
        user = deps.current_user(request)
        artifact = deps.store().get_artifact(
            user_id=user.user_id,
            artifact_id=artifact_id,
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return artifact_response(deps.store(), artifact, disposition="inline")

    @router.get("/api/artifacts/{artifact_id}/download")
    async def download_artifact(request: Request, artifact_id: str) -> Response:
        user = deps.current_user(request)
        artifact = deps.store().get_artifact(
            user_id=user.user_id,
            artifact_id=artifact_id,
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return artifact_response(deps.store(), artifact, disposition="attachment")

    @router.get("/api/sessions/{workflow_id}/events")
    async def events(workflow_id: str, request: Request) -> StreamingResponse:
        await deps.require_conversation_owner(request, workflow_id)
        return StreamingResponse(
            deps.stream_broker().event_stream(workflow_id, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/api/sessions/{workflow_id}/stream/events")
    async def stream_events(
        workflow_id: str,
        request: Request,
        cursor: str | None = Query(None),
        limit: int = Query(1000, ge=1, le=5000),
    ) -> dict[str, Any]:
        await deps.require_conversation_owner(request, workflow_id)
        return deps.stream_broker().replay(
            workflow_id,
            cursor=cursor,
            limit=limit,
        )

    @router.delete("/api/sessions/{workflow_id}")
    async def delete_session(request: Request, workflow_id: str) -> dict[str, str]:
        user = await deps.require_conversation_owner(request, workflow_id)
        await (
            await deps.ensure_user_chats_workflow(user.user_id, user.username)
        ).execute_update(
            UserChatsWorkflow.delete_chat,
            workflow_id,
        )
        deps.store().delete_artifacts_for_conversation(
            user_id=user.user_id,
            workflow_id=workflow_id,
        )
        deps.stream_broker().clear(workflow_id)
        await deps.unregister_demo_workspace_chat(workflow_id)
        return {"status": "ok"}

    return router
