"""Speech-to-text and text-to-speech runtime helpers."""

from __future__ import annotations

import re
import threading
import time
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .app_logging import preview
from .config import AppConfig


LOGGER = logging.getLogger(__name__)
SENTENCE_RE = re.compile(r"(.+?(?<![A-Z])[.!?](?:\s+|$))", re.DOTALL)

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"\*(.+?)\*")
_MD_CODE_RE = re.compile(r"`(.+?)`")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
# Only add space when 2+ chars precede the punctuation — avoids splitting abbreviations like D.C.
_SENTENCE_SPACE_RE = re.compile(r"(\w{2,}[.!?])([A-Z])")
# Strip dots from single-letter abbreviations so D.C. → DC, U.S.A. → USA (prevents "dot" pronunciation)
_ABBREV_RE = re.compile(r"\b([A-Z])\.")


def _clean_for_tts(text: str) -> str:
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _ABBREV_RE.sub(r"\1", text)
    text = _SENTENCE_SPACE_RE.sub(r"\1 \2", text)
    text = re.sub(r"\.$", ".  ", text)
    return text

_ASSETS_DIR = Path(__file__).parent.parent / "assets"
_SOUND_LONG = str(_ASSETS_DIR / "interface-long.wav")
_SOUND_SHORT = str(_ASSETS_DIR / "interface-short.wav")


@dataclass(slots=True)
class SentenceBuffer:
    _buffer: str = ""

    def push(self, text: str, final: bool = False) -> list[str]:
        self._buffer += text
        ready: list[str] = []

        while True:
            match = SENTENCE_RE.match(self._buffer)
            if not match:
                break
            sentence = match.group(1).strip()
            self._buffer = self._buffer[match.end() :]
            if sentence:
                ready.append(sentence)

        if final:
            tail = self._buffer.strip()
            self._buffer = ""
            if tail:
                ready.append(tail)

        return ready

    def clear(self) -> None:
        self._buffer = ""


@dataclass(slots=True)
class TTSService:
    config: AppConfig
    on_error: Callable[[str], None] | None = None
    on_audio_done: Callable[[], None] | None = None
    _stream: object | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _buffer: SentenceBuffer = field(default_factory=SentenceBuffer)
    _reported_setup_error: bool = False

    def speak_delta(self, text: str, final: bool = False) -> None:
        for sentence in self._buffer.push(text, final=final):
            self.speak(sentence)

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        if not self.config.piper_model_path:
            if not self._reported_setup_error:
                self._reported_setup_error = True
                self._report_error("TTS is not configured: set the Piper voice .onnx path.")
            return
        try:
            text = _clean_for_tts(text)
            if not text.strip():
                return
            LOGGER.info("Speaking text via TTS: chars=%s", len(text))
            LOGGER.debug("TTS text: %r", preview(text))
            stream = self._get_stream()
            stream.feed(text)
            stream.play_async()
        except Exception as exc:  # pragma: no cover - depends on audio backend
            self._report_error(f"TTS failed: {exc}")

    def stop(self) -> None:
        self._buffer.clear()
        with self._lock:
            stream = self._stream
        if stream is None:
            return
        try:
            LOGGER.info("Stopping TTS")
            stream.stop()
        except Exception as exc:  # pragma: no cover - depends on audio backend
            self._report_error(f"Could not stop TTS: {exc}")

    def _get_stream(self) -> object:
        with self._lock:
            if self._stream is not None:
                return self._stream

            from RealtimeTTS import PiperEngine, PiperVoice, TextToAudioStream

            kwargs = {}
            if self.config.piper_executable:
                kwargs["piper_path"] = self.config.piper_executable
            if self.config.piper_model_path:
                kwargs["voice"] = PiperVoice(
                    model_file=self.config.piper_model_path,
                    config_file=self.config.piper_config_path or None,
                )

            LOGGER.info(
                "Initializing Piper TTS: executable=%s model_configured=%s config_configured=%s",
                self.config.piper_executable or "default",
                bool(self.config.piper_model_path),
                bool(self.config.piper_config_path),
            )
            engine = PiperEngine(**kwargs)
            on_done = self.on_audio_done
            self._stream = TextToAudioStream(
                engine,
                on_audio_stream_stop=on_done,
            )
            return self._stream

    def warmup(self) -> None:
        if not self.config.piper_model_path:
            return
        threading.Thread(target=self._get_stream, daemon=True, name="tts-warmup").start()

    def _report_error(self, message: str) -> None:
        LOGGER.error(message)
        if self.on_error:
            self.on_error(message)


