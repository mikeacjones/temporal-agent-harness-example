from __future__ import annotations

import hmac
import os


def local_auth_requested() -> bool:
    return os.environ.get("SIMPLE_CHAT_LOCAL_AUTH_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def local_auth_username() -> str:
    return os.environ.get("SIMPLE_CHAT_LOCAL_AUTH_USERNAME", "demo")


def local_auth_password() -> str:
    return os.environ.get("SIMPLE_CHAT_LOCAL_AUTH_PASSWORD", "demo")


def local_auth_credentials_valid(username: str, password: str) -> bool:
    return hmac.compare_digest(username, local_auth_username()) and hmac.compare_digest(
        password,
        local_auth_password(),
    )
