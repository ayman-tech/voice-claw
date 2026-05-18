from unittest import TestCase

from claw_app.config import AppConfig
from claw_app.openclaw_client import (
    ENGLISH_ONLY_PREFIX,
    abort_request,
    connect_request,
    extract_assistant_delta,
    extract_text,
    history_request,
    send_request,
)


class OpenClawClientTests(TestCase):
    def test_history_request_shape(self) -> None:
        config = AppConfig(session_key="webchat:voice")

        request = history_request(config, limit=25)

        self.assertEqual(request["type"], "req")
        self.assertEqual(request["method"], "chat.history")
        self.assertEqual(request["params"], {"sessionKey": "webchat:voice", "limit": 25})

    def test_connect_request_shape(self) -> None:
        config = AppConfig(auth_token="token")

        request = connect_request(config, request_id="connect-1")

        self.assertEqual(request["type"], "req")
        self.assertEqual(request["id"], "connect-1")
        self.assertEqual(request["method"], "connect")
        self.assertEqual(request["params"]["minProtocol"], 4)
        self.assertEqual(request["params"]["maxProtocol"], 4)
        self.assertEqual(request["params"]["client"]["id"], "gateway-client")
        self.assertEqual(request["params"]["client"]["mode"], "backend")
        self.assertEqual(request["params"]["role"], "operator")
        self.assertEqual(request["params"]["scopes"], ["operator.read", "operator.write"])
        self.assertEqual(request["params"]["auth"], {"token": "token"})

    def test_send_request_shape(self) -> None:
        config = AppConfig(
            session_key="webchat:voice",
            model="openclaw/default",
            agent_id="default",
            message_channel="webchat",
        )

        request = send_request(config, "hello", idempotency_key="turn-1")

        self.assertEqual(request["method"], "chat.send")
        self.assertEqual(request["params"]["message"], f"{ENGLISH_ONLY_PREFIX}hello")
        self.assertEqual(request["params"]["sessionKey"], "webchat:voice")
        self.assertEqual(request["params"]["idempotencyKey"], "turn-1")
        self.assertNotIn("model", request["params"])
        self.assertNotIn("agentId", request["params"])
        self.assertNotIn("channel", request["params"])

    def test_abort_request_shape(self) -> None:
        config = AppConfig(session_key="webchat:voice")

        request = abort_request(config, run_id="run-123")

        self.assertEqual(request["method"], "chat.abort")
        self.assertEqual(request["params"], {"sessionKey": "webchat:voice", "runId": "run-123"})

    def test_extract_assistant_delta_from_legacy_chat_event(self) -> None:
        delta = extract_assistant_delta(
            {
                "type": "event",
                "event": "chat",
                "params": {
                    "role": "assistant",
                    "delta": "Hello",
                    "runId": "run-1",
                },
            }
        )

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta.text, "Hello")
        self.assertEqual(delta.run_id, "run-1")
        self.assertFalse(delta.final)

    def test_extract_assistant_delta_ignores_user_events(self) -> None:
        delta = extract_assistant_delta(
            {
                "type": "event",
                "event": "chat",
                "params": {
                    "role": "user",
                    "text": "ignore me",
                },
            }
        )

        self.assertIsNone(delta)

    def test_extract_assistant_delta_final_message(self) -> None:
        delta = extract_assistant_delta(
            {
                "type": "event",
                "event": "chat.message",
                "payload": {
                    "role": "assistant",
                    "body": "Done.",
                    "final": True,
                },
            }
        )

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta.text, "Done.")
        self.assertTrue(delta.final)

    def test_extract_assistant_delta_protocol_v4_delta_text(self) -> None:
        delta = extract_assistant_delta(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "role": "assistant",
                    "deltaText": "Hi there",
                },
            }
        )

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta.text, "Hi there")

    def test_extract_text_from_structured_content(self) -> None:
        text = extract_text(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": " world"}],
            }
        )

        self.assertEqual(text, "Hello world")

    def test_extract_assistant_delta_from_structured_payload_content(self) -> None:
        delta = extract_assistant_delta(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello from chat"}],
                    "final": True,
                },
            }
        )

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta.text, "Hello from chat")
        self.assertTrue(delta.final)

    def test_extract_assistant_delta_from_agent_data(self) -> None:
        delta = extract_assistant_delta(
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "runId": "run-1",
                    "data": {
                        "role": "assistant",
                        "deltaText": "Streaming hello",
                    },
                },
            }
        )

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta.text, "Streaming hello")
        self.assertEqual(delta.run_id, "run-1")
