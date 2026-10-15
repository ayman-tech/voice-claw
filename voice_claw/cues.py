"""Interruptible, local state acknowledgments; originals live in assets/melanie11."""
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtMultimedia import QSoundEffect

LOGGER = logging.getLogger(__name__)
CUE_DIR = Path(__file__).resolve().parent.parent / 'assets' / 'melanie11' / 'playback'
CUES = {
    'listening': ('ya', 'uhhuh', 'yes', 'hey'),
    'thinking': ('let-me-see', 'let-me-think', 'one-moment', 'on-it', 'okay', 'sure'),
    'retry': ('say-that-again',),
    'disconnected': ('i-lost-the-connection',),
}


class CuePlayer(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sound = QSoundEffect(self)
        self._sound.setVolume(0.8)
        self._sound.playingChanged.connect(self._playing_changed)
        self._sound.statusChanged.connect(self._status_changed)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._finish)
        self._done: Callable[[], None] | None = None
        self._started = False
        self._last: dict[str, str] = {}

    def play(self, kind: str, done: Callable[[], None] | None = None) -> None:
        self.stop()
        choices = [name for name in CUES[kind] if (CUE_DIR / f'{name}.wav').is_file()]
        if not choices:
            LOGGER.warning('No audio cues available for %s', kind)
            if done:
                done()
            return
        alternatives = [name for name in choices if name != self._last.get(kind)]
        name = random.choice(alternatives or choices)
        self._last[kind] = name
        self._done = done
        self._sound.setSource(QUrl.fromLocalFile(str(CUE_DIR / f'{name}.wav')))
        # Release the microphone even if the audio device or decoder fails.
        self._timeout.start(5000)
        LOGGER.info('Playing %s cue: %s', kind, name)
        self._sound.play()

    def stop(self) -> None:
        self._done = None
        self._started = False
        self._timeout.stop()
        self._sound.stop()

    def _playing_changed(self) -> None:
        if self._sound.isPlaying():
            self._started = True
        elif self._started:
            self._finish()

    def _status_changed(self) -> None:
        if self._sound.status() == QSoundEffect.Status.Error:
            LOGGER.warning('Audio cue failed to load')
            self._finish()

    def _finish(self) -> None:
        done, self._done = self._done, None
        self.stop()
        if done:
            # Let speaker audio decay before enabling microphone capture.
            QTimer.singleShot(120, done)
