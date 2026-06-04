from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from temporalio import workflow
from temporalio.common import (
    RetryPolicy,
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
)

with workflow.unsafe.imports_passed_through():
    from simple_chat_agent.worker.workflow import SimpleChatWorkflow


DEMO_WORKSPACE_PREFIX = "simple-chat-demo-workspace-"
IDLE_TIMEOUT = timedelta(hours=1)
DemoWorkspaceStatus = Literal[
    "inactive",
    "provisioning",
    "active",
    "deleting",
    "deleted",
    "failed",
]


@dataclass
class DemoWorkspaceInput:
    user_id: str
    user_email: str = ""
    search_attr_name: str = ""
    state: "DemoWorkspaceState | None" = None


@dataclass
class DemoWorkspaceConfig:
    user_id: str
    user_email: str
    control_workflow_id: str
    temporal_namespace: str
    source_namespace: str
    source_secret_name: str
    tls_secret_name: str
    host_suffix: str
    parent_public_url: str
    task_queue_prefix: str
    workflow_prefix_prefix: str
    source_web_deployment: str
    source_api_deployment: str
    source_worker_deployment: str
    service_account_role_arn: str = ""
    search_attr_name: str = ""


@dataclass
class WorkspaceChatRecord:
    workflow_id: str
    run_id: str = ""
    task_queue: str = ""
    created_at: str = ""


@dataclass
class DemoWorkspaceState:
    user_id: str
    user_email: str
    status: DemoWorkspaceStatus = "inactive"
    workspace_id: str = ""
    namespace: str = ""
    temporal_namespace: str = ""
    host: str = ""
    url: str = ""
    task_queue: str = ""
    workflow_prefix: str = ""
    registry_workflow_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_activity_at: str = ""
    error: str = ""
    provisioning_step: str = ""
    provisioning_message: str = ""
    chats: list[WorkspaceChatRecord] = field(default_factory=list)


def demo_workspace_workflow_id(user_id: str) -> str:
    import os

    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    prefix = os.environ.get("SIMPLE_CHAT_WORKFLOW_PREFIX", "")
    return f"{prefix}{DEMO_WORKSPACE_PREFIX}{digest}"


def demo_workspace_search_attributes(
    *,
    search_attr_name: str,
    user_email: str,
) -> TypedSearchAttributes | None:
    if not search_attr_name or not user_email:
        return None
    key = SearchAttributeKey.for_keyword(search_attr_name)
    return TypedSearchAttributes([SearchAttributePair(key, user_email)])


