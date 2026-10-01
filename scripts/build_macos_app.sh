#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="VoiceClaw"
APP_DIR="$ROOT_DIR/dist/$APP_NAME.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
EXECUTABLE="$MACOS_DIR/$APP_NAME"
ICONSET_DIR="$ROOT_DIR/dist/$APP_NAME.iconset"
ICON_PNG="$ROOT_DIR/dist/$APP_NAME-icon.png"
ICON_ICNS="$RESOURCES_DIR/$APP_NAME.icns"

rm -rf "$APP_DIR" "$ICONSET_DIR" "$ICON_PNG"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$ICONSET_DIR"

cat > "$CONTENTS_DIR/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleDisplayName</key>
  <string>VoiceClaw</string>
  <key>CFBundleExecutable</key>
  <string>$APP_NAME</string>
  <key>CFBundleIconFile</key>
  <string>$APP_NAME</string>
  <key>CFBundleIdentifier</key>
  <string>com.openclaw.voiceclaw</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>VoiceClaw</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>VoiceClaw needs microphone access for push-to-talk transcription.</string>
  <key>NSSpeechRecognitionUsageDescription</key>
  <string>VoiceClaw uses local speech recognition to transcribe your voice.</string>
</dict>
</plist>
PLIST

cat > "$EXECUTABLE" <<LAUNCHER
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$ROOT_DIR"
cd "\$PROJECT_DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:\$PATH"
export PYTHONPATH="\$PROJECT_DIR:\${PYTHONPATH:-}"

if [[ ! -x "\$PROJECT_DIR/.venv/bin/python" ]]; then
  if ! command -v uv >/dev/null 2>&1; then
    osascript -e 'display alert "VoiceClaw cannot start" message "The project virtual environment is missing, and uv was not found on PATH."'
    exit 1
  fi
  uv sync
fi

exec "\$PROJECT_DIR/.venv/bin/python" "\$PROJECT_DIR/main.pyw"
LAUNCHER
chmod +x "$EXECUTABLE"

if [[ -f "$ROOT_DIR/assets/voice-claw.ico" ]] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  sips -s format png "$ROOT_DIR/assets/voice-claw.ico" --out "$ICON_PNG" >/dev/null
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$ICON_PNG" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
    sips -z "$((size * 2))" "$((size * 2))" "$ICON_PNG" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET_DIR" -o "$ICON_ICNS"
  rm -rf "$ICONSET_DIR" "$ICON_PNG"
fi

echo "Created $APP_DIR"
