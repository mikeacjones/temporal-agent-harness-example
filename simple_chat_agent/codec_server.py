from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.protobuf.json_format import MessageToDict, ParseDict
from temporalio.api.common.v1 import Payloads
from temporalio.converter import DataConverter

from simple_chat_agent.external_storage import simple_chat_data_converter

DEFAULT_CODEC_SERVER_HOST = "127.0.0.1"
DEFAULT_CODEC_SERVER_PORT = 8001

CodecOperation = Literal["encode", "decode"]


def codec_server_url() -> str:
    return f"http://{codec_server_host()}:{codec_server_port()}"


def codec_server_host() -> str:
    return os.environ.get("SIMPLE_CHAT_CODEC_SERVER_HOST", DEFAULT_CODEC_SERVER_HOST)


def codec_server_port() -> int:
    return int(
        os.environ.get("SIMPLE_CHAT_CODEC_SERVER_PORT", str(DEFAULT_CODEC_SERVER_PORT))
    )


def codec_server_enabled() -> bool:
    value = os.environ.get("SIMPLE_CHAT_CODEC_SERVER_ENABLED", "1")
    return value.lower() not in {"0", "false", "no", "off"}


def create_codec_app(
    data_converter: DataConverter | None = None,
) -> FastAPI:
    converter = data_converter or simple_chat_data_converter()
    app = FastAPI(title="Simple Chat Temporal Codec Server")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "status": "ok",
            "encode": "/encode",
            "decode": "/decode",
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/encode")
    async def encode(request: dict[str, Any]) -> dict[str, Any]:
        return await _transform_payloads(converter, request, "encode")

    @app.post("/decode")
    async def decode(request: dict[str, Any]) -> dict[str, Any]:
        return await _transform_payloads(converter, request, "decode")

    return app


async def _transform_payloads(
    converter: DataConverter,
    request: dict[str, Any],
    operation: CodecOperation,
) -> dict[str, Any]:
    payloads = _payloads_from_request(request)
    try:
        if operation == "encode":
            await converter._transform_outbound_payloads(payloads)
        else:
            await converter._transform_inbound_payloads(payloads)
    except Exception as err:
        raise HTTPException(
            status_code=400,
            detail=f"Payload {operation} failed: {type(err).__name__}: {err}",
        ) from err

    return _payloads_to_response(payloads)


def _payloads_from_request(request: dict[str, Any]) -> Payloads:
    if not isinstance(request, dict):
        raise HTTPException(status_code=400, detail="Codec request must be an object.")
    if "payloads" not in request:
        raise HTTPException(
            status_code=400,
            detail="Codec request must contain a 'payloads' field.",
        )

    payloads = Payloads()
    try:
        ParseDict(request, payloads, ignore_unknown_fields=True)
    except Exception as err:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid payloads request: {type(err).__name__}: {err}",
        ) from err
    return payloads


def _payloads_to_response(payloads: Payloads) -> dict[str, Any]:
    return MessageToDict(
        payloads,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )
