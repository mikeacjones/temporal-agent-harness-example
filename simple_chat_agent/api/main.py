from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from claude_harness.claude_agent import (
    DEFAULT_THINKING_BUDGET_TOKENS,
    MIN_THINKING_BUDGET_TOKENS,
)
from claude_harness.mcp import (
    configure_mcp_auth_resolver,
    configure_mcp_http_auth_resolver,
)
from claude_harness.mcp_types import HttpMcpServerConfig
from simple_chat_agent import TASK_QUEUE
from simple_chat_agent.api.auth import (
    DEFAULT_SESSION_SECONDS,
    SESSION_COOKIE,
    AuthenticatedUser,
    AuthError,
    create_session_token,
    user_from_google_subject,
    user_from_session_token,
)
from simple_chat_agent.common.env import load_dotenv
from simple_chat_agent.common.external_storage import (
    purge_workflow_payloads,
    simple_chat_data_converter,
)
from simple_chat_agent.api.github_oauth import (
    GITHUB_PROVIDER,
)
from simple_chat_agent.api.anthropic_models import (
    EFFORT_ORDER,
    default_effort,
    default_thinking_mode,
    get_anthropic_model_catalog,
)
from simple_chat_agent.api.google_oauth import (
    GOOGLE_PROVIDER,
    GoogleOAuthError,
    exchange_google_code,
    google_allowed_domain,
    google_authorize_url,
    google_oauth_configured,
    google_redirect_uri_from_base,
    identity_from_id_token,
)
from simple_chat_agent.api.routes.sessions import (
    SessionRouteDeps,
    create_sessions_router,
)
from simple_chat_agent.api.routes.tools import (
    ToolRouteDeps,
    create_tools_router,
)
from simple_chat_agent.api.streaming import StreamBroker
from simple_chat_agent.api.temporal_ui import temporal_ui_user_workflows_url
from simple_chat_agent.common.mcp_auth import (
    resolve_mcp_auth_headers,
    resolve_mcp_http_auth,
)
from simple_chat_agent.common.mcp_oauth import PendingMcpOAuthFlow
from simple_chat_agent.common.store import AppStore
from simple_chat_agent.worker.tools import tool_names_for_connections
from simple_chat_agent.worker.user_chats_workflow import (
    ChatRecord,
    TouchChatRequest,
    UpdateMcpServerRequest,
    UserChatsInput,
    UserChatsWorkflow,
    user_chats_workflow_id,
    user_email_search_attributes,
)
from simple_chat_agent.worker.workflow import (
    SimpleChatSnapshot,
    SimpleChatState,
    SimpleChatWorkflow,
    TranscriptDeltaResult,
    TranscriptPage,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    load_dotenv()
    configure_mcp_auth_resolver(resolve_mcp_auth_headers)
    configure_mcp_http_auth_resolver(resolve_mcp_http_auth)

    client_config = {
        "namespace": os.environ.get("TEMPORAL_NAMESPACE", "default"),
        "data_converter": simple_chat_data_converter(),
        "tls": os.environ.get("TEMPORAL_TLS", "false").lower() in ["true", "1"],
    }
    if os.environ.get("TEMPORAL_API_KEY"):
        client_config["api_key"] = os.environ.get("TEMPORAL_API_KEY")

    app.state.temporal_client = await Client.connect(
        os.environ.get("TEMPORAL_ENDPOINT", "localhost:7233"), **client_config
    )

    app.state.store = AppStore()
    app.state.mcp_oauth_flows = {}
    # In-memory per-stream event buffers, used when streaming arrives over the
    # API-owned HTTP endpoint (deployment) instead of local files (local dev).
    app.state.stream_broker = StreamBroker()
    yield


app = FastAPI(lifespan=lifespan)
# Starlette excludes text/event-stream from gzip, so live SSE latency is not
# traded for buffering while large JSON state snapshots still compress.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

# Local-dev fallback: the dedicated frontend server owns static assets in
# deployment, but FastAPI can still serve them when run directly.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "static"
_FRONTEND_INDEX = _STATIC_DIR / "dist" / "index.html"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_FRONTEND_INDEX)


