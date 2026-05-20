"""Application logging with basic secret redaction."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path


LOG_DIR = Path("logs")
_KEEP_DAYS = 14

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


def _today_log_path() -> Path:
    return LOG_DIR / f"{date.today()}.log"


def _cleanup_old_logs(keep_days: int = _KEEP_DAYS) -> None:
    cutoff = date.today() - timedelta(days=keep_days)
    for log_file in LOG_DIR.glob("*.log"):
        try:
            file_date = date.fromisoformat(log_file.stem)
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                log_file.unlink()
            except OSError:
                pass


def setup_logging() -> Path:
    path = _today_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _cleanup_old_logs()

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if any(
        isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", "") == str(path.resolve())
        for h in logger.handlers
    ):
        return path

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    logger.addHandler(handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return path


def get_log_path() -> Path:
    return _today_log_path()
