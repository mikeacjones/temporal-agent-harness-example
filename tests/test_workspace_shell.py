from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from simple_chat_agent.worker.sandbox.executor import (
    ExecuteRequest,
    _CapturedOutput,
    _StreamBudget,
    _changed_files,
    _credential_boundary_self_test,
    _load_cached_result,
    _normalize_cwd,
    _prepare_executor_root,
    _result_payload,
    _sandbox_command,
    _state_path,
    _store_cached_result,
    _workspace_key,
)
from simple_chat_agent.worker.tools import (
    PYTHON_SANDBOX_TOOL,
    WORKSPACE_SHELL_TOOL,
    tool_names_for_connections,
)
from simple_chat_agent.worker.tools.workspace_shell import (
    MAX_COMMAND_CHARS,
    MAX_STDIN_CHARS,
    _validated_request,
)


class WorkspaceShellToolTests(unittest.TestCase):
    def test_new_conversations_advertise_workspace_shell_not_legacy_python(self) -> None:
        with patch.dict(
            "os.environ", {"SIMPLE_CHAT_WORKSPACE_SHELL_ENABLED": "1"}
        ):
            names = tool_names_for_connections(github_connection_id=None)

        self.assertIn(WORKSPACE_SHELL_TOOL, names)
        self.assertNotIn(PYTHON_SANDBOX_TOOL, names)

    def test_unconfigured_local_runtime_does_not_advertise_shell(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            names = tool_names_for_connections(github_connection_id=None)

        self.assertNotIn(WORKSPACE_SHELL_TOOL, names)

    def test_tool_request_is_bounded_and_keeps_network_choice(self) -> None:
        request = _validated_request(
            workspace_id="workflow-1",
            execution_id="workspace_shell:abc",
            command="python - <<'PY'\nprint('hello')\nPY",
            cwd="/workspace",
            timeout_seconds=10_000,
            network=False,
            stdin=None,
        )

        self.assertEqual(request["timeout_seconds"], 900)
        self.assertFalse(request["network"])

    def test_tool_request_rejects_oversized_command_and_stdin(self) -> None:
        with self.assertRaisesRegex(ValueError, "Command is too large"):
            _validated_request(
                workspace_id="workflow-1",
                execution_id="workspace_shell:abc",
                command="x" * (MAX_COMMAND_CHARS + 1),
                cwd="/workspace",
                timeout_seconds=30,
                network=True,
                stdin=None,
            )
        with self.assertRaisesRegex(ValueError, "stdin is too large"):
            _validated_request(
                workspace_id="workflow-1",
                execution_id="workspace_shell:abc",
                command="python -",
                cwd="/workspace",
                timeout_seconds=30,
                network=True,
                stdin="x" * (MAX_STDIN_CHARS + 1),
            )


class WorkspaceExecutorTests(unittest.TestCase):
    def test_workspace_key_does_not_expose_workflow_id(self) -> None:
        key = _workspace_key("simple-chat-user@example.com-secret")

        self.assertEqual(len(key), 40)
        self.assertNotIn("simple-chat", key)
        self.assertEqual(key, _workspace_key("simple-chat-user@example.com-secret"))

    def test_cwd_is_confined_to_workspace(self) -> None:
        self.assertEqual(_normalize_cwd("reports"), "/workspace/reports")
        self.assertEqual(_normalize_cwd("/workspace"), "/workspace")
        with self.assertRaises(HTTPException):
            _normalize_cwd("/etc")
        with self.assertRaises(HTTPException):
            _normalize_cwd("/workspace/../etc")

    def test_bubblewrap_command_shares_network_only_when_requested(self) -> None:
        workspace = Path("/workspaces/sessions/abc")
        online = _sandbox_command(
            ExecuteRequest(
                workspace_id="workflow-1",
                execution_id="workspace_shell:abc",
                command="curl https://example.com",
                network=True,
            ),
            workspace,
        )
        offline = _sandbox_command(
            ExecuteRequest(
                workspace_id="workflow-1",
                execution_id="workspace_shell:def",
                command="python script.py",
                network=False,
            ),
            workspace,
        )

        self.assertIn("--unshare-all", online)
        self.assertIn("--share-net", online)
        self.assertNotIn("--share-net", offline)
        self.assertIn("--clearenv", online)
        self.assertIn("--ro-bind", online)
        self.assertEqual(online[-1], "curl https://example.com")
        proc_index = online.index("/proc")
        self.assertEqual(online[proc_index - 1], "--tmpfs")
        self.assertNotIn("--proc", online)

        bind_index = online.index("--bind")
        mask_index = online.index("--tmpfs", online.index("--tmpfs") + 1)
        while online[mask_index + 1] in ("/tmp", "/run"):
            mask_index = online.index("--tmpfs", mask_index + 1)
        self.assertLess(bind_index, mask_index)
        self.assertEqual(online[bind_index + 1], str(workspace))
        self.assertEqual(online[mask_index + 1], "/workspaces")

    def test_changed_files_distinguishes_create_modify_delete(self) -> None:
        before = {"same.txt": (1, 1), "changed.txt": (1, 1), "gone.txt": (2, 2)}
        after = {"same.txt": (1, 1), "changed.txt": (3, 3), "new.txt": (4, 4)}

        self.assertEqual(
            _changed_files(before, after),
            {
                "created": ["new.txt"],
                "modified": ["changed.txt"],
                "deleted": ["gone.txt"],
            },
        )

    def test_result_cache_is_outside_selected_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"WORKSPACE_EXECUTOR_ROOT": temp_dir}):
                path = _state_path("workspace-key", "workspace_shell:abc")
                payload = {"exit_code": 0, "stdout": "ok\n"}
                _store_cached_result(path, payload)

                self.assertEqual(_load_cached_result(path), payload)
                self.assertIn(".executor-state", path.parts)

    def test_executor_root_does_not_chmod_a_group_writable_volume_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict("os.environ", {"WORKSPACE_EXECUTOR_ROOT": temp_dir}),
                patch(
                    "simple_chat_agent.worker.sandbox.executor.os.geteuid",
                    return_value=-1,
                ),
                patch.object(Path, "chmod") as chmod,
            ):
                _prepare_executor_root()

            chmod.assert_not_called()
            self.assertTrue((Path(temp_dir) / ".executor-state").is_dir())

    def test_executor_rejects_ambient_aws_credentials(self) -> None:
        with patch.dict(
            "os.environ", {"AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/token"}, clear=True
        ):
            error = _credential_boundary_self_test()

        self.assertIn("AWS_WEB_IDENTITY_TOKEN_FILE", error or "")

    def test_executor_rejects_reachable_instance_metadata_credentials(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(Path, "exists", return_value=False),
            patch(
                "simple_chat_agent.worker.sandbox.executor._metadata_status",
                side_effect=[None, 200],
            ),
        ):
            error = _credential_boundary_self_test()

        self.assertIn("IMDSv2 token", error or "")

    def test_executor_accepts_blocked_instance_metadata_credentials(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(Path, "exists", return_value=False),
            patch(
                "simple_chat_agent.worker.sandbox.executor._metadata_status",
                side_effect=[None, None],
            ),
        ):
            error = _credential_boundary_self_test()

        self.assertIsNone(error)

    def test_executor_rejects_a_reachable_kubernetes_api(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"KUBERNETES_SERVICE_HOST": "172.20.0.1"},
                clear=True,
            ),
            patch.object(Path, "exists", return_value=False),
            patch(
                "simple_chat_agent.worker.sandbox.executor._metadata_status",
                side_effect=[None, None],
            ),
            patch(
                "simple_chat_agent.worker.sandbox.executor._tcp_endpoint_reachable",
                return_value=True,
            ),
        ):
            error = _credential_boundary_self_test()

        self.assertIn("Kubernetes API", error or "")

    def test_nonzero_exit_is_a_tool_error_with_captured_output(self) -> None:
        stdout = _CapturedOutput(100)
        stderr = _CapturedOutput(100)
        stdout.append("before failure\n")
        stderr.append("boom\n")

        result = _result_payload(
            request=ExecuteRequest(
                workspace_id="workflow-1",
                execution_id="workspace_shell:abc",
                command="false",
            ),
            workspace_key="abc",
            exit_code=1,
            stdout=stdout,
            stderr=stderr,
            stream_budget=_StreamBudget(100),
            timed_out=False,
            elapsed_seconds=0.25,
            before={},
            after={},
        )

        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["stdout"], "before failure\n")
        self.assertEqual(result["stderr"], "boom\n")
        self.assertEqual(result["type"], "WorkspaceShellProcessError")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
