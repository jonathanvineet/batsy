#!/usr/bin/env bash
# Batsy installer for Fedora. Run from the repo folder:  ./install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/batsy"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/batsy"
VENV="$DATA/venv"
BIN="$HOME/.local/bin"
VOSK_MODEL="vosk-model-small-en-us-0.15"
PIPER_VOICE="en_GB-alan-medium"
PIPER_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium"

say() { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }

say "Installing system packages (needs your password)"
PKGS=(python3 python3-pip python3-devel gcc gcc-c++ portaudio portaudio-devel espeak-ng
      pipewire-utils alsa-utils libnotify ffmpeg-free gnome-screenshot brightnessctl playerctl
      lm_sensors ydotool v4l-utils unzip curl git)
if ! sudo dnf install -y "${PKGS[@]}"; then
  # older dnf has no --skip-unavailable; fall back to one at a time
  for p in "${PKGS[@]}"; do sudo dnf install -y "$p" || echo "  (skipped $p)"; done
fi

say "Giving you access to Arduino serial ports (dialout group)"
sudo usermod -aG dialout "$USER" || true

say "Creating Python environment in $VENV"
mkdir -p "$DATA/models" "$CONF" "$BIN"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip wheel
"$VENV/bin/pip" install -q -e "$REPO"
"$VENV/bin/pip" install -q sounddevice vosk opencv-python
"$VENV/bin/pip" install -q piper-tts || echo "  piper-tts failed to install; Batsy will speak with espeak-ng."
"$VENV/bin/pip" install -q faster-whisper || echo "  faster-whisper failed to install; Batsy will use Vosk for speech recognition."

say "Downloading wake-word model ($VOSK_MODEL)"
if [ ! -d "$DATA/models/$VOSK_MODEL" ]; then
  curl -fL --progress-bar -o /tmp/vosk.zip "https://alphacephei.com/vosk/models/$VOSK_MODEL.zip"
  unzip -q /tmp/vosk.zip -d "$DATA/models" && rm /tmp/vosk.zip
fi

say "Downloading Batsy's voice ($PIPER_VOICE)"
mkdir -p "$DATA/models/piper"
for f in "$PIPER_VOICE.onnx" "$PIPER_VOICE.onnx.json"; do
  [ -f "$DATA/models/piper/$f" ] || curl -fL --progress-bar -o "$DATA/models/piper/$f" "$PIPER_URL/$f"
done

say "Pre-loading the speech recognition model"
"$VENV/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('small.en', device='cpu', compute_type='int8')" \
  2>/dev/null || echo "  (skipped)"

say "Installing arduino-cli"
if ! command -v arduino-cli >/dev/null && [ ! -x "$BIN/arduino-cli" ]; then
  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR="$BIN" sh
fi
ARDUINO="$(command -v arduino-cli || echo "$BIN/arduino-cli")"
"$ARDUINO" config init --overwrite >/dev/null 2>&1 || true
"$ARDUINO" config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json >/dev/null 2>&1 || true
"$ARDUINO" core update-index
"$ARDUINO" core install arduino:avr

say "Claude API key"
if [ ! -f "$CONF/env" ] || ! grep -q ANTHROPIC_API_KEY "$CONF/env"; then
  read -rsp "Paste your Claude API key (sk-ant-...): " KEY; echo
  umask 077
  printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" > "$CONF/env"
  chmod 600 "$CONF/env"
else
  echo "  already set in $CONF/env"
fi
[ -f "$CONF/config.toml" ] || cp "$REPO/packaging/config.example.toml" "$CONF/config.toml"

say "Adding the batsy command and autostart"
ln -sf "$VENV/bin/batsy" "$BIN/batsy"
mkdir -p "$HOME/.config/autostart" "$HOME/.local/share/applications"
sed "s|@BATSY@|$VENV/bin/batsy|" "$REPO/packaging/batsy.desktop" > "$HOME/.config/autostart/batsy.desktop"
cp "$HOME/.config/autostart/batsy.desktop" "$HOME/.local/share/applications/batsy.desktop"

say "Done"
"$VENV/bin/batsy" doctor || true
cat <<EOF

Batsy will start automatically when you log in.
Start it now:        batsy -v          (say "Batsy" and wait for the chime)
Type instead:        batsy chat
Log out and back in once so the Arduino serial permission applies.
EOF
