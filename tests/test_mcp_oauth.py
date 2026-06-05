from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from simple_chat_agent.common.mcp_auth import mcp_oauth_provider
from simple_chat_agent.common.mcp_oauth import (
    AppMcpTokenStorage,
    mcp_oauth_provider_for_connection,
)
from simple_chat_agent.common.store import AppStore


class McpOAuthStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "SIMPLE_CHAT_DYNAMODB_TABLE": "",
                "SIMPLE_CHAT_ARTIFACTS_TABLE": "",
                "SIMPLE_CHAT_ARTIFACT_BUCKET": "",
            },
        )
        self._env.start()
        base = Path(self._tempdir.name)
        self.store = AppStore(
            path=str(base / "simple_chat.sqlite3"),
            artifact_dir=str(base / "artifacts"),
        )
        self.storage = AppMcpTokenStorage(
            store=self.store,
            user_id="user-1",
            server_id="server-1",
        )

    def tearDown(self) -> None:
        self._env.stop()
        self._tempdir.cleanup()

    async def test_set_tokens_preserves_refresh_token_on_refresh_response(self) -> None:
        await self.storage.set_tokens(
            OAuthToken(
                access_token="access-1",
                expires_in=3600,
                refresh_token="refresh-1",
                scope="read write",
            )
        )

        await self.storage.set_tokens(
            OAuthToken(
                access_token="access-2",
                expires_in=1800,
            )
        )

        connection = self.store.get_oauth_connection(
            user_id="user-1",
            provider=mcp_oauth_provider("server-1"),
        )
        self.assertIsNotNone(connection)
        token_payload = connection.metadata["oauth_token"]
        self.assertEqual(token_payload["access_token"], "access-2")
        self.assertEqual(token_payload["refresh_token"], "refresh-1")
        self.assertEqual(token_payload["scope"], "read write")
        self.assertIn("oauth_token_expires_at", connection.metadata)

        expires_at = datetime.fromisoformat(connection.metadata["oauth_token_expires_at"])
        refresh_time = await self.storage.get_token_refresh_time()
        self.assertIsNotNone(refresh_time)
        self.assertLess(refresh_time, expires_at.timestamp())

    async def test_provider_restores_refresh_state_for_later_tool_calls(self) -> None:
        protected_resource = ProtectedResourceMetadata(
            resource="https://mcp.example.com",
            authorization_servers=["https://auth.example.com"],
        )
        oauth_metadata = OAuthMetadata(
            issuer="https://auth.example.com",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/custom-token",
        )
        await self.storage.set_oauth_context(
            protected_resource_metadata=protected_resource,
            oauth_metadata=oauth_metadata,
            auth_server_url="https://auth.example.com",
        )
        await self.storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=["http://127.0.0.1:8000/oauth/mcp/callback"],
                client_id="client-1",
                token_endpoint_auth_method="none",
            )
        )
        await self.storage.set_tokens(
            OAuthToken(
                access_token="short-lived",
                expires_in=1,
                refresh_token="refresh-1",
            )
        )

        connection = self.store.get_oauth_connection(
            user_id="user-1",
            provider=mcp_oauth_provider("server-1"),
        )
        self.assertIsNotNone(connection)
        provider = mcp_oauth_provider_for_connection(
            connection=connection,
            server_url="https://mcp.example.com/mcp",
            store=self.store,
        )

        await provider._initialize()

        self.assertEqual(
            str(provider.context.oauth_metadata.token_endpoint),
            "https://auth.example.com/custom-token",
        )
        self.assertEqual(
            str(provider.context.protected_resource_metadata.resource),
            "https://mcp.example.com/",
        )
        self.assertEqual(provider.context.auth_server_url, "https://auth.example.com")
        self.assertTrue(provider.context.can_refresh_token())
        self.assertLessEqual(provider.context.token_expiry_time, time.time())
        self.assertFalse(provider.context.is_token_valid())

    async def test_provider_discovers_metadata_for_legacy_expired_tokens(self) -> None:
        await self.storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=["http://127.0.0.1:8000/oauth/mcp/callback"],
                client_id="client-1",
                token_endpoint_auth_method="none",
            )
        )
        await self.storage.set_tokens(
            OAuthToken(
                access_token="short-lived",
                expires_in=1,
                refresh_token="refresh-1",
            )
        )
        connection = self.store.get_oauth_connection(
            user_id="user-1",
            provider=mcp_oauth_provider("server-1"),
        )
        self.assertIsNotNone(connection)
        provider = mcp_oauth_provider_for_connection(
            connection=connection,
            server_url="https://mcp.example.com/mcp",
            store=self.store,
        )

        async def discover(provider_arg, _client) -> None:
            provider_arg.context.oauth_metadata = OAuthMetadata(
                issuer="https://auth.example.com",
                authorization_endpoint="https://auth.example.com/authorize",
                token_endpoint="https://auth.example.com/custom-token",
            )

        with (
            patch(
                "simple_chat_agent.common.mcp_oauth._discover_oauth_metadata",
                new=AsyncMock(side_effect=discover),
            ) as discover_mock,
            patch(
                "simple_chat_agent.common.mcp_oauth._persist_oauth_context",
                new=AsyncMock(),
            ) as persist_mock,
        ):
            await provider._initialize()

        discover_mock.assert_awaited_once()
        persist_mock.assert_awaited_once_with(provider)

    async def test_refresh_response_preserves_refresh_token_in_provider(self) -> None:
        await self.storage.set_tokens(
            OAuthToken(
                access_token="access-1",
                expires_in=1,
                refresh_token="refresh-1",
            )
        )
        connection = self.store.get_oauth_connection(
            user_id="user-1",
            provider=mcp_oauth_provider("server-1"),
        )
        self.assertIsNotNone(connection)
        provider = mcp_oauth_provider_for_connection(
            connection=connection,
            server_url="https://mcp.example.com/mcp",
            store=self.store,
        )

        refreshed = await provider._handle_refresh_response(
            httpx.Response(
                200,
                json={
                    "access_token": "access-2",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        )

        self.assertTrue(refreshed)
        self.assertIsNotNone(provider.context.current_tokens)
        self.assertEqual(provider.context.current_tokens.access_token, "access-2")
        self.assertEqual(provider.context.current_tokens.refresh_token, "refresh-1")


if __name__ == "__main__":
    unittest.main()
