"""Application configuration persistence."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from platformdirs import user_config_dir
except ModuleNotFoundError:  # pragma: no cover - dependency is installed in normal app use
    def user_config_dir(appname: str, appauthor: str | None = None) -> str:
        return str(Path.home() / ".config" / appname)


APP_NAME = "OpenClaw Voice"
APP_AUTHOR = "OpenClaw"


@dataclass(slots=True)
class AppConfig:
    gateway_url: str = "ws://127.0.0.1:18789"
    auth_token: str = ""
    auth_password: str = ""
    model: str = "openclaw/default"
    agent_id: str = ""
    session_key: str = "webchat:voice-desktop"
    message_channel: str = "webchat"
    device_id: str = ""
    stt_model: str = "base"
    stt_language: str = "en"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_silence_duration: float = 1.5
    stt_initial_prompt: str = "Donna"
    piper_executable: str = ""
    piper_model_path: str = ""
    piper_config_path: str = ""


ENV_CONFIG_MAP = {
    "VOICECLAW_GATEWAY_URL": "gateway_url",
    "VOICECLAW_TOKEN": "auth_token",
    "VOICECLAW_PASSWORD": "auth_password",
    "VOICECLAW_MODEL": "model",
    "VOICECLAW_AGENT_ID": "agent_id",
    "VOICECLAW_SESSION_KEY": "session_key",
    "VOICECLAW_MESSAGE_CHANNEL": "message_channel",
    "VOICECLAW_STT_MODEL": "stt_model",
    "VOICECLAW_STT_LANGUAGE": "stt_language",
    "VOICECLAW_STT_DEVICE": "stt_device",
    "VOICECLAW_STT_COMPUTE_TYPE": "stt_compute_type",
    "VOICECLAW_STT_SILENCE": "stt_silence_duration",
    "VOICECLAW_STT_PROMPT": "stt_initial_prompt",
    "PIPER_EXECUTABLE": "piper_executable",
    "PIPER_MODEL_PATH": "piper_model_path",
    "PIPER_CONFIG_PATH": "piper_config_path",
}


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def config_path() -> Path:
    return Path(user_config_dir(APP_NAME, APP_AUTHOR)) / "settings.json"


def load_config(path: Path | None = None, dotenv_path: Path = Path(".env")) -> AppConfig:
    load_dotenv(dotenv_path)
    target = path or config_path()
    if not target.exists():
        config = _with_env_overrides(AppConfig(device_id=str(uuid.uuid4())))
        save_config(config, target)
        return config

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _with_env_overrides(AppConfig())

    defaults = asdict(AppConfig())
    merged: dict[str, Any] = defaults | {
        key: value for key, value in raw.items() if key in defaults
    }
    config = _with_env_overrides(AppConfig(**merged))
    if not config.device_id:
        config = AppConfig(**{**asdict(config), "device_id": str(uuid.uuid4())})
        save_config(config, target)
    return config


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(asdict(config), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


def _with_env_overrides(config: AppConfig) -> AppConfig:
    values = asdict(config)
    for env_name, field_name in ENV_CONFIG_MAP.items():
        env_value = os.environ.get(env_name)
        if env_value:
            values[field_name] = env_value
    return AppConfig(**values)
