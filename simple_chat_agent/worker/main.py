from __future__ import annotations

import asyncio
import os
from contextlib import suppress

import uvicorn
from temporalio.client import Client
from temporalio.worker import Worker

from agent_harness.guards import run_guard_activity
from agent_harness.mcp import (
    configure_mcp_auth_resolver,
    configure_mcp_http_auth_resolver,
)
from agent_harness.streaming import configure_stream_sink
from agent_harness.tools import run_tool_activity
from agent_harness.providers.claude import call_agent_api
from simple_chat_agent import TASK_QUEUE
from simple_chat_agent.worker.demo_workspace_activities import (
    configure_demo_workspace,
    create_demo_workspace_namespace,
    crash_demo_workspace,
    delete_demo_workspace,
    deploy_demo_workspace_workloads,
    provision_demo_workspace,
    purge_demo_workspace_payloads,
    resolve_demo_workspace_images,
    wait_demo_workspace_deployment,
)
from simple_chat_agent.worker.demo_workspace_workflow import DemoWorkspaceWorkflow
from simple_chat_agent.worker.codec_server import (
    codec_server_enabled,
    codec_server_host,
    codec_server_port,
    codec_server_url,
    create_codec_app,
)
from simple_chat_agent.common.env import load_dotenv
from simple_chat_agent.common.external_storage import simple_chat_data_converter
from simple_chat_agent.common.mcp_auth import resolve_mcp_auth_headers, resolve_mcp_http_auth
from simple_chat_agent.common.streaming import configured_stream_sink
from simple_chat_agent.worker.tools.subagent import SubagentWorkflow
from simple_chat_agent.worker.user_chats_workflow import UserChatsWorkflow
from simple_chat_agent.worker.workflow import SimpleChatWorkflow


async def main() -> None:
    load_dotenv()
    configure_stream_sink(configured_stream_sink())
    configure_mcp_auth_resolver(resolve_mcp_auth_headers)
    configure_mcp_http_auth_resolver(resolve_mcp_http_auth)
    data_converter = simple_chat_data_converter()

    client_config = {
        "namespace": os.environ.get("TEMPORAL_NAMESPACE", "default"),
        "data_converter": data_converter,
        "tls": os.environ.get("TEMPORAL_TLS", "false").lower() in ["true", "1"],
    }
    if os.environ.get("TEMPORAL_API_KEY"):
        client_config["api_key"] = os.environ.get("TEMPORAL_API_KEY")

    client = await Client.connect(
        os.environ.get("TEMPORAL_ENDPOINT", "localhost:7233"), **client_config
    )

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[
            SimpleChatWorkflow,
            UserChatsWorkflow,
            SubagentWorkflow,
            DemoWorkspaceWorkflow,
        ],
        activities=[
            call_agent_api,
            run_tool_activity,
            run_guard_activity,
            provision_demo_workspace,
            resolve_demo_workspace_images,
            create_demo_workspace_namespace,
            configure_demo_workspace,
            deploy_demo_workspace_workloads,
            wait_demo_workspace_deployment,
            crash_demo_workspace,
            delete_demo_workspace,
            purge_demo_workspace_payloads,
        ],
    )
    if not codec_server_enabled():
        await worker.run()
        return

    codec_server = uvicorn.Server(
        uvicorn.Config(
            create_codec_app(data_converter),
            host=codec_server_host(),
            port=codec_server_port(),
            log_level="info",
        )
    )
    print(f"Temporal Web codec server listening on {codec_server_url()}")
    await _run_worker_and_codec_server(worker, codec_server)


async def _run_worker_and_codec_server(
    worker: Worker,
    codec_server: uvicorn.Server,
) -> None:
    tasks = [
        asyncio.create_task(worker.run(), name="temporal-worker"),
        asyncio.create_task(codec_server.serve(), name="codec-server"),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
    for task in pending:
        with suppress(asyncio.CancelledError):
            await task
    for task in done:
        task.result()


if __name__ == "__main__":
    asyncio.run(main())
