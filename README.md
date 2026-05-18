# OpenClaw Voice

PySide6 desktop voice companion for an existing OpenClaw Gateway session.

The MVP is local-first:

- Push-to-talk voice input.
- RealtimeSTT with faster-whisper for transcription.
- OpenClaw Gateway/WebChat WebSocket for session chat.
- RealtimeTTS with Piper for spoken replies.
- Configurable Piper executable and voice model paths, compatible with current `OHF-Voice/piper1-gpl` style installs and legacy `rhasspy/piper` installs.

## Architecture

```text
┌──────────────────────────────┐
│        PySide6 Desktop       │
│  settings, transcript, PTT   │
└───────────────┬──────────────┘
                │
                │ starts/stops workers
                ▼
┌──────────────────────────────┐        ┌──────────────────────────────┐
│       Voice Input Worker     │        │      Voice Output Worker     │
│ RealtimeSTT + faster-whisper │        │   RealtimeTTS + PiperEngine  │
│ microphone -> transcript text│        │ assistant text -> speaker    │
└───────────────┬──────────────┘        └──────────────▲───────────────┘
                │                                      │
                │ user text                            │ streamed reply text
                ▼                                      │
┌──────────────────────────────┐   WebSocket JSON-RPC  │
│    OpenClaw Gateway Client   │◄──────────────────────┘
│chat.history/send/abort/events│
└───────────────┬──────────────┘
                │
                │ ws://127.0.0.1:18789
                ▼
┌──────────────────────────────┐
│      OpenClaw Gateway        │
│existing agent + session store│
└──────────────────────────────┘
```

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

RealtimeSTT/RealtimeTTS audio dependencies may require platform audio packages. On Windows, install any missing PortAudio/PyAudio wheels as prompted by pip.

## Run

Start OpenClaw separately:

```powershell
openclaw gateway
```

Optionally set the Gateway token through the environment:

```powershell
Copy-Item .env.example .env
# Edit .env and set CLAW_TOKEN. The app reads .env automatically.
```

Then run the app:

```powershell
python main.py
```

Defaults:

- Gateway URL: `ws://127.0.0.1:18789`
- Model target: `openclaw/default`
- Session key: `webchat:voice-desktop`
- STT model: `base`
- STT language: `en`
- STT device: `cpu`
- STT compute type: `int8`
- Piper voice: `models/piper/en_US-hfc_female-medium.onnx`

## Getting The Gateway Token

OpenClaw usually stores the Gateway token in `~/.openclaw/openclaw.json` under `gateway.auth.token`.

Fastest option:

```powershell
openclaw dashboard
```

If you do not have a token yet, generate one:

```powershell
openclaw doctor --generate-gateway-token
```

Then restart or let the Gateway reload, and paste the token into this app's `Token` field.

Alternatively, set `CLAW_TOKEN` in your shell or `.env`. The app uses `CLAW_TOKEN` automatically and it takes precedence over any token saved through the UI.

You can also inspect the config manually:

```powershell
Get-Content $HOME\.openclaw\openclaw.json
```

Look for:

```json
{
  "gateway": {
    "auth": {
      "mode": "token",
      "token": "paste-this-value-into-the-app"
    }
  }
}
```

## How To Use

### Setup (once per session)

1. Start the Gateway and run the app:
   ```powershell
   openclaw gateway
   uv run main.py
   ```
2. Click **Connect** and wait for the status bar to show `Connected`.
3. Confirm `[history loaded]` appears in the transcript.

### Each conversation turn

1. **Click "Push To Talk"** — the status changes to `Listening...`.
   Wait about a second before speaking; the app clears any audio echo first.
2. **Speak your message** naturally, then stop talking.
   The app detects ~1–2 seconds of silence to know you are done, then shows `Sending...`.
3. **Wait for the reply.** Simple responses take 5–15 seconds; tool-use responses (weather, search, etc.) can take 20–40 seconds.
4. The reply appears in the transcript and is spoken aloud.
   Status returns to `Ready` when done.
5. Repeat from step 1.

### Stop button

Use **Stop** only to interrupt a reply mid-speech — for example if the assistant is talking too long or you want to ask a follow-up immediately.
It stops Piper and aborts the current run. Wait a moment, then click **Push To Talk** again.

### What to watch in the transcript

| You see | Means |
|---|---|
| `[history loaded]` | Connected successfully |
| `user: <words>` | Your speech was transcribed and sent |
| `assistant: <reply>` | Response received and spoken |
| `assistant:` with nothing after it | Response came via history fallback — check logs |

### Things to avoid

- Do not click **Push To Talk** while the assistant is still speaking — the microphone will pick up Piper's audio.
- Do not click **Push To Talk** multiple times in a row — each click queues a new recording session.
- Do not close the window mid-response — click **Stop** first.

## Manual Smoke Checklist

1. Start `openclaw gateway`.
2. Confirm Gateway auth details are available if your Gateway requires a token or password.
3. Launch `uv run main.py`.
4. Click `Connect` and confirm `[history loaded]` appears in the transcript.
5. Configure Piper executable, `.onnx` voice model, and optional `.json` config path if not set in `.env`.
6. Click `Push To Talk`, wait ~1 second, speak one turn, then stop talking.
7. Confirm the transcribed text appears as a `user:` message.
8. Wait for the assistant reply to appear in the transcript and be spoken aloud.
9. Click `Stop` during a reply and confirm speech stops and the OpenClaw run aborts.
10. Start another push-to-talk turn and confirm the same session continues.

## Tests

The unit tests cover pure config, Gateway request shaping, event parsing, and sentence buffering.

```powershell
python -m unittest discover -s tests
```

## Logs

The app writes a rotating log file to:

```text
logs/openclaw-voice.log
```

Tokens and passwords are redacted before log lines are written. If Gateway connection or message sending fails, this file is the first thing to inspect.

## Language And Speech

The app defaults STT language to `en` and wraps each voice turn with an instruction asking OpenClaw to reply in English only.

Piper speech requires a voice model:

- `Piper executable`: optional if `piper`/`piper.exe` is already on `PATH`.
- `Piper voice .onnx`: required.
- `Piper config .json`: optional; Piper can often derive it from `<voice>.onnx.json`.