@app.get("/api/me")
async def me(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    return {
        "user_id": user.user_id,
        "username": user.username,
        # Link to all of this user's workflows in the Temporal UI, filtered by
        # the UserEmail search attribute. None when the attribute is disabled.
        "temporal_ui_workflows_url": temporal_ui_user_workflows_url(
            user.username,
            namespace=_client().namespace,
            search_attr_name=_user_email_sa_name(),
        ),
    }


@app.get("/api/config")
async def config(request: Request) -> dict[str, Any]:
    _current_user(request)
    model_catalog = await asyncio.to_thread(get_anthropic_model_catalog)
    default_model = model_catalog.model_by_id(model_catalog.default_model)
    return {
        "default_model": model_catalog.default_model,
        "model_options": model_catalog.model_ids(),
        "models": [model.to_api_dict() for model in model_catalog.models],
        "model_source": model_catalog.source,
        "model_error": model_catalog.error,
        "thinking": {
            "enabled": False,
            "budget_tokens": DEFAULT_THINKING_BUDGET_TOKENS,
            "min_budget_tokens": MIN_THINKING_BUDGET_TOKENS,
            "mode": default_thinking_mode(default_model),
            "mode_options": list(default_model.thinking_modes if default_model else ()),
            "effort": default_effort(default_model),
            "effort_options": list(default_model.effort_options if default_model else EFFORT_ORDER),
        },
    }


@app.get("/api/auth/google/configured")
async def google_auth_configured() -> dict[str, Any]:
    return {
        "configured": google_oauth_configured(),
        "allowed_domain": google_allowed_domain(),
    }


@app.get("/oauth/google/start")
async def google_oauth_start(request: Request) -> RedirectResponse:
    if not google_oauth_configured():
        raise HTTPException(status_code=400, detail="Google OAuth is not configured")

    state = _store().create_oauth_state(
        user_id="",
        provider=GOOGLE_PROVIDER,
    )
    redirect_uri = google_redirect_uri_from_base(str(request.base_url))
    return RedirectResponse(
        google_authorize_url(state=state, redirect_uri=redirect_uri)
    )


@app.get("/oauth/google/callback")
async def google_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    if error is not None:
        return RedirectResponse(
            f"/?oauth_error={quote(error_description or error, safe='')}"
        )
    if not code or not state:
        return RedirectResponse("/?oauth_error=Missing%20Google%20OAuth%20callback")

    consumed = _store().consume_oauth_state(
        state=state,
        provider=GOOGLE_PROVIDER,
    )
    if consumed is None:
        return RedirectResponse("/?oauth_error=Invalid%20or%20expired%20OAuth%20state")

    redirect_uri = google_redirect_uri_from_base(str(request.base_url))
    try:
        token_payload = await asyncio.to_thread(
            exchange_google_code, code, redirect_uri=redirect_uri
        )
        id_token = token_payload.get("id_token")
        if not isinstance(id_token, str):
            raise GoogleOAuthError("Google did not return an ID token.")
        identity = identity_from_id_token(id_token)
    except GoogleOAuthError as err:
        return RedirectResponse(f"/?oauth_error={quote(str(err), safe='')}")

    user = user_from_google_subject(subject=identity.subject, email=identity.email)
    response = RedirectResponse("/")
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user),
        max_age=DEFAULT_SESSION_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/api/logout")
async def logout() -> Response:
    response = Response(
        content=json.dumps({"status": "ok"}),
        media_type="application/json",
    )
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.post("/internal/stream")
async def internal_stream(request: Request) -> dict[str, str]:
    # Worker -> web: append a stream event to the in-memory per-stream buffer.
    # Authenticated with a shared token (cluster-internal); not user-facing.
    token = os.environ.get("SIMPLE_CHAT_STREAM_TOKEN", "").strip()
    if not token or request.headers.get("x-stream-token") != token:
        raise HTTPException(status_code=401, detail="Invalid stream token.")
    event = await request.json()
    stream_id = event.get("stream_id")
    if stream_id:
        _stream_broker().append(stream_id, event)
    return {"status": "ok"}


async def _query_state(workflow_id: str) -> SimpleChatState:
    return await _handle(workflow_id).query(SimpleChatWorkflow.state)


async def _query_snapshot(workflow_id: str, *, limit: int) -> SimpleChatSnapshot:
    return await _handle(workflow_id).query(SimpleChatWorkflow.snapshot, limit)


