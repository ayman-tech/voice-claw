"""PySide6 desktop application."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import logging
import sys
import threading
from collections.abc import Callable

from .config import AppConfig, load_config
from .app_logging import setup_logging
from .openclaw_client import AssistantDelta, OpenClawClient, extract_text
from .voice import STTService, TTSService


LOGGER = logging.getLogger(__name__)


class AsyncRunner:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="openclaw-async", daemon=True)
        self.thread.start()

    def submit(self, coro: object) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()


class TranscriptionThread:
    def __init__(
        self,
        stt: STTService,
        on_text: Callable[[str], None],
        on_error: Callable[[str], None],
        on_done: Callable[[], None],
    ) -> None:
        self.stt = stt
        self.on_text = on_text
        self.on_error = on_error
        self.on_done = on_done
        self.thread = threading.Thread(target=self._run, name="openclaw-stt", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            text = self.stt.transcribe_once()
            if text:
                self.on_text(text)
        finally:
            self.on_done()


def run() -> int:
    from PySide6.QtCore import QObject, QTimer, Qt, Signal
    from PySide6.QtGui import QTextCursor
    from PySide6.QtWidgets import (
        QApplication,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QPlainTextEdit,
        QVBoxLayout,
        QWidget,
    )
    log_path = setup_logging()
    LOGGER.info("Starting OpenClaw Voice")

    class UiBridge(QObject):
        connected = Signal(object)
        history = Signal(object)
        fallback_history = Signal(object)
        sent = Signal(object)
        aborted = Signal(object)
        delta = Signal(object)
        status = Signal(str)
        error = Signal(str)
        user_text = Signal(str)
        stt_done = Signal()
        run_ended = Signal()
        hotkey_ptt = Signal()

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.config = load_config()
            self.runner = AsyncRunner()
            self.bridge = UiBridge()
            self.client: OpenClawClient | None = None
            self.tts = TTSService(self.config, self.show_error)
            self.stt = STTService(self.config, self.show_error)
            self.pending_assistant_text = ""
            self._history_message_count = 0
            self.tts_flush_timer = QTimer(self)
            self.tts_flush_timer.setSingleShot(True)
            self.tts_flush_timer.setInterval(1200)
            self.tts_flush_timer.timeout.connect(self.flush_tts_tail)
            self.setWindowTitle("OpenClaw Voice")
            self.resize(900, 720)
            self._build_ui()
            self._connect_bridge()
            self._sync_fields_from_config()
            self._register_hotkey()

        def _connect_bridge(self) -> None:
            self.bridge.connected.connect(self._after_connect)
            self.bridge.history.connect(self._after_history)
            self.bridge.sent.connect(self._after_send)
            self.bridge.aborted.connect(self._after_abort)
            self.bridge.delta.connect(self.handle_delta)
            self.bridge.status.connect(self.set_status)
            self.bridge.error.connect(self.show_error)
            self.bridge.user_text.connect(self.send_user_text)
            self.bridge.stt_done.connect(lambda: self.talk_button.setEnabled(True))
            self.bridge.run_ended.connect(self._on_run_ended)
            self.bridge.fallback_history.connect(self._after_fallback_history)
            self.bridge.hotkey_ptt.connect(self._on_hotkey_ptt)

        def _build_ui(self) -> None:
            root = QWidget()
            layout = QVBoxLayout(root)

            buttons = QHBoxLayout()
            self.connect_button = QPushButton("Connect")
            self.talk_button = QPushButton("Push To Talk")
            self.stop_button = QPushButton("Stop")
            self.talk_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            buttons.addWidget(self.connect_button)
            buttons.addWidget(self.talk_button)
            buttons.addWidget(self.stop_button)
            layout.addLayout(buttons)

            self.summary = QLabel()
            self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(self.summary)

            self.status = QLabel("Disconnected")
            self.status.setAlignment(Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(self.status)

            self.log_label = QLabel(f"Log file: {log_path.resolve()}")
            self.log_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(self.log_label)

            self.transcript = QPlainTextEdit()
            self.transcript.setReadOnly(True)
            layout.addWidget(self.transcript, stretch=1)

            self.connect_button.clicked.connect(self.connect_gateway)
            self.talk_button.clicked.connect(self.start_transcription)
            self.stop_button.clicked.connect(self.stop_current)

            self.setCentralWidget(root)

        def _sync_fields_from_config(self) -> None:
            tts_state = "configured" if self.config.piper_model_path else "missing Piper voice"
            token_state = "token set" if self.config.auth_token else "token missing"
            self.summary.setText(
                f"{self.config.gateway_url} | session {self.config.session_key} | "
                f"STT {self.config.stt_model}/{self.config.stt_language}/{self.config.stt_device} | "
                f"TTS {tts_state} | {token_state}"
            )

        def _sync_config_from_fields(self) -> None:
            self.config = load_config()
            self._sync_fields_from_config()

        def save_settings(self) -> None:
            self._sync_config_from_fields()
            self.tts = TTSService(self.config, self.show_error)
            self.stt.shutdown()
            self.stt = STTService(self.config, self.show_error)
            self.set_status("Settings loaded")

        def connect_gateway(self) -> None:
            self.save_settings()
            self.connect_button.setEnabled(False)
            self.set_status("Connecting...")
            self.client = OpenClawClient(
                self.config,
                on_delta=self.bridge.delta.emit,
                on_status=self.bridge.status.emit,
                on_run_ended=self.bridge.run_ended.emit,
            )
            future = self.runner.submit(self.client.connect())
            future.add_done_callback(self.bridge.connected.emit)

        def _after_connect(self, future: Future) -> None:
            try:
                future.result()
            except Exception as exc:
                LOGGER.exception("Gateway connection failed")
                self.connect_button.setEnabled(True)
                self.talk_button.setEnabled(False)
                self.stop_button.setEnabled(False)
                self.show_error(f"Gateway connection failed: {exc}")
                return
            self.talk_button.setEnabled(True)
            self.stop_button.setEnabled(True)
            self.set_status("Connected")
            self.load_history()

        def load_history(self) -> None:
            if not self.client:
                return
            future = self.runner.submit(self.client.history())
            future.add_done_callback(self.bridge.history.emit)

        def _after_history(self, future: Future) -> None:
            try:
                response = future.result()
            except Exception as exc:
                LOGGER.exception("Could not load history")
                self.show_error(f"Could not load history: {exc}")
                return
            self.transcript.appendPlainText("[history loaded]")
            payload = response.get("payload", {}) if isinstance(response, dict) else {}
            messages = payload.get("messages", []) if isinstance(payload, dict) else []
            self._history_message_count = len(messages)
            for item in messages:
                if not isinstance(item, dict):
                    continue
                role = item.get("role", "message")
                body = extract_text(item.get("body") or item.get("content") or item.get("text") or item)
                if body:
                    self.transcript.appendPlainText(f"{role}: {body}")

        def _on_run_ended(self) -> None:
            if self.pending_assistant_text:
                return
            if not self.client:
                return
            LOGGER.info("Run ended with no streamed text — scheduling fallback history fetch in 2 s")
            QTimer.singleShot(2000, self._fetch_fallback_history)

        def _fetch_fallback_history(self) -> None:
            if self.pending_assistant_text:
                return
            if not self.client:
                return
            LOGGER.info("Fetching last 10 messages for fallback reply")
            future = self.runner.submit(self.client.history(limit=10))
            future.add_done_callback(self.bridge.fallback_history.emit)

        def _after_fallback_history(self, future: Future) -> None:
            try:
                response = future.result()
            except Exception as exc:
                LOGGER.warning("Fallback history fetch failed: %s", exc)
                return
            payload = response.get("payload", {}) if isinstance(response, dict) else {}
            messages = payload.get("messages", []) if isinstance(payload, dict) else []
            LOGGER.info(
                "Fallback history: got=%s roles=%s stop_reasons=%s",
                len(messages),
                [m.get("role") for m in messages if isinstance(m, dict)],
                [m.get("stopReason") for m in messages if isinstance(m, dict)],
            )
            for item in reversed(messages):
                if not isinstance(item, dict):
                    continue
                role = item.get("role", "")
                if role not in {"assistant", "model"}:
                    continue
                stop_reason = item.get("stopReason", "")
                raw_content = item.get("body") or item.get("content") or item.get("text") or item
                LOGGER.info(
                    "Fallback history candidate: role=%s stopReason=%s content_preview=%r",
                    role,
                    stop_reason,
                    repr(raw_content)[:200],
                )
                if stop_reason == "tool_calls":
                    LOGGER.info("Skipping tool-call assistant turn")
                    continue
                body = extract_text(raw_content)
                LOGGER.info("Fallback history: body_chars=%s", len(body))
                if not body:
                    continue
                self.transcript.appendPlainText("assistant: ")
                cursor = self.transcript.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(body)
                self.transcript.appendPlainText("")
                self.set_status("Ready")
                self.tts.speak(body)
                return

        def start_transcription(self) -> None:
            self.tts.stop()
            self.talk_button.setEnabled(False)
            self.set_status("Listening...")
            QTimer.singleShot(600, self._start_transcription_delayed)

        def _start_transcription_delayed(self) -> None:
            worker = TranscriptionThread(
                self.stt,
                on_text=self.bridge.user_text.emit,
                on_error=self.bridge.error.emit,
                on_done=self.bridge.stt_done.emit,
            )
            worker.start()

        def send_user_text(self, text: str) -> None:
            if not self.client:
                self.show_error("Connect to OpenClaw Gateway before speaking.")
                return
            self.pending_assistant_text = ""
            self.transcript.appendPlainText(f"user: {text}")
            LOGGER.info("Sending transcribed user text: chars=%s", len(text))
            self.set_status("Sending...")
            future = self.runner.submit(self.client.send_text(text))
            future.add_done_callback(self.bridge.sent.emit)

        def _after_send(self, future: Future) -> None:
            try:
                future.result()
            except Exception as exc:
                LOGGER.exception("Could not send message")
                self.show_error(f"Could not send message: {exc}")
                return
            self.set_status("Receiving...")

        def handle_delta(self, delta: AssistantDelta) -> None:
            text_to_append = delta.text
            if delta.final and self.pending_assistant_text and text_to_append.startswith(self.pending_assistant_text):
                text_to_append = text_to_append[len(self.pending_assistant_text) :]

            if text_to_append:
                if not self.pending_assistant_text:
                    self.transcript.appendPlainText("assistant: ")
                self.pending_assistant_text += text_to_append
                cursor = self.transcript.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(text_to_append)
                self.tts.speak_delta(text_to_append, final=delta.final)
                if not delta.final:
                    self.tts_flush_timer.start()
            if delta.final:
                self.tts_flush_timer.stop()
                self.tts.speak_delta("", final=True)
            if delta.final:
                self.pending_assistant_text = ""
                self.transcript.appendPlainText("")
                self.set_status("Ready")

        def flush_tts_tail(self) -> None:
            LOGGER.info("Flushing TTS after assistant stream idle")
            self.tts.speak_delta("", final=True)

        def stop_current(self) -> None:
            self.tts.stop()
            if self.client:
                future = self.runner.submit(self.client.abort())
                future.add_done_callback(self.bridge.aborted.emit)
            self.set_status("Stopping...")

        def _after_abort(self, future: Future) -> None:
            try:
                future.result()
            except Exception as exc:
                LOGGER.exception("Abort failed")
                self.show_error(f"Abort failed: {exc}")
                return
            self.set_status("Ready")

        def set_status(self, text: str) -> None:
            self.status.setText(text)

        def show_error(self, message: str) -> None:
            LOGGER.error(message)
            self.set_status(message)
            QMessageBox.warning(self, "OpenClaw Voice", message)

        def _on_hotkey_ptt(self) -> None:
            if self.talk_button.isEnabled():
                self.start_transcription()

        def _register_hotkey(self) -> None:
            try:
                import keyboard
                self._hotkey_handle = keyboard.add_hotkey(
                    "alt+z", self.bridge.hotkey_ptt.emit, suppress=False
                )
                LOGGER.info("Global hotkey registered: Alt+Z")
            except Exception as exc:
                LOGGER.warning("Could not register global hotkey: %s", exc)
                self._hotkey_handle = None

        def closeEvent(self, event: object) -> None:
            if self._hotkey_handle is not None:
                try:
                    import keyboard
                    keyboard.remove_hotkey(self._hotkey_handle)
                except Exception:
                    pass
            if self.client:
                future = self.runner.submit(self.client.close())
                try:
                    future.result(timeout=2)
                except Exception:
                    pass
            self.stt.shutdown()
            self.runner.stop()
            super().closeEvent(event)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