@workflow.defn
class DemoWorkspaceWorkflow:
    def __init__(self) -> None:
        self._state = DemoWorkspaceState(user_id="", user_email="")
        self._finished = False
        self._activity_version = 0

    @workflow.run
    async def run(self, request: DemoWorkspaceInput) -> None:
        self._state = request.state or DemoWorkspaceState(
            user_id=request.user_id,
            user_email=request.user_email,
        )
        self._finished = self._state.status == "deleted"

        while not self._finished:
            if workflow.info().is_continue_as_new_suggested() and workflow.all_handlers_finished():
                workflow.continue_as_new(
                    DemoWorkspaceInput(
                        user_id=self._state.user_id,
                        user_email=self._state.user_email,
                        search_attr_name=request.search_attr_name,
                        state=self._state,
                    ),
                    initial_versioning_behavior=(
                        workflow.ContinueAsNewVersioningBehavior.AUTO_UPGRADE
                    ),
                )
            if self._state.status != "active":
                await workflow.wait_condition(
                    lambda: self._state.status == "active"
                    or self._finished
                    or workflow.info().is_continue_as_new_suggested()
                )
                continue

            activity_version = self._activity_version
            try:
                await workflow.wait_condition(
                    lambda: self._state.status != "active"
                    or self._finished
                    or workflow.info().is_continue_as_new_suggested()
                    or self._activity_version != activity_version,
                    timeout=self._idle_timeout(),
                )
            except asyncio.TimeoutError:
                pass
            if self._state.status != "active" or self._finished:
                continue
            if self._idle_expired() and workflow.all_handlers_finished():
                await self._delete_current_workspace()

    @workflow.update
    async def ensure_workspace(self, config: DemoWorkspaceConfig) -> DemoWorkspaceState:
        if self._state.status in {"active", "provisioning"}:
            self._touch()
            return self._state

        now = workflow.now().isoformat()
        workspace_id = _workspace_id(config.user_id)
        namespace = f"agent-harness-demo-{workspace_id}"
        task_queue = f"{config.task_queue_prefix}-{workspace_id}"
        workflow_prefix = f"{config.workflow_prefix_prefix}{workspace_id}-"
        registry_workflow_id = _registry_workflow_id(
            user_id=config.user_id,
            workflow_prefix=workflow_prefix,
        )
        host = f"{workspace_id}{config.host_suffix}"

        self._state = DemoWorkspaceState(
            user_id=config.user_id,
            user_email=config.user_email,
            status="provisioning",
            workspace_id=workspace_id,
            namespace=namespace,
            temporal_namespace=config.temporal_namespace,
            host=host,
            url=f"https://{host}",
            task_queue=task_queue,
            workflow_prefix=workflow_prefix,
            registry_workflow_id=registry_workflow_id,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
            chats=[],
        )

        try:
            self._set_provisioning_step(
                "resolving-images",
                "Resolving deployment images...",
            )
            source_images = await workflow.execute_activity(
                "resolve_demo_workspace_images",
                _provision_request(config, self._state),
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self._set_provisioning_step(
                "creating-namespace",
                "Creating Kubernetes namespace...",
            )
            await workflow.execute_activity(
                "create_demo_workspace_namespace",
                _provision_request(config, self._state),
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self._set_provisioning_step(
                "copying-configuration",
                "Copying configuration and TLS...",
            )
            await workflow.execute_activity(
                "configure_demo_workspace",
                _provision_request(config, self._state),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self._set_provisioning_step(
                "starting-pods",
                "Creating services, routes, and deployments...",
            )
            await workflow.execute_activity(
                "deploy_demo_workspace_workloads",
                args=[_provision_request(config, self._state), source_images],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            for deployment_name, message in [
                ("agent-harness-web", "Waiting for web pod readiness..."),
                ("agent-harness-api", "Waiting for API pod readiness..."),
                ("agent-harness-worker", "Waiting for worker pod readiness..."),
            ]:
                self._set_provisioning_step(
                    f"waiting-{deployment_name.removeprefix('agent-harness-')}",
                    message,
                )
                await workflow.execute_activity(
                    "wait_demo_workspace_deployment",
                    args=[self._state.namespace, deployment_name],
                    start_to_close_timeout=timedelta(minutes=4),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
        except Exception as err:
            self._state.status = "failed"
            self._state.error = str(err)
            self._state.provisioning_message = "Workspace creation failed."
            self._state.updated_at = workflow.now().isoformat()
            raise

        self._state.status = "active"
        self._state.error = ""
        self._state.provisioning_step = "ready"
        self._state.provisioning_message = ""
        self._touch()
        return self._state

    @workflow.update
    async def crash_workspace(self) -> DemoWorkspaceState:
        if self._state.status != "active" or not self._state.namespace:
            return self._state
        await workflow.execute_activity(
            "crash_demo_workspace",
            self._state.namespace,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        self._touch()
        return self._state

    @workflow.update
    async def delete_workspace(self) -> DemoWorkspaceState:
        if self._state.status in {"inactive", "deleting", "deleted"}:
            return self._state
        await self._delete_current_workspace()
        return self._state

    async def _delete_current_workspace(self) -> None:
        self._state.status = "deleting"
        self._state.provisioning_step = "deleting"
        self._state.provisioning_message = "Deleting demo workspace..."
        self._touch()

        for chat in list(self._state.chats):
            handle = workflow.get_external_workflow_handle(chat.workflow_id)
            try:
                await handle.signal(SimpleChatWorkflow.delete)
                await handle.cancel()
            except Exception:
                pass

        workflow_ids = [chat.workflow_id for chat in self._state.chats if chat.workflow_id]
        if workflow_ids and self._state.temporal_namespace:
            self._state.provisioning_step = "deleting-payloads"
            self._state.provisioning_message = "Deleting offloaded chat payloads..."
            self._mark_updated()
            await workflow.execute_activity(
                "purge_demo_workspace_payloads",
                args=[self._state.temporal_namespace, workflow_ids],
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        if self._state.registry_workflow_id:
            try:
                await workflow.get_external_workflow_handle(
                    self._state.registry_workflow_id
                ).cancel()
            except Exception:
                pass

        if self._state.namespace:
            self._state.provisioning_step = "deleting-namespace"
            self._state.provisioning_message = "Deleting Kubernetes namespace..."
            self._mark_updated()
            await workflow.execute_activity(
                "delete_demo_workspace",
                self._state.namespace,
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        self._state = DemoWorkspaceState(
            user_id=self._state.user_id,
            user_email=self._state.user_email,
            status="deleted",
            updated_at=workflow.now().isoformat(),
            last_activity_at=workflow.now().isoformat(),
            provisioning_step="deleted",
            provisioning_message="Workspace deleted.",
        )
        self._finished = True

    @workflow.signal
    async def register_chat(self, chat: WorkspaceChatRecord) -> None:
        if not chat.workflow_id:
            return
        created_at = chat.created_at or workflow.now().isoformat()
        existing = [item for item in self._state.chats if item.workflow_id != chat.workflow_id]
        existing.append(
            WorkspaceChatRecord(
                workflow_id=chat.workflow_id,
                run_id=chat.run_id,
                task_queue=chat.task_queue or self._state.task_queue,
                created_at=created_at,
            )
        )
        self._state.chats = existing
        self._touch()

    @workflow.signal
    async def unregister_chat(self, workflow_id: str) -> None:
        self._state.chats = [
            chat for chat in self._state.chats if chat.workflow_id != workflow_id
        ]
        self._touch()

    @workflow.signal
    async def touch_workspace(self) -> None:
        self._touch()

    @workflow.query
    def state(self) -> DemoWorkspaceState:
        return self._state

    def _touch(self) -> None:
        now = workflow.now().isoformat()
        self._state.updated_at = now
        self._state.last_activity_at = now
        self._activity_version += 1

    def _mark_updated(self) -> None:
        self._state.updated_at = workflow.now().isoformat()

    def _set_provisioning_step(self, step: str, message: str) -> None:
        self._state.provisioning_step = step
        self._state.provisioning_message = message
        self._mark_updated()

    def _idle_timeout(self) -> timedelta:
        return IDLE_TIMEOUT

    def _idle_expired(self) -> bool:
        if self._state.status != "active":
            return False
        try:
            last_activity = datetime.fromisoformat(self._state.last_activity_at)
        except ValueError:
            return False
        return workflow.now() >= last_activity + IDLE_TIMEOUT


@dataclass
class ProvisionDemoWorkspaceRequest:
    namespace: str
    temporal_namespace: str
    host: str
    url: str
    task_queue: str
    workflow_prefix: str
    control_workflow_id: str
    parent_public_url: str
    source_namespace: str
    source_secret_name: str
    tls_secret_name: str
    source_web_deployment: str
    source_api_deployment: str
    source_worker_deployment: str
    user_email: str
    service_account_role_arn: str = ""
    search_attr_name: str = ""


def _provision_request(
    config: DemoWorkspaceConfig,
    state: DemoWorkspaceState,
) -> ProvisionDemoWorkspaceRequest:
    return ProvisionDemoWorkspaceRequest(
        namespace=state.namespace,
        temporal_namespace=state.temporal_namespace,
        host=state.host,
        url=state.url,
        task_queue=state.task_queue,
        workflow_prefix=state.workflow_prefix,
        control_workflow_id=config.control_workflow_id,
        parent_public_url=config.parent_public_url,
        source_namespace=config.source_namespace,
        source_secret_name=config.source_secret_name,
        tls_secret_name=config.tls_secret_name,
        source_web_deployment=config.source_web_deployment,
        source_api_deployment=config.source_api_deployment,
        source_worker_deployment=config.source_worker_deployment,
        user_email=config.user_email,
        service_account_role_arn=config.service_account_role_arn,
        search_attr_name=config.search_attr_name,
    )


def _workspace_id(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return f"temp-{digest[:8]}"


def _registry_workflow_id(*, user_id: str, workflow_prefix: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return f"{workflow_prefix}simple-chat-user-{digest}"