def _play_sound(path: str) -> None:
    """Play a WAV file without blocking the caller."""
    try:
        import sys
        if sys.platform == "win32":
            import winsound
            threading.Thread(
                target=winsound.PlaySound,
                args=(path, winsound.SND_FILENAME),
                daemon=True,
            ).start()
        elif sys.platform == "darwin":
            import subprocess
            threading.Thread(
                target=subprocess.run,
                args=(["afplay", path],),
                kwargs={"capture_output": True},
                daemon=True,
            ).start()
    except Exception:
        pass


class STTService:
    """Persistent speech recorder with optional "Hey Donna" wake word.

    One AudioToTextRecorder is built once (Whisper model + wake word model load
    a single time) and reused for every turn. With a wake word model configured
    the recorder listens for it continuously; `trigger_manual` bypasses it for
    orb clicks and the global hotkey. Without a wake word model the recorder
    only listens after `trigger_manual`.
    """

    def __init__(self, config: AppConfig, on_error: Callable[[str], None] | None = None) -> None:
        self.config = config
        self.on_error = on_error
        self._recorder: object | None = None
        self._recorder_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._manual = threading.Event()
        self._wake_fired = threading.Event()
        self._t_speech_end = 0.0
        self._thread: threading.Thread | None = None
        self._wake_enabled = False
        self._on_wake: Callable[[], None] | None = None
        self._on_idle: Callable[[], None] | None = None

    @property
    def wake_enabled(self) -> bool:
        return self._wake_enabled

    def _wake_model_path(self) -> str:
        if not self.config.wake_word_enabled or not self.config.wake_word_model_path:
            return ""
        path = Path(self.config.wake_word_model_path)
        if not path.is_absolute() and not path.exists():
            path = _ASSETS_DIR.parent / path
        if path.exists():
            return str(path)
        LOGGER.warning("Wake word model not found at %s — push-to-talk only", path)
        return ""

    def prewarm(self) -> None:
        threading.Thread(target=self._get_recorder, daemon=True, name="stt-warmup").start()

    def _get_recorder(self) -> object | None:
        with self._recorder_lock:
            if self._recorder is not None or self._shutdown.is_set():
                return self._recorder
            try:
                from RealtimeSTT import AudioToTextRecorder

                def on_recording_stop() -> None:
                    self._t_speech_end = time.monotonic()
                    LOGGER.info("STT: recording stopped — transcribing")
                    _play_sound(_SOUND_SHORT)

                def on_wakeword_detected() -> None:
                    # The recorder fires this on every audio frame the score stays above the
                    # threshold (~17x per utterance); react only to the first until the turn ends.
                    if self._wake_fired.is_set():
                        return
                    self._wake_fired.set()
                    LOGGER.info("Wake word detected")
                    _play_sound(_SOUND_LONG)
                    if self._on_wake:
                        self._on_wake()

                def on_wakeword_timeout() -> None:
                    self._wake_fired.clear()
                    LOGGER.info("Wake word heard but no speech followed")
                    if self._on_idle:
                        self._on_idle()

                wake_model = self._wake_model_path()
                kwargs: dict[str, object] = {
                    "model": self.config.stt_model,
                    "language": "en",
                    "device": self.config.stt_device,
                    "compute_type": self.config.stt_compute_type,
                    "post_speech_silence_duration": self.config.stt_silence_duration,
                    "spinner": False,  # set to true when using console
                    "on_recording_stop": on_recording_stop,
                }
                if self.config.stt_initial_prompt:
                    kwargs["initial_prompt"] = self.config.stt_initial_prompt
                if wake_model:
                    kwargs.update(
                        wakeword_backend="openwakeword",
                        openwakeword_model_paths=wake_model,
                        wake_words_sensitivity=self.config.wake_word_sensitivity,
                        wake_word_timeout=5.0,
                        on_wakeword_detected=on_wakeword_detected,
                        on_wakeword_timeout=on_wakeword_timeout,
                    )
                LOGGER.info(
                    "Starting STT: model=%s device=%s wake_word=%s",
                    self.config.stt_model,
                    self.config.stt_device,
                    bool(wake_model),
                )
                self._recorder = AudioToTextRecorder(**kwargs)
                self._wake_enabled = bool(wake_model)
                if not self._wake_enabled:
                    # Push-to-talk only: keep the mic closed until trigger_manual.
                    self._recorder.set_microphone(False)  # type: ignore[attr-defined]
                LOGGER.info("STT: recorder ready")
            except Exception as exc:  # pragma: no cover - depends on microphone/model backend
                LOGGER.exception("STT init failed")
                if self.on_error:
                    self.on_error(f"STT failed: {exc}")
            return self._recorder

    def start(
        self,
        on_text: Callable[[str], None],
        on_idle: Callable[[], None],
        on_wake: Callable[[], None] | None = None,
    ) -> None:
        """Start the listen loop (idempotent). `on_idle` fires when a turn yields no text."""
        self._on_wake = on_wake
        self._on_idle = on_idle
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, args=(on_text, on_idle), name="openclaw-stt", daemon=True
        )
        self._thread.start()

    def _loop(self, on_text: Callable[[str], None], on_idle: Callable[[], None]) -> None:
        recorder = self._get_recorder()
        while recorder is not None and not self._shutdown.is_set():
            if not self._wake_enabled:
                self._manual.wait()
                self._manual.clear()
                if self._shutdown.is_set():
                    break
                recorder.listen()  # type: ignore[attr-defined]
            try:
                text = recorder.text().strip()  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover - depends on microphone/model backend
                if self._shutdown.is_set():
                    break
                LOGGER.exception("STT failed")
                if self.on_error:
                    self.on_error(f"STT failed: {exc}")
                break
            if self._shutdown.is_set():
                break
            self._wake_fired.clear()
            took = time.monotonic() - self._t_speech_end if self._t_speech_end else 0.0
            self._t_speech_end = 0.0
            LOGGER.info("STT completed: chars=%s transcribe=%.2fs", len(text), took)
            LOGGER.debug("STT text: %r", preview(text))
            if not self._wake_enabled:
                # Without wake words the recorder re-arms itself for continuous
                # listening after each turn; close the mic and disarm it.
                self.pause()
                recorder.start_recording_on_voice_activity = False  # type: ignore[attr-defined]
                recorder.continuous_listening = False  # type: ignore[attr-defined]
            if text:
                on_text(text)
            else:
                on_idle()

    def trigger_manual(self) -> None:
        """Start listening now without needing the wake word."""
        _play_sound(_SOUND_LONG)
        self._set_microphone(True)
        recorder = self._recorder
        if recorder is not None and self._wake_enabled:
            # Same state the recorder enters after a real wake word detection
            # (the recording worker runs in-process, so this is safe to set here).
            recorder.wake_word_detect_time = time.time()  # type: ignore[attr-defined]
            recorder.wakeword_detected = True  # type: ignore[attr-defined]
        self._manual.set()

    def pause(self) -> None:
        self._set_microphone(False)

    def resume(self) -> None:
        """Re-open the mic for wake word detection (no-op in push-to-talk-only mode)."""
        if self._wake_enabled:
            self._set_microphone(True)

    def _set_microphone(self, on: bool) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        try:
            recorder.set_microphone(on)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - backend cleanup
            LOGGER.warning("Could not set microphone %s: %s", on, exc)

    def shutdown(self) -> None:
        self._shutdown.set()
        self._manual.set()
        with self._recorder_lock:
            recorder, self._recorder = self._recorder, None
        if recorder is not None:
            self._shutdown_recorder(recorder)

    def _shutdown_recorder(self, recorder: object) -> None:
        try:
            LOGGER.info("Shutting down STT recorder")
            recorder.shutdown()  # type: ignore[attr-defined]
        except OSError as exc:  # pragma: no cover - backend cleanup race on Windows
            if getattr(exc, "winerror", None) == 6:
                LOGGER.info("Ignored RealtimeSTT WinError 6 during shutdown")
            else:
                LOGGER.warning("STT shutdown failed: %s", exc)
        except Exception as exc:  # pragma: no cover - backend cleanup
            LOGGER.warning("STT shutdown failed: %s", exc)
