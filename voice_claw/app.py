"""PySide6 desktop application."""

from __future__ import annotations

import asyncio
from pathlib import Path
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
        async def _cancel_all() -> None:
            tasks = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.loop.stop()

        asyncio.run_coroutine_threadsafe(_cancel_all(), self.loop)
        self.thread.join(timeout=3)

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
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        self.stt = stt
        self.on_text = on_text
        self.on_error = on_error
        self.on_done = on_done
        self.on_ready = on_ready
        self.thread = threading.Thread(target=self._run, name="openclaw-stt", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            text = self.stt.transcribe_once(on_ready=self.on_ready)
            if text:
                self.on_text(text)
        finally:
            self.on_done()


_DARK_STYLESHEET = """
QMainWindow, QWidget { background-color: #0a0a14; color: #c0c0d8; }
QLabel#title {
    color: #d0d0f0;
    font-size: 28px;
    font-weight: 700;
    letter-spacing: 1px;
}
QLabel#subtitle {
    color: #8080cc;
    font-size: 15px;
    letter-spacing: 2px;
}
QLabel#status_label {
    color: #9090b8;
    font-size: 12px;
}
QPushButton {
    background-color: #1a2a4a;
    color: #a0b8d8;
    border: 1px solid #2a3e60;
    border-radius: 14px;
    padding: 8px 16px;
    font-size: 15px;
    min-width: 110px;
}
QPushButton:hover {
    background-color: #243860;
    color: #c8daf0;
    border-color: #3a5480;
}
QPushButton:pressed { background-color: #111e36; }
QPushButton:disabled { color: #2a3a50; background-color: #0e1626; border-color: #141e30; }
QScrollArea#chat, QScrollArea#chat > QWidget {
    background-color: #0a0a14;
    border: none;
}
QWidget#chat_container { background-color: #0a0a14; }
QLabel#bubble_user {
    background-color: #1a1a38;
    color: #a0a0cc;
    border-radius: 12px;
    padding: 8px 12px;
    font-size: 14px;
}
QLabel#bubble_assistant {
    background-color: #161630;
    color: #a8a8d8;
    border-radius: 12px;
    padding: 8px 12px;
    font-size: 14px;
}
QScrollBar:vertical {
    background: #0a0a14;
    width: 5px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #1c1c38;
    border-radius: 2px;
    min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def run() -> int:
    from PySide6.QtCore import QObject, QPointF, QRectF, QTimer, Qt, Signal
    from PySide6.QtGui import (
        QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QVBoxLayout,
        QWidget,
    )
    setup_logging()
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
        stt_ready = Signal()
        stt_done = Signal()
        tts_audio_done = Signal()
        run_ended = Signal()
        hotkey_ptt = Signal()
        gateway_disconnected = Signal(str)

    # ------------------------------------------------------------------ orb --

    class OrbWidget(QWidget):
        """Animated orb that replaces the Push-To-Talk button.

        States
        ------
        disconnected  gray   – static
        ready         blue   – slow breathe
        listening     green  – expanding rings
        thinking      amber  – rotating arc
        speaking      purple – fast breathe
        """

        clicked = Signal()

        _PALETTE: dict[str, tuple[int, int, int]] = {
            "disconnected": (65, 65, 80),
            "ready":        (50, 130, 255),
            "listening":    (40, 210, 115),
            "thinking":     (240, 155, 30),
            "speaking":     (155, 55, 255),
        }

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setFixedSize(260, 260)
            self._state = "disconnected"
            self._breath = 0.0
            self._breath_dir = 1
            self._ring_phase = 0.0
            self._rotation = 0.0
            self._timer = QTimer(self)
            self._timer.setInterval(16)   # ~60 fps
            self._timer.timeout.connect(self._tick)
            self._timer.start()
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            _assets = Path(__file__).parent.parent / "assets"
            _avatar_path = next(
                (p for p in (_assets / "avatar.png", _assets / "avatar.jpg", _assets / "avatar.jpeg") if p.exists()),
                None,
            )
            _dpr = QApplication.primaryScreen().devicePixelRatio()
            _img_d = int((70 - 6) * 2 * _dpr)  # physical pixels = logical 128px × dpr
            _raw = QPixmap(str(_avatar_path)) if _avatar_path else None
            if _raw and not _raw.isNull():
                _scaled = _raw.scaled(
                    _img_d, _img_d,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                _scaled.setDevicePixelRatio(_dpr)
                self._avatar: QPixmap | None = _scaled
            else:
                self._avatar = None

        @property
        def state(self) -> str:
            return self._state

        def set_state(self, state: str) -> None:
            if self._state == state:
                return
            self._state = state
            if state == "listening":
                self._ring_phase = 0.0

        def _tick(self) -> None:
            speed = {"ready": 0.007, "speaking": 0.028}.get(self._state, 0.013)
            self._breath += speed * self._breath_dir
            if self._breath >= 1.0:
                self._breath_dir = -1
            elif self._breath <= 0.0:
                self._breath_dir = 1

            if self._state in ("listening", "speaking"):
                self._ring_phase = (self._ring_phase + 0.011) % 1.0
            if self._state == "thinking":
                self._rotation = (self._rotation + 4.5) % 360.0

            self.update()

        def paintEvent(self, _event: object) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            cx = self.width() / 2.0
            cy = self.height() / 2.0
            radius = 70.0  # fixed orb size; widget is larger to give rings room

            r, g, b = self._PALETTE.get(self._state, (65, 65, 80))

            # Converging rings (listening) — start far out, shrink inward toward orb
            if self._state == "listening":
                outer_gap = 55.0
                for i in range(2):
                    phase = (self._ring_phase + i * 0.5) % 1.0
                    ring_r = radius + outer_gap * (1.0 - phase)
                    fade = 1.0 - max(0.0, (phase - 0.65) / 0.35)
                    alpha = int(fade * 95)
                    if alpha > 0:
                        painter.setPen(QPen(QColor(r, g, b, alpha), 1.5))
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawEllipse(QPointF(cx, cy), ring_r, ring_r)

            # Pulse rings (speaking) — expand outward, 3 rings, sqrt fade stays bright longer
            if self._state == "speaking":
                for i in range(3):
                    phase = (self._ring_phase + i / 3.0) % 1.0
                    ring_r = radius + 55.0 * phase
                    alpha = int((1.0 - phase) ** 0.5 * 190)
                    if alpha > 0:
                        painter.setPen(QPen(QColor(r, g, b, alpha), 2.5))
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawEllipse(QPointF(cx, cy), ring_r, ring_r)

            # Dual counter-rotating arcs (thinking)
            if self._state == "thinking":
                outer = radius + 13
                inner = radius + 6
                # Outer arc — clockwise
                painter.save()
                painter.translate(cx, cy)
                painter.rotate(self._rotation)
                pen = QPen(QColor(r, g, b, 190), 2.5)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawArc(QRectF(-outer, -outer, outer * 2, outer * 2), 0, 200 * 16)
                painter.restore()
                # Inner arc — counter-clockwise, offset start angle, slower
                painter.save()
                painter.translate(cx, cy)
                painter.rotate(-self._rotation * 0.7)
                pen2 = QPen(QColor(r, g, b, 115), 2.0)
                pen2.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen2)
                painter.drawArc(QRectF(-inner, -inner, inner * 2, inner * 2), 90 * 16, 160 * 16)
                painter.restore()

            # Breathing scale (idle = no pulse)
            scale = 1.0 if self._state == "disconnected" else 0.93 + 0.07 * self._breath
            ar = radius * scale

            # Outer soft glow
            glow = QRadialGradient(QPointF(cx, cy), ar * 1.65)
            glow.setColorAt(0.35, QColor(r, g, b, 35))
            glow.setColorAt(1.0, QColor(r, g, b, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(glow))
            painter.drawEllipse(QPointF(cx, cy), ar * 1.65, ar * 1.65)

            if self._avatar:
                # Clip to circle and draw avatar with inner padding
                # img_r uses fixed radius so the image doesn't move during breathing
                padding = 6.0
                img_r = radius - padding
                clip_path = QPainterPath()
                clip_path.addEllipse(QPointF(cx, cy), img_r, img_r)
                painter.save()
                painter.setClipPath(clip_path)
                painter.drawPixmap(int(cx - img_r), int(cy - img_r), self._avatar)
                painter.restore()
                # State-colored ring border
                painter.setPen(QPen(QColor(r, g, b, 220), 3.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(cx, cy), ar, ar)
            else:
                # Fallback: original solid gradient orb
                body = QRadialGradient(QPointF(cx - ar * 0.24, cy - ar * 0.28), ar * 1.25)
                body.setColorAt(0.0, QColor(min(255, r + 95), min(255, g + 95), min(255, b + 95)))
                body.setColorAt(0.55, QColor(r, g, b))
                body.setColorAt(1.0, QColor(max(0, r - 50), max(0, g - 50), max(0, b - 50)))
                painter.setBrush(QBrush(body))
                painter.drawEllipse(QPointF(cx, cy), ar, ar)

            # Specular highlight
            spec = QRadialGradient(QPointF(cx - ar * 0.27, cy - ar * 0.30), ar * 0.52)
            spec.setColorAt(0.0, QColor(255, 255, 255, 72))
            spec.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setBrush(QBrush(spec))
            painter.drawEllipse(QPointF(cx, cy), ar, ar)

            painter.end()

        def mousePressEvent(self, event: object) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()

    # --------------------------------------------------------------- chat ----

    class ChatWidget(QScrollArea):
        """Scrollable chat bubble area — user right, assistant left."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setObjectName("chat")
            self.setWidgetResizable(True)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

            self._container = QWidget()
            self._container.setObjectName("chat_container")
            self._vbox = QVBoxLayout(self._container)
            self._vbox.setContentsMargins(10, 10, 10, 10)
            self._vbox.setSpacing(8)
            self._vbox.addStretch()
            self.setWidget(self._container)

            self._assistant_label: QLabel | None = None
            self._assistant_text: str = ""

        def _make_label(self, object_name: str, text: str = "") -> QLabel:
            label = QLabel(text)
            label.setObjectName(object_name)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            return label

        def _make_row(self, label: QLabel, role: str) -> QWidget:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(0)
            if role == "user":
                rl.addStretch(1)        # 10% gap on left
                rl.addWidget(label, 9)  # 90% bubble
            else:
                rl.addWidget(label, 9)  # 90% bubble
                rl.addStretch(1)        # 10% gap on right
            return row

        def add_user(self, text: str) -> None:
            self._clear()
            self._assistant_label = None
            self._assistant_text = ""
            label = self._make_label("bubble_user", text)
            self._vbox.addWidget(self._make_row(label, "user"))
            self._scroll_bottom()

        def start_assistant(self) -> None:
            label = self._make_label("bubble_assistant")
            self._vbox.addWidget(self._make_row(label, "assistant"))
            self._assistant_label = label
            self._assistant_text = ""
            self._scroll_bottom()

        def append_assistant(self, text: str) -> None:
            if self._assistant_label is None:
                self.start_assistant()
            self._assistant_text += text
            self._assistant_label.setText(self._assistant_text)
            self._scroll_bottom()

        def _push(self, role: str, text: str) -> None:
            label = self._make_label(f"bubble_{role}", text)
            self._vbox.addWidget(self._make_row(label, role))
            self._scroll_bottom()

        def _clear(self) -> None:
            while self._vbox.count() > 1:
                item = self._vbox.takeAt(1)
                if item.widget():
                    item.widget().deleteLater()

        def _scroll_bottom(self) -> None:
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(
                self.verticalScrollBar().maximum()
            ))

    # ------------------------------------------------------------- window ----

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.config = load_config()
            self.runner = AsyncRunner()
            self.bridge = UiBridge()
            self.client: OpenClawClient | None = None
            self.tts = TTSService(self.config, self.show_error, on_audio_done=self.bridge.tts_audio_done.emit)
            self.stt = STTService(self.config, self.show_error)
            self.pending_assistant_text = ""
            self._history_message_count = 0
            self.tts_flush_timer = QTimer(self)
            self.tts_flush_timer.setSingleShot(True)
            self.tts_flush_timer.setInterval(1200)
            self.tts_flush_timer.timeout.connect(self.flush_tts_tail)
            self.response_timeout_timer = QTimer(self)
            self.response_timeout_timer.setSingleShot(True)
            self.response_timeout_timer.setInterval(60_000*3) # 3min timeout
            self.response_timeout_timer.timeout.connect(self._on_response_timeout)
            self.setWindowTitle("Donna")
            self.resize(400, 600)
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
            self.bridge.stt_ready.connect(self._on_stt_ready)
            self.bridge.stt_done.connect(self._on_stt_done)
            self.bridge.tts_audio_done.connect(self._on_tts_audio_done)
            self.bridge.run_ended.connect(self._on_run_ended)
            self.bridge.fallback_history.connect(self._after_fallback_history)
            self.bridge.hotkey_ptt.connect(self._on_hotkey_ptt)
            self.bridge.gateway_disconnected.connect(self._on_gateway_disconnected)

        def _build_ui(self) -> None:
            root = QWidget()
            root.setObjectName("root")
            layout = QVBoxLayout(root)
            layout.setContentsMargins(24, 16, 24, 14)
            layout.setSpacing(6)

            # Top bar: title on left, connect button on right
            top_bar = QHBoxLayout()
            top_bar.setSpacing(12)

            title = QLabel("Donna")
            title.setObjectName("title")
            title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._title_label = title

            self.connect_button = QPushButton("Connect")

            top_bar.addWidget(title)
            top_bar.addStretch()
            top_bar.addWidget(self.connect_button, alignment=Qt.AlignmentFlag.AlignRight)
            layout.addLayout(top_bar)

            layout.addSpacing(6)

            # Orb centred horizontally
            orb_row = QHBoxLayout()
            orb_row.setContentsMargins(0, 0, 0, 0)
            self.orb = OrbWidget()
            orb_row.addStretch()
            orb_row.addWidget(self.orb)
            orb_row.addStretch()
            layout.addLayout(orb_row)

            self.status = QLabel("Disconnected")
            self.status.setObjectName("status_label")
            self.status.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(self.status, alignment=Qt.AlignmentFlag.AlignHCenter)

            layout.addSpacing(4)

            self.chat = ChatWidget()
            layout.addWidget(self.chat, stretch=1)

            self.summary = ""

            self.connect_button.clicked.connect(self._on_connect_button)
            self.orb.clicked.connect(self._on_orb_clicked)

            self.setCentralWidget(root)

        def _on_orb_clicked(self) -> None:
            state = self.orb.state
            if state == "ready":
                self.start_transcription()
            elif state in ("speaking", "thinking"):
                self.stop_current()
            # listening: ignore (STT is running in background)

        def _on_stt_done(self) -> None:
            # If STT finished but produced no text (no send_user_text call), return to ready.
            if self.orb.state in ("listening", "thinking"):
                self.orb.set_state("ready")
                self.set_status("Ready — tap the orb to speak")

        def _on_tts_audio_done(self) -> None:
            # Audio finished playing — return orb to ready if still showing speaking.
            if self.orb.state == "speaking":
                self.orb.set_state("ready")
                self.set_status("Ready — tap the orb to speak")

        def _sync_fields_from_config(self) -> None:
            tts_state = "configured" if self.config.piper_model_path else "missing Piper voice"
            token_state = "token set" if self.config.auth_token else "token missing"
            self.summary = (
                f"{self.config.gateway_url} | session {self.config.session_key} | "
                f"STT {self.config.stt_model}/{self.config.stt_language}/{self.config.stt_device} | "
                f"TTS {tts_state} | {token_state}"
            )
            self._title_label.setToolTip(self.summary)

        def _sync_config_from_fields(self) -> None:
            self.config = load_config()
            self._sync_fields_from_config()

        def save_settings(self) -> None:
            self._sync_config_from_fields()
            self.tts = TTSService(self.config, self.show_error, on_audio_done=self.bridge.tts_audio_done.emit)
            self.stt.shutdown()
            self.stt = STTService(self.config, self.show_error)
            self.set_status("Settings loaded")

        def _on_connect_button(self) -> None:
            if self.client is None:
                self.connect_gateway()
            else:
                self.disconnect_gateway()

        def connect_gateway(self) -> None:
            self.save_settings()
            self.connect_button.setEnabled(False)
            self.set_status("Connecting...")
            self.client = OpenClawClient(
                self.config,
                on_delta=self.bridge.delta.emit,
                on_status=self.bridge.status.emit,
                on_run_ended=self.bridge.run_ended.emit,
                on_disconnect=self.bridge.gateway_disconnected.emit,
            )
            future = self.runner.submit(self.client.connect())
            future.add_done_callback(self.bridge.connected.emit)

        def _after_connect(self, future: Future) -> None:
            try:
                future.result()
            except Exception as exc:
                LOGGER.exception("Gateway connection failed")
                self.connect_button.setText("Connect")
                self.connect_button.setEnabled(True)
                self.orb.set_state("disconnected")
                self.show_error(f"Gateway connection failed: {exc}")
                return
            self.connect_button.setText("Disconnect")
            self.connect_button.setEnabled(True)
            self.orb.set_state("ready")
            self.set_status("Connected — tap the orb to speak")
            self.stt.prewarm_import()

        def disconnect_gateway(self) -> None:
            self.tts.stop()
            self.connect_button.setEnabled(False)
            self.set_status("Disconnecting...")
            if self.client:
                future = self.runner.submit(self.client.abort())
                future.add_done_callback(self.bridge.aborted.emit)
            else:
                self._reset_to_disconnected()
            self.tts.warmup()
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
            payload = response.get("payload", {}) if isinstance(response, dict) else {}
            messages = payload.get("messages", []) if isinstance(payload, dict) else []
            self._history_message_count = len(messages)
            LOGGER.info("History loaded: %s messages", len(messages))

        def _on_run_ended(self) -> None:
            self.response_timeout_timer.stop()
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
                    role, stop_reason, repr(raw_content)[:200],
                )
                body = extract_text(raw_content)
                LOGGER.info("Fallback history: body_chars=%s", len(body))
                if not body:
                    continue
                self.chat.start_assistant()
                self.chat.append_assistant(body)
                self.orb.set_state("speaking")
                self.set_status("Speaking...")
                self.tts.speak(body)
                return

        def start_transcription(self) -> None:
            self.tts.stop()
            self.orb.set_state("thinking")
            self.set_status("Preparing...")
            QTimer.singleShot(50, self._start_transcription_delayed)

        def _on_stt_ready(self) -> None:
            self.orb.set_state("listening")
            self.set_status("Listening...")

        def _start_transcription_delayed(self) -> None:
            worker = TranscriptionThread(
                self.stt,
                on_text=self.bridge.user_text.emit,
                on_error=self.bridge.error.emit,
                on_done=self.bridge.stt_done.emit,
                on_ready=self.bridge.stt_ready.emit,
            )
            worker.start()

        def send_user_text(self, text: str) -> None:
            if not self.client:
                self.show_error("Connect to OpenClaw Gateway before speaking.")
                return
            self.pending_assistant_text = ""
            self.chat.add_user(text)
            LOGGER.info("Sending transcribed user text: chars=%s", len(text))
            self.orb.set_state("thinking")
            self.set_status("Sending...")
            self.response_timeout_timer.start()
            future = self.runner.submit(self.client.send_text(text))
            future.add_done_callback(self.bridge.sent.emit)

        def _after_send(self, future: Future) -> None:
            try:
                future.result()
            except Exception as exc:
                LOGGER.exception("Could not send message")
                self.response_timeout_timer.stop()
                self.show_error(f"Could not send message: {exc}")
                return
            self.orb.set_state("thinking")
            self.set_status("Thinking...")

        def _on_response_timeout(self) -> None:
            if self.orb.state != "thinking":
                return
            if not self.client or not self.client.connected:
                LOGGER.warning("Response timeout — connection is dead, resetting")
                self._reset_to_disconnected()
                self.set_status("Connection lost — click Connect to reconnect")
                return
            LOGGER.warning("Response timeout — falling back to history fetch")
            self._fetch_fallback_history()

        def handle_delta(self, delta: AssistantDelta) -> None:
            text_to_append = delta.text
            if delta.final and self.pending_assistant_text and text_to_append.startswith(self.pending_assistant_text):
                text_to_append = text_to_append[len(self.pending_assistant_text):]

            if text_to_append:
                if not self.pending_assistant_text:
                    self.response_timeout_timer.stop()
                    self.orb.set_state("speaking")
                    self.chat.start_assistant()
                self.pending_assistant_text += text_to_append
                self.chat.append_assistant(text_to_append)
                self.tts.speak_delta(text_to_append, final=delta.final)
                if not delta.final:
                    self.tts_flush_timer.start()
            if delta.final:
                self.response_timeout_timer.stop()
                self.tts_flush_timer.stop()
                self.tts.speak_delta("", final=True)
            if delta.final:
                self.pending_assistant_text = ""
                # Orb stays "speaking" until on_audio_stream_stop fires via _on_tts_audio_done

        def flush_tts_tail(self) -> None:
            LOGGER.info("Flushing TTS after assistant stream idle")
            self.tts.speak_delta("", final=True)

        def stop_current(self) -> None:
            self.tts.stop()
            if self.client:
                future = self.runner.submit(self.client.abort())
                future.add_done_callback(self.bridge.aborted.emit)
            self.orb.set_state("thinking")
            self.set_status("Stopping...")

        def _reset_to_disconnected(self) -> None:
            self.client = None
            self.orb.set_state("disconnected")
            self.connect_button.setText("Connect")
            self.connect_button.setEnabled(True)
            self.set_status("Disconnected")

        def _on_gateway_disconnected(self, reason: str) -> None:
            self.response_timeout_timer.stop()
            LOGGER.warning("Gateway connection lost: %s", reason)
            self._reset_to_disconnected()
            self.set_status(f"Connection lost — click Connect to reconnect")

        def _after_abort(self, future: Future) -> None:
            disconnecting = self.connect_button.text() == "Disconnect" and not self.connect_button.isEnabled()
            try:
                future.result()
            except Exception as exc:
                LOGGER.exception("Abort failed")
                self._reset_to_disconnected()
                self.set_status(f"Connection lost — click Connect to reconnect ({exc})")
                return
            if disconnecting:
                self._reset_to_disconnected()
            else:
                self.orb.set_state("ready")
                self.set_status("Ready")

        def set_status(self, text: str) -> None:
            self.status.setText(text)

        def show_error(self, message: str) -> None:
            LOGGER.error(message)
            self.set_status(message)
            QMessageBox.warning(self, "OpenClaw Voice", message)

        def _on_hotkey_ptt(self) -> None:
            if self.orb.state == "ready":
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
    app.setStyleSheet(_DARK_STYLESHEET)
    _icon_path = Path(__file__).parent.parent / "assets" / "voice-claw.ico"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))
    window = MainWindow()
    window.show()
    return app.exec()
