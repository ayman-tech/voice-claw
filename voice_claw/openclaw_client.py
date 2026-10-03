"""OpenClaw Gateway/WebChat WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .app_logging import preview


JsonObject = dict[str, Any]
LOGGER = logging.getLogger(__name__)
PROTOCOL_VERSION = 4


# Without this capability the Gateway also streams every assistant text twice more to us as
# `agent` events (items). We only consume `chat` events, so ask it not to send those.
CAP_CHAT_ONLY_ASSISTANT_TEXT = "chat-only-assistant-text"


# `chat` event states. Before any text the Gateway sends "status" frames (phase=preparing_workspace,
# preparing_context, starting_model); "delta" frames stream the reply; the run ends with exactly one
# of final / aborted / error.
CHAT_TERMINAL_STATES = frozenset({"final", "aborted", "error"})


@dataclass(slots=True)
class ChatFrame:
    """One `chat` event for a run.

    For `state="delta"`, `delta_text` is the text added since the previous delta, or the whole
    reply when `replace` is set (the Gateway rewrote it, e.g. dropped an interim "On it." lead-in).
    Only some deltas also carry the reply so far in `text`. Terminal events carry the final text.
    """

    run_id: str
    state: str
    text: str | None = None
    delta_text: str = ""
    replace: bool = False
    stop_reason: str | None = None
    error: str | None = None
    yielded: bool = False
    phase: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in CHAT_TERMINAL_STATES


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
        "caps": [CAP_CHAT_ONLY_ASSISTANT_TEXT],
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
        # Agent delivered reply via message.send tool call
        if value.get("type") == "toolCall" and value.get("name") == "message":
            args = value.get("arguments", {})
            if isinstance(args, dict) and args.get("action") == "send":
                msg = args.get("message", "")
                if msg:
                    return str(msg)
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


def parse_chat_event(message: JsonObject) -> ChatFrame | None:
    """Parse a protocol-v4 `chat` event, or return None for anything else."""
    if message.get("type") != "event" or message.get("event") != "chat":
        return None
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    run_id = payload.get("runId")
    state = payload.get("state")
    if not isinstance(run_id, str) or not isinstance(state, str):
        return None

    raw_message = payload.get("message")
    text = extract_text(raw_message) if isinstance(raw_message, dict) else None
    delta_text = payload.get("deltaText")
    stop_reason = payload.get("stopReason")
    error = payload.get("errorMessage")
    return ChatFrame(
        run_id=run_id,
        state=state,
        text=text,
        delta_text=delta_text if isinstance(delta_text, str) else "",
        replace=payload.get("replace") is True,
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        error=error if isinstance(error, str) else None,
        yielded=payload.get("yielded") is True,
        phase=payload.get("phase") if isinstance(payload.get("phase"), str) else None,
    )


class OpenClawClient:
    def __init__(
        self,
        config: AppConfig,
        on_chat: Callable[[ChatFrame], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_run_ended: Callable[[], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.on_chat = on_chat
        self.on_status = on_status
        self.on_run_ended = on_run_ended
        self.on_disconnect = on_disconnect
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[JsonObject]] = {}
        # Run id of the turn in flight. The Gateway broadcasts events for every run it hosts
        # (other sessions, earlier turns, heartbeats); only this run's events belong to us.
        self.active_run_id: str | None = None

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

    def _fail_pending(self, reason: str) -> None:
        """Fail in-flight requests now instead of letting each wait out its 60 s timeout."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError(reason))
        self._pending.clear()

    async def close(self) -> None:
        self._fail_pending("Gateway connection closed")
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
        # chat.send uses the idempotency key as the run id, so the run is known before any event.
        run_id = str(uuid.uuid4())
        self.active_run_id = run_id
        return await self._call(send_request(self.config, text, idempotency_key=run_id))

    async def abort(self) -> JsonObject:
        return await self._call(abort_request(self.config, self.active_run_id))

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
        ended_runs: set[str] = set()
        disconnect_reason = "Connection closed"
        try:
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

                self._handle_event(message, ended_runs)
        except Exception as exc:
            disconnect_reason = str(exc)
            LOGGER.warning("Gateway reader exited with error: %s", exc)
        finally:
            self._ws = None
            self._fail_pending(f"Gateway disconnected: {disconnect_reason}")
            LOGGER.warning("Gateway disconnected: %s", disconnect_reason)
            if self.on_disconnect:
                self.on_disconnect(disconnect_reason)

    def _handle_event(self, message: JsonObject, ended_runs: set[str]) -> None:
        frame = parse_chat_event(message)
        if frame:
            self._handle_chat_frame(frame, ended_runs)
            return
        # Everything else (agent lifecycle/tool/usage streams, presence, sessions.changed, ...)
        # is not part of the reply. Agent lifecycle `endedAt` frames also arrive before the run
        # is really over ("finishing"), so only the chat terminal event ends a turn.
        event_name = message.get("event") or message.get("method") or message.get("type") or "?"
        if event_name not in {"health", "tick"}:
            LOGGER.debug("Ignoring Gateway event: %s", event_name)

    def _handle_chat_frame(self, frame: ChatFrame, ended_runs: set[str]) -> None:
        if frame.run_id != self.active_run_id:
            LOGGER.debug("Ignoring chat %s for run %s (active run %s)", frame.state, frame.run_id[:8], (self.active_run_id or "-")[:8])
            return
        LOGGER.debug(
            "Chat %s run=%s phase=%s replace=%s chars=%s delta=%r stop=%s",
            frame.state,
            frame.run_id[:8],
            frame.phase,
            frame.replace,
            len(frame.text) if frame.text is not None else None,
            preview(frame.delta_text),
            frame.stop_reason,
        )
        if frame.state == "status":
            return  # progress only; nothing to speak and the run is not over
        if frame.terminal:
            LOGGER.info(
                "Chat %s: run=%s stop=%s yielded=%s chars=%s",
                frame.state, frame.run_id[:8], frame.stop_reason, frame.yielded,
                len(frame.text) if frame.text is not None else 0,
            )
        if self.on_chat:
            self.on_chat(frame)
        if frame.terminal:
            self._handle_run_end(frame.run_id, ended_runs)

    def _handle_run_end(self, run_id: str, ended_runs: set[str]) -> None:
        if run_id != self.active_run_id or run_id in ended_runs:
            return
        ended_runs.add(run_id)
        LOGGER.info("Run ended: run=%s", run_id[:8])
        if self.on_run_ended:
            self.on_run_ended()

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