async def _query_transcript_page(
    workflow_id: str,
    *,
    before: int | None,
    limit: int,
) -> TranscriptPage:
    return await _handle(workflow_id).query(
        SimpleChatWorkflow.transcript_page,
        before,
        limit,
    )


async def _query_transcript_deltas_since(
    workflow_id: str,
    *,
    after_revision: int,
) -> TranscriptDeltaResult:
    return await _handle(workflow_id).query(
        SimpleChatWorkflow.transcript_deltas_since,
        after_revision,
    )


async def _signal_workflow(
    request: Request,
    workflow_id: str,
    signal: Any,
    *signal_args: Any,
    args: list[Any] | None = None,
) -> None:
    try:
        if args is not None:
            if signal_args:
                raise TypeError("Use either positional signal args or args=, not both")
            await _handle(workflow_id).signal(signal, args=args)
        else:
            await _handle(workflow_id).signal(signal, *signal_args)
    except Exception as err:
        if not _is_temporal_not_found(err):
            raise

        user = _current_user(request)
        await _forget_conversation(user.user_id, workflow_id, user.username)
        raise HTTPException(
            status_code=404,
            detail="Workflow execution not found. Start a new chat.",
        ) from err


async def _list_user_chats(user_id: str, user_email: str = "") -> list[ChatRecord]:
    handle = await _ensure_user_chats_workflow(user_id, user_email)
    return await handle.query(UserChatsWorkflow.list_chats)


async def _touch_conversation(
    user_id: str,
    workflow_id: str,
    *,
    title: str | None = None,
    user_email: str = "",
) -> None:
    await (await _ensure_user_chats_workflow(user_id, user_email)).execute_update(
        UserChatsWorkflow.touch_chat,
        TouchChatRequest(workflow_id=workflow_id, title=title),
    )


async def _forget_conversation(
    user_id: str, workflow_id: str, user_email: str = ""
) -> None:
    await (await _ensure_user_chats_workflow(user_id, user_email)).execute_update(
        UserChatsWorkflow.forget_chat,
        workflow_id,
    )
    _store().delete_artifacts_for_conversation(
        user_id=user_id,
        workflow_id=workflow_id,
    )
    _stream_broker().clear(workflow_id)
    # Purge the chat's offloaded payloads from external storage. Best-effort:
    # a purge failure must not block forgetting the conversation. No-op when
    # S3 storage is not configured (local dev).
    try:
        await asyncio.to_thread(
            purge_workflow_payloads,
            namespace=_client().namespace,
            workflow_id=workflow_id,
        )
    except Exception as err:  # noqa: BLE001 - cleanup is best-effort
        print(f"Failed to purge external payloads for {workflow_id}: {err!r}")


def _is_temporal_not_found(err: BaseException) -> bool:
    return isinstance(err, RPCError) and err.status == RPCStatusCode.NOT_FOUND


def _handle(workflow_id: str) -> Any:
    return _client().get_workflow_handle(workflow_id)


def _user_email_sa_name() -> str:
    return os.environ.get("SIMPLE_CHAT_USER_EMAIL_SEARCH_ATTR", "").strip()


async def _ensure_user_chats_workflow(user_id: str, user_email: str = "") -> Any:
    workflow_id = user_chats_workflow_id(user_id)
    search_attr_name = _user_email_sa_name()
    return await _client().start_workflow(
        UserChatsWorkflow.run,
        UserChatsInput(
            user_id=user_id,
            user_email=user_email,
            search_attr_name=search_attr_name,
        ),
        id=workflow_id,
        task_queue=TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        static_summary="simple chat user registry",
        search_attributes=user_email_search_attributes(
            search_attr_name=search_attr_name,
            user_email=user_email,
        ),
    )


def _client() -> Client:
    client = getattr(app.state, "temporal_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Temporal client is not ready")
    return client


def _store() -> AppStore:
    store = getattr(app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="App store is not ready")
    return store


def _stream_broker() -> StreamBroker:
    broker = getattr(app.state, "stream_broker", None)
    if broker is None:
        broker = StreamBroker()
        app.state.stream_broker = broker
    return broker


