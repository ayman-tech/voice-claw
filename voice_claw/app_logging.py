"""Application logging with basic secret redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_PATH = Path("logs") / "openclaw-voice.log"
SECRET_PATTERNS = [
    re.compile(r'("token"\s*:\s*")([^"]+)(")', re.IGNORECASE),
    re.compile(r'("password"\s*:\s*")([^"]+)(")', re.IGNORECASE),
    re.compile(r"(Authorization:\s*Bearer\s+)([^\s]+)()", re.IGNORECASE),
    re.compile(r"(VOICECLAW_TOKEN=)(.+)()", re.IGNORECASE),
]


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for pattern in SECRET_PATTERNS:
            rendered = pattern.sub(r"\1***\3", rendered)
        return rendered


def setup_logging(path: Path = LOG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if any(isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", "") == str(path.resolve()) for handler in logger.handlers):
        return path

    handler = RotatingFileHandler(
        path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    logger.addHandler(handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return path


def get_log_path() -> Path:
    return LOG_PATH
