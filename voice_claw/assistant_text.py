"""Turn the Gateway's chat events for a run into the text that is new to the listener.

For one run the Gateway sends `chat` delta events whose `text` is the whole reply so far, then
a terminal event whose `text` is the final reply. The reply can be rewritten along the way
(`replace`), the final text is a re-projection of the same buffer that may drop interim lead-in
fragments, and a slow consumer can miss deltas (the terminal text then carries them). So:

* cumulative text is the source of truth, never the sum of `delta_text`;
* only the part of it that extends what was already emitted is new;
* when the reply was rewritten, only what differs from the previous text is new.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from os.path import commonprefix

from .app_logging import preview
from .openclaw_client import ChatFrame

LOGGER = logging.getLogger(__name__)


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclass(slots=True)
class AssistantTextTracker:
    run_id: str | None = None
    current: str = ""  # reply text as of the last frame
    skipped: int = 0  # frames that added nothing new, for the turn summary

    def reset(self) -> None:
        self.run_id = None
        self.current = ""
        self.skipped = 0

    def feed(self, frame: ChatFrame) -> str:
        """Return the part of this frame's text that has not been emitted yet."""
        if frame.run_id != self.run_id:
            self.reset()
            self.run_id = frame.run_id

        text = frame.text
        if text is None:
            # No cumulative text on the frame: all we have is the increment.
            text = frame.delta_text if frame.replace else self.current + frame.delta_text
        new = self._new_text(text, terminal=frame.terminal)
        if text:
            self.current = text

        if new:
            LOGGER.debug("Tracker emit chars=%s text=%r", len(new), preview(new))
        else:
            self.skipped += 1
            LOGGER.debug("Tracker skip state=%s chars=%s (nothing new)", frame.state, len(text))
        return new

    def _new_text(self, text: str, terminal: bool) -> str:
        current = self.current
        if text == current or not text:
            return ""
        if text.startswith(current):
            return text[len(current):]
        if current.startswith(text):
            return ""  # an older, shorter copy
        if terminal and _norm(text) in _norm(current):
            # The final text re-projects the streamed buffer (trimmed, lead-in dropped).
            return ""
        # The reply was rewritten, or the final carries text from deltas we never saw.
        return text[len(commonprefix([current, text])):]