def _current_user(request: Request) -> AuthenticatedUser:
    token = request.cookies.get(SESSION_COOKIE)
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return user_from_session_token(token)
    except AuthError as err:
        raise HTTPException(status_code=401, detail=str(err)) from err


async def _require_conversation_owner(
    request: Request,
    workflow_id: str,
) -> AuthenticatedUser:
    user = _current_user(request)
    if not await (await _ensure_user_chats_workflow(user.user_id, user.username)).query(
        UserChatsWorkflow.has_chat,
        workflow_id,
    ):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return user


async def _update_user_workflows_tool_connections(
    user: AuthenticatedUser,
) -> None:
    github_connection_id = _github_connection_id_for_user(user)
    mcp_servers = await (
        await _ensure_user_chats_workflow(user.user_id, user.username)
    ).query(UserChatsWorkflow.list_mcp_servers)
    available_tool_names = tool_names_for_connections(
        github_connection_id=github_connection_id,
        mcp_servers=mcp_servers,
    )

    for conversation in await _list_user_chats(user.user_id, user.username):
        with suppress(Exception):
            await _handle(conversation.workflow_id).signal(
                SimpleChatWorkflow.update_tool_connections,
                args=[available_tool_names, github_connection_id, mcp_servers],
            )


async def _upsert_user_mcp_server(
    user: AuthenticatedUser,
    server: HttpMcpServerConfig,
) -> None:
    registry = await _ensure_user_chats_workflow(user.user_id, user.username)
    await registry.execute_update(
        UserChatsWorkflow.upsert_mcp_server,
        UpdateMcpServerRequest(
            server=server,
            available_tool_names=tool_names_for_connections(
                github_connection_id=_github_connection_id_for_user(user),
                mcp_servers=[
                    *[
                        existing
                        for existing in await registry.query(
                            UserChatsWorkflow.list_mcp_servers
                        )
                        if existing.server_id != server.server_id
                    ],
                    server,
                ],
            ),
            github_connection_id=_github_connection_id_for_user(user),
        ),
    )


async def _available_tool_names_for_user(user: AuthenticatedUser) -> list[str]:
    mcp_servers = await (
        await _ensure_user_chats_workflow(user.user_id, user.username)
    ).query(UserChatsWorkflow.list_mcp_servers)
    return tool_names_for_connections(
        github_connection_id=_github_connection_id_for_user(user),
        mcp_servers=mcp_servers,
    )


def _github_connection_id_for_user(user: AuthenticatedUser) -> str | None:
    github_connection = _store().get_oauth_connection(
        user_id=user.user_id,
        provider=GITHUB_PROVIDER,
    )
    return github_connection.connection_id if github_connection is not None else None


def _mcp_oauth_flows() -> dict[str, PendingMcpOAuthFlow]:
    flows = getattr(app.state, "mcp_oauth_flows", None)
    if flows is None:
        flows = {}
        app.state.mcp_oauth_flows = flows
    return flows


app.include_router(
    create_sessions_router(
        SessionRouteDeps(
            client=_client,
            store=_store,
            stream_broker=_stream_broker,
            current_user=_current_user,
            require_conversation_owner=_require_conversation_owner,
            ensure_user_chats_workflow=_ensure_user_chats_workflow,
            list_user_chats=_list_user_chats,
            query_state=_query_state,
            query_snapshot=_query_snapshot,
            query_transcript_page=_query_transcript_page,
            query_transcript_deltas_since=_query_transcript_deltas_since,
            signal_workflow=_signal_workflow,
            touch_conversation=_touch_conversation,
            forget_conversation=_forget_conversation,
            is_temporal_not_found=_is_temporal_not_found,
            github_connection_id_for_user=_github_connection_id_for_user,
        )
    )
)
app.include_router(
    create_tools_router(
        ToolRouteDeps(
            store=_store,
            current_user=_current_user,
            ensure_user_chats_workflow=_ensure_user_chats_workflow,
            update_user_workflows_tool_connections=(
                _update_user_workflows_tool_connections
            ),
            upsert_user_mcp_server=_upsert_user_mcp_server,
            github_connection_id_for_user=_github_connection_id_for_user,
            mcp_oauth_flows=_mcp_oauth_flows,
        )
    )
)


def main() -> None:
    uvicorn.run(
        "simple_chat_agent.api.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
