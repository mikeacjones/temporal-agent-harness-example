from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import quote, urlparse
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from claude_harness.claude_agent import (
    DEFAULT_THINKING_BUDGET_TOKENS,
    MIN_THINKING_BUDGET_TOKENS,
    ClaudeThinkingConfig,
    ClaudeThinkingEffort,
)
from claude_harness.mcp import (
    configure_mcp_auth_resolver,
    configure_mcp_http_auth_resolver,
    discover_http_mcp_tools,
    public_mcp_tool_name,
)
from claude_harness.mcp_types import HttpMcpServerConfig
from simple_chat_agent import TASK_QUEUE
from simple_chat_agent.auth import (
    DEFAULT_SESSION_SECONDS,
    SESSION_COOKIE,
    AuthenticatedUser,
    AuthError,
    create_session_token,
    user_from_google_subject,
    user_from_session_token,
)
from simple_chat_agent.env import load_dotenv
from simple_chat_agent.external_storage import (
    purge_workflow_payloads,
    simple_chat_data_converter,
)
from simple_chat_agent.github_oauth import (
    GITHUB_PROVIDER,
    GitHubOAuthError,
    exchange_github_code,
    fetch_github_user,
    github_authorize_url,
    github_oauth_configured,
    github_scopes,
)
from simple_chat_agent.google_oauth import (
    GOOGLE_PROVIDER,
    GoogleOAuthError,
    exchange_google_code,
    google_allowed_domain,
    google_authorize_url,
    google_oauth_configured,
    google_redirect_uri_from_base,
    identity_from_id_token,
)
from simple_chat_agent.mcp_auth import (
    mcp_oauth_provider,
    resolve_mcp_auth_headers,
    resolve_mcp_http_auth,
)
from simple_chat_agent.mcp_oauth import (
    PendingMcpOAuthFlow,
    authorize_mcp_oauth_flow,
)
from simple_chat_agent.store import AppStore, ArtifactRecord
from simple_chat_agent.streaming import stream_path
from simple_chat_agent.tools import (
    CREATE_ARTIFACT_TOOL,
    CREATE_SUBAGENT_TOOL,
    FETCH_URL_TOOL,
    GITHUB_TOOL_NAMES,
    PYTHON_SANDBOX_TOOL,
    tool_names_for_connections,
)
from simple_chat_agent.user_chats_workflow import (
    ChatRecord,
    CreateChatRequest,
    DeleteMcpServerRequest,
    TouchChatRequest,
    UpdateMcpServerRequest,
    UserChatsInput,
    UserChatsWorkflow,
    user_chats_workflow_id,
    user_email_search_attributes,
)
from simple_chat_agent.workflow import (
    DEFAULT_MAX_TOKENS,
    SimpleChatState,
    SimpleChatWorkflow,
)

STATE_POLL_INTERVAL_SECONDS = 0.1
STREAM_POLL_INTERVAL_SECONDS = 0.02
DEFAULT_THINKING_EFFORT: ClaudeThinkingEffort = "medium"
ADAPTIVE_THINKING_MODEL_PREFIXES = ("claude-opus-4-7",)
DEFAULT_MODEL_OPTIONS = [
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-7",
    "claude-haiku-4-5",
]


class ThinkingSessionRequest(BaseModel):
    enabled: bool = False
    budget_tokens: int = DEFAULT_THINKING_BUDGET_TOKENS
    effort: ClaudeThinkingEffort = DEFAULT_THINKING_EFFORT


class CreateSessionRequest(BaseModel):
    system_prompt: str = "You are a concise test chatbot."
    model: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_turns: int = 20
    thinking: ThinkingSessionRequest = Field(default_factory=ThinkingSessionRequest)
    initial_message: str | None = None


class MessageRequest(BaseModel):
    message: str


class SteerRequest(MessageRequest):
    mode: Literal["immediate", "after_next_tool_result"] = "immediate"


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["allow", "always_allow", "deny"]


class McpServerRequest(BaseModel):
    label: str
    server_url: str
    tool_prefix: str
    auth_mode: Literal["none", "bearer", "oauth"] = "none"
    bearer_token: str | None = None


class McpServerEnabledRequest(BaseModel):
    enabled: bool


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
    # web-owned HTTP API (deployment) instead of local files (local dev).
    app.state.stream_buffers = {}
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.get("/api/me")
async def me(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    return {
        "user_id": user.user_id,
        "username": user.username,
        # Link to all of this user's workflows in the Temporal UI, filtered by
        # the UserEmail search attribute. None when the attribute is disabled.
        "temporal_ui_workflows_url": _temporal_ui_user_workflows_url(user.username),
    }


@app.get("/api/config")
async def config(request: Request) -> dict[str, Any]:
    _current_user(request)
    return {
        "default_model": _default_model(),
        "model_options": _model_options(),
        "thinking": {
            "enabled": False,
            "budget_tokens": DEFAULT_THINKING_BUDGET_TOKENS,
            "min_budget_tokens": MIN_THINKING_BUDGET_TOKENS,
            "effort": DEFAULT_THINKING_EFFORT,
            "effort_options": ["low", "medium", "high", "xhigh", "max"],
            "adaptive_model_prefixes": list(ADAPTIVE_THINKING_MODEL_PREFIXES),
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
    return RedirectResponse(google_authorize_url(state=state, redirect_uri=redirect_uri))


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


@app.get("/api/conversations")
async def conversations(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    conversations = await _list_user_chats(user.user_id, user.username)
    return {
        "conversations": [
            {
                **asdict(conversation),
                "temporal_ui_url": _temporal_ui_url(
                    namespace=_client().namespace,
                    workflow_id=conversation.workflow_id,
                    run_id=conversation.run_id,
                ),
            }
            for conversation in conversations
        ]
    }


@app.post("/api/sessions")
async def create_session(
    request: Request,
    session_request: CreateSessionRequest,
) -> dict[str, str]:
    user = _current_user(request)
    client = _client()
    github_connection = _store().get_oauth_connection(
        user_id=user.user_id,
        provider=GITHUB_PROVIDER,
    )
    github_connection_id = (
        github_connection.connection_id if github_connection is not None else None
    )
    registry = await _ensure_user_chats_workflow(user.user_id, user.username)
    mcp_servers = await registry.query(UserChatsWorkflow.list_mcp_servers)
    model = session_request.model or _default_model()
    conversation = await registry.execute_update(
        UserChatsWorkflow.create_chat,
        CreateChatRequest(
            system_prompt=session_request.system_prompt,
            model=model,
            max_tokens=session_request.max_tokens,
            max_turns=session_request.max_turns,
            thinking=_thinking_config_from_request(
                session_request.thinking,
                model=model,
                max_tokens=session_request.max_tokens,
            ),
            initial_message=session_request.initial_message,
            available_tool_names=tool_names_for_connections(
                github_connection_id=github_connection_id,
                mcp_servers=mcp_servers,
            ),
            github_connection_id=github_connection_id,
            mcp_servers=mcp_servers,
        ),
    )
    _clear_stream(conversation.workflow_id)
    return {
        "workflow_id": conversation.workflow_id,
        "run_id": conversation.run_id,
        "temporal_ui_url": _temporal_ui_url(
            namespace=client.namespace,
            workflow_id=conversation.workflow_id,
            run_id=conversation.run_id,
        ),
    }


@app.get("/api/sessions/{workflow_id}/state")
async def get_state(request: Request, workflow_id: str) -> dict[str, Any]:
    user = await _require_conversation_owner(request, workflow_id)
    try:
        state = await _query_state(workflow_id)
    except Exception as err:
        if not _is_temporal_not_found(err):
            raise
        await _forget_conversation(user.user_id, workflow_id, user.username)
        raise HTTPException(
            status_code=404,
            detail="Workflow execution not found. Start a new chat.",
        ) from err
    return _state_to_dict(
        state,
        artifacts=_store().list_artifacts(
            user_id=user.user_id,
            workflow_id=workflow_id,
        ),
    )


@app.post("/api/sessions/{workflow_id}/chat")
async def chat(
    http_request: Request,
    workflow_id: str,
    request: MessageRequest,
) -> dict[str, str]:
    user = await _require_conversation_owner(http_request, workflow_id)
    await _signal_workflow(
        http_request,
        workflow_id,
        SimpleChatWorkflow.chat,
        request.message,
    )
    await _touch_conversation(
        user.user_id,
        workflow_id,
        title=_conversation_title(request.message),
        user_email=user.username,
    )
    return {"status": "ok"}


@app.post("/api/sessions/{workflow_id}/steer")
async def steer(
    http_request: Request,
    workflow_id: str,
    request: SteerRequest,
) -> dict[str, str]:
    user = await _require_conversation_owner(http_request, workflow_id)
    await _signal_workflow(
        http_request,
        workflow_id,
        SimpleChatWorkflow.steer,
        args=[request.message, request.mode],
    )
    await _touch_conversation(user.user_id, workflow_id, user_email=user.username)
    return {"status": "ok"}


@app.post("/api/sessions/{workflow_id}/interrupt")
async def interrupt(
    http_request: Request,
    workflow_id: str,
    request: MessageRequest,
) -> dict[str, str]:
    user = await _require_conversation_owner(http_request, workflow_id)
    await _signal_workflow(
        http_request,
        workflow_id,
        SimpleChatWorkflow.interrupt,
        request.message,
    )
    await _touch_conversation(user.user_id, workflow_id, user_email=user.username)
    return {"status": "ok"}


@app.post("/api/sessions/{workflow_id}/approvals/{approval_id}")
async def resolve_approval(
    http_request: Request,
    workflow_id: str,
    approval_id: str,
    request: ApprovalDecisionRequest,
) -> dict[str, str]:
    user = await _require_conversation_owner(http_request, workflow_id)
    await _signal_workflow(
        http_request,
        workflow_id,
        SimpleChatWorkflow.resolve_approval,
        args=[approval_id, request.decision],
    )
    await _touch_conversation(user.user_id, workflow_id, user_email=user.username)
    return {"status": "ok"}


@app.get("/api/sessions/{workflow_id}/artifacts")
async def list_session_artifacts(
    request: Request,
    workflow_id: str,
) -> dict[str, Any]:
    user = await _require_conversation_owner(request, workflow_id)
    return {
        "artifacts": _artifact_dicts(
            _store().list_artifacts(user_id=user.user_id, workflow_id=workflow_id)
        )
    }


@app.get("/api/artifacts/{artifact_id}")
async def view_artifact(request: Request, artifact_id: str) -> Response:
    user = _current_user(request)
    artifact = _store().get_artifact(
        user_id=user.user_id,
        artifact_id=artifact_id,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _artifact_response(artifact, disposition="inline")


@app.get("/api/artifacts/{artifact_id}/download")
async def download_artifact(request: Request, artifact_id: str) -> Response:
    user = _current_user(request)
    artifact = _store().get_artifact(
        user_id=user.user_id,
        artifact_id=artifact_id,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _artifact_response(artifact, disposition="attachment")


@app.get("/api/sessions/{workflow_id}/events")
async def events(workflow_id: str, request: Request) -> StreamingResponse:
    await _require_conversation_owner(request, workflow_id)
    return StreamingResponse(
        _event_stream(workflow_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
        _append_stream_event(stream_id, event)
    return {"status": "ok"}


@app.delete("/api/sessions/{workflow_id}")
async def delete_session(request: Request, workflow_id: str) -> dict[str, str]:
    user = await _require_conversation_owner(request, workflow_id)
    await (await _ensure_user_chats_workflow(user.user_id, user.username)).execute_update(
        UserChatsWorkflow.delete_chat,
        workflow_id,
    )
    _store().delete_artifacts_for_conversation(
        user_id=user.user_id,
        workflow_id=workflow_id,
    )
    _clear_stream(workflow_id)
    return {"status": "ok"}


@app.get("/api/tools")
async def tools(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    github_connection = _store().get_oauth_connection(
        user_id=user.user_id,
        provider=GITHUB_PROVIDER,
    )
    mcp_servers = await (await _ensure_user_chats_workflow(user.user_id, user.username)).query(
        UserChatsWorkflow.list_mcp_servers
    )
    return {
        "tools": [
            {
                "provider": "builtin:core",
                "label": "Harness tools",
                "configured": True,
                "connected": True,
                "enabled": True,
                "login": "local workflow activities",
                "scopes": "local",
                "available_tools": [
                    FETCH_URL_TOOL,
                    PYTHON_SANDBOX_TOOL,
                    CREATE_ARTIFACT_TOOL,
                    CREATE_SUBAGENT_TOOL,
                ],
            },
            {
                "provider": GITHUB_PROVIDER,
                "label": "GitHub",
                "configured": github_oauth_configured(),
                "connected": github_connection is not None,
                "enabled": github_connection is not None,
                "login": (
                    github_connection.provider_user_login
                    if github_connection is not None
                    else None
                ),
                "scopes": (
                    github_connection.scope
                    if github_connection is not None
                    else github_scopes()
                ),
                "available_tools": GITHUB_TOOL_NAMES,
            },
            *[
                {
                    "provider": f"mcp:{server.server_id}",
                    "server_id": server.server_id,
                    "server_url": server.server_url,
                    "tool_prefix": server.tool_prefix,
                    "auth_mode": server.auth_mode,
                    "label": server.label,
                    "configured": True,
                    "connected": _mcp_server_connected(user, server),
                    "enabled": server.enabled,
                    "login": server.server_url,
                    "scopes": server.auth_mode,
                    "available_tools": [
                        tool.public_name
                        or public_mcp_tool_name(server.tool_prefix, tool.name)
                        for tool in server.tools
                    ],
                }
                for server in mcp_servers
            ],
        ]
    }


@app.post("/api/tools/github/disconnect")
async def disconnect_github(request: Request) -> dict[str, str]:
    user = _current_user(request)
    _store().delete_oauth_connection(
        user_id=user.user_id,
        provider=GITHUB_PROVIDER,
    )
    await _update_user_workflows_tool_connections(user)
    return {"status": "ok"}


@app.get("/api/mcp-servers")
async def list_mcp_servers(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    servers = await (await _ensure_user_chats_workflow(user.user_id, user.username)).query(
        UserChatsWorkflow.list_mcp_servers
    )
    return {"servers": [asdict(server) for server in servers]}


@app.post("/api/mcp-servers")
async def add_mcp_server(
    http_request: Request,
    request: McpServerRequest,
) -> dict[str, Any]:
    user = _current_user(http_request)
    server_id = f"mcp-{uuid4().hex[:12]}"
    auth_ref = None

    if request.auth_mode == "oauth":
        raise HTTPException(
            status_code=400,
            detail="Use the MCP OAuth flow to add OAuth-discovered MCP servers.",
        )

    if request.auth_mode == "bearer":
        if not request.bearer_token:
            raise HTTPException(status_code=400, detail="Bearer token is required.")
        auth_ref = mcp_oauth_provider(server_id)
        _store().upsert_oauth_connection(
            user_id=user.user_id,
            provider=auth_ref,
            access_token=request.bearer_token,
            token_type="Bearer",
            scope="",
            provider_user_id=None,
            provider_user_login=request.label,
            metadata={"auth_mode": "bearer"},
        )

    try:
        discovered_url, tools = await _discover_mcp_tools_for_user_request(
            request.server_url,
            tool_prefix=request.tool_prefix,
            auth_ref=auth_ref,
        )
    except Exception as err:
        if auth_ref is not None:
            _store().delete_oauth_connection(
                user_id=user.user_id,
                provider=mcp_oauth_provider(server_id),
            )
        raise HTTPException(
            status_code=400,
            detail=_mcp_discovery_error_message(err),
        ) from err

    if not tools:
        if auth_ref is not None:
            _store().delete_oauth_connection(
                user_id=user.user_id,
                provider=mcp_oauth_provider(server_id),
            )
        raise HTTPException(
            status_code=400,
            detail="MCP discovery succeeded, but the server returned no tools.",
        )

    server = HttpMcpServerConfig(
        server_id=server_id,
        label=request.label,
        server_url=discovered_url,
        tool_prefix=request.tool_prefix,
        auth_ref=auth_ref,
        auth_mode=request.auth_mode,
        tools=tools,
    )
    await _upsert_user_mcp_server(user, server)
    return {"server": asdict(server)}


@app.post("/api/mcp-servers/{server_id}/enabled")
async def set_mcp_server_enabled(
    request: Request,
    server_id: str,
    update: McpServerEnabledRequest,
) -> dict[str, Any]:
    user = _current_user(request)
    registry = await _ensure_user_chats_workflow(user.user_id, user.username)
    servers = await registry.query(UserChatsWorkflow.list_mcp_servers)
    existing = next(
        (server for server in servers if server.server_id == server_id),
        None,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="MCP server not found.")

    updated = replace(existing, enabled=update.enabled)
    await _upsert_user_mcp_server(user, updated)
    return {"server": asdict(updated)}


@app.delete("/api/mcp-servers/{server_id}")
async def delete_mcp_server(request: Request, server_id: str) -> dict[str, str]:
    user = _current_user(request)
    registry = await _ensure_user_chats_workflow(user.user_id, user.username)
    remaining_servers = [
        server
        for server in await registry.query(UserChatsWorkflow.list_mcp_servers)
        if server.server_id != server_id
    ]
    await registry.execute_update(
        UserChatsWorkflow.delete_mcp_server,
        DeleteMcpServerRequest(
            server_id=server_id,
            available_tool_names=tool_names_for_connections(
                github_connection_id=_github_connection_id_for_user(user),
                mcp_servers=remaining_servers,
            ),
            github_connection_id=_github_connection_id_for_user(user),
        ),
    )
    _store().delete_oauth_connection(
        user_id=user.user_id,
        provider=mcp_oauth_provider(server_id),
    )
    return {"status": "ok"}


@app.get("/api/mcp-servers/oauth/start")
async def start_mcp_oauth(
    request: Request,
    label: str,
    server_url: str,
    tool_prefix: str,
    server_id: str | None = None,
) -> RedirectResponse:
    user = _current_user(request)
    auth_url = await _start_mcp_oauth_flow(
        user=user,
        label=label,
        server_url=server_url,
        tool_prefix=tool_prefix,
        server_id=server_id,
    )
    return RedirectResponse(auth_url)


async def _start_mcp_oauth_flow(
    *,
    user: AuthenticatedUser,
    label: str,
    server_url: str,
    tool_prefix: str,
    server_id: str | None = None,
) -> str:
    normalized_label = label.strip()
    normalized_server_url = server_url.strip()
    normalized_tool_prefix = tool_prefix.strip()
    if not normalized_label:
        raise HTTPException(status_code=400, detail="MCP server label is required.")
    if not normalized_server_url:
        raise HTTPException(status_code=400, detail="MCP server URL is required.")
    if not normalized_tool_prefix:
        raise HTTPException(status_code=400, detail="MCP tool prefix is required.")

    existing_server = None
    if server_id is None:
        existing_server = await _find_matching_mcp_server(
            user,
            server_url=normalized_server_url,
            tool_prefix=normalized_tool_prefix,
        )

    flow = PendingMcpOAuthFlow(
        user_id=user.user_id,
        server_id=_mcp_server_id(
            server_id or (existing_server.server_id if existing_server else None)
        ),
        server_url=normalized_server_url,
        tool_prefix=normalized_tool_prefix,
        label=normalized_label,
    )
    _mcp_oauth_flows()[flow.flow_id] = flow
    flow.task = asyncio.create_task(_complete_mcp_oauth_flow(flow))

    try:
        await asyncio.wait_for(flow.auth_url_ready.wait(), timeout=30)
    except asyncio.TimeoutError as err:
        _mcp_oauth_flows().pop(flow.flow_id, None)
        if flow.task is not None:
            flow.task.cancel()
        raise HTTPException(
            status_code=504,
            detail="Timed out starting MCP OAuth.",
        ) from err

    if flow.start_error:
        _mcp_oauth_flows().pop(flow.flow_id, None)
        raise HTTPException(status_code=400, detail=flow.start_error)
    if not flow.auth_url:
        raise HTTPException(status_code=500, detail="MCP OAuth did not start.")
    return flow.auth_url


@app.get("/oauth/mcp/callback")
async def mcp_oauth_callback(
    flow_id: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    flow = _mcp_oauth_flows().get(flow_id)
    if flow is None:
        return RedirectResponse("/?oauth_error=Unknown%20MCP%20OAuth%20flow")
    if error is not None:
        flow.fail(RuntimeError(error_description or error))
        return RedirectResponse(
            f"/?oauth_error={quote(error_description or error, safe='')}"
        )
    if not code:
        flow.fail(RuntimeError("Missing MCP OAuth callback code"))
        return RedirectResponse("/?oauth_error=Missing%20MCP%20OAuth%20code")

    flow.complete(code, state)
    if flow.task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(flow.task), timeout=30)
        except Exception as err:
            return RedirectResponse(f"/?oauth_error={quote(str(err), safe='')}")
    return RedirectResponse("/?mcp=connected")


@app.get("/oauth/github/start")
async def github_oauth_start(request: Request) -> RedirectResponse:
    user = _current_user(request)
    if not github_oauth_configured():
        raise HTTPException(status_code=400, detail="GitHub OAuth is not configured")

    state = _store().create_oauth_state(
        user_id=user.user_id,
        provider=GITHUB_PROVIDER,
    )
    return RedirectResponse(github_authorize_url(state=state))


@app.get("/oauth/github/callback")
async def github_oauth_callback(
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
        return RedirectResponse("/?oauth_error=Missing%20GitHub%20OAuth%20callback")

    consumed = _store().consume_oauth_state(
        state=state,
        provider=GITHUB_PROVIDER,
    )
    if consumed is None:
        return RedirectResponse("/?oauth_error=Invalid%20or%20expired%20OAuth%20state")

    user_id, _metadata = consumed
    try:
        token_payload = await asyncio.to_thread(exchange_github_code, code)
        access_token = token_payload.get("access_token")
        if not isinstance(access_token, str):
            raise GitHubOAuthError("GitHub did not return an access token.")
        github_user = await asyncio.to_thread(fetch_github_user, access_token)
    except GitHubOAuthError as err:
        return RedirectResponse(f"/?oauth_error={quote(str(err), safe='')}")

    _store().upsert_oauth_connection(
        user_id=user_id,
        provider=GITHUB_PROVIDER,
        access_token=access_token,
        token_type=str(token_payload.get("token_type") or "bearer"),
        scope=str(token_payload.get("scope") or ""),
        provider_user_id=(
            str(github_user.get("id")) if github_user.get("id") is not None else None
        ),
        provider_user_login=(
            str(github_user.get("login"))
            if github_user.get("login") is not None
            else None
        ),
    )
    await _update_user_workflows_tool_connections(
        AuthenticatedUser(user_id=user_id, username="")
    )
    return RedirectResponse("/?github=connected")


async def _complete_mcp_oauth_flow(flow: PendingMcpOAuthFlow) -> None:
    try:
        await authorize_mcp_oauth_flow(flow=flow, store=_store())
        connection = _store().get_oauth_connection(
            user_id=flow.user_id,
            provider=mcp_oauth_provider(flow.server_id),
        )
        if connection is None or not connection.access_token:
            raise RuntimeError("MCP OAuth completed without storing a connection.")

        discovered_url, tools = await _discover_mcp_tools_for_user_request(
            flow.server_url,
            tool_prefix=flow.tool_prefix,
            auth_ref=mcp_oauth_provider(flow.server_id),
        )
        server = HttpMcpServerConfig(
            server_id=flow.server_id,
            label=flow.label,
            server_url=discovered_url,
            tool_prefix=flow.tool_prefix,
            auth_ref=mcp_oauth_provider(flow.server_id),
            auth_mode="oauth",
            tools=tools,
        )
        await _upsert_user_mcp_server(
            AuthenticatedUser(user_id=flow.user_id, username=""),
            server,
        )
    except Exception as err:
        flow.fail(err)
        raise
    finally:
        _mcp_oauth_flows().pop(flow.flow_id, None)


async def _discover_mcp_tools_for_user_request(
    server_url: str,
    *,
    tool_prefix: str,
    auth_ref: str | None,
) -> tuple[str, list[Any]]:
    first_error: Exception | None = None
    for candidate_url in _mcp_server_url_candidates(server_url):
        try:
            return candidate_url, await discover_http_mcp_tools(
                server_url=candidate_url,
                tool_prefix=tool_prefix,
                auth_ref=auth_ref,
            )
        except Exception as err:
            if first_error is None:
                first_error = err
            if _mcp_error_requires_auth(err):
                raise err

    if first_error is not None:
        raise first_error
    raise ValueError("MCP server URL is required.")


def _mcp_server_url_candidates(server_url: str) -> list[str]:
    normalized = server_url.strip().rstrip("/")
    if not normalized:
        return []

    candidates = [normalized]
    parsed = urlparse(normalized)
    if parsed.scheme in ("http", "https") and parsed.path in ("", "/"):
        candidates.append(f"{normalized}/mcp")
    return candidates


def _mcp_server_id(server_id: str | None) -> str:
    if server_id is None:
        return f"mcp-{uuid4().hex[:12]}"

    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", server_id.strip())
    sanitized = sanitized.strip("-_")
    return sanitized or f"mcp-{uuid4().hex[:12]}"


async def _find_matching_mcp_server(
    user: AuthenticatedUser,
    *,
    server_url: str,
    tool_prefix: str,
) -> HttpMcpServerConfig | None:
    normalized_url = server_url.strip().rstrip("/")
    candidate_urls = set(_mcp_server_url_candidates(normalized_url))
    for server in await (await _ensure_user_chats_workflow(user.user_id, user.username)).query(
        UserChatsWorkflow.list_mcp_servers
    ):
        if server.tool_prefix != tool_prefix:
            continue
        server_urls = set(_mcp_server_url_candidates(server.server_url))
        if (
            normalized_url in server_urls
            or server.server_url.rstrip("/") in candidate_urls
        ):
            return server
    return None


def _mcp_server_connected(
    user: AuthenticatedUser,
    server: HttpMcpServerConfig,
) -> bool:
    if server.auth_mode == "none":
        return True
    if server.auth_ref is None:
        return False

    connection = _store().get_oauth_connection_by_id(server.auth_ref)
    if connection is None:
        connection = _store().get_oauth_connection(
            user_id=user.user_id,
            provider=mcp_oauth_provider(server.server_id),
        )
    return bool(connection and connection.access_token)


def _mcp_discovery_error_message(err: BaseException) -> str:
    if _mcp_error_requires_auth(err):
        return (
            "MCP server requires authentication. Select OAuth authorization if the "
            "server supports MCP OAuth, or use bearer auth if you already have "
            "an access token."
        )

    message = _first_exception_message(err)
    if message:
        return f"MCP discovery failed: {message}"
    return "MCP discovery failed."


def _mcp_error_requires_auth(err: BaseException) -> bool:
    for nested in _walk_exception_tree(err):
        response = getattr(nested, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in (401, 403):
            return True
        message = str(nested)
        if "401 Unauthorized" in message or "403 Forbidden" in message:
            return True
    return False


def _first_exception_message(err: BaseException) -> str:
    for nested in _walk_exception_tree(err):
        message = str(nested).strip()
        if message:
            return message
    return ""


def _walk_exception_tree(err: BaseException) -> list[BaseException]:
    if isinstance(err, BaseExceptionGroup):
        nested_errors: list[BaseException] = []
        for nested in err.exceptions:
            nested_errors.extend(_walk_exception_tree(nested))
        return nested_errors
    return [err]


def _stream_http_enabled() -> bool:
    # When a shared stream token is configured, streaming arrives over the
    # web-owned HTTP API and is served from the in-memory buffer. Otherwise
    # (local dev) it is tailed from per-stream files on disk.
    return bool(os.environ.get("SIMPLE_CHAT_STREAM_TOKEN", "").strip())


STREAM_BUFFER_TTL_SECONDS = 1800.0


def _stream_buffers() -> dict[str, dict[str, Any]]:
    buffers = getattr(app.state, "stream_buffers", None)
    if buffers is None:
        buffers = {}
        app.state.stream_buffers = buffers
    return buffers


def _stream_buffer_events(stream_id: str) -> list[dict[str, Any]]:
    entry = _stream_buffers().get(stream_id)
    return entry["events"] if entry else []


def _append_stream_event(stream_id: str, event: dict[str, Any]) -> None:
    buffers = _stream_buffers()
    entry = buffers.get(stream_id)
    if entry is None:
        entry = {"events": [], "updated": 0.0}
        buffers[stream_id] = entry
    entry["events"].append(event)
    now = time.monotonic()
    entry["updated"] = now
    # Lazily evict whole streams that have gone idle, to bound memory.
    for stale in [
        sid
        for sid, value in buffers.items()
        if now - value["updated"] > STREAM_BUFFER_TTL_SECONDS
    ]:
        buffers.pop(stale, None)


def _clear_stream(stream_id: str) -> None:
    if _stream_http_enabled():
        _stream_buffers().pop(stream_id, None)
    else:
        stream_path(stream_id).unlink(missing_ok=True)


async def _poll_state(
    workflow_id: str,
    request: Request,
    user: AuthenticatedUser,
    last_state_json: str | None,
) -> tuple[list[str], str | None, bool]:
    """Query workflow state; return (sse chunks to emit, new last_state_json, stop)."""
    try:
        state = _state_to_dict(
            await _query_state(workflow_id),
            artifacts=_store().list_artifacts(
                user_id=user.user_id,
                workflow_id=workflow_id,
            ),
        )
    except Exception as err:
        if _is_temporal_not_found(err):
            await _forget_conversation(user.user_id, workflow_id, user.username)
            return (
                [
                    _sse(
                        "missing",
                        {
                            "workflow_id": workflow_id,
                            "message": "Workflow execution was not found.",
                        },
                    )
                ],
                last_state_json,
                True,
            )
        return (
            [_sse("error", {"message": f"{type(err).__name__}: {err}"})],
            last_state_json,
            False,
        )

    state_json = json.dumps(state, separators=(",", ":"))
    if state_json != last_state_json:
        return ([_sse("state", state)], state_json, False)
    return ([], last_state_json, False)


async def _event_stream(workflow_id: str, request: Request) -> AsyncIterator[str]:
    source = (
        _buffer_event_stream(workflow_id, request)
        if _stream_http_enabled()
        else _file_event_stream(workflow_id, request)
    )
    async for chunk in source:
        yield chunk


async def _file_event_stream(
    workflow_id: str, request: Request
) -> AsyncIterator[str]:
    path = stream_path(workflow_id)
    # Resume from where this EventSource left off (the browser replays its last
    # received id on auto-reconnect, e.g. after a backgrounded tab). Without
    # this the whole stream file is re-sent on every reconnect, which duplicates
    # already-finalized turns in the UI.
    offset = 0
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        with suppress(ValueError):
            offset = max(0, int(last_event_id))
    # Guard against a stale/oversized cursor (e.g. the stream file was reset).
    if path.exists() and offset > path.stat().st_size:
        offset = 0
    last_state_json: str | None = None
    state_elapsed = STATE_POLL_INTERVAL_SECONDS
    user = _current_user(request)

    while not await request.is_disconnected():
        if path.exists():
            new_lines: list[tuple[str, int]] = []
            with path.open("r", encoding="utf-8") as stream:
                stream.seek(offset)
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    new_lines.append((line, stream.tell()))
                offset = stream.tell()

            for line, position in new_lines:
                with suppress(json.JSONDecodeError):
                    yield _sse("stream", json.loads(line), event_id=str(position))

        state_elapsed += STREAM_POLL_INTERVAL_SECONDS
        if state_elapsed >= STATE_POLL_INTERVAL_SECONDS:
            state_elapsed = 0
            chunks, last_state_json, stop = await _poll_state(
                workflow_id, request, user, last_state_json
            )
            for chunk in chunks:
                yield chunk
            if stop:
                break

        await asyncio.sleep(STREAM_POLL_INTERVAL_SECONDS)


async def _buffer_event_stream(
    workflow_id: str, request: Request
) -> AsyncIterator[str]:
    # Resume by buffer index (the browser replays its last received id).
    resume = 0
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        with suppress(ValueError):
            resume = max(0, int(last_event_id))
    last_state_json: str | None = None
    state_elapsed = STATE_POLL_INTERVAL_SECONDS
    user = _current_user(request)

    while not await request.is_disconnected():
        events = _stream_buffer_events(workflow_id)
        if resume > len(events):
            resume = 0
        for index in range(resume, len(events)):
            yield _sse("stream", events[index], event_id=str(index + 1))
        resume = len(events)

        state_elapsed += STREAM_POLL_INTERVAL_SECONDS
        if state_elapsed >= STATE_POLL_INTERVAL_SECONDS:
            state_elapsed = 0
            chunks, last_state_json, stop = await _poll_state(
                workflow_id, request, user, last_state_json
            )
            for chunk in chunks:
                yield chunk
            if stop:
                break

        await asyncio.sleep(STREAM_POLL_INTERVAL_SECONDS)


async def _query_state(workflow_id: str) -> SimpleChatState:
    return await _handle(workflow_id).query(SimpleChatWorkflow.state)


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
    _clear_stream(workflow_id)
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
    mcp_servers = await (await _ensure_user_chats_workflow(user.user_id, user.username)).query(
        UserChatsWorkflow.list_mcp_servers
    )
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
    mcp_servers = await (await _ensure_user_chats_workflow(user.user_id, user.username)).query(
        UserChatsWorkflow.list_mcp_servers
    )
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


def _state_to_dict(
    state: Any,
    *,
    artifacts: list[ArtifactRecord] | None = None,
) -> dict[str, Any]:
    if is_dataclass(state):
        state_dict = asdict(state)
    elif isinstance(state, dict):
        state_dict = dict(state)
    else:
        raise TypeError(f"Unsupported state type: {type(state).__name__}")

    if artifacts is not None:
        state_dict["artifacts"] = _artifact_dicts(artifacts)
    return state_dict


def _artifact_dicts(artifacts: list[ArtifactRecord]) -> list[dict[str, Any]]:
    return [_artifact_dict(artifact) for artifact in artifacts]


def _artifact_dict(artifact: ArtifactRecord) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "conversation_id": artifact.conversation_id,
        "workflow_id": artifact.workflow_id,
        "name": artifact.name,
        "mime_type": artifact.mime_type,
        "size_bytes": artifact.size_bytes,
        "created_at": artifact.created_at,
        "metadata": artifact.metadata,
        "view_url": f"/api/artifacts/{artifact.artifact_id}",
        "download_url": f"/api/artifacts/{artifact.artifact_id}/download",
    }


def _artifact_response(
    artifact: ArtifactRecord,
    *,
    disposition: Literal["inline", "attachment"],
) -> Response:
    try:
        content = _store().read_artifact_bytes(artifact)
    except Exception as err:
        raise HTTPException(
            status_code=404, detail="Artifact file not found"
        ) from err

    return Response(
        content,
        media_type=(
            artifact.mime_type
            if disposition == "attachment"
            else _safe_inline_media_type(artifact.mime_type)
        ),
        headers={
            "Content-Disposition": _content_disposition(disposition, artifact.name),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _content_disposition(disposition: str, filename: str) -> str:
    ascii_filename = re.sub(r'["\\\r\n]+', "_", filename) or "artifact"
    encoded_filename = quote(filename, safe="")
    return (
        f'{disposition}; filename="{ascii_filename}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )


def _safe_inline_media_type(mime_type: str) -> str:
    if mime_type == "application/pdf":
        return mime_type
    if mime_type.startswith("image/") and mime_type != "image/svg+xml":
        return mime_type
    return "text/plain; charset=utf-8"


def _sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _temporal_ui_base_url() -> str:
    # Explicit override always wins.
    explicit = os.environ.get("TEMPORAL_UI_URL")
    if explicit:
        return explicit
    # Otherwise derive from the environment: a Temporal Cloud endpoint maps to
    # the Cloud Web UI; anything else falls back to the local dev server.
    endpoint = os.environ.get("TEMPORAL_ENDPOINT", "")
    if "tmprl.cloud" in endpoint:
        return "https://cloud.temporal.io"
    return "http://localhost:8233"


def _temporal_ui_user_workflows_url(email: str) -> str | None:
    """Temporal UI workflow-list URL filtered by the UserEmail search attribute.

    Returns None when the search attribute is not configured.
    """
    search_attr_name = _user_email_sa_name()
    if not search_attr_name or not email:
        return None
    try:
        namespace = _client().namespace
    except HTTPException:
        return None
    base_url = _temporal_ui_base_url().rstrip("/")
    namespace_path = quote(namespace, safe="")
    # UserEmail is a Text (tokenized) search attribute, so including the email
    # domain would match the "temporal"/"io" tokens shared by every user and
    # return everyone. Filter on the local part only for a per-user result.
    local_part = email.split("@", 1)[0]
    query = quote(f'{search_attr_name} = "{local_part}"', safe="")
    return f"{base_url}/namespaces/{namespace_path}/workflows?query={query}"


def _temporal_ui_url(*, namespace: str, workflow_id: str, run_id: str) -> str:
    base_url = _temporal_ui_base_url().rstrip("/")
    namespace_path = quote(namespace, safe="")
    workflow_path = quote(workflow_id, safe="")
    run_path = quote(run_id, safe="")
    if run_path:
        return (
            f"{base_url}/namespaces/{namespace_path}/workflows/"
            f"{workflow_path}/{run_path}/history"
        )
    return f"{base_url}/namespaces/{namespace_path}/workflows/{workflow_path}"


def _conversation_title(message: str) -> str:
    normalized = " ".join(message.split())
    if not normalized:
        return "New chat"
    if len(normalized) <= 64:
        return normalized
    return f"{normalized[:61]}..."


def _default_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL_OPTIONS[0])


def _model_options() -> list[str]:
    configured = [
        model.strip()
        for model in os.environ.get("ANTHROPIC_MODEL_OPTIONS", "").split(",")
        if model.strip()
    ]
    options = configured or DEFAULT_MODEL_OPTIONS
    return _dedupe([_default_model(), *options])


def _thinking_config_from_request(
    request: ThinkingSessionRequest,
    *,
    model: str,
    max_tokens: int,
) -> ClaudeThinkingConfig | None:
    if not request.enabled:
        return None
    if _uses_adaptive_thinking(model):
        return ClaudeThinkingConfig(
            enabled=True,
            mode="adaptive",
            effort=request.effort,
        )
    if max_tokens <= MIN_THINKING_BUDGET_TOKENS:
        raise HTTPException(
            status_code=400,
            detail="max_tokens must be greater than 1024 for extended thinking.",
        )
    budget_tokens = min(
        max(request.budget_tokens, MIN_THINKING_BUDGET_TOKENS),
        max_tokens - 1,
    )
    return ClaudeThinkingConfig(enabled=True, budget_tokens=budget_tokens)


def _uses_adaptive_thinking(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in ADAPTIVE_THINKING_MODEL_PREFIXES)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Simple Chat Agent</title>
  <style>
    :root {
      color-scheme: dark;
      --color-bg-primary: #0d1117;
      --color-bg-secondary: #161b22;
      --color-bg-tertiary: #21262d;
      --color-bg-elevated: #1c2128;
      --color-bg-hover: #30363d;
      --color-bg-active: #388bfd1a;
      --color-border: #30363d;
      --color-border-light: #21262d;
      --color-border-focus: #388bfd;
      --color-text-primary: #e6edf3;
      --color-text-secondary: #8b949e;
      --color-text-tertiary: #6e7681;
      --color-text-link: #58a6ff;
      --color-text-inverse: #0d1117;
      --color-primary: #238636;
      --color-primary-hover: #2ea043;
      --color-danger: #da3633;
      --color-danger-hover: #f85149;
      --color-warning: #d29922;
      --color-info: #58a6ff;
      --color-success: #3fb950;
      --color-queued: #a371f7;
      --shadow-lg: 0 10px 20px rgba(0, 0, 0, 0.4);
      --space-xs: 4px;
      --space-sm: 8px;
      --space-md: 16px;
      --space-lg: 24px;
      --radius-sm: 4px;
      --radius-md: 6px;
      --radius-lg: 8px;
      --sidebar-width: 260px;
      --details-width: 360px;
      --top-bar-height: 56px;
      --font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Courier New", monospace;
      --font-size-xs: 11px;
      --font-size-sm: 12px;
      --font-size-md: 14px;
      --font-size-lg: 16px;
      --transition-fast: 150ms ease;
    }
    *, *::before, *::after { box-sizing: border-box; }
    * { margin: 0; padding: 0; }
    html, body { height: 100%; }
    body {
      background: var(--color-bg-primary);
      color: var(--color-text-primary);
      font: var(--font-size-md)/1.5 var(--font-family);
      overflow: hidden;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }
    ::selection {
      background: var(--color-bg-active);
      color: var(--color-text-primary);
    }
    ::-webkit-scrollbar {
      width: 8px;
      height: 8px;
    }
    ::-webkit-scrollbar-track { background: var(--color-bg-secondary); }
    ::-webkit-scrollbar-thumb {
      background: var(--color-bg-hover);
      border-radius: 999px;
    }
    ::-webkit-scrollbar-thumb:hover { background: var(--color-text-tertiary); }
    * {
      scrollbar-width: thin;
      scrollbar-color: var(--color-bg-hover) var(--color-bg-secondary);
    }
    [hidden] { display: none !important; }
    .login-screen {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: var(--space-md);
      background: var(--color-bg-primary);
    }
    .login-card {
      width: min(380px, 100%);
      padding: var(--space-lg);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-lg);
      background: var(--color-bg-secondary);
      box-shadow: var(--shadow-lg);
    }
    .login-card h1 {
      margin-bottom: var(--space-md);
    }
    .login-form {
      display: grid;
      gap: var(--space-md);
    }
    .login-field {
      display: grid;
      gap: var(--space-xs);
    }
    .login-field label {
      color: var(--color-text-secondary);
      font-size: var(--font-size-sm);
    }
    .login-field input {
      width: 100%;
      height: 40px;
      padding: 0 var(--space-md);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      background: var(--color-bg-tertiary);
      color: var(--color-text-primary);
      font: inherit;
    }
    .login-error {
      min-height: 18px;
      color: var(--color-danger-hover);
      font-size: var(--font-size-sm);
    }
    .login-subtitle {
      margin-bottom: var(--space-md);
      color: var(--color-text-secondary);
      font-size: var(--font-size-sm);
    }
    .login-subtitle:empty {
      display: none;
    }
    .login-google {
      display: flex;
      align-items: center;
      justify-content: center;
      height: 40px;
      border-radius: var(--radius-md);
      text-decoration: none;
      font-weight: 600;
    }
    .login-google[aria-disabled="true"] {
      opacity: 0.6;
      pointer-events: none;
    }
    .app {
      display: grid;
      grid-template-columns: var(--sidebar-width) minmax(0, 1fr) var(--details-width);
      grid-template-rows: minmax(0, 1fr) auto;
      height: 100vh;
      min-height: 0;
    }
    header {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      grid-column: 1;
      grid-row: 1 / -1;
      min-height: 0;
      background: var(--color-bg-secondary);
      border-right: 1px solid var(--color-border);
    }
    .header-left {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-sm);
      padding: var(--space-md);
      border-bottom: 1px solid var(--color-border);
    }
    h1 {
      display: flex;
      align-items: center;
      gap: var(--space-sm);
      color: var(--color-text-primary);
      font-size: var(--font-size-lg);
      font-weight: 600;
      line-height: 1.25;
    }
    h1::before {
      content: "";
      width: 32px;
      height: 32px;
      /* Official Temporal symbol mark (temporal.io brand assets). */
      background: url("data:image/svg+xml;base64,PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0idXRmLTgiPz4KPCEtLSBHZW5lcmF0b3I6IEFkb2JlIElsbHVzdHJhdG9yIDI0LjAuMCwgU1ZHIEV4cG9ydCBQbHVnLUluIC4gU1ZHIFZlcnNpb246IDYuMDAgQnVpbGQgMCkgIC0tPgo8c3ZnIHZlcnNpb249IjEuMSIgaWQ9IkxheWVyXzMiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyIgeG1sbnM6eGxpbms9Imh0dHA6Ly93d3cudzMub3JnLzE5OTkveGxpbmsiIHg9IjBweCIgeT0iMHB4IgoJIHZpZXdCb3g9IjAgMCAxMjAwIDEyMDAiIHN0eWxlPSJlbmFibGUtYmFja2dyb3VuZDpuZXcgMCAwIDEyMDAgMTIwMDsiIHhtbDpzcGFjZT0icHJlc2VydmUiPgo8c3R5bGUgdHlwZT0idGV4dC9jc3MiPgoJLnN0MHtmaWxsOiNGMkYyRjI7fQo8L3N0eWxlPgo8cGF0aCBjbGFzcz0ic3QwIiBkPSJNNjUxLjE0LDUxNy4zNUM2NDIuMDIsNDQ5LjAzLDYxOC45NCwzOTIsNTgzLjQ5LDM5MnMtNTguNTMsNTcuMDMtNjcuNjUsMTI1LjM1CgljLTY4LjMyLDkuMTItMTI1LjM1LDMyLjItMTI1LjM1LDY3LjY1czU3LjA0LDU4LjUzLDEyNS4zNSw2Ny42NWM5LjEyLDY4LjMxLDMyLjIsMTI1LjM1LDY3LjY1LDEyNS4zNXM1OC41My01Ny4wNCw2Ny42NS0xMjUuMzUKCWM2OC4zMi05LjEyLDEyNS4zNS0zMi4yLDEyNS4zNS02Ny42NVM3MTkuNDUsNTI2LjQ3LDY1MS4xNCw1MTcuMzV6IE01MTMuNjEsNjMyLjc1Yy02NS40My05LjQ1LTEwMy41OS0zMS4wOC0xMDMuNTktNDcuNzUKCXMzOC4xNi0zOC4zLDEwMy41OS00Ny43NWMtMS40NCwxNS43NS0yLjE5LDMxLjgzLTIuMTksNDcuNzVDNTExLjQyLDYwMC45Miw1MTIuMTcsNjE3LjAxLDUxMy42MSw2MzIuNzV6IE01ODMuNDksNDExLjUzCgljMTYuNjcsMCwzOC4zLDM4LjE2LDQ3Ljc1LDEwMy41OWMtMTUuNzQtMS40NC0zMS44My0yLjE5LTQ3Ljc1LTIuMTlzLTMyLjAxLDAuNzUtNDcuNzUsMi4xOQoJQzU0NS4xOSw0NDkuNjksNTY2LjgyLDQxMS41Myw1ODMuNDksNDExLjUzeiBNNjUzLjM3LDYzMi43NWMtMy4yMiwwLjQ3LTE2LjQzLDIuMDItMTkuNzcsMi4zNWMtMC4zMywzLjM1LTEuODksMTYuNTUtMi4zNSwxOS43NwoJYy05LjQ1LDY1LjQzLTMxLjA4LDEwMy41OS00Ny43NSwxMDMuNTlzLTM4LjMtMzguMTYtNDcuNzUtMTAzLjU5Yy0wLjQ2LTMuMjItMi4wMi0xNi40My0yLjM1LTE5Ljc3CgljLTEuNTItMTUuNTEtMi40NC0zMi4xNy0yLjQ0LTUwLjFzMC45Mi0zNC41OSwyLjQ0LTUwLjExYzE1LjUxLTEuNTIsMzIuMTctMi40NCw1MC4xLTIuNDRzMzQuNTksMC45Miw1MC4xLDIuNDQKCWMzLjM1LDAuMzMsMTYuNTUsMS44OSwxOS43NywyLjM1YzY1LjQzLDkuNDUsMTAzLjYsMzEuMDksMTAzLjYsNDcuNzVTNzE4LjgsNjIzLjMsNjUzLjM3LDYzMi43NXoiLz4KPC9zdmc+Cg==") center / contain no-repeat;
      flex: 0 0 auto;
    }
    .temporal-link {
      display: none;
      min-height: 36px;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      padding: var(--space-sm) var(--space-md);
      color: var(--color-text-primary);
      text-decoration: none;
      font-size: var(--font-size-sm);
      font-weight: 500;
      white-space: nowrap;
      background: var(--color-bg-tertiary);
      transition: background-color var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
    }
    .chat-footer {
      display: flex;
      justify-content: flex-end;
      padding: var(--space-xs) var(--space-md) 0;
    }
    .chat-footer .temporal-link {
      display: inline-flex;
      min-height: 28px;
      padding: var(--space-xs) var(--space-sm);
      font-size: var(--font-size-xs);
      color: var(--color-text-secondary);
    }
    .temporal-link:hover {
      border-color: var(--color-text-tertiary);
      background: var(--color-bg-hover);
      color: var(--color-text-primary);
      text-decoration: none;
    }
    .status {
      margin-top: auto;
      padding: var(--space-md);
      border-top: 1px solid var(--color-border);
      color: var(--color-text-secondary);
      font-size: var(--font-size-sm);
      white-space: normal;
    }
    .status::before {
      content: "";
      display: inline-block;
      width: 8px;
      height: 8px;
      margin-right: var(--space-sm);
      border-radius: 50%;
      background: var(--color-success);
    }
    .side-panel {
      min-height: 0;
      overflow-y: auto;
      padding: var(--space-sm) var(--space-md);
    }
    .side-section {
      margin-bottom: var(--space-lg);
    }
    .side-section-title {
      margin: var(--space-md) 0 var(--space-xs);
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .side-actions {
      display: grid;
      grid-template-columns: 1fr;
      gap: var(--space-sm);
      margin-top: var(--space-sm);
    }
    .agent-settings {
      display: grid;
      gap: var(--space-sm);
      padding: var(--space-sm);
      border: 1px solid var(--color-border-light);
      border-radius: var(--radius-md);
      background: rgba(110, 118, 129, 0.08);
    }
    .agent-field {
      display: grid;
      gap: 3px;
    }
    .agent-field label,
    .agent-toggle {
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
      font-weight: 600;
      text-transform: uppercase;
    }
    .agent-field select,
    .agent-field input {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      padding: 0 var(--space-sm);
      background: var(--color-bg-tertiary);
      color: var(--color-text-primary);
      font: inherit;
      font-size: var(--font-size-sm);
    }
    .agent-field select:focus,
    .agent-field input:focus {
      outline: none;
      border-color: var(--color-border-focus);
      box-shadow: 0 0 0 3px rgba(56, 139, 253, 0.14);
    }
    .agent-toggle {
      display: flex;
      align-items: center;
      gap: var(--space-sm);
      min-height: 28px;
    }
    .agent-toggle input {
      width: 16px;
      height: 16px;
      accent-color: var(--color-primary);
    }
    .conversation-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: var(--space-xs);
      align-items: center;
      margin-bottom: 2px;
    }
    .conversation-item,
    .tool-card {
      width: 100%;
      margin-bottom: 2px;
      padding: var(--space-sm);
      border: 0;
      border-radius: var(--radius-sm);
      background: transparent;
      color: var(--color-text-secondary);
      text-align: left;
      font-size: var(--font-size-sm);
    }
    .conversation-row .conversation-item {
      margin-bottom: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .conversation-delete {
      width: 44px;
      min-height: 32px;
      padding: 0;
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
    }
    .conversation-delete:hover {
      border-color: rgba(218, 54, 51, 0.68);
      color: #ffb4af;
    }
    .conversation-item:hover,
    .tool-card:hover {
      background: var(--color-bg-hover);
      color: var(--color-text-primary);
    }
    .conversation-item.active {
      background: var(--color-bg-active);
      color: var(--color-text-link);
    }
    .tool-card {
      display: grid;
      gap: var(--space-xs);
      border: 1px solid var(--color-border-light);
      background: var(--color-bg-primary);
    }
    .tool-card.connected {
      border-color: rgba(63, 185, 80, 0.34);
    }
    .tool-card.disabled {
      border-color: rgba(110, 118, 129, 0.3);
      opacity: 0.72;
    }
    .tool-actions {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: var(--space-xs);
      margin-top: var(--space-xs);
    }
    .tool-actions button {
      height: 34px;
      min-height: 34px;
      padding: 0 var(--space-sm);
      font-size: var(--font-size-xs);
    }
    .tool-actions .danger {
      border-color: rgba(218, 54, 51, 0.68);
      color: #ffb4af;
    }
    .tool-actions .danger:hover {
      background: rgba(218, 54, 51, 0.16);
      border-color: var(--color-danger-hover);
      color: #ffd4d0;
    }
    .tool-chip-list {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-xs);
      margin-top: var(--space-xs);
      min-width: 0;
    }
    .tool-chip {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      min-width: 0;
      min-height: 22px;
      padding: 0 var(--space-xs);
      border: 1px solid var(--color-border-light);
      border-radius: var(--radius-sm);
      background: rgba(110, 118, 129, 0.1);
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      overflow-wrap: anywhere;
    }
    .mcp-form {
      display: grid;
      gap: var(--space-sm);
      margin: var(--space-sm) 0;
      padding: var(--space-sm);
      border: 1px solid rgba(88, 166, 255, 0.28);
      border-radius: var(--radius-md);
      background: rgba(88, 166, 255, 0.06);
    }
    .mcp-field {
      display: grid;
      gap: 3px;
    }
    .mcp-field label {
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
      font-weight: 600;
      text-transform: uppercase;
    }
    .mcp-field input,
    .mcp-field select {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      padding: 0 var(--space-sm);
      background: var(--color-bg-tertiary);
      color: var(--color-text-primary);
      font: inherit;
      font-size: var(--font-size-sm);
    }
    .mcp-field input:focus,
    .mcp-field select:focus {
      outline: none;
      border-color: var(--color-border-focus);
      box-shadow: 0 0 0 3px rgba(56, 139, 253, 0.14);
    }
    .mcp-form-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: var(--space-xs);
    }
    .mcp-form-actions button {
      height: 34px;
      padding: 0 var(--space-sm);
      font-size: var(--font-size-xs);
    }
    .mcp-error {
      color: #ffb4af;
      font-size: var(--font-size-xs);
      overflow-wrap: anywhere;
    }
    .tool-title {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: var(--space-sm);
      color: var(--color-text-primary);
      font-weight: 600;
      min-width: 0;
    }
    .tool-label {
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .tool-meta {
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      overflow-wrap: anywhere;
    }
    .tool-status {
      display: inline-flex;
      align-items: center;
      gap: var(--space-xs);
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      max-width: 100%;
      white-space: nowrap;
    }
    .tool-status::before {
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--color-text-tertiary);
    }
    .tool-card.connected .tool-status::before {
      background: var(--color-success);
    }
    .tool-card.disabled .tool-status::before {
      background: var(--color-warning);
    }
    .tools-overlay {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: flex;
      align-items: stretch;
      justify-content: flex-end;
      background: rgba(1, 4, 9, 0.62);
    }
    .tools-window {
      width: min(760px, calc(100vw - 24px));
      height: 100%;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      border-left: 1px solid var(--color-border);
      background: var(--color-bg-secondary);
      box-shadow: var(--shadow-lg);
    }
    .tools-window-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-md);
      padding: var(--space-md);
      border-bottom: 1px solid var(--color-border);
      background: var(--color-bg-elevated);
    }
    .tools-window-title {
      color: var(--color-text-primary);
      font-size: var(--font-size-lg);
      font-weight: 600;
    }
    .tools-window-body {
      min-height: 0;
      overflow-y: auto;
      padding: var(--space-md);
    }
    .tools-section {
      display: grid;
      gap: var(--space-sm);
      margin-bottom: var(--space-lg);
    }
    .tools-section-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-sm);
    }
    .tools-section-title {
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
      font-weight: 600;
      text-transform: uppercase;
    }
    .tools-section-actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-xs);
    }
    .tools-section-actions button {
      height: 34px;
      min-height: 34px;
      padding: 0 var(--space-sm);
      font-size: var(--font-size-xs);
    }
    .tools-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
      gap: var(--space-sm);
    }
    .approval-panel {
      position: sticky;
      bottom: var(--space-sm);
      z-index: 3;
      width: min(980px, 92%);
      margin: 0 auto var(--space-md);
      display: grid;
      gap: var(--space-sm);
    }
    .approval-panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-sm);
      color: #ffb85c;
      font-size: var(--font-size-xs);
      font-weight: 700;
      text-transform: uppercase;
    }
    .approval-panel-count {
      color: var(--color-text-tertiary);
      font-weight: 500;
      text-transform: none;
    }
    .approval-card {
      position: relative;
      display: grid;
      gap: var(--space-sm);
      padding: var(--space-md);
      border: 1px solid rgba(248, 81, 73, 0.34);
      border-radius: var(--radius-md);
      background:
        linear-gradient(90deg, rgba(248, 81, 73, 0.16), rgba(210, 153, 34, 0.08));
      box-shadow: inset 3px 0 0 rgba(248, 81, 73, 0.88), 0 1px 2px rgba(0, 0, 0, 0.3);
    }
    .approval-title {
      color: var(--color-text-primary);
      font-size: var(--font-size-md);
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .approval-meta {
      display: grid;
      gap: 2px;
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      overflow-wrap: anywhere;
    }
    .approval-meta strong {
      color: var(--color-text-primary);
      font-weight: 600;
    }
    .approval-details {
      max-height: 130px;
      overflow: auto;
      padding: var(--space-sm);
      border: 1px solid rgba(248, 81, 73, 0.2);
      border-radius: var(--radius-sm);
      background: rgba(13, 17, 23, 0.42);
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      overflow-wrap: anywhere;
    }
    .approval-actions {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: var(--space-xs);
    }
    .approval-actions button {
      height: 36px;
      min-height: 36px;
      padding: 0 var(--space-sm);
      font-size: var(--font-size-xs);
      font-weight: 600;
    }
    .approval-actions .allow {
      border-color: rgba(63, 185, 80, 0.52);
      color: #aff5b4;
    }
    .approval-actions .allow:hover {
      background: rgba(63, 185, 80, 0.14);
      border-color: var(--color-success);
    }
    .approval-actions .always {
      border-color: rgba(210, 153, 34, 0.54);
      color: #ffd58a;
    }
    .approval-actions .always:hover {
      background: rgba(210, 153, 34, 0.14);
      border-color: var(--color-warning);
    }
    .approval-actions .deny {
      border-color: rgba(248, 81, 73, 0.72);
      color: #ffb4af;
    }
    .approval-actions .deny:hover {
      background: rgba(248, 81, 73, 0.16);
      border-color: var(--color-danger-hover);
      color: #ffd4d0;
    }
    .artifact-panel {
      display: grid;
      align-content: start;
      gap: var(--space-sm);
      padding: var(--space-md);
      border: 1px solid rgba(88, 166, 255, 0.22);
      border-radius: var(--radius-md);
      background: rgba(88, 166, 255, 0.045);
      box-shadow: inset 3px 0 0 rgba(126, 231, 135, 0.72), 0 1px 2px rgba(0, 0, 0, 0.25);
    }
    .artifacts-sidebar {
      grid-column: 3;
      grid-row: 1 / -1;
      min-height: 0;
      overflow-y: auto;
      padding: var(--space-md);
      border-left: 1px solid var(--color-border);
      background: var(--color-bg-secondary);
    }
    .artifacts-sidebar .artifact-panel {
      min-height: 100%;
      border-color: var(--color-border-light);
      background: transparent;
      box-shadow: none;
      padding: 0;
    }
    .artifact-empty {
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
      line-height: 1.45;
    }
    .artifact-panel-header {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: var(--space-sm);
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      font-weight: 700;
      text-transform: uppercase;
    }
    .artifact-panel-count {
      color: var(--color-text-tertiary);
      font-weight: 500;
      text-transform: none;
    }
    .artifact-list {
      display: grid;
      grid-template-columns: 1fr;
      gap: var(--space-sm);
    }
    .artifact-card {
      display: grid;
      gap: var(--space-xs);
      min-width: 0;
      padding: var(--space-sm);
      border: 1px solid rgba(63, 185, 80, 0.26);
      border-radius: var(--radius-sm);
      background: rgba(13, 17, 23, 0.44);
    }
    .artifact-name {
      color: var(--color-text-primary);
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .artifact-meta {
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      overflow-wrap: anywhere;
    }
    .artifact-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: var(--space-xs);
      margin-top: var(--space-xs);
    }
    .artifact-actions a,
    .artifact-actions button {
      min-height: 32px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      background: var(--color-bg-tertiary);
      color: var(--color-text-primary);
      text-decoration: none;
      font-size: var(--font-size-xs);
      font-weight: 600;
      transition: background-color var(--transition-fast), border-color var(--transition-fast);
    }
    .artifact-actions a:hover,
    .artifact-actions button:hover {
      border-color: var(--color-border-focus);
      background: var(--color-bg-hover);
      text-decoration: none;
    }
    .artifact-viewer-overlay {
      position: fixed;
      inset: 0;
      z-index: 30;
      display: grid;
      place-items: center;
      padding: var(--space-lg);
      background: rgba(1, 4, 9, 0.72);
    }
    .artifact-viewer {
      width: min(1100px, calc(100vw - var(--details-width) - var(--sidebar-width) - 64px));
      height: min(780px, 90vh);
      min-width: min(720px, 96vw);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-lg);
      background: var(--color-bg-secondary);
      box-shadow: var(--shadow-lg);
      overflow: hidden;
    }
    .artifact-viewer-header {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: var(--space-sm);
      align-items: center;
      padding: var(--space-md);
      border-bottom: 1px solid var(--color-border);
      background: var(--color-bg-elevated);
    }
    .artifact-viewer-title {
      min-width: 0;
      display: grid;
      gap: 2px;
    }
    .artifact-viewer-name {
      color: var(--color-text-primary);
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .artifact-viewer-meta {
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      overflow-wrap: anywhere;
    }
    .artifact-viewer-actions {
      display: flex;
      gap: var(--space-xs);
      align-items: center;
      justify-content: flex-end;
      min-width: max-content;
    }
    .artifact-viewer-actions a,
    .artifact-viewer-actions button {
      min-height: 34px;
      height: 34px;
      min-width: 78px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 var(--space-sm);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      background: var(--color-bg-tertiary);
      color: var(--color-text-primary);
      text-decoration: none;
      font-size: var(--font-size-xs);
      font-weight: 600;
    }
    .artifact-viewer-body {
      min-height: 0;
      overflow: auto;
      padding: var(--space-md);
      background: var(--color-bg-primary);
    }
    .artifact-viewer-body .bubble-content pre {
      min-height: 100%;
    }
    .artifact-viewer-body .artifact-markdown pre {
      min-height: 0;
    }
    .artifact-viewer-image {
      display: block;
      max-width: 100%;
      max-height: 100%;
      margin: 0 auto;
      object-fit: contain;
      border-radius: var(--radius-sm);
    }
    .artifact-viewer-frame {
      width: 100%;
      height: 100%;
      min-height: 520px;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      background: white;
    }
    .artifact-viewer-error {
      color: #ffb4af;
      font-size: var(--font-size-sm);
    }
    main {
      display: grid;
      grid-column: 2;
      grid-row: 1;
      grid-template-columns: minmax(0, 1fr);
      min-height: 0;
      background: var(--color-bg-primary);
    }
    .messages {
      min-height: 0;
      overflow-y: auto;
      padding: var(--space-md);
      scroll-behavior: smooth;
    }
    .sidebar { display: none; }
    .bubble {
      position: relative;
      max-width: min(980px, 88%);
      margin: 0 0 var(--space-md);
      padding: var(--space-md);
      background: var(--color-bg-secondary);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    }
    .bubble:hover {
      background: var(--color-bg-tertiary);
      border-color: var(--color-text-tertiary);
    }
    .bubble::after {
      content: "";
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      width: 3px;
      border-radius: var(--radius-md) 0 0 var(--radius-md);
      background: var(--color-text-tertiary);
    }
    .bubble.user, .bubble.pending { margin-left: auto; }
    .bubble.user {
      background: rgba(63, 185, 80, 0.08);
      border-color: rgba(63, 185, 80, 0.28);
    }
    .bubble.user::after { background: var(--color-success); }
    .bubble.assistant {
      background: rgba(88, 166, 255, 0.08);
      border-color: rgba(88, 166, 255, 0.24);
    }
    .bubble.assistant::after { background: var(--color-info); }
    .bubble.system {
      background: rgba(210, 153, 34, 0.08);
      border-color: rgba(210, 153, 34, 0.26);
      color: var(--color-warning);
    }
    .bubble.system::after { background: var(--color-warning); }
    .bubble.pending {
      background: rgba(163, 113, 247, 0.1);
      border-color: rgba(163, 113, 247, 0.28);
      color: #d2b6ff;
      font-style: italic;
    }
    .bubble.pending::after { background: var(--color-queued); }
    .stream-panel {
      width: min(980px, 92%);
      margin: 0 auto var(--space-md);
      border: 1px solid rgba(88, 166, 255, 0.28);
      border-radius: var(--radius-md);
      background: rgba(88, 166, 255, 0.055);
      box-shadow: inset 3px 0 0 rgba(88, 166, 255, 0.75), 0 1px 2px rgba(0, 0, 0, 0.25);
      overflow: hidden;
    }
    .stream-panel.complete {
      border-color: rgba(110, 118, 129, 0.32);
      background: rgba(110, 118, 129, 0.075);
      box-shadow: inset 3px 0 0 rgba(110, 118, 129, 0.78), 0 1px 2px rgba(0, 0, 0, 0.25);
    }
    .stream-panel.collapsed {
      width: min(780px, 86%);
    }
    .stream-panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-sm);
      padding: var(--space-sm) var(--space-md);
      border-bottom: 1px solid rgba(88, 166, 255, 0.18);
      background: rgba(13, 17, 23, 0.34);
    }
    .stream-panel-title {
      display: flex;
      align-items: baseline;
      gap: var(--space-sm);
      min-width: 0;
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      font-weight: 600;
      text-transform: uppercase;
    }
    .stream-panel-status {
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
      font-weight: 400;
      text-transform: none;
    }
    .stream-panel-toggle {
      min-height: 26px;
      padding: 0 var(--space-sm);
      font-size: var(--font-size-xs);
      color: var(--color-text-secondary);
    }
    .stream-panel-body {
      display: grid;
      gap: var(--space-sm);
      padding: var(--space-sm) var(--space-md) var(--space-md);
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      line-height: 1.45;
    }
    .stream-preview {
      overflow: hidden;
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      line-height: 1.45;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .stream-text {
      max-height: 220px;
      overflow-y: auto;
      padding: var(--space-sm);
      border: 1px solid rgba(88, 166, 255, 0.14);
      border-radius: var(--radius-sm);
      background: rgba(13, 17, 23, 0.38);
      color: #b9c7d8;
      font-size: var(--font-size-xs);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .stream-thinking {
      max-height: 160px;
      overflow-y: auto;
      padding: var(--space-sm);
      border: 1px solid rgba(163, 113, 247, 0.16);
      border-radius: var(--radius-sm);
      background: rgba(163, 113, 247, 0.055);
      color: #cdb8ff;
      font-size: var(--font-size-xs);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .stream-finished-list {
      display: grid;
      gap: var(--space-sm);
    }
    .stream-finished-turn {
      display: grid;
      gap: var(--space-xs);
      padding: var(--space-sm);
      border: 1px solid rgba(63, 185, 80, 0.18);
      border-radius: var(--radius-sm);
      background: rgba(63, 185, 80, 0.055);
      color: #b9c7d8;
      font-size: var(--font-size-xs);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .stream-finished-title {
      color: var(--color-text-tertiary);
      font-weight: 600;
      text-transform: uppercase;
    }
    .stream-current-turn {
      display: grid;
      gap: var(--space-sm);
      padding: var(--space-sm);
      border: 1px solid rgba(88, 166, 255, 0.18);
      border-radius: var(--radius-sm);
      background: rgba(88, 166, 255, 0.04);
    }
    .stream-tool-list {
      display: grid;
      gap: var(--space-xs);
    }
    .stream-tool-event {
      display: grid;
      gap: 2px;
      padding: var(--space-xs) var(--space-sm);
      border: 1px solid rgba(110, 118, 129, 0.24);
      border-radius: var(--radius-sm);
      background: rgba(13, 17, 23, 0.28);
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
    }
    .stream-tool-event.input-streaming {
      border-color: rgba(210, 153, 34, 0.26);
      background: rgba(210, 153, 34, 0.06);
    }
    .stream-tool-name {
      color: var(--color-text-primary);
      font-weight: 600;
    }
    .stream-tool-payload {
      color: var(--color-text-tertiary);
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .label {
      display: block;
      margin-bottom: var(--space-xs);
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .bubble-content {
      white-space: normal;
    }
    .bubble-content > * + * {
      margin-top: var(--space-sm);
    }
    .bubble-content p {
      margin: 0;
    }
    .bubble-content a {
      color: var(--color-text-link);
      text-decoration: none;
    }
    .bubble-content a:hover {
      text-decoration: underline;
    }
    .bubble-content ul,
    .bubble-content ol {
      margin: 0;
      padding-left: 22px;
    }
    .bubble-content li + li {
      margin-top: 2px;
    }
    .bubble-content strong {
      color: var(--color-text-primary);
      font-weight: 700;
    }
    .bubble-content em {
      color: var(--color-text-secondary);
      font-style: italic;
    }
    .bubble-content code {
      padding: 1px 4px;
      border: 1px solid var(--color-border-light);
      border-radius: var(--radius-sm);
      background: var(--color-bg-primary);
      color: var(--color-text-primary);
      font: inherit;
      font-size: var(--font-size-sm);
    }
    .bubble-content pre {
      position: relative;
      margin: 0;
      padding: var(--space-sm);
      border: 1px solid var(--color-border-light);
      border-radius: var(--radius-sm);
      background: var(--color-bg-primary);
      overflow-x: auto;
      white-space: pre;
    }
    .bubble-content pre[data-language] {
      padding-top: calc(var(--space-sm) + 18px);
    }
    .bubble-content pre[data-language]::before {
      content: attr(data-language);
      position: absolute;
      top: 5px;
      right: 8px;
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
      text-transform: uppercase;
    }
    .bubble-content pre code {
      display: block;
      padding: 0;
      border: 0;
      background: transparent;
      white-space: pre;
    }
    .bubble-content blockquote {
      margin: 0;
      padding: var(--space-xs) var(--space-sm);
      border-left: 3px solid var(--color-border-focus);
      background: rgba(88, 166, 255, 0.06);
      color: var(--color-text-secondary);
    }
    .bubble-content hr {
      height: 1px;
      border: 0;
      background: var(--color-border);
    }
    .markdown-table-wrap {
      max-width: 100%;
      overflow-x: auto;
      border: 1px solid var(--color-border-light);
      border-radius: var(--radius-sm);
    }
    .bubble-content table {
      width: 100%;
      min-width: min(620px, 100%);
      border-collapse: collapse;
      background: rgba(13, 17, 23, 0.28);
    }
    .bubble-content th,
    .bubble-content td {
      padding: var(--space-xs) var(--space-sm);
      border-bottom: 1px solid var(--color-border-light);
      border-right: 1px solid var(--color-border-light);
      text-align: left;
      vertical-align: top;
    }
    .bubble-content th:last-child,
    .bubble-content td:last-child {
      border-right: 0;
    }
    .bubble-content tr:last-child td {
      border-bottom: 0;
    }
    .bubble-content th {
      color: var(--color-text-primary);
      font-weight: 700;
      background: rgba(88, 166, 255, 0.08);
    }
    .hl-comment { color: #8b949e; font-style: italic; }
    .hl-keyword { color: #ff7b72; }
    .hl-string { color: #a5d6ff; }
    .hl-number { color: #79c0ff; }
    .hl-function { color: #d2a8ff; }
    .hl-operator { color: #ff7b72; }
    .hl-property { color: #7ee787; }
    .hl-type { color: #ffa657; }
    .hl-tag { color: #7ee787; }
    .hl-attr { color: #d2a8ff; }
    .md-heading {
      color: var(--color-text-primary);
      font-weight: 700;
    }
    .composer {
      display: grid;
      grid-template-columns: 1fr auto auto auto auto;
      grid-column: 2;
      grid-row: 2;
      gap: var(--space-sm);
      padding: var(--space-sm) var(--space-md);
      border-top: 1px solid var(--color-border);
      background: var(--color-bg-secondary);
    }
    textarea {
      width: 100%;
      min-height: 44px;
      max-height: 140px;
      resize: vertical;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      padding: 11px 12px;
      background: var(--color-bg-tertiary);
      color: var(--color-text-primary);
      font: inherit;
      transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
    }
    textarea:hover { border-color: var(--color-text-tertiary); }
    textarea:focus {
      outline: none;
      border-color: var(--color-border-focus);
      box-shadow: 0 0 0 3px rgba(56, 139, 253, 0.15);
    }
    textarea::placeholder {
      color: var(--color-text-tertiary);
    }
    button {
      height: 44px;
      min-width: 44px;
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      padding: 0 13px;
      background: var(--color-bg-tertiary);
      color: var(--color-text-primary);
      font: inherit;
      font-size: var(--font-size-sm);
      font-weight: 500;
      cursor: pointer;
      transition: background-color var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
      white-space: nowrap;
    }
    button.primary {
      border-color: var(--color-primary);
      background: var(--color-primary);
      color: var(--color-text-inverse);
    }
    button.primary:hover {
      border-color: var(--color-primary-hover);
      background: var(--color-primary-hover);
    }
    button:hover {
      background: var(--color-bg-hover);
      border-color: var(--color-text-tertiary);
    }
    #interrupt {
      border-color: rgba(218, 54, 51, 0.68);
      color: #ffb4af;
    }
    #interrupt:hover {
      background: rgba(218, 54, 51, 0.16);
      border-color: var(--color-danger-hover);
      color: #ffd4d0;
    }
    button:disabled { opacity: .55; cursor: wait; }
    .events-title {
      margin: 0 0 var(--space-sm);
      color: var(--color-text-tertiary);
      font-size: var(--font-size-xs);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .event {
      margin: 0 0 var(--space-sm);
      padding: var(--space-sm);
      border: 1px solid var(--color-border-light);
      border-radius: var(--radius-sm);
      background: var(--color-bg-primary);
      color: var(--color-text-secondary);
      font-size: var(--font-size-xs);
      overflow-wrap: anywhere;
    }
    .empty {
      color: var(--color-text-secondary);
      margin: 28px auto;
      max-width: 520px;
      text-align: center;
    }
    @media (max-width: 860px) {
      .app {
        grid-template-columns: 1fr;
        grid-template-rows: auto minmax(0, 1fr) auto auto;
      }
      header {
        grid-column: 1;
        grid-row: 1;
        flex-direction: row;
        align-items: center;
        gap: var(--space-md);
        padding: var(--space-sm) var(--space-md);
        border-right: 0;
        border-bottom: 1px solid var(--color-border);
      }
      .header-left {
        flex: 1;
        flex-direction: row;
        align-items: center;
        min-width: 0;
        padding: 0;
        border-bottom: 0;
      }
      .side-panel { display: none; }
      h1 { font-size: var(--font-size-md); }
      h1::before {
        width: 24px;
        height: 24px;
      }
      .status {
        margin-top: 0;
        padding: 0;
        border-top: 0;
        white-space: nowrap;
      }
      main {
        grid-column: 1;
        grid-row: 2;
        grid-template-columns: 1fr;
      }
      .artifacts-sidebar {
        grid-column: 1;
        grid-row: 3;
        max-height: 180px;
        border-left: 0;
        border-top: 1px solid var(--color-border);
      }
      .sidebar { display: none; }
      .composer {
        grid-column: 1;
        grid-row: 4;
        grid-template-columns: 1fr 1fr;
      }
      textarea { grid-column: 1 / -1; }
      .bubble { max-width: 100%; }
      .artifact-panel { width: 100%; }
      .approval-panel { width: 100%; }
      .approval-actions { grid-template-columns: 1fr; }
      .artifact-viewer {
        width: min(96vw, 100%);
        min-width: 0;
      }
      .artifact-viewer-header {
        grid-template-columns: 1fr;
      }
      .artifact-viewer-actions {
        justify-content: stretch;
      }
      .artifact-viewer-actions a,
      .artifact-viewer-actions button {
        flex: 1;
      }
    }
  </style>
</head>
<body>
  <section class="login-screen" id="loginScreen" hidden>
    <div class="login-card">
      <h1>Simple Chat Agent</h1>
      <p class="login-subtitle" id="loginSubtitle"></p>
      <div class="login-form">
        <a class="primary login-google" id="loginGoogle" href="/oauth/google/start">Sign in with Google</a>
        <p class="login-error" id="loginError"></p>
      </div>
    </div>
  </section>
  <div class="app" id="appRoot" hidden>
    <header>
      <div class="header-left">
        <h1>Simple Chat Agent</h1>
        <a class="temporal-link" id="temporalLink" href="#" target="_blank" rel="noreferrer">Temporal UI</a>
      </div>
      <div class="side-panel">
        <section class="side-section">
          <div class="side-actions">
            <button class="primary" type="button" id="newChat">New Chat</button>
            <button type="button" id="toolsButton">Tools</button>
            <button type="button" id="logout">Logout</button>
          </div>
        </section>
        <section class="side-section">
          <p class="side-section-title">Agent</p>
          <div class="agent-settings">
            <div class="agent-field">
              <label for="modelSelect">Model</label>
              <select id="modelSelect"></select>
            </div>
            <label class="agent-toggle" for="thinkingEnabled">
              <input id="thinkingEnabled" type="checkbox" />
              Extended thinking
            </label>
            <div class="agent-field" id="thinkingBudgetField">
              <label for="thinkingBudget">Budget tokens</label>
              <input id="thinkingBudget" type="number" min="1024" step="1024" />
            </div>
            <div class="agent-field" id="thinkingEffortField">
              <label for="thinkingEffort">Effort</label>
              <select id="thinkingEffort"></select>
            </div>
          </div>
        </section>
        <section class="side-section">
          <p class="side-section-title">Chats</p>
          <div id="conversationList"></div>
        </section>
      </div>
      <div class="status" id="status">connecting...</div>
    </header>
    <main>
      <section class="messages" id="messages">
        <div class="empty">Starting a Temporal workflow...</div>
      </section>
      <aside class="sidebar">
        <p class="events-title">Sideband Stream</p>
        <div id="events"></div>
      </aside>
    </main>
    <aside class="artifacts-sidebar" id="artifactsSidebar"></aside>
    <div class="chat-footer" id="chatFooter" hidden>
      <a class="temporal-link" id="chatWorkflowLink" href="#" target="_blank" rel="noreferrer">Workflow timeline ↗</a>
    </div>
    <form class="composer" id="composer">
      <textarea id="message" placeholder="Type to chat. While responding, Send becomes steering."></textarea>
      <button class="primary" type="submit">Send</button>
      <button type="button" id="queue">Queue</button>
      <button type="button" id="afterTool">After Tool</button>
      <button type="button" id="interrupt">Interrupt</button>
    </form>
    <section class="tools-overlay" id="toolsOverlay" hidden>
      <div class="tools-window" role="dialog" aria-modal="true" aria-labelledby="toolsWindowTitle">
        <div class="tools-window-header">
          <div class="tools-window-title" id="toolsWindowTitle">Tools</div>
          <button type="button" id="closeTools">Close</button>
        </div>
        <div class="tools-window-body" id="toolsWindowBody"></div>
      </div>
    </section>
    <section class="artifact-viewer-overlay" id="artifactViewerOverlay" hidden></section>
  </div>
  <script>
    const state = {
      user: null,
      config: null,
      agentSettings: {
        model: "",
        thinkingEnabled: false,
        thinkingBudgetTokens: 4096,
        thinkingEffort: "medium",
      },
      conversations: [],
      tools: [],
      workflowId: null,
      runId: null,
      temporalUiUrl: null,
      workflowState: null,
      eventSource: null,
      streamTurn: null,
      streamPanelCollapsed: false,
      currentClaudeSequence: null,
      ignoreClaudeUntilStart: false,
      localPending: [],
      lastAssistantCount: 0,
      recoveringMissingWorkflow: false,
      toolsWindowOpen: false,
      artifactViewer: {
        open: false,
        artifact: null,
        loading: false,
        error: "",
        text: "",
        objectUrl: "",
      },
      draftConversation: true,
      mcpFormOpen: false,
      mcpFormSubmitting: false,
      mcpFormError: "",
      mcpFormValues: {
        label: "",
        server_url: "",
        tool_prefix: "",
        auth_mode: "none",
        bearer_token: "",
      },
    };

    const appRootEl = document.getElementById("appRoot");
    const loginScreenEl = document.getElementById("loginScreen");
    const loginGoogleEl = document.getElementById("loginGoogle");
    const loginSubtitleEl = document.getElementById("loginSubtitle");
    const loginErrorEl = document.getElementById("loginError");
    const conversationListEl = document.getElementById("conversationList");
    const toolsOverlayEl = document.getElementById("toolsOverlay");
    const toolsWindowBodyEl = document.getElementById("toolsWindowBody");
    const artifactsSidebarEl = document.getElementById("artifactsSidebar");
    const artifactViewerOverlayEl = document.getElementById("artifactViewerOverlay");
    const messagesEl = document.getElementById("messages");
    const eventsEl = document.getElementById("events");
    const statusEl = document.getElementById("status");
    const temporalLinkEl = document.getElementById("temporalLink");
    const chatFooterEl = document.getElementById("chatFooter");
    const chatWorkflowLinkEl = document.getElementById("chatWorkflowLink");
    const inputEl = document.getElementById("message");
    const formEl = document.getElementById("composer");
    const modelSelectEl = document.getElementById("modelSelect");
    const thinkingEnabledEl = document.getElementById("thinkingEnabled");
    const thinkingBudgetEl = document.getElementById("thinkingBudget");
    const thinkingBudgetFieldEl = document.getElementById("thinkingBudgetField");
    const thinkingEffortEl = document.getElementById("thinkingEffort");
    const thinkingEffortFieldEl = document.getElementById("thinkingEffortField");

    boot().catch((err) => {
      statusEl.textContent = `failed: ${err}`;
    });

    async function boot() {
      const authenticated = await refreshUser();
      if (!authenticated) {
        showLogin();
        return;
      }

      showApp();
      await Promise.all([loadConfig(), loadTools(), loadConversations()]);

      const savedWorkflowId = localStorage.getItem("simpleChatWorkflowId");
      const savedConversation = state.conversations.find((conversation) => conversation.workflow_id === savedWorkflowId);
      const conversation = savedConversation || state.conversations[0];
      if (conversation) {
        selectConversation(conversation.workflow_id);
      } else {
        startDraftConversation();
      }
      showOAuthCallbackStatus();
    }

    async function refreshUser() {
      const response = await fetch("/api/me");
      if (response.status === 401) return false;
      if (!response.ok) throw new Error(await response.text());
      state.user = await response.json();
      applyUserTemporalLink();
      return true;
    }

    // The header "Temporal UI" link points at all of the signed-in user's
    // workflows (filtered by the UserEmail search attribute). The per-chat link
    // (bottom of the chat pane) points at the specific chat workflow.
    function applyUserTemporalLink() {
      const url = state.user?.temporal_ui_workflows_url;
      if (url) {
        temporalLinkEl.href = url;
        temporalLinkEl.style.display = "inline-flex";
      } else {
        temporalLinkEl.removeAttribute("href");
        temporalLinkEl.style.display = "none";
      }
    }

    function showLogin() {
      appRootEl.hidden = true;
      loginScreenEl.hidden = false;
      if (state.eventSource) state.eventSource.close();
      const params = new URLSearchParams(window.location.search);
      if (params.has("oauth_error")) {
        loginErrorEl.textContent = params.get("oauth_error");
        history.replaceState({}, "", "/");
      } else {
        loginErrorEl.textContent = "";
      }
      configureLoginButton();
    }

    async function configureLoginButton() {
      try {
        const response = await fetch("/api/auth/google/configured");
        if (!response.ok) throw new Error(await response.text());
        const body = await response.json();
        if (!body.configured) {
          loginGoogleEl.setAttribute("aria-disabled", "true");
          loginSubtitleEl.textContent = "Google OAuth is not configured. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.";
          return;
        }
        loginGoogleEl.removeAttribute("aria-disabled");
        loginSubtitleEl.textContent = "";
      } catch (err) {
        loginSubtitleEl.textContent = `Could not check auth config: ${err}`;
      }
    }

    function showApp() {
      loginScreenEl.hidden = true;
      appRootEl.hidden = false;
    }

    async function loadConversations() {
      const response = await fetch("/api/conversations");
      if (response.status === 401) {
        showLogin();
        return;
      }
      if (!response.ok) throw new Error(await response.text());
      const body = await response.json();
      state.conversations = body.conversations || [];
      renderSidebar();
    }

    async function loadTools() {
      const response = await fetch("/api/tools");
      if (response.status === 401) {
        showLogin();
        return;
      }
      if (!response.ok) throw new Error(await response.text());
      const body = await response.json();
      state.tools = body.tools || [];
      renderSidebar();
      renderToolsWindow();
    }

    async function loadConfig() {
      const response = await fetch("/api/config");
      if (response.status === 401) {
        showLogin();
        return;
      }
      if (!response.ok) throw new Error(await response.text());
      state.config = await response.json();
      const savedModel = localStorage.getItem("simpleChatModel");
      const modelOptions = state.config.model_options || [];
      state.agentSettings.model = (
        savedModel && modelOptions.includes(savedModel)
      ) ? savedModel : state.config.default_model;
      state.agentSettings.thinkingEnabled =
        localStorage.getItem("simpleChatThinkingEnabled") === "true";
      state.agentSettings.thinkingBudgetTokens = Number(
        localStorage.getItem("simpleChatThinkingBudgetTokens") ||
        state.config.thinking?.budget_tokens ||
        4096
      );
      const savedEffort = localStorage.getItem("simpleChatThinkingEffort");
      const effortOptions = state.config.thinking?.effort_options || ["medium"];
      state.agentSettings.thinkingEffort = (
        savedEffort && effortOptions.includes(savedEffort)
      ) ? savedEffort : state.config.thinking?.effort || "medium";
      renderAgentSettings();
    }

    function renderAgentSettings() {
      const config = state.config || {};
      const modelOptions = config.model_options || [];
      modelSelectEl.replaceChildren();
      for (const model of modelOptions) {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = model;
        modelSelectEl.append(option);
      }
      modelSelectEl.value = state.agentSettings.model || config.default_model || "";
      thinkingEnabledEl.checked = state.agentSettings.thinkingEnabled;
      const effortOptions = config.thinking?.effort_options || ["medium"];
      thinkingEffortEl.replaceChildren();
      for (const effort of effortOptions) {
        const option = document.createElement("option");
        option.value = effort;
        option.textContent = effort;
        thinkingEffortEl.append(option);
      }
      thinkingEffortEl.value = state.agentSettings.thinkingEffort || config.thinking?.effort || "medium";
      const minBudget = config.thinking?.min_budget_tokens || 1024;
      thinkingBudgetEl.min = String(minBudget);
      thinkingBudgetEl.value = String(
        Math.max(minBudget, state.agentSettings.thinkingBudgetTokens || minBudget)
      );
      const adaptive = selectedModelUsesAdaptiveThinking();
      thinkingBudgetFieldEl.hidden = !state.agentSettings.thinkingEnabled || adaptive;
      thinkingEffortFieldEl.hidden = !state.agentSettings.thinkingEnabled || !adaptive;
    }

    function newConversationRequest() {
      const config = state.config || {};
      const minBudget = config.thinking?.min_budget_tokens || 1024;
      const budgetTokens = Math.max(
        minBudget,
        Number(
          thinkingBudgetEl.value ||
          state.agentSettings.thinkingBudgetTokens ||
          config.thinking?.budget_tokens ||
          4096
        ),
      );
      state.agentSettings.model =
        modelSelectEl.value || state.agentSettings.model || config.default_model;
      state.agentSettings.thinkingEnabled = thinkingEnabledEl.checked;
      state.agentSettings.thinkingBudgetTokens = budgetTokens;
      state.agentSettings.thinkingEffort = thinkingEffortEl.value || state.agentSettings.thinkingEffort || "medium";
      saveAgentSettings();
      return {
        model: state.agentSettings.model,
        thinking: {
          enabled: state.agentSettings.thinkingEnabled,
          budget_tokens: budgetTokens,
          effort: state.agentSettings.thinkingEffort,
        },
      };
    }

    function saveAgentSettings() {
      localStorage.setItem("simpleChatModel", state.agentSettings.model);
      localStorage.setItem(
        "simpleChatThinkingEnabled",
        String(state.agentSettings.thinkingEnabled),
      );
      localStorage.setItem(
        "simpleChatThinkingBudgetTokens",
        String(state.agentSettings.thinkingBudgetTokens),
      );
      localStorage.setItem(
        "simpleChatThinkingEffort",
        state.agentSettings.thinkingEffort,
      );
    }

    function selectedModelUsesAdaptiveThinking() {
      const model = modelSelectEl.value || state.agentSettings.model || "";
      return (state.config?.thinking?.adaptive_model_prefixes || []).some((prefix) => (
        model.startsWith(prefix)
      ));
    }

    function startDraftConversation() {
      if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
      }
      state.workflowId = null;
      state.runId = null;
      state.temporalUiUrl = null;
      state.workflowState = null;
      state.streamTurn = null;
      state.streamPanelCollapsed = false;
      state.currentClaudeSequence = null;
      state.ignoreClaudeUntilStart = false;
      state.localPending = [];
      state.draftConversation = true;
      closeArtifactViewer();
      localStorage.removeItem("simpleChatWorkflowId");
      chatFooterEl.hidden = true;
      chatWorkflowLinkEl.removeAttribute("href");
      renderSidebar();
      render();
    }

    async function createConversation(initialMessage = null, options = {}) {
      const response = await fetch("/api/sessions", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({
          ...newConversationRequest(),
          initial_message: initialMessage,
        }),
      });
      if (response.status === 401) {
        showLogin();
        return;
      }
      if (!response.ok) throw new Error(await response.text());
      const body = await response.json();
      await loadConversations();
      selectConversation(body.workflow_id, options);
      return body;
    }

    function selectConversation(workflowId, options = {}) {
      const conversation = state.conversations.find((item) => item.workflow_id === workflowId);
      if (!conversation) return;
      if (state.eventSource) state.eventSource.close();
      state.workflowId = conversation.workflow_id;
      state.runId = conversation.run_id;
      state.temporalUiUrl = temporalUiUrl(conversation);
      state.workflowState = null;
      state.streamTurn = null;
      state.streamPanelCollapsed = false;
      state.currentClaudeSequence = null;
      state.ignoreClaudeUntilStart = false;
      state.draftConversation = false;
      if (!options.preserveLocalPending) {
        state.localPending = [];
      }
      closeArtifactViewer();
      localStorage.setItem("simpleChatWorkflowId", state.workflowId);
      if (state.temporalUiUrl) {
        chatWorkflowLinkEl.href = state.temporalUiUrl;
        chatFooterEl.hidden = false;
      } else {
        chatFooterEl.hidden = true;
      }
      renderSidebar();
      render();
      connectEvents();
    }

    function connectEvents() {
      if (!state.workflowId) return;
      state.eventSource = new EventSource(`/api/sessions/${state.workflowId}/events`);
      state.eventSource.addEventListener("state", (event) => {
        const nextState = JSON.parse(event.data);
        updateWorkflowState(nextState);
      });
      state.eventSource.addEventListener("stream", (event) => {
        handleStreamEvent(JSON.parse(event.data));
      });
      state.eventSource.addEventListener("missing", async () => {
        await handleMissingWorkflow();
      });
      state.eventSource.addEventListener("error", () => {
        statusEl.textContent = "event stream reconnecting...";
      });
    }

    document.getElementById("newChat").addEventListener("click", () => startDraftConversation());
    modelSelectEl.addEventListener("change", () => {
      state.agentSettings.model = modelSelectEl.value;
      saveAgentSettings();
      renderAgentSettings();
    });
    thinkingEnabledEl.addEventListener("change", () => {
      state.agentSettings.thinkingEnabled = thinkingEnabledEl.checked;
      saveAgentSettings();
      renderAgentSettings();
    });
    function updateThinkingBudget() {
      const minBudget = state.config?.thinking?.min_budget_tokens || 1024;
      state.agentSettings.thinkingBudgetTokens = Math.max(
        minBudget,
        Number(thinkingBudgetEl.value || minBudget),
      );
      saveAgentSettings();
      renderAgentSettings();
    }
    thinkingBudgetEl.addEventListener("change", updateThinkingBudget);
    thinkingEffortEl.addEventListener("change", () => {
      state.agentSettings.thinkingEffort = thinkingEffortEl.value;
      saveAgentSettings();
    });
    document.getElementById("toolsButton").addEventListener("click", () => {
      state.toolsWindowOpen = true;
      renderToolsWindow();
    });
    document.getElementById("closeTools").addEventListener("click", () => {
      state.toolsWindowOpen = false;
      state.mcpFormOpen = false;
      state.mcpFormError = "";
      renderToolsWindow();
    });
    toolsOverlayEl.addEventListener("click", (event) => {
      if (event.target !== toolsOverlayEl) return;
      state.toolsWindowOpen = false;
      state.mcpFormOpen = false;
      state.mcpFormError = "";
      renderToolsWindow();
    });
    artifactViewerOverlayEl.addEventListener("click", (event) => {
      if (event.target !== artifactViewerOverlayEl) return;
      closeArtifactViewer();
    });
    document.getElementById("logout").addEventListener("click", async () => {
      await post("/api/logout", {});
      localStorage.removeItem("simpleChatWorkflowId");
      state.user = null;
      state.conversations = [];
      state.workflowId = null;
      state.workflowState = null;
      state.draftConversation = true;
      closeArtifactViewer();
      state.toolsWindowOpen = false;
      state.mcpFormOpen = false;
      showLogin();
    });
    formEl.addEventListener("submit", (event) => {
      event.preventDefault();
      sendDefault();
    });
    document.getElementById("queue").addEventListener("click", () => sendAction("chat", "you", "sending"));
    document.getElementById("afterTool").addEventListener("click", () => sendAction("after-tool", "you after tool", "sending"));
    document.getElementById("interrupt").addEventListener("click", () => sendAction("interrupt", "you interrupt", "sending"));
    inputEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendDefault();
      }
    });

    function sendDefault() {
      const busy = state.workflowState?.status === "responding";
      sendAction(busy ? "steer" : "chat", busy ? "you steering" : "you", "sending");
    }

    async function sendAction(action, label, phase) {
      let message = inputEl.value.trim();
      if (!message && action === "interrupt") {
        message = "Stop the current response.";
      }
      if (!message) return;
      if (!state.workflowId) {
        if (action === "interrupt" || action === "steer" || action === "after-tool") return;
        inputEl.value = "";
        const pending = { id: crypto.randomUUID(), label, content: message, phase };
        state.localPending.push(pending);
        render();
        try {
          await createConversation(message, { preserveLocalPending: true });
        } catch (err) {
          pending.phase = `failed: ${err}`;
          render();
        }
        return;
      }
      inputEl.value = "";
      const pending = { id: crypto.randomUUID(), label, content: message, phase };
      state.localPending.push(pending);
      if (action === "interrupt") {
        markStreamInterrupted();
        state.ignoreClaudeUntilStart = true;
      }
      render();

      try {
        if (action === "chat") {
          await post(`/api/sessions/${state.workflowId}/chat`, { message });
        } else if (action === "steer") {
          await post(`/api/sessions/${state.workflowId}/steer`, { message, mode: "immediate" });
        } else if (action === "after-tool") {
          await post(`/api/sessions/${state.workflowId}/steer`, { message, mode: "after_next_tool_result" });
        } else if (action === "interrupt") {
          await post(`/api/sessions/${state.workflowId}/interrupt`, { message });
        }
        await loadConversations();
      } catch (err) {
        pending.phase = `failed: ${err}`;
        render();
      }
    }

    async function post(url, payload) {
      const response = await fetch(url, { method: "POST", headers: jsonHeaders(), body: JSON.stringify(payload) });
      if (response.status === 401) {
        showLogin();
      }
      if (response.status === 404) {
        await handleMissingWorkflow();
      }
      if (!response.ok) throw new Error(await responseErrorText(response));
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) return await response.json();
      return {};
    }

    async function responseErrorText(response) {
      const text = await response.text();
      try {
        const body = JSON.parse(text);
        if (typeof body.detail === "string") return body.detail;
        if (body.detail) return JSON.stringify(body.detail);
      } catch (_err) {
      }
      return text || `${response.status} ${response.statusText}`;
    }

    async function handleMissingWorkflow() {
      if (state.recoveringMissingWorkflow) return;
      state.recoveringMissingWorkflow = true;
      const missingWorkflowId = state.workflowId;
      try {
        if (state.eventSource) {
          state.eventSource.close();
          state.eventSource = null;
        }
        if (missingWorkflowId) {
          statusEl.textContent = "Workflow no longer exists; selecting a live chat...";
          if (localStorage.getItem("simpleChatWorkflowId") === missingWorkflowId) {
            localStorage.removeItem("simpleChatWorkflowId");
          }
        }

        await loadConversations();
        const nextConversation = state.conversations[0];
        if (nextConversation) {
          selectConversation(nextConversation.workflow_id);
        } else {
          startDraftConversation();
        }
      } finally {
        state.recoveringMissingWorkflow = false;
      }
    }

    async function deleteConversation(workflowId) {
      if (!confirm("Delete this chat?")) return;
      const response = await fetch(`/api/sessions/${workflowId}`, { method: "DELETE" });
      if (response.status === 401) {
        showLogin();
        return;
      }
      if (!response.ok) throw new Error(await response.text());

      if (state.workflowId === workflowId) {
        if (state.eventSource) {
          state.eventSource.close();
          state.eventSource = null;
        }
        if (localStorage.getItem("simpleChatWorkflowId") === workflowId) {
          localStorage.removeItem("simpleChatWorkflowId");
        }
        state.workflowId = null;
        state.workflowState = null;
        state.streamTurn = null;
        state.localPending = [];
        closeArtifactViewer();
      }

      await loadConversations();
      if (!state.workflowId) {
        const nextConversation = state.conversations[0];
        if (nextConversation) {
          selectConversation(nextConversation.workflow_id);
        } else {
          startDraftConversation();
        }
      }
    }

    async function resolveApproval(approvalId, decision) {
      await post(`/api/sessions/${state.workflowId}/approvals/${approvalId}`, { decision });
    }

    function renderSidebar() {
      const conversationFragment = document.createDocumentFragment();
      if (state.draftConversation) {
        const row = document.createElement("div");
        row.className = "conversation-row";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "conversation-item active";
        button.textContent = "New chat";
        button.addEventListener("click", () => startDraftConversation());
        row.append(button);
        conversationFragment.append(row);
      }
      for (const conversation of state.conversations) {
        const row = document.createElement("div");
        row.className = "conversation-row";
        const button = document.createElement("button");
        button.type = "button";
        button.className = `conversation-item${conversation.workflow_id === state.workflowId ? " active" : ""}`;
        button.textContent = conversation.title || "New chat";
        button.addEventListener("click", () => selectConversation(conversation.workflow_id));

        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = "conversation-delete";
        deleteButton.textContent = "Del";
        deleteButton.title = "Delete chat";
        deleteButton.addEventListener("click", (event) => {
          event.stopPropagation();
          deleteConversation(conversation.workflow_id).catch((err) => {
            statusEl.textContent = `delete failed: ${err}`;
          });
        });

        row.append(button, deleteButton);
        conversationFragment.append(row);
      }
      if (state.conversations.length === 0 && !state.draftConversation) {
        const empty = document.createElement("div");
        empty.className = "tool-meta";
        empty.textContent = "No chats yet.";
        conversationFragment.append(empty);
      }
      conversationListEl.replaceChildren(conversationFragment);
    }

    function renderApprovalsPanel() {
      const approvals = state.workflowState?.pending_approvals || [];
      if (approvals.length === 0) return null;

      const panel = document.createElement("section");
      panel.className = "approval-panel";

      const header = document.createElement("div");
      header.className = "approval-panel-header";
      const title = document.createElement("span");
      title.textContent = "Approval Required";
      const count = document.createElement("span");
      count.className = "approval-panel-count";
      count.textContent = `${approvals.length} pending`;
      header.append(title, count);
      panel.append(header);

      for (const approval of approvals) {
        panel.append(renderApprovalCard(approval));
      }

      return panel;
    }

    function renderApprovalCard(approval) {
      const card = document.createElement("div");
      card.className = "approval-card";

      const title = document.createElement("div");
      title.className = "approval-title";
      title.textContent = approval.summary || approval.tool_name;
      card.append(title);

      const meta = document.createElement("div");
      meta.className = "approval-meta";
      meta.append(
        approvalMetaRow("Tool", approval.tool_name),
        approvalMetaRow("Scope", approval.memory_key || "one time"),
      );
      card.append(meta);

      const details = document.createElement("div");
      details.className = "approval-details bubble-content";
      renderApprovalArgs(details, approval.tool_args || {});
      card.append(details);

      const actions = document.createElement("div");
      actions.className = "approval-actions";
      actions.append(
        approvalButton("Allow", approval.approval_id, "allow", "allow"),
        approvalButton("Always Allow", approval.approval_id, "always_allow", "always"),
        approvalButton("Deny", approval.approval_id, "deny", "deny"),
      );
      card.append(actions);

      return card;
    }

    function approvalMetaRow(label, value) {
      const row = document.createElement("div");
      const labelNode = document.createElement("strong");
      labelNode.textContent = `${label}: `;
      row.append(labelNode, document.createTextNode(value || "unknown"));
      return row;
    }

    function renderApprovalArgs(container, args) {
      if (typeof args.code === "string") {
        container.append(createCodeBlock(args.code, "python"));
        const rest = { ...args };
        delete rest.code;
        if (Object.keys(rest).length > 0) {
          container.append(createCodeBlock(JSON.stringify(rest, null, 2), "json"));
        }
        return;
      }

      if (typeof args.content === "string" && typeof args.name === "string") {
        const metadata = { ...args };
        delete metadata.content;
        container.append(createCodeBlock(JSON.stringify(metadata, null, 2), "json"));
        const truncated = args.content.length > 12000;
        const preview = truncated
          ? `${args.content.slice(0, 12000)}\n...[truncated for approval preview]`
          : args.content;
        container.append(createCodeBlock(preview, languageFromFileName(args.name)));
        return;
      }

      container.append(createCodeBlock(JSON.stringify(args, null, 2), "json"));
    }

    function renderToolsWindow() {
      toolsOverlayEl.hidden = !state.toolsWindowOpen;
      if (!state.toolsWindowOpen) {
        toolsWindowBodyEl.replaceChildren();
        return;
      }

      const fragment = document.createDocumentFragment();
      const builtInTools = state.tools.filter((tool) => !tool.provider?.startsWith("mcp:"));
      const mcpTools = state.tools.filter((tool) => tool.provider?.startsWith("mcp:"));

      fragment.append(renderBuiltInToolsSection(builtInTools));
      fragment.append(renderMcpToolsSection(mcpTools));
      toolsWindowBodyEl.replaceChildren(fragment);
    }

    function renderBuiltInToolsSection(tools) {
      const section = document.createElement("section");
      section.className = "tools-section";
      section.append(toolsSectionHeader("Built-in tools"));

      const grid = document.createElement("div");
      grid.className = "tools-grid";
      for (const tool of tools) {
        grid.append(renderBuiltInToolCard(tool));
      }
      section.append(grid);
      return section;
    }

    function renderMcpToolsSection(tools) {
      const section = document.createElement("section");
      section.className = "tools-section";

      const actions = document.createElement("div");
      actions.className = "tools-section-actions";
      const addMcpButton = document.createElement("button");
      addMcpButton.type = "button";
      addMcpButton.textContent = "Add HTTP MCP";
      addMcpButton.addEventListener("click", () => {
        state.mcpFormOpen = true;
        state.mcpFormError = "";
        renderToolsWindow();
      });
      actions.append(addMcpButton);
      section.append(toolsSectionHeader("MCP servers", actions));

      if (state.mcpFormOpen) {
        section.append(renderMcpForm());
      }

      const grid = document.createElement("div");
      grid.className = "tools-grid";
      for (const tool of tools) {
        grid.append(renderMcpToolCard(tool));
      }
      if (tools.length === 0) {
        const empty = document.createElement("div");
        empty.className = "tool-meta";
        empty.textContent = "No MCP servers connected.";
        grid.append(empty);
      }
      section.append(grid);
      return section;
    }

    function toolsSectionHeader(titleText, actions = null) {
      const header = document.createElement("div");
      header.className = "tools-section-header";
      const title = document.createElement("div");
      title.className = "tools-section-title";
      title.textContent = titleText;
      header.append(title);
      if (actions) header.append(actions);
      return header;
    }

    function renderBuiltInToolCard(tool) {
      const card = baseToolCard(tool, {
        status: tool.connected ? "Connected" : "Disconnected",
        connected: Boolean(tool.connected),
        disabled: false,
      });

      if (tool.provider === "github") {
        const actions = document.createElement("div");
        actions.className = "tool-actions";
        const action = document.createElement("button");
        action.type = "button";
        action.textContent = tool.connected ? "Disconnect" : "Connect";
        action.disabled = !tool.configured;
        action.addEventListener("click", async () => {
          if (tool.connected) {
            await post("/api/tools/github/disconnect", {});
            statusEl.textContent = "GitHub disconnected";
            await loadTools();
          } else {
            window.location.href = "/oauth/github/start";
          }
        });
        actions.append(action);
        card.append(actions);
      }

      return card;
    }

    function renderMcpToolCard(tool) {
      const connected = Boolean(tool.connected);
      const enabled = Boolean(tool.enabled);
      const card = baseToolCard(tool, {
        status: connected ? (enabled ? "Enabled" : "Disabled") : "Disconnected",
        connected: connected && enabled,
        disabled: !enabled,
      });

      const actions = document.createElement("div");
      actions.className = "tool-actions";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.textContent = enabled ? "Disable" : "Enable";
      toggle.addEventListener("click", async () => {
        await setMcpServerEnabled(tool, !enabled);
      });
      if (tool.auth_mode === "oauth") {
        const reconnect = document.createElement("button");
        reconnect.type = "button";
        reconnect.textContent = "Reconnect";
        reconnect.addEventListener("click", () => {
          window.location.href = mcpOAuthStartUrl({
            label: tool.label,
            serverUrl: tool.server_url || tool.login || "",
            toolPrefix: tool.tool_prefix || "",
            serverId: tool.server_id || tool.provider.slice("mcp:".length),
          });
        });
        actions.append(reconnect);
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "Delete";
      remove.addEventListener("click", async () => {
        await deleteMcpServer(tool);
      });
      actions.append(toggle, remove);
      card.append(actions);
      return card;
    }

    function baseToolCard(tool, { status, connected, disabled }) {
      const card = document.createElement("div");
      card.className = `tool-card${connected ? " connected" : ""}${disabled ? " disabled" : ""}`;

      const title = document.createElement("div");
      title.className = "tool-title";
      const label = document.createElement("span");
      label.className = "tool-label";
      label.textContent = tool.label;
      const statusNode = document.createElement("span");
      statusNode.className = "tool-status";
      statusNode.textContent = status;
      title.append(label, statusNode);
      card.append(title);

      const meta = document.createElement("div");
      meta.className = "tool-meta";
      if (!tool.configured) {
        meta.textContent = "Set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET.";
      } else if (tool.provider?.startsWith("mcp:")) {
        meta.textContent = `${tool.login || "HTTP MCP"} | ${tool.available_tools?.length || 0} tools | ${tool.scopes}`;
      } else if (tool.connected && tool.login) {
        meta.textContent = `@${tool.login} | ${tool.scopes || "no scopes returned"}`;
      } else {
        meta.textContent = `Scopes: ${tool.scopes || "none"}`;
      }
      card.append(meta);

      if (tool.available_tools?.length) {
        const chips = document.createElement("div");
        chips.className = "tool-chip-list";
        for (const toolName of tool.available_tools.slice(0, 8)) {
          const chip = document.createElement("span");
          chip.className = "tool-chip";
          chip.textContent = toolName;
          chips.append(chip);
        }
        if (tool.available_tools.length > 8) {
          const chip = document.createElement("span");
          chip.className = "tool-chip";
          chip.textContent = `+${tool.available_tools.length - 8}`;
          chips.append(chip);
        }
        card.append(chips);
      }

      return card;
    }

    async function setMcpServerEnabled(tool, enabled) {
      const serverId = tool.provider.slice("mcp:".length);
      await post(`/api/mcp-servers/${encodeURIComponent(serverId)}/enabled`, { enabled });
      statusEl.textContent = `${tool.label} ${enabled ? "enabled" : "disabled"}`;
      await loadTools();
    }

    async function deleteMcpServer(tool) {
      if (!confirm(`Delete ${tool.label}?`)) return;
      const serverId = tool.provider.slice("mcp:".length);
      const response = await fetch(`/api/mcp-servers/${encodeURIComponent(serverId)}`, { method: "DELETE" });
      if (response.status === 401) {
        showLogin();
        return;
      }
      if (!response.ok) throw new Error(await responseErrorText(response));
      statusEl.textContent = `${tool.label} deleted`;
      await loadTools();
    }

    function renderMcpForm() {
      const values = state.mcpFormValues;
      const form = document.createElement("form");
      form.className = "mcp-form";
      form.append(
        mcpField("Label", "label", "Temporal docs", "text", values.label),
        mcpField("HTTP URL", "server_url", "https://example.com/mcp", "text", values.server_url),
        mcpField("Tool prefix", "tool_prefix", "temporal", "text", values.tool_prefix),
        mcpAuthField(values.auth_mode),
        mcpField("Bearer token", "bearer_token", "", "password", values.bearer_token),
      );

      const bearerField = form.querySelector('[data-field="bearer_token"]');
      const authMode = form.querySelector('[name="auth_mode"]');
      bearerField.hidden = authMode.value !== "bearer";
      authMode.addEventListener("change", () => {
        bearerField.hidden = authMode.value !== "bearer";
      });

      const labelInput = form.querySelector('[name="label"]');
      const prefixInput = form.querySelector('[name="tool_prefix"]');
      let prefixTouched = false;
      prefixInput.addEventListener("input", () => {
        prefixTouched = true;
      });
      labelInput.addEventListener("input", () => {
        if (!prefixTouched) prefixInput.value = toolPrefixFromLabel(labelInput.value);
      });

      if (state.mcpFormError) {
        const error = document.createElement("div");
        error.className = "mcp-error";
        error.textContent = state.mcpFormError;
        form.append(error);
      }

      const actions = document.createElement("div");
      actions.className = "mcp-form-actions";
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "primary";
      submit.textContent = state.mcpFormSubmitting ? "Adding..." : "Add";
      submit.disabled = state.mcpFormSubmitting;
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.disabled = state.mcpFormSubmitting;
      cancel.addEventListener("click", () => {
        state.mcpFormOpen = false;
        state.mcpFormError = "";
        resetMcpFormValues();
        renderToolsWindow();
      });
      actions.append(submit, cancel);
      form.append(actions);

      form.addEventListener("submit", (event) => {
        event.preventDefault();
        addHttpMcpServer(form).catch((err) => {
          state.mcpFormError = String(err);
          state.mcpFormSubmitting = false;
          renderToolsWindow();
        });
      });

      return form;
    }

    function mcpField(label, name, placeholder, type = "text", value = "") {
      const field = document.createElement("div");
      field.className = "mcp-field";
      field.dataset.field = name;
      const labelNode = document.createElement("label");
      labelNode.textContent = label;
      const input = document.createElement("input");
      input.name = name;
      input.type = type;
      input.placeholder = placeholder;
      input.value = value;
      input.required = name !== "bearer_token";
      field.append(labelNode, input);
      return field;
    }

    function mcpAuthField(value = "none") {
      const field = document.createElement("div");
      field.className = "mcp-field";
      const labelNode = document.createElement("label");
      labelNode.textContent = "Auth";
      const select = document.createElement("select");
      select.name = "auth_mode";
      const none = document.createElement("option");
      none.value = "none";
      none.textContent = "No auth";
      const oauth = document.createElement("option");
      oauth.value = "oauth";
      oauth.textContent = "OAuth authorization";
      const bearer = document.createElement("option");
      bearer.value = "bearer";
      bearer.textContent = "Bearer token";
      select.append(none, oauth, bearer);
      select.value = value;
      field.append(labelNode, select);
      return field;
    }

    async function addHttpMcpServer(form) {
      const formData = new FormData(form);
      const label = String(formData.get("label") || "").trim();
      const serverUrl = String(formData.get("server_url") || "").trim();
      const toolPrefix = String(formData.get("tool_prefix") || "").trim();
      const authMode = String(formData.get("auth_mode") || "none");
      const bearerToken = String(formData.get("bearer_token") || "").trim();
      state.mcpFormValues = {
        label,
        server_url: serverUrl,
        tool_prefix: toolPrefix,
        auth_mode: authMode,
        bearer_token: bearerToken,
      };

      if (authMode === "oauth") {
        window.location.href = mcpOAuthStartUrl({
          label,
          serverUrl,
          toolPrefix,
        });
        return;
      }

      state.mcpFormSubmitting = true;
      state.mcpFormError = "";
      renderToolsWindow();

      const body = await post("/api/mcp-servers", {
        label,
        server_url: serverUrl,
        tool_prefix: toolPrefix,
        auth_mode: authMode,
        bearer_token: authMode === "bearer" ? bearerToken : null,
      });
      state.mcpFormOpen = false;
      state.mcpFormSubmitting = false;
      state.mcpFormError = "";
      resetMcpFormValues();
      statusEl.textContent = `Added MCP server: ${body.server?.label || label}`;
      await loadTools();
    }

    function mcpOAuthStartUrl({ label, serverUrl, toolPrefix, serverId = "" }) {
      const params = new URLSearchParams({
        label,
        server_url: serverUrl,
        tool_prefix: toolPrefix,
      });
      if (serverId) params.set("server_id", serverId);
      return `/api/mcp-servers/oauth/start?${params.toString()}`;
    }

    function resetMcpFormValues() {
      state.mcpFormValues = {
        label: "",
        server_url: "",
        tool_prefix: "",
        auth_mode: "none",
        bearer_token: "",
      };
    }

    function toolPrefixFromLabel(label) {
      return label.toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "") || "mcp";
    }

    function approvalButton(label, approvalId, decision, className = "") {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      if (className) button.className = className;
      button.addEventListener("click", () => resolveApproval(approvalId, decision));
      return button;
    }

    function temporalUiUrl(conversation) {
      if (conversation.temporal_ui_url) return conversation.temporal_ui_url;
      const workflow = encodeURIComponent(conversation.workflow_id);
      const run = encodeURIComponent(conversation.run_id || "");
      if (run) {
        return `http://localhost:8233/namespaces/default/workflows/${workflow}/${run}/history`;
      }
      return `http://localhost:8233/namespaces/default/workflows/${workflow}`;
    }

    function showOAuthCallbackStatus() {
      const params = new URLSearchParams(window.location.search);
      if (params.has("oauth_error")) {
        statusEl.textContent = `OAuth failed: ${params.get("oauth_error")}`;
      } else if (params.has("github")) {
        statusEl.textContent = "GitHub connected";
      } else if (params.has("mcp")) {
        statusEl.textContent = "MCP server connected";
        loadTools().catch((err) => {
          statusEl.textContent = `tool refresh failed: ${err}`;
        });
      }
      if (params.has("oauth_error") || params.has("github") || params.has("mcp")) {
        history.replaceState({}, "", "/");
      }
    }

    function updateWorkflowState(nextState) {
      const previousAssistantCount = state.workflowState
        ? state.workflowState.transcript.filter((m) => m.role === "assistant").length
        : 0;
      const nextAssistantCount = nextState.transcript.filter((m) => m.role === "assistant").length;
      state.workflowState = nextState;
      state.localPending = state.localPending.filter((pending) => !isAcknowledged(pending, nextState));
      if (nextAssistantCount > previousAssistantCount) markStreamCommitted();
      render();
    }

    function handleStreamEvent(event) {
      const sequence = event.payload?.sequence ?? null;
      if (event.kind === "claude_start") {
        state.currentClaudeSequence = sequence;
        state.ignoreClaudeUntilStart = false;
        if (isOpenStreamTurn(state.streamTurn)) {
          registerStreamSequence(state.streamTurn, sequence);
          state.streamTurn.status = "streaming";
          state.streamTurn.activeSequence = sequence;
        }
      } else if (event.kind === "claude_text_delta" && event.payload?.text) {
        if (state.ignoreClaudeUntilStart) return;
        if (sequence !== state.currentClaudeSequence) return;
        const turn = ensureStreamTurn(sequence);
        turn.status = "streaming";
        turn.text += event.payload.text;
      } else if (event.kind === "claude_thinking_start") {
        if (state.ignoreClaudeUntilStart) return;
        if (sequence !== state.currentClaudeSequence) return;
        const turn = ensureStreamTurn(sequence);
        turn.status = "streaming";
      } else if (event.kind === "claude_thinking_delta" && event.payload?.thinking) {
        if (state.ignoreClaudeUntilStart) return;
        if (sequence !== state.currentClaudeSequence) return;
        const turn = ensureStreamTurn(sequence);
        turn.status = "streaming";
        turn.thinking += event.payload.thinking;
      } else if (event.kind === "claude_cancelled") {
        if (sequence === state.currentClaudeSequence) {
          markStreamInterrupted();
          state.ignoreClaudeUntilStart = true;
        }
      } else if (event.kind === "claude_complete") {
        if (sequence !== state.currentClaudeSequence) return;
        const turn = ensureStreamTurn(sequence);
        if (turn) {
          finishStreamClaudeTurn(turn, event.payload || {});
          turn.status = turn.currentEvents.length ? "tooling" : "waiting";
          turn.lastClaudeCompletedAt = new Date().toISOString();
        }
      } else if (isClaudeToolEvent(event)) {
        const turn = ensureStreamTurn(state.currentClaudeSequence);
        appendStreamToolEvent(turn, event);
        if (turn.status !== "complete" && turn.status !== "interrupted") {
          turn.status = "tooling";
        }
      } else if (!event.kind?.startsWith("claude_")) {
        const turn = ensureStreamTurn(state.currentClaudeSequence);
        appendStreamToolEvent(turn, event);
        if (turn.status !== "complete" && turn.status !== "interrupted") {
          turn.status = "tooling";
        }
      }
      render();
    }

    function ensureStreamTurn(sequence) {
      if (!isOpenStreamTurn(state.streamTurn)) {
        state.streamTurn = createStreamTurn(sequence);
      } else {
        registerStreamSequence(state.streamTurn, sequence);
      }
      return state.streamTurn;
    }

    function streamTurnForSequence(sequence) {
      if (!isOpenStreamTurn(state.streamTurn)) return null;
      if (sequence === null) return state.streamTurn;
      return state.streamTurn.sequences.includes(sequence) ? state.streamTurn : null;
    }

    function isOpenStreamTurn(turn) {
      return Boolean(
        turn &&
        turn.status !== "complete" &&
        turn.status !== "interrupted"
      );
    }

    function registerStreamSequence(turn, sequence) {
      if (sequence !== null && !turn.sequences.includes(sequence)) {
        turn.sequences.push(sequence);
      }
    }

    function createStreamTurn(sequence) {
      return {
        sequence,
        sequences: sequence === null ? [] : [sequence],
        activeSequence: sequence,
        status: "streaming",
        text: "",
        thinking: "",
        finishedTurns: [],
        currentEvents: [],
        startedAt: new Date().toISOString(),
        completedAt: null,
        lastClaudeCompletedAt: null,
        interrupted: false,
      };
    }

    function finishStreamClaudeTurn(turn, payload) {
      const text = String(payload.text || turn.text || "").trim();
      const stopReason = payload.stop_reason || "unknown";
      const sequence = payload.sequence ?? turn.activeSequence;
      turn.finishedTurns.push({
        sequence,
        text,
        thinking: String(turn.thinking || "").trim(),
        stopReason,
        usage: payload.usage || null,
        events: turn.currentEvents,
        completedAt: new Date().toISOString(),
      });
      turn.finishedTurns = turn.finishedTurns.slice(-12);
      turn.text = "";
      turn.thinking = "";
      turn.currentEvents = [];
    }

    function appendStreamToolEvent(turn, event) {
      const finishedTurn = latestFinishedToolUseTurn(turn);
      if (finishedTurn) {
        finishedTurn.events = mergeStreamToolEvent(finishedTurn.events || [], event);
        return;
      }

      turn.currentEvents = mergeStreamToolEvent(turn.currentEvents, event);
    }

    function isClaudeToolEvent(event) {
      return event.kind?.startsWith("claude_tool_input_");
    }

    function mergeStreamToolEvent(events, event) {
      if (!event.kind?.startsWith("claude_tool_input_")) {
        return [...events, event].slice(-5);
      }

      const key = streamToolInputKey(event);
      const nextEvents = [...events];
      const existingIndex = nextEvents.findIndex((candidate) => (
        candidate.kind?.startsWith("claude_tool_input_") &&
        streamToolInputKey(candidate) === key
      ));
      const existing = existingIndex >= 0 ? nextEvents[existingIndex] : null;
      const merged = mergeToolInputEvent(existing, event, key);
      if (existingIndex >= 0) {
        nextEvents[existingIndex] = merged;
      } else {
        nextEvents.push(merged);
      }
      return nextEvents.slice(-5);
    }

    function mergeToolInputEvent(existing, event, key) {
      const existingPayload = existing?.payload || {};
      const payload = event.payload || {};
      const nextPayload = { ...existingPayload, ...payload };
      const existingPartial = String(existingPayload.input_partial || "");

      if (event.kind === "claude_tool_input_delta") {
        nextPayload.input_partial = existingPartial + String(payload.partial_json || "");
        nextPayload.status = "streaming input";
      } else if (event.kind === "claude_tool_input_complete") {
        nextPayload.input_partial = existingPartial;
        nextPayload.status = "input complete";
      } else {
        nextPayload.input_partial = existingPartial;
        nextPayload.status = "building input";
      }

      return {
        ...(existing || event),
        kind: event.kind,
        payload: nextPayload,
        streamToolInputKey: key,
      };
    }

    function streamToolInputKey(event) {
      return (
        event.streamToolInputKey ||
        event.payload?.tool_use_id ||
        `block:${event.payload?.content_block_index ?? "unknown"}`
      );
    }

    function latestFinishedToolUseTurn(turn) {
      const latest = turn.finishedTurns[turn.finishedTurns.length - 1];
      if (!latest || latest.stopReason !== "tool_use") return null;
      return latest;
    }

    function markStreamCommitted() {
      state.streamTurn = null;
      state.currentClaudeSequence = null;
      state.ignoreClaudeUntilStart = false;
    }

    function markStreamInterrupted() {
      state.streamTurn = null;
      state.currentClaudeSequence = null;
    }

    function isAcknowledged(pending, workflowState) {
      return workflowState.transcript.some((message) => {
        if (message.role === "user" && message.content === pending.content) return true;
        if (message.role === "system" && message.content.includes(pending.content)) return true;
        return false;
      });
    }

    function render() {
      const workflowState = state.workflowState;
      const thinkingLabel = workflowState?.thinking?.enabled ? " | thinking" : "";
      const modelLabel = workflowState?.model ? ` | ${workflowState.model}${thinkingLabel}` : "";
      statusEl.textContent = state.draftConversation
        ? "draft | workflow not started"
        : workflowState
        ? `${workflowState.status}${workflowState.pending_messages ? `, queued: ${workflowState.pending_messages}` : ""}${modelLabel}`
        : "starting...";
      renderSidebar();
      renderArtifactsPanel();
      renderArtifactViewer();

      const fragment = document.createDocumentFragment();
      if (!workflowState && state.localPending.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = state.draftConversation
          ? "Type your first message to start a Temporal workflow."
          : "Starting a Temporal workflow...";
        fragment.append(empty);
      }

      for (const [index, message] of (workflowState?.transcript || []).entries()) {
        fragment.append(renderMessage(message, index, workflowState));
      }
      for (const pending of state.localPending) {
        fragment.append(bubble("pending", pending.label, `${pending.content} (${pending.phase})`));
      }
      const streamPanel = renderStreamPanel();
      if (streamPanel) {
        fragment.append(streamPanel);
      }
      const approvalsPanel = renderApprovalsPanel();
      if (approvalsPanel) {
        fragment.append(approvalsPanel);
      }

      messagesEl.replaceChildren(fragment);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      // The live streaming text/thinking boxes have their own max-height scroll
      // region; keep them pinned to the latest output as text streams in.
      messagesEl
        .querySelectorAll(
          ".stream-current-turn .stream-text, .stream-current-turn .stream-thinking",
        )
        .forEach((el) => {
          el.scrollTop = el.scrollHeight;
        });
      eventsEl.replaceChildren();
    }

    function renderMessage(message, index, workflowState) {
      if (message.role === "user") {
        if (workflowState.active_message_index === index) {
          return bubble("pending", "you -> agent", `${message.content} (delivered)`);
        }
        if (workflowState.queued_message_indices.includes(index)) {
          return bubble("pending", "you", `${message.content} (queued)`);
        }
        return bubble("user", "you", message.content);
      }
      if (message.role === "assistant") return bubble("assistant", "assistant", message.content);
      return bubble("system", "system", message.content);
    }

    function renderArtifactsPanel() {
      const artifacts = state.workflowState?.artifacts || [];
      const panel = document.createElement("section");
      panel.className = "artifact-panel";

      const header = document.createElement("div");
      header.className = "artifact-panel-header";
      const title = document.createElement("span");
      title.textContent = "Artifacts";
      const count = document.createElement("span");
      count.className = "artifact-panel-count";
      count.textContent = artifacts.length === 1 ? "1 file" : `${artifacts.length} files`;
      header.append(title, count);
      panel.append(header);

      if (artifacts.length === 0) {
        const empty = document.createElement("div");
        empty.className = "artifact-empty";
        empty.textContent = "Artifacts created by the agent will appear here.";
        panel.append(empty);
        artifactsSidebarEl.replaceChildren(panel);
        return;
      }

      const list = document.createElement("div");
      list.className = "artifact-list";
      for (const artifact of [...artifacts].reverse()) {
        list.append(renderArtifactCard(artifact));
      }
      panel.append(list);
      artifactsSidebarEl.replaceChildren(panel);
    }

    function renderArtifactCard(artifact) {
      const card = document.createElement("article");
      card.className = "artifact-card";

      const name = document.createElement("div");
      name.className = "artifact-name";
      name.textContent = artifact.name || artifact.artifact_id;

      const meta = document.createElement("div");
      meta.className = "artifact-meta";
      meta.textContent = `${artifact.mime_type || "application/octet-stream"} | ${formatBytes(artifact.size_bytes || 0)}`;

      const actions = document.createElement("div");
      actions.className = "artifact-actions";
      actions.append(artifactViewButton(artifact));
      actions.append(artifactLink(artifact.download_url, "Download", true));

      card.append(name, meta, actions);
      return card;
    }

    function artifactViewButton(artifact) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "View";
      button.addEventListener("click", () => {
        openArtifactViewer(artifact).catch((err) => {
          state.artifactViewer.error = String(err);
          state.artifactViewer.loading = false;
          renderArtifactViewer();
        });
      });
      return button;
    }

    function artifactLink(url, label, download) {
      const link = document.createElement("a");
      link.href = url;
      link.textContent = label;
      if (download) {
        link.setAttribute("download", "");
      } else {
        link.target = "_blank";
        link.rel = "noreferrer";
      }
      return link;
    }

    async function openArtifactViewer(artifact) {
      closeArtifactObjectUrl();
      state.artifactViewer = {
        open: true,
        artifact,
        loading: true,
        error: "",
        text: "",
        objectUrl: "",
      };
      renderArtifactViewer();

      if (isImageArtifact(artifact) || isPdfArtifact(artifact)) {
        state.artifactViewer.loading = false;
        renderArtifactViewer();
        return;
      }

      const response = await fetch(artifact.view_url);
      if (!response.ok) throw new Error(await responseErrorText(response));
      state.artifactViewer.text = await response.text();
      state.artifactViewer.loading = false;
      renderArtifactViewer();
    }

    function closeArtifactViewer() {
      closeArtifactObjectUrl();
      state.artifactViewer = {
        open: false,
        artifact: null,
        loading: false,
        error: "",
        text: "",
        objectUrl: "",
      };
      renderArtifactViewer();
    }

    function closeArtifactObjectUrl() {
      if (state.artifactViewer?.objectUrl) {
        URL.revokeObjectURL(state.artifactViewer.objectUrl);
      }
    }

    function renderArtifactViewer() {
      const viewer = state.artifactViewer;
      artifactViewerOverlayEl.hidden = !viewer.open;
      if (!viewer.open || !viewer.artifact) {
        artifactViewerOverlayEl.replaceChildren();
        return;
      }

      const artifact = viewer.artifact;
      const shell = document.createElement("div");
      shell.className = "artifact-viewer";

      const header = document.createElement("div");
      header.className = "artifact-viewer-header";

      const title = document.createElement("div");
      title.className = "artifact-viewer-title";
      const name = document.createElement("div");
      name.className = "artifact-viewer-name";
      name.textContent = artifact.name || artifact.artifact_id;
      const meta = document.createElement("div");
      meta.className = "artifact-viewer-meta";
      meta.textContent = `${artifact.mime_type || "application/octet-stream"} | ${formatBytes(artifact.size_bytes || 0)}`;
      title.append(name, meta);

      const actions = document.createElement("div");
      actions.className = "artifact-viewer-actions";
      actions.append(artifactLink(artifact.download_url, "Download", true));
      const close = document.createElement("button");
      close.type = "button";
      close.textContent = "Close";
      close.addEventListener("click", closeArtifactViewer);
      actions.append(close);

      header.append(title, actions);
      shell.append(header);

      const body = document.createElement("div");
      body.className = "artifact-viewer-body";
      renderArtifactViewerBody(body, viewer);
      shell.append(body);

      artifactViewerOverlayEl.replaceChildren(shell);
    }

    function renderArtifactViewerBody(body, viewer) {
      const artifact = viewer.artifact;
      if (viewer.loading) {
        const loading = document.createElement("div");
        loading.className = "empty";
        loading.textContent = "Loading artifact...";
        body.append(loading);
        return;
      }
      if (viewer.error) {
        const error = document.createElement("div");
        error.className = "artifact-viewer-error";
        error.textContent = viewer.error;
        body.append(error);
        return;
      }
      if (isImageArtifact(artifact)) {
        const image = document.createElement("img");
        image.className = "artifact-viewer-image";
        image.src = artifact.view_url;
        image.alt = artifact.name || "Artifact";
        body.append(image);
        return;
      }
      if (isPdfArtifact(artifact)) {
        const frame = document.createElement("iframe");
        frame.className = "artifact-viewer-frame";
        frame.src = artifact.view_url;
        body.append(frame);
        return;
      }

      const content = document.createElement("div");
      content.className = "bubble-content";
      if (isMarkdownArtifact(artifact)) {
        content.classList.add("artifact-markdown");
        renderFormattedContent(content, viewer.text);
      } else {
        content.append(createCodeBlock(viewer.text, languageFromFileName(artifact.name)));
      }
      body.append(content);
    }

    function isImageArtifact(artifact) {
      const mimeType = artifact?.mime_type || "";
      return mimeType.startsWith("image/") && mimeType !== "image/svg+xml";
    }

    function isPdfArtifact(artifact) {
      return artifact?.mime_type === "application/pdf";
    }

    function isMarkdownArtifact(artifact) {
      const mimeType = String(artifact?.mime_type || "").toLowerCase();
      const name = String(artifact?.name || artifact?.artifact_id || "").toLowerCase();
      return (
        mimeType === "text/markdown" ||
        mimeType === "text/x-markdown" ||
        name.endsWith(".md") ||
        name.endsWith(".markdown")
      );
    }

    function formatBytes(size) {
      if (!Number.isFinite(size)) return "0 B";
      if (size < 1024) return `${size} B`;
      if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
      return `${(size / 1024 / 1024).toFixed(1)} MB`;
    }

    function languageFromFileName(name) {
      const extension = String(name || "").split(".").pop()?.toLowerCase();
      const languages = {
        bash: "bash",
        css: "css",
        html: "html",
        js: "javascript",
        json: "json",
        md: "markdown",
        markdown: "markdown",
        py: "python",
        sh: "bash",
        sql: "sql",
        ts: "typescript",
        xml: "xml",
        yaml: "yaml",
        yml: "yaml",
      };
      return languages[extension] || null;
    }

    function bubble(kind, label, content) {
      const node = document.createElement("div");
      node.className = `bubble ${kind}`;
      const labelNode = document.createElement("span");
      labelNode.className = "label";
      labelNode.textContent = label;
      const contentNode = document.createElement("div");
      contentNode.className = "bubble-content";
      renderFormattedContent(contentNode, content);
      node.append(labelNode, contentNode);
      return node;
    }

    function renderStreamPanel() {
      const turn = state.streamTurn;
      if (!turn) return null;
      if (!turn.text && !turn.thinking && turn.currentEvents.length === 0 && turn.finishedTurns.length === 0) return null;

      const collapsed = state.streamPanelCollapsed;
      const node = document.createElement("section");
      node.className = `stream-panel ${turn.status}${collapsed ? " collapsed" : ""}`;

      const header = document.createElement("div");
      header.className = "stream-panel-header";

      const title = document.createElement("div");
      title.className = "stream-panel-title";
      title.textContent = "Streaming visibility";
      const status = document.createElement("span");
      status.className = "stream-panel-status";
      status.textContent = streamPanelStatus(turn);
      title.append(status);

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "stream-panel-toggle";
      toggle.textContent = collapsed ? "Expand" : "Collapse";
      toggle.addEventListener("click", () => {
        state.streamPanelCollapsed = !state.streamPanelCollapsed;
        render();
      });

      header.append(title, toggle);
      node.append(header);

      const body = document.createElement("div");
      body.className = "stream-panel-body";

      if (collapsed) {
        const preview = document.createElement("div");
        preview.className = "stream-preview";
        preview.textContent = streamPanelPreview(turn);
        body.append(preview);
        node.append(body);
        return node;
      }

      if (turn.finishedTurns.length) {
        const finishedList = document.createElement("div");
        finishedList.className = "stream-finished-list";
        for (const finishedTurn of turn.finishedTurns) {
          finishedList.append(renderFinishedStreamTurn(finishedTurn));
        }
        body.append(finishedList);
      }

      if (turn.text) {
        const currentTurn = document.createElement("div");
        currentTurn.className = "stream-current-turn";

        const title = document.createElement("div");
        title.className = "stream-finished-title";
        title.textContent = `Claude turn ${turn.activeSequence ?? ""} streaming`;
        currentTurn.append(title);

        if (turn.thinking) {
          const thinking = document.createElement("div");
          thinking.className = "stream-thinking";
          thinking.textContent = turn.thinking;
          currentTurn.append(thinking);
        }

        const text = document.createElement("div");
        text.className = "stream-text";
        text.textContent = turn.text;
        currentTurn.append(text);

        if (turn.currentEvents.length) {
          currentTurn.append(renderStreamToolList(turn.currentEvents));
        }

        body.append(currentTurn);
      }

      if (!turn.text && turn.thinking) {
        const currentThinking = document.createElement("div");
        currentThinking.className = "stream-current-turn";
        const title = document.createElement("div");
        title.className = "stream-finished-title";
        title.textContent = `Claude turn ${turn.activeSequence ?? ""} thinking`;
        const text = document.createElement("div");
        text.className = "stream-thinking";
        text.textContent = turn.thinking;
        currentThinking.append(title, text);
        body.append(currentThinking);
      }

      if (!turn.text && turn.currentEvents.length) {
        body.append(renderStreamToolList(turn.currentEvents));
      }

      if (!turn.text && !turn.thinking && !turn.currentEvents.length && !turn.finishedTurns.length) {
        const preview = document.createElement("div");
        preview.className = "stream-preview";
        preview.textContent = "Waiting for streamed tokens or tool activity...";
        body.append(preview);
      }

      node.append(body);
      return node;
    }

    function renderFinishedStreamTurn(finishedTurn) {
      const node = document.createElement("div");
      node.className = "stream-finished-turn";
      const title = document.createElement("div");
      title.className = "stream-finished-title";
      title.textContent = `Claude turn ${finishedTurn.sequence ?? ""} complete | ${finishedTurn.stopReason}`;
      if (finishedTurn.thinking) {
        const thinking = document.createElement("div");
        thinking.className = "stream-thinking";
        thinking.textContent = finishedTurn.thinking;
        node.append(title, thinking);
      } else {
        node.append(title);
      }
      const text = document.createElement("div");
      text.textContent = finishedTurn.text || `Completed without text (${finishedTurn.stopReason}).`;
      node.append(text);
      if (finishedTurn.events?.length) {
        node.append(renderStreamToolList(finishedTurn.events));
      }
      return node;
    }

    function renderStreamToolList(events) {
      const toolList = document.createElement("div");
      toolList.className = "stream-tool-list";
      for (const event of events.slice(-5)) {
        toolList.append(renderStreamToolEvent(event));
      }
      return toolList;
    }

    function renderStreamToolEvent(event) {
      const node = document.createElement("div");
      node.className = "stream-tool-event";
      if (event.kind?.startsWith("claude_tool_input_")) {
        node.classList.add("input-streaming");
      }

      const name = document.createElement("div");
      name.className = "stream-tool-name";
      name.textContent = streamToolLabel(event);
      node.append(name);

      const payload = document.createElement("div");
      payload.className = "stream-tool-payload";
      payload.textContent = streamToolPayloadText(event);
      node.append(payload);

      return node;
    }

    function streamToolPayloadText(event) {
      const payload = event.payload || {};
      if (event.kind?.startsWith("claude_tool_input_")) {
        const status = payload.status || "building input";
        if (event.kind === "claude_tool_input_complete") {
          return `${status}:\n${truncateStreamText(formatStreamValue(payload.input ?? payload.input_partial ?? payload.input_preview))}`;
        }
        const partial = payload.input_partial || payload.partial_json || "";
        return `${status}:\n${truncateStreamText(String(partial))}`;
      }

      return `${event.kind}: ${truncateStreamText(formatStreamValue(payload))}`;
    }

    function streamPanelStatus(turn) {
      const count = turn.currentEvents.length + turn.finishedTurns.reduce(
        (total, finishedTurn) => total + (finishedTurn.events?.length || 0),
        0,
      );
      const toolText = count === 1 ? "1 tool event" : `${count} tool events`;
      const turnCount = turn.finishedTurns.length;
      const turnText = turnCount === 1 ? "1 Claude turn" : `${turnCount} Claude turns`;
      if (turn.status === "interrupted") return `interrupted | ${toolText}`;
      if (turn.status === "complete") return `complete | ${turnText} | ${toolText}`;
      if (turn.status === "tooling") return `tool activity | ${turnText} | ${toolText}`;
      if (turn.status === "waiting") return `finalizing | ${turnText} | ${toolText}`;
      return `streaming | ${turnText} | ${toolText}`;
    }

    function streamPanelPreview(turn) {
      const text = turn.text.trim();
      const thinking = String(turn.thinking || "").trim();
      const latestEvent = turn.currentEvents[turn.currentEvents.length - 1];
      if (text) return text.replace(/\s+/g, " ").slice(-240);
      if (thinking) return thinking.replace(/\s+/g, " ").slice(-240);
      const latestFinished = turn.finishedTurns[turn.finishedTurns.length - 1];
      if (latestFinished?.text) {
        return latestFinished.text.replace(/\s+/g, " ").slice(-240);
      }
      if (latestFinished?.thinking) {
        return latestFinished.thinking.replace(/\s+/g, " ").slice(-240);
      }
      if (latestEvent) {
        return `${streamToolLabel(latestEvent)} | ${latestEvent.kind}`;
      }
      return streamPanelStatus(turn);
    }

    function streamToolLabel(event) {
      const payloadToolName = event.payload?.tool_name;
      const name = payloadToolName || event.tool_name || "stream";
      return event.step ? `${name}:${event.step}` : name;
    }

    function formatStreamValue(value) {
      if (typeof value === "string") return value;
      try {
        return JSON.stringify(value, null, 2);
      } catch (_err) {
        return String(value);
      }
    }

    function truncateStreamText(value) {
      const text = String(value || "");
      if (text.length <= 4000) return text;
      return text.slice(-4000);
    }

    function renderFormattedContent(container, content) {
      const lines = String(content || "").replace(/\r\n/g, "\n").split("\n");
      let paragraphLines = [];
      let listNode = null;
      let listType = null;
      let codeLines = null;
      let codeLanguage = null;

      function flushParagraph() {
        if (paragraphLines.length === 0) return;
        const paragraph = document.createElement("p");
        paragraphLines.forEach((line, index) => {
          if (index > 0) paragraph.append(document.createElement("br"));
          renderInline(paragraph, line);
        });
        container.append(paragraph);
        paragraphLines = [];
      }

      function flushList() {
        if (!listNode) return;
        container.append(listNode);
        listNode = null;
        listType = null;
      }

      function flushCode() {
        if (codeLines === null) return;
        const source = codeLines.join("\n");
        container.append(createCodeBlock(source, codeLanguage));
        codeLines = null;
        codeLanguage = null;
      }

      for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
        const line = lines[lineIndex];
        const fence = line.trim().match(/^```(?:\s*([A-Za-z0-9_+.#-]+))?.*$/);
        if (fence) {
          if (codeLines === null) {
            flushParagraph();
            flushList();
            codeLines = [];
            codeLanguage = fence[1] || null;
          } else {
            flushCode();
          }
          continue;
        }

        if (codeLines !== null) {
          codeLines.push(line);
          continue;
        }

        if (isMarkdownTableAt(lines, lineIndex)) {
          flushParagraph();
          flushList();
          lineIndex = renderMarkdownTable(container, lines, lineIndex) - 1;
          continue;
        }

        if (line.trim() === "") {
          flushParagraph();
          flushList();
          continue;
        }

        if (/^\s*-{3,}\s*$/.test(line)) {
          flushParagraph();
          flushList();
          container.append(document.createElement("hr"));
          continue;
        }

        const heading = line.match(/^(#{1,4})\s+(.+)$/);
        if (heading) {
          flushParagraph();
          flushList();
          const headingNode = document.createElement("div");
          headingNode.className = "md-heading";
          renderInline(headingNode, heading[2]);
          container.append(headingNode);
          continue;
        }

        if (/^\s*>\s?/.test(line)) {
          flushParagraph();
          flushList();
          const quoteLines = [];
          let quoteIndex = lineIndex;
          while (quoteIndex < lines.length && /^\s*>\s?/.test(lines[quoteIndex])) {
            quoteLines.push(lines[quoteIndex].replace(/^\s*>\s?/, ""));
            quoteIndex += 1;
          }
          const quote = document.createElement("blockquote");
          renderFormattedContent(quote, quoteLines.join("\n"));
          container.append(quote);
          lineIndex = quoteIndex - 1;
          continue;
        }

        const unordered = line.match(/^\s*[-*]\s+(.+)$/);
        const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
        if (unordered || ordered) {
          flushParagraph();
          const nextType = unordered ? "ul" : "ol";
          if (!listNode || listType !== nextType) {
            flushList();
            listNode = document.createElement(nextType);
            listType = nextType;
          }
          const item = document.createElement("li");
          renderInline(item, unordered ? unordered[1] : ordered[1]);
          listNode.append(item);
          continue;
        }

        flushList();
        paragraphLines.push(line);
      }

      flushParagraph();
      flushList();
      flushCode();
    }

    function isMarkdownTableAt(lines, index) {
      const header = lines[index] || "";
      const separator = lines[index + 1] || "";
      return header.includes("|") && isMarkdownTableSeparator(separator);
    }

    function isMarkdownTableSeparator(line) {
      const cells = splitMarkdownTableRow(line);
      return (
        cells.length >= 2 &&
        cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()))
      );
    }

    function renderMarkdownTable(container, lines, startIndex) {
      const tableWrap = document.createElement("div");
      tableWrap.className = "markdown-table-wrap";
      const table = document.createElement("table");
      const head = document.createElement("thead");
      const body = document.createElement("tbody");

      const headerRow = document.createElement("tr");
      for (const cell of splitMarkdownTableRow(lines[startIndex])) {
        const th = document.createElement("th");
        renderInline(th, cell.trim());
        headerRow.append(th);
      }
      head.append(headerRow);

      let index = startIndex + 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
        const row = document.createElement("tr");
        for (const cell of splitMarkdownTableRow(lines[index])) {
          const td = document.createElement("td");
          renderInline(td, cell.trim());
          row.append(td);
        }
        body.append(row);
        index += 1;
      }

      table.append(head, body);
      tableWrap.append(table);
      container.append(tableWrap);
      return index;
    }

    function splitMarkdownTableRow(line) {
      let value = String(line || "").trim();
      if (value.startsWith("|")) value = value.slice(1);
      if (value.endsWith("|")) value = value.slice(0, -1);
      return value.split("|").map((cell) => cell.trim());
    }

    function createCodeBlock(source, languageHint = null) {
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      const language = normalizeCodeLanguage(languageHint) || inferCodeLanguage(source);
      if (language) {
        pre.dataset.language = language;
        code.className = `language-${language}`;
        renderHighlightedCode(code, source, language);
      } else {
        code.textContent = source;
      }
      pre.append(code);
      return pre;
    }

    function renderInline(parent, text) {
      const pattern = /(\[[^\]]+\]\(https?:\/\/[^)\s]+\)|`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g;
      let index = 0;
      for (const match of text.matchAll(pattern)) {
        if (match.index > index) {
          parent.append(document.createTextNode(text.slice(index, match.index)));
        }
        const token = match[0];
        if (token.startsWith("[") && token.includes("](")) {
          const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
          if (link) {
            const anchor = document.createElement("a");
            anchor.href = link[2];
            anchor.target = "_blank";
            anchor.rel = "noreferrer";
            anchor.textContent = link[1];
            parent.append(anchor);
          } else {
            parent.append(document.createTextNode(token));
          }
        } else if (token.startsWith("`")) {
          const code = document.createElement("code");
          code.textContent = token.slice(1, -1);
          parent.append(code);
        } else if (token.startsWith("**")) {
          const strong = document.createElement("strong");
          strong.textContent = token.slice(2, -2);
          parent.append(strong);
        } else {
          const emphasis = document.createElement("em");
          emphasis.textContent = token.slice(1, -1);
          parent.append(emphasis);
        }
        index = match.index + token.length;
      }
      if (index < text.length) {
        parent.append(document.createTextNode(text.slice(index)));
      }
    }

    function renderHighlightedCode(parent, source, language) {
      const rules = highlightRules(language);
      let index = 0;

      while (index < source.length) {
        const chunk = source.slice(index);
        let matched = false;

        for (const [className, rule] of rules) {
          const match = chunk.match(rule);
          if (!match) continue;

          const text = match[0];
          if (!text) continue;

          if (className === null) {
            parent.append(document.createTextNode(text));
          } else {
            const span = document.createElement("span");
            span.className = className;
            span.textContent = text;
            parent.append(span);
          }
          index += text.length;
          matched = true;
          break;
        }

        if (!matched) {
          parent.append(document.createTextNode(source[index]));
          index += 1;
        }
      }
    }

    function highlightRules(language) {
      const common = [
        [null, /^\s+/],
        ["hl-number", /^\b(?:0x[\da-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b/],
        ["hl-operator", /^[{}()[\].,;:+\-*/%<>=!&|^~?]+/],
      ];

      if (language === "python") {
        return [
          [null, /^\s+/],
          ["hl-comment", /^#[^\n]*/],
          ["hl-string", /^(?:(?:[rubfRUBF]{0,3})(?:"{3}[\s\S]*?"{3}|'{3}[\s\S]*?'{3}|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'))/],
          ["hl-keyword", wordRule("and|as|assert|async|await|break|class|continue|def|del|elif|else|except|False|finally|for|from|global|if|import|in|is|lambda|None|nonlocal|not|or|pass|raise|return|True|try|while|with|yield")],
          ["hl-function", wordRule("abs|all|any|bool|dict|enumerate|filter|float|int|len|list|map|max|min|open|print|range|set|str|sum|super|tuple|zip")],
          ...common.slice(1),
        ];
      }

      if (language === "javascript" || language === "typescript") {
        return [
          [null, /^\s+/],
          ["hl-comment", /^\/\/[^\n]*/],
          ["hl-comment", /^\/\*[\s\S]*?\*\//],
          ["hl-string", /^`(?:\\.|[^`\\])*`/],
          ["hl-string", /^"(?:\\.|[^"\\])*"/],
          ["hl-string", /^'(?:\\.|[^'\\])*'/],
          ["hl-keyword", wordRule("async|await|break|case|catch|class|const|continue|debugger|default|delete|do|else|export|extends|false|finally|for|from|function|if|import|in|instanceof|let|new|null|of|return|static|super|switch|this|throw|true|try|typeof|undefined|var|void|while|with|yield")],
          ["hl-type", wordRule("interface|type|implements|private|protected|public|readonly|enum|namespace|abstract|declare")],
          ["hl-function", /^[A-Za-z_$][\w$]*(?=\s*\()/],
          ...common.slice(1),
        ];
      }

      if (language === "json") {
        return [
          [null, /^\s+/],
          ["hl-property", /^"(?:\\.|[^"\\])*"(?=\s*:)/],
          ["hl-string", /^"(?:\\.|[^"\\])*"/],
          ["hl-keyword", wordRule("true|false|null")],
          ...common.slice(1),
        ];
      }

      if (language === "bash") {
        return [
          [null, /^\s+/],
          ["hl-comment", /^#[^\n]*/],
          ["hl-string", /^"(?:\\.|[^"\\])*"/],
          ["hl-string", /^'(?:\\.|[^'\\])*'/],
          ["hl-property", /^\$[A-Za-z_][\w]*/],
          ["hl-keyword", wordRule("alias|case|do|done|elif|else|esac|export|fi|for|function|if|in|local|readonly|return|set|shift|source|then|unalias|unset|while")],
          ["hl-function", /^[A-Za-z_][\w.-]*(?=\s)/],
          ...common.slice(1),
        ];
      }

      if (language === "sql") {
        return [
          [null, /^\s+/],
          ["hl-comment", /^--[^\n]*/],
          ["hl-comment", /^\/\*[\s\S]*?\*\//],
          ["hl-string", /^'(?:''|[^'])*'/],
          ["hl-keyword", wordRule("alter|and|as|avg|by|case|count|create|delete|desc|distinct|drop|else|end|from|group|having|in|inner|insert|into|is|join|left|limit|max|min|not|null|offset|on|or|order|outer|right|select|set|sum|table|then|update|values|view|when|where", "i")],
          ...common.slice(1),
        ];
      }

      if (language === "html" || language === "xml") {
        return [
          [null, /^\s+/],
          ["hl-comment", /^<!--[\s\S]*?-->/],
          ["hl-tag", /^<!doctype[^>]*>/i],
          ["hl-tag", /^<\/?[A-Za-z][\w:-]*/],
          ["hl-attr", /^[A-Za-z_:][\w:.-]*(?=\=)/],
          ["hl-string", /^"(?:\\.|[^"\\])*"/],
          ["hl-string", /^'(?:\\.|[^'\\])*'/],
          ["hl-operator", /^\/?>/],
          ...common.slice(1),
        ];
      }

      if (language === "css") {
        return [
          [null, /^\s+/],
          ["hl-comment", /^\/\*[\s\S]*?\*\//],
          ["hl-string", /^"(?:\\.|[^"\\])*"/],
          ["hl-string", /^'(?:\\.|[^'\\])*'/],
          ["hl-property", /^--?[A-Za-z_][\w-]*(?=\s*:)/],
          ["hl-keyword", /^@[A-Za-z-]+/],
          ["hl-number", /^\b\d+(?:\.\d+)?(?:px|rem|em|vh|vw|%|s|ms)?\b/],
          ["hl-operator", /^[{}()[\].,;:+\-*/%<>=!&|^~?]+/],
        ];
      }

      if (language === "yaml") {
        return [
          [null, /^\s+/],
          ["hl-comment", /^#[^\n]*/],
          ["hl-property", /^[A-Za-z_][\w.-]*(?=\s*:)/],
          ["hl-string", /^"(?:\\.|[^"\\])*"/],
          ["hl-string", /^'(?:\\.|[^'\\])*'/],
          ["hl-keyword", wordRule("true|false|null|yes|no|on|off")],
          ...common.slice(1),
        ];
      }

      if (language === "markdown") {
        return [
          [null, /^[\s\S]+/],
        ];
      }

      return [
        [null, /^\s+/],
        ["hl-comment", /^#[^\n]*/],
        ["hl-comment", /^\/\/[^\n]*/],
        ["hl-comment", /^\/\*[\s\S]*?\*\//],
        ["hl-string", /^`(?:\\.|[^`\\])*`/],
        ["hl-string", /^"(?:\\.|[^"\\])*"/],
        ["hl-string", /^'(?:\\.|[^'\\])*'/],
        ["hl-function", /^[A-Za-z_$][\w$]*(?=\s*\()/],
        ...common.slice(1),
      ];
    }

    function wordRule(words, flags = "") {
      return new RegExp(`^\\b(?:${words})\\b`, flags);
    }

    function normalizeCodeLanguage(language) {
      if (!language) return null;
      const normalized = language.toLowerCase();
      const aliases = {
        bash: "bash",
        cjs: "javascript",
        css: "css",
        html: "html",
        javascript: "javascript",
        js: "javascript",
        json: "json",
        jsonc: "json",
        jsx: "javascript",
        markdown: "markdown",
        md: "markdown",
        mjs: "javascript",
        py: "python",
        python: "python",
        sh: "bash",
        shell: "bash",
        sql: "sql",
        ts: "typescript",
        tsx: "typescript",
        typescript: "typescript",
        xml: "xml",
        yaml: "yaml",
        yml: "yaml",
        zsh: "bash",
      };
      return aliases[normalized] || null;
    }

    function inferCodeLanguage(source) {
      const trimmed = source.trim();
      if (!trimmed) return null;
      if ((trimmed.startsWith("{") || trimmed.startsWith("[")) && looksLikeJson(trimmed)) return "json";
      if (/^\s*(from\s+\w+\s+import|import\s+\w+|def\s+\w+|class\s+\w+|async\s+def\s+\w+)\b/m.test(source)) return "python";
      if (/\b(print|range|len)\s*\(/.test(source) && /(^|\n)\s*#/.test(source)) return "python";
      if (/\b(const|let|function|console\.log|=>|import\s+.+\s+from)\b/.test(source)) return "javascript";
      if (/^#!.*\b(?:bash|sh|zsh)\b/m.test(source) || /\b(?:echo|curl|export|chmod|sudo)\b/.test(source)) return "bash";
      if (/\bselect\b[\s\S]+\bfrom\b/i.test(source)) return "sql";
      if (/^\s*</.test(source) && /<\/?[A-Za-z][\s\S]*>/.test(source)) return "html";
      if (/^[\s\S]*\{[\s\S]*:[\s\S]*\}/.test(source) && /[.#]?[A-Za-z][\w-]*\s*\{/.test(source)) return "css";
      if (/^[A-Za-z_][\w.-]*\s*:/m.test(source)) return "yaml";
      return null;
    }

    function looksLikeJson(source) {
      try {
        JSON.parse(source);
        return true;
      } catch (_err) {
        return false;
      }
    }

    function jsonHeaders() {
      return { "content-type": "application/json" };
    }
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(
        "simple_chat_agent.web:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
