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
      lm_sensors ydotool v4l-utils unzip curl git
      xcb-util-cursor libxkbcommon-x11 qt6-qtwayland)
if ! sudo dnf install -y "${PKGS[@]}"; then
  # older dnf has no --skip-unavailable; fall back to one at a time
  for p in "${PKGS[@]}"; do sudo dnf install -y "$p" || echo "  (skipped $p)"; done
fi

say "Giving you access to Arduino serial ports (dialout group)"
sudo usermod -aG dialout "$USER" || true

say "Creating Python environment in $VENV"
mkdir -p "$DATA/models" "$CONF" "$BIN"
# Speech/voice libraries ship wheels for 3.12 first; newer Fedora defaults to a newer Python.
sudo dnf install -y python3.12 python3.12-devel >/dev/null 2>&1 || true
PY="$(command -v python3.12 || command -v python3)"
echo "  using $($PY --version)"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip wheel
"$VENV/bin/pip" install -q -e "$REPO"
"$VENV/bin/pip" install -q sounddevice vosk opencv-python ddgs
"$VENV/bin/pip" install -q PySide6 || echo "  PySide6 failed to install; the full-screen overlay will be off."
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

say "Installing the local AI brain (Ollama + qwen2.5:3b, ~2 GB download)"
if ! command -v ollama >/dev/null; then
  sudo dnf install -y ollama || curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama 2>/dev/null || (ollama serve >/dev/null 2>&1 &)
sleep 2
ollama pull qwen2.5:3b

say "Optional: Claude Code for 'ask Claude ...' (uses your Claude Pro plan)"
if command -v claude >/dev/null; then
  echo "  Claude Code found. Run 'claude' once and log in with your Pro account if you haven't."
else
  echo "  Not installed. To enable: sudo dnf install -y nodejs && npm install -g @anthropic-ai/claude-code && claude"
fi
touch "$CONF/env" && chmod 600 "$CONF/env"
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
