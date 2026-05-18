"""OpenClaw Gateway/WebChat WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from .config import AppConfig


JsonObject = dict[str, Any]
LOGGER = logging.getLogger(__name__)
PROTOCOL_VERSION = 4


@dataclass(slots=True)
class AssistantDelta:
    text: str
    final: bool = False
    run_id: str | None = None


ENGLISH_ONLY_PREFIX = (
    "Voice client instruction: Reply in English only, regardless of the language "
    "used in the transcript. Keep the reply concise and natural for text-to-speech.\n\n"
    "User said: "
)


def make_gateway_request(method: str, params: JsonObject | None = None, request_id: str | None = None) -> JsonObject:
    return {
        "type": "req",
        "id": request_id or str(uuid.uuid4()),
        "method": method,
        "params": params or {},
    }


def connect_request(config: AppConfig, request_id: str | None = None) -> JsonObject:
    auth: JsonObject = {}
    if config.auth_token:
        auth["token"] = config.auth_token
    if config.auth_password:
        auth["password"] = config.auth_password

    params: JsonObject = {
        "minProtocol": PROTOCOL_VERSION,
        "maxProtocol": PROTOCOL_VERSION,
        "client": {
            "id": "gateway-client",
            "displayName": "OpenClaw Voice",
            "version": "0.1.0",
            "platform": platform.system().lower(),
            "mode": "backend",
        },
        "role": "operator",
        "scopes": ["operator.read", "operator.write"],
        "caps": [],
        "commands": [],
        "permissions": {},
        "locale": "en-US",
        "userAgent": "openclaw-voice/0.1.0",
    }
    if auth:
        params["auth"] = auth
    return make_gateway_request("connect", params, request_id)


def history_request(config: AppConfig, limit: int = 50) -> JsonObject:
    return make_gateway_request(
        "chat.history",
        {
            "sessionKey": config.session_key,
            "limit": limit,
        },
    )


def send_request(config: AppConfig, text: str, idempotency_key: str | None = None) -> JsonObject:
    params: JsonObject = {
        "message": f"{ENGLISH_ONLY_PREFIX}{text}",
        "sessionKey": config.session_key,
        "idempotencyKey": idempotency_key or str(uuid.uuid4()),
    }
    return make_gateway_request("chat.send", params)


def abort_request(config: AppConfig, run_id: str | None = None) -> JsonObject:
    params: JsonObject = {"sessionKey": config.session_key}
    if run_id:
        params["runId"] = run_id
    return make_gateway_request("chat.abort", params)


def extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(extract_text(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("message"), dict):
            return extract_text(value.get("message"))
        if "data" in value and isinstance(value["data"], (dict, list)):
            result = extract_text(value["data"])
            if result:
                return result
        if "state" in value:
            return extract_text(value.get("state"))
        if "parts" in value:
            return extract_text(value.get("parts"))
        if "output" in value:
            return extract_text(value.get("output"))
        if "response" in value:
            return extract_text(value.get("response"))
        if "reply" in value:
            return extract_text(value.get("reply"))
        if "result" in value:
            return extract_text(value.get("result"))
        if "text" in value:
            return extract_text(value.get("text"))
        if "content" in value:
            return extract_text(value.get("content"))
        if "body" in value:
            return extract_text(value.get("body"))
        if "deltaText" in value:
            return extract_text(value.get("deltaText"))
        if "delta" in value:
            return extract_text(value.get("delta"))
        return ""
    return str(value)


_LIFECYCLE_STRINGS = frozenset({
    "final", "complete", "completed", "started", "running", "pending",
    "error", "aborted", "cancelled", "done", "idle", "active",
})


def extract_assistant_delta(message: JsonObject) -> AssistantDelta | None:
    """Normalize likely OpenClaw chat event shapes into assistant text deltas.

    OpenClaw's Gateway protocol has moved over time, so this accepts a few
    compatible shapes while keeping UI/TTS code stable.
    """

    method = message.get("event") if message.get("type") == "event" else message.get("method")
    if method not in {"chat", "chat.event", "chat.delta", "chat.message", "chat.response", "agent"}:
        return None

    payload = message.get("params") or message.get("payload") or message.get("data") or {}
    if not isinstance(payload, dict):
        return None

    nested_message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    nested_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nested_state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    role = (
        payload.get("role")
        or payload.get("senderRole")
        or nested_message.get("role")
        or nested_data.get("role")
        or nested_state.get("role")
    )
    kind = (
        payload.get("kind")
        or payload.get("type")
        or payload.get("event")
        or nested_data.get("kind")
        or nested_data.get("type")
        or nested_data.get("event")
        or nested_state.get("kind")
        or nested_state.get("type")
        or nested_state.get("event")
    )
    if role and role != "assistant":
        return None
    if kind and "tool" in str(kind).lower():
        return None

    text = extract_text(payload)
    if text.strip().lower() in _LIFECYCLE_STRINGS:
        text = ""

    final = bool(
        payload.get("final")
        or payload.get("done")
        or payload.get("isFinal")
        or nested_data.get("final")
        or nested_data.get("done")
        or nested_data.get("isFinal")
        or nested_state.get("final")
        or nested_state.get("done")
        or nested_state.get("isFinal")
        or str(kind).lower() in {"final", "complete", "completed", "message", "assistant_message", "assistant-message"}
    )
    run_id = payload.get("runId") or payload.get("run_id")
    if not text and not final:
        return None
    return AssistantDelta(text=text, final=final, run_id=run_id if isinstance(run_id, str) else None)


class OpenClawClient:
    def __init__(
        self,
        config: AppConfig,
        on_delta: Callable[[AssistantDelta], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_run_ended: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.on_delta = on_delta
        self.on_status = on_status
        self.on_run_ended = on_run_ended
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[JsonObject]] = {}
        self.current_run_id: str | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self) -> None:
        import websockets

        LOGGER.info("Connecting to OpenClaw Gateway at %s", self.config.gateway_url)
        self._ws = await websockets.connect(
            self.config.gateway_url,
            ping_interval=20,
            ping_timeout=20,
            max_size=25 * 1024 * 1024,
        )
        await self._handshake()
        self._reader_task = asyncio.create_task(self._reader())
        self._emit_status("Connected")
        LOGGER.info("Connected to OpenClaw Gateway")

    async def _handshake(self) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket is not open")

        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=15)
            message = json.loads(raw)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Gateway did not send connect.challenge in time") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Gateway sent an invalid pre-connect frame") from exc

        if not isinstance(message, dict) or message.get("event") != "connect.challenge":
            raise RuntimeError(f"Expected connect.challenge, got {message!r}")

        response = await self._call_without_reader(connect_request(self.config), timeout=30)
        payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
        if payload.get("type") != "hello-ok":
            raise RuntimeError(f"Unexpected Gateway handshake payload: {payload!r}")
        LOGGER.info(
            "Gateway handshake ok: protocol=%s scopes=%s",
            payload.get("protocol"),
            (payload.get("auth") or {}).get("scopes"),
        )

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        self._ws = None
        self._emit_status("Disconnected")

    async def history(self, limit: int = 50) -> JsonObject:
        return await self._call(history_request(self.config, limit))

    async def send_text(self, text: str) -> JsonObject:
        response = await self._call(send_request(self.config, text))
        payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
        run_id = payload.get("runId") or response.get("runId")
        self.current_run_id = run_id if isinstance(run_id, str) else self.current_run_id
        return response

    async def abort(self) -> JsonObject:
        return await self._call(abort_request(self.config, self.current_run_id))

    async def _call(self, request: JsonObject) -> JsonObject:
        if self._ws is None:
            raise RuntimeError("Not connected to OpenClaw Gateway")
        request_id = str(request["id"])
        loop = asyncio.get_running_loop()
        future: asyncio.Future[JsonObject] = loop.create_future()
        self._pending[request_id] = future
        try:
            LOGGER.info("Gateway request: method=%s id=%s", request.get("method"), request_id)
            await self._ws.send(json.dumps(request))
            return await asyncio.wait_for(future, timeout=60)
        finally:
            self._pending.pop(request_id, None)

    async def _call_without_reader(self, request: JsonObject, timeout: float) -> JsonObject:
        if self._ws is None:
            raise RuntimeError("Not connected to OpenClaw Gateway")
        request_id = str(request["id"])
        LOGGER.info("Gateway request: method=%s id=%s", request.get("method"), request_id)
        await self._ws.send(json.dumps(request))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if str(message.get("id")) != request_id:
                LOGGER.info("Ignoring pre-handshake frame: type=%s event=%s", message.get("type"), message.get("event"))
                continue
            return self._response_or_raise(message)

    async def _reader(self) -> None:
        assert self._ws is not None
        seen_frames: set[tuple[str, int]] = set()
        async for raw in self._ws:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                LOGGER.warning("Ignoring invalid JSON frame from Gateway")
                continue
            if not isinstance(message, dict):
                continue

            message_id = message.get("id")
            if message_id is not None and str(message_id) in self._pending:
                future = self._pending.pop(str(message_id))
                if not future.done():
                    try:
                        future.set_result(self._response_or_raise(message))
                    except Exception as exc:
                        future.set_exception(exc)
                continue

            delta = extract_assistant_delta(message)
            if delta:
                frame_payload = message.get("params") or message.get("payload") or message.get("data") or {}
                if isinstance(frame_payload, dict):
                    frame_run = str(frame_payload.get("runId", ""))
                    frame_seq = frame_payload.get("seq")
                    if frame_run and frame_seq is not None:
                        frame_key = (frame_run, int(frame_seq))
                        if frame_key in seen_frames:
                            continue
                        seen_frames.add(frame_key)
                LOGGER.info(
                    "Assistant delta: chars=%s final=%s run_id=%s frame_type=%s frame_event=%s frame_method=%s payload_keys=%s data_keys=%s",
                    len(delta.text),
                    delta.final,
                    delta.run_id,
                    message.get("type"),
                    message.get("event"),
                    message.get("method"),
                    sorted(frame_payload.keys()) if isinstance(frame_payload, dict) else None,
                    sorted(frame_payload.get("data", {}).keys()) if isinstance(frame_payload, dict) and isinstance(frame_payload.get("data"), dict) else repr(frame_payload.get("data"))[:60] if isinstance(frame_payload, dict) else None,
                )
                if delta.run_id:
                    self.current_run_id = delta.run_id
                if self.on_delta:
                    self.on_delta(delta)
            else:
                payload = message.get("payload") or message.get("params") or {}
                event_name = message.get("event") or message.get("method") or message.get("type") or "?"
                if isinstance(payload, dict) and event_name not in {"health", "tick"}:
                    nested = payload.get("data")
                    data_summary = sorted(nested.keys()) if isinstance(nested, dict) else repr(nested)[:60]
                    state_val = payload.get("state")
                    state_summary = sorted(state_val.keys()) if isinstance(state_val, dict) else repr(state_val)[:80]
                    LOGGER.info(
                        "Unhandled Gateway frame: event=%s payload_keys=%s data=%s state=%s",
                        event_name,
                        sorted(payload.keys()),
                        data_summary,
                        state_summary,
                    )
                    if isinstance(nested, dict) and "endedAt" in nested:
                        LOGGER.info("Run ended detected, firing on_run_ended callback")
                        if self.on_run_ended:
                            self.on_run_ended()
                elif event_name not in {"health", "tick"}:
                    LOGGER.info("Gateway event: %s", event_name)

    def _response_or_raise(self, message: JsonObject) -> JsonObject:
        error = message.get("error")
        if error or message.get("ok") is False:
            LOGGER.error("Gateway error response for id=%s: %s", message.get("id"), error or message)
            raise RuntimeError(str(error or message))
        LOGGER.info("Gateway response ok: id=%s", message.get("id"))
        return message

    def _emit_status(self, status: str) -> None:
        if self.on_status:
            self.on_status(status)


async def stream_assistant_events(messages: AsyncIterator[JsonObject]) -> AsyncIterator[AssistantDelta]:
    async for message in messages:
        delta = extract_assistant_delta(message)
        if delta:
            yield delta
