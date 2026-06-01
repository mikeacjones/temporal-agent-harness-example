from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from simple_chat_agent.api.auth import (
    DEFAULT_SESSION_SECONDS,
    SESSION_COOKIE,
    AuthenticatedUser,
    AuthError,
    create_session_token,
    user_from_session_token,
)
from simple_chat_agent.worker.demo_workspace_workflow import (
    DemoWorkspaceConfig,
    DemoWorkspaceState,
    DemoWorkspaceWorkflow,
)
from simple_chat_agent.worker.user_chats_workflow import (
    UserChatsWorkflow,
    UserDemoWorkspaceRecord,
)


@dataclass(frozen=True)
class DemoWorkspaceRouteDeps:
    current_user: Callable[[Request], AuthenticatedUser]
    workflow_handle: Callable[[str], Any]
    ensure_demo_workspace_workflow: Callable[..., Any]
    ensure_user_chats_workflow: Callable[..., Any]
    demo_workspace_parent_workflow: Callable[[], Any | None]
    demo_workspaces_enabled: Callable[[], bool]
    demo_workspace_mode: Callable[[], bool]
    demo_workspace_config: Callable[[AuthenticatedUser], DemoWorkspaceConfig]
    is_demo_workspace_absent: Callable[[BaseException], bool]


