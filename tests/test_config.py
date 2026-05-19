from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from voice_claw.config import AppConfig, load_config, load_dotenv, save_config


class ConfigTests(TestCase):
    def test_load_config_defaults_when_missing(self) -> None:
        config = load_config(
            Path("missing-test-settings.json"),
            dotenv_path=Path("missing-test-env-file"),
        )

        self.assertEqual(config.gateway_url, "ws://127.0.0.1:18789")
        self.assertEqual(config.model, "openclaw/default")
        self.assertEqual(config.session_key, "webchat:voice-desktop")
        self.assertEqual(config.stt_language, "en")

    def test_save_and_load_config_roundtrip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = AppConfig(
                gateway_url="ws://localhost:9999",
                auth_token="token",
                model="openclaw/research",
                session_key="webchat:custom",
                piper_model_path="voice.onnx",
            )

            save_config(original, path)
            loaded = load_config(path, dotenv_path=Path(directory) / "missing.env")

            self.assertEqual(loaded, original)

    def test_load_config_uses_claw_token_when_no_saved_token(self) -> None:
        with patch.dict("os.environ", {"VOICECLAW_TOKEN": "env-token"}, clear=False):
            config = load_config(
                Path("missing-test-settings.json"),
                dotenv_path=Path("missing-test-env-file"),
            )

        self.assertEqual(config.auth_token, "env-token")

    def test_claw_token_overrides_saved_token(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_config(AppConfig(auth_token="saved-token"), path)

            with patch.dict("os.environ", {"VOICECLAW_TOKEN": "env-token"}, clear=False):
                config = load_config(path, dotenv_path=Path(directory) / "missing.env")

        self.assertEqual(config.auth_token, "env-token")

    def test_env_overrides_voice_paths(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "PIPER_MODEL_PATH": "models/piper/voice.onnx",
                "PIPER_CONFIG_PATH": "models/piper/voice.onnx.json",
                "VOICECLAW_STT_LANGUAGE": "en",
            },
            clear=False,
        ):
            config = load_config(
                Path("missing-test-settings.json"),
                dotenv_path=Path("missing-test-env-file"),
            )

        self.assertEqual(config.piper_model_path, "models/piper/voice.onnx")
        self.assertEqual(config.piper_config_path, "models/piper/voice.onnx.json")
        self.assertEqual(config.stt_language, "en")

    def test_load_dotenv_sets_claw_token(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("VOICECLAW_TOKEN=dotenv-token\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                load_dotenv(path)
                self.assertEqual(__import__("os").environ["VOICECLAW_TOKEN"], "dotenv-token")
