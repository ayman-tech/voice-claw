"""Speech-to-text and text-to-speech runtime helpers."""

from __future__ import annotations

import re
import threading
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig


LOGGER = logging.getLogger(__name__)
SENTENCE_RE = re.compile(r"(.+?[.!?](?:\s+|$))", re.DOTALL)

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"\*(.+?)\*")
_MD_CODE_RE = re.compile(r"`(.+?)`")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_SENTENCE_SPACE_RE = re.compile(r"([.!?])([A-Z])")


def _clean_for_tts(text: str) -> str:
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _SENTENCE_SPACE_RE.sub(r"\1 \2", text)
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
    def __init__(self, config: AppConfig, on_error: Callable[[str], None] | None = None) -> None:
        self.config = config
        self.on_error = on_error

    def prewarm_import(self) -> None:
        def _do() -> None:
            try:
                from RealtimeSTT import AudioToTextRecorder  # noqa: F401
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True, name="stt-import-warmup").start()

    def transcribe_once(self, on_ready: Callable[[], None] | None = None) -> str:
        from RealtimeSTT import AudioToTextRecorder

        def on_recording_stop() -> None:
            LOGGER.info("STT: recording stopped — transcribing")
            _play_sound(_SOUND_SHORT)

        kwargs = {
            "model": self.config.stt_model,
            "language": self.config.stt_language or "en",
            "device": self.config.stt_device,
            "compute_type": self.config.stt_compute_type,
            "post_speech_silence_duration": self.config.stt_silence_duration,
            "spinner": False, # set to true when using console
            "on_recording_stop": on_recording_stop,
        }
        if self.config.stt_initial_prompt:
            kwargs["initial_prompt"] = self.config.stt_initial_prompt
        LOGGER.info(
            "Starting STT: model=%s language=%s device=%s",
            self.config.stt_model,
            self.config.stt_language,
            self.config.stt_device,
        )
        recorder = AudioToTextRecorder(**kwargs)
        LOGGER.info("STT: recorder ready — speak now")
        _play_sound(_SOUND_LONG)
        if on_ready:
            on_ready()
        try:
            text = recorder.text().strip()
            LOGGER.info("STT completed: chars=%s", len(text))
            return text
        except Exception as exc:  # pragma: no cover - depends on microphone/model backend
            if self.on_error:
                self.on_error(f"STT failed: {exc}")
            return ""
        finally:
            self._shutdown_recorder(recorder)

    def shutdown(self) -> None:
        pass

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