def create_demo_workspace_router(deps: DemoWorkspaceRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/demo-workspace")
    async def get_demo_workspace(request: Request) -> dict[str, Any]:
        user = deps.current_user(request)
        if deps.demo_workspace_mode():
            state = await _parent_workspace_state(deps)
            return {
                "enabled": False,
                "in_demo_workspace": True,
                "workspace": asdict(state) if state is not None else None,
            }
        if not deps.demo_workspaces_enabled():
            return {
                "enabled": False,
                "in_demo_workspace": False,
                "workspace": None,
            }

        state = await _user_workspace_state(deps, user)
        return _response_body(user, state, enabled=True)

    @router.post("/api/demo-workspace")
    async def ensure_demo_workspace(request: Request) -> dict[str, Any]:
        user = deps.current_user(request)
        _require_enabled(deps)
        handle = await deps.ensure_demo_workspace_workflow(user)
        registry = await deps.ensure_user_chats_workflow(user.user_id, user.username)
        await registry.execute_update(
            UserChatsWorkflow.set_demo_workspace,
            UserDemoWorkspaceRecord(
                control_workflow_id=handle.id,
                status="provisioning",
            ),
        )
        state: DemoWorkspaceState = await handle.execute_update(
            DemoWorkspaceWorkflow.ensure_workspace,
            deps.demo_workspace_config(user),
        )
        await registry.execute_update(
            UserChatsWorkflow.set_demo_workspace,
            _record_from_state(handle.id, state),
        )
        return _response_body(user, state, enabled=True)

    @router.post("/api/demo-workspace/crash")
    async def crash_demo_workspace(request: Request) -> dict[str, Any]:
        user = deps.current_user(request)
        if deps.demo_workspace_mode():
            handle = deps.demo_workspace_parent_workflow()
            if handle is None:
                raise HTTPException(
                    status_code=403,
                    detail="Demo workspace controller is not configured.",
                )
            state: DemoWorkspaceState = await handle.execute_update(
                DemoWorkspaceWorkflow.crash_workspace
            )
            return _demo_workspace_response_body(state)

        _require_enabled(deps)
        registry = await deps.ensure_user_chats_workflow(user.user_id, user.username)
        record: UserDemoWorkspaceRecord | None = await registry.query(
            UserChatsWorkflow.demo_workspace
        )
        if record is None:
            return _response_body(user, None, enabled=True)
        handle = deps.workflow_handle(record.control_workflow_id)
        try:
            state: DemoWorkspaceState | None = await handle.execute_update(
                DemoWorkspaceWorkflow.crash_workspace
            )
        except Exception as err:
            if not deps.is_demo_workspace_absent(err):
                raise
            await registry.execute_update(UserChatsWorkflow.clear_demo_workspace)
            state = None
        if state is not None:
            await registry.execute_update(
                UserChatsWorkflow.set_demo_workspace,
                _record_from_state(record.control_workflow_id, state),
            )
        return _response_body(user, state, enabled=True)

    @router.delete("/api/demo-workspace")
    async def delete_demo_workspace(request: Request) -> dict[str, Any]:
        user = deps.current_user(request)
        _require_enabled(deps)
        registry = await deps.ensure_user_chats_workflow(user.user_id, user.username)
        record: UserDemoWorkspaceRecord | None = await registry.query(
            UserChatsWorkflow.demo_workspace
        )
        if record is None:
            return _response_body(user, None, enabled=True)
        handle = deps.workflow_handle(record.control_workflow_id)
        try:
            state: DemoWorkspaceState | None = await handle.execute_update(
                DemoWorkspaceWorkflow.delete_workspace
            )
        except Exception as err:
            if not deps.is_demo_workspace_absent(err):
                raise
            state = None
        await registry.execute_update(UserChatsWorkflow.clear_demo_workspace)
        return _response_body(user, state, enabled=True)

    @router.get("/api/demo-workspace/login")
    async def demo_workspace_login(token: str = Query(...)) -> RedirectResponse:
        try:
            user = user_from_session_token(token)
        except AuthError as err:
            raise HTTPException(status_code=401, detail=str(err)) from err

        response = RedirectResponse("/")
        response.set_cookie(
            SESSION_COOKIE,
            create_session_token(user),
            max_age=DEFAULT_SESSION_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return response

    return router


async def _parent_workspace_state(
    deps: DemoWorkspaceRouteDeps,
) -> DemoWorkspaceState | None:
    handle = deps.demo_workspace_parent_workflow()
    if handle is None:
        return None
    try:
        return await handle.query(DemoWorkspaceWorkflow.state)
    except Exception:
        return None


async def _user_workspace_state(
    deps: DemoWorkspaceRouteDeps,
    user: AuthenticatedUser,
) -> DemoWorkspaceState | None:
    registry = await deps.ensure_user_chats_workflow(user.user_id, user.username)
    record: UserDemoWorkspaceRecord | None = await registry.query(
        UserChatsWorkflow.demo_workspace
    )
    if record is None:
        return None
    handle = deps.workflow_handle(record.control_workflow_id)
    try:
        state: DemoWorkspaceState = await handle.query(DemoWorkspaceWorkflow.state)
        latest_record = _record_from_state(record.control_workflow_id, state)
        if latest_record != record:
            await registry.execute_update(
                UserChatsWorkflow.set_demo_workspace,
                latest_record,
            )
        return state
    except Exception as err:
        if deps.is_demo_workspace_absent(err):
            await registry.execute_update(UserChatsWorkflow.clear_demo_workspace)
            return None
        raise


def _require_enabled(deps: DemoWorkspaceRouteDeps) -> None:
    if deps.demo_workspace_mode() or not deps.demo_workspaces_enabled():
        raise HTTPException(status_code=403, detail="Demo workspaces are disabled.")


def _response_body(
    user: AuthenticatedUser,
    state: DemoWorkspaceState | None,
    *,
    enabled: bool,
) -> dict[str, Any]:
    body = asdict(state) if state is not None else None
    login_url = ""
    if state is not None and state.url and state.status == "active":
        token = quote(create_session_token(user), safe="")
        login_url = f"{state.url}/api/demo-workspace/login?token={token}"
    return {
        "enabled": enabled,
        "in_demo_workspace": False,
        "workspace": body,
        "login_url": login_url,
    }


def _demo_workspace_response_body(state: DemoWorkspaceState) -> dict[str, Any]:
    return {
        "enabled": False,
        "in_demo_workspace": True,
        "workspace": asdict(state),
        "login_url": "",
    }


def _record_from_state(
    control_workflow_id: str,
    state: DemoWorkspaceState,
) -> UserDemoWorkspaceRecord:
    return UserDemoWorkspaceRecord(
        control_workflow_id=control_workflow_id,
        status=state.status,
        workspace_id=state.workspace_id,
        namespace=state.namespace,
        host=state.host,
        url=state.url,
        task_queue=state.task_queue,
        updated_at=state.updated_at,
    )
