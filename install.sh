#!/usr/bin/env bash
# Batsy installer for Fedora. Run from the repo folder:  ./install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/batsy"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/batsy"
VENV="$DATA/venv"
BIN="$HOME/.local/bin"
VOSK_MODEL="vosk-model-small-en-us-0.15"
PARAKEET_MODEL="sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
PIPER_VOICE="en_GB-alan-medium"
PIPER_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium"

say() { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }

say "Installing system packages (needs your password)"
PKGS=(python3 python3-pip python3-devel gcc gcc-c++ portaudio portaudio-devel espeak-ng
      pipewire-utils alsa-utils libnotify ffmpeg-free gnome-screenshot brightnessctl playerctl
      lm_sensors ydotool v4l-utils unzip curl git tesseract tesseract-langpack-eng
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
"$VENV/bin/pip" install -q sounddevice vosk opencv-python ddgs pymupdf pytesseract python-docx
"$VENV/bin/pip" install -q PySide6 || echo "  PySide6 failed to install; the full-screen overlay will be off."
"$VENV/bin/pip" install -q piper-tts || echo "  piper-tts failed to install; Batsy will speak with espeak-ng."
"$VENV/bin/pip" install -q pyvista pyvistaqt trimesh manifold3d scipy networkx shapely \
  || echo "  3D viewer libraries failed to install; the 3D viewer will be off."
"$VENV/bin/pip" install -q cascadio || echo "  (STEP file support not available; STL/OBJ/3MF/GLB still work)"
"$VENV/bin/pip" install -q faster-whisper || echo "  faster-whisper failed to install; Batsy will use Vosk for speech recognition."
"$VENV/bin/pip" install -q sherpa-onnx || echo "  sherpa-onnx failed to install; Batsy will use Whisper for speech recognition."

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

say "Downloading the speech recognition model ($PARAKEET_MODEL, ~480 MB)"
if [ ! -d "$DATA/models/$PARAKEET_MODEL" ]; then
  curl -fL --progress-bar -o /tmp/parakeet.tar.bz2 \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$PARAKEET_MODEL.tar.bz2" \
    && tar xjf /tmp/parakeet.tar.bz2 -C "$DATA/models" && rm /tmp/parakeet.tar.bz2 \
    || echo "  (skipped; Batsy will use Whisper)"
fi

say "Pre-loading the fallback speech recognition model"
"$VENV/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')" \
  2>/dev/null || echo "  (skipped)"

say "Installing arduino-cli"
if ! command -v arduino-cli >/dev/null && [ ! -x "$BIN/arduino-cli" ]; then
  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR="$BIN" sh
fi
ARDUINO="$(command -v arduino-cli || echo "$BIN/arduino-cli")"
"$ARDUINO" config init --overwrite >/dev/null 2>&1 || true
"$ARDUINO" config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json >/dev/null 2>&1 || true
"$ARDUINO" core update-index || echo "  (couldn't update the board index; run: arduino-cli core update-index)"
"$ARDUINO" core install arduino:avr || echo "  (couldn't install Arduino AVR boards; run: arduino-cli core install arduino:avr)"

say "Installing the local AI brain (Ollama + qwen2.5:3b, ~2 GB download)"
if ! command -v ollama >/dev/null; then
  sudo dnf install -y ollama || curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama 2>/dev/null || (nohup ollama serve >/dev/null 2>&1 &)
for _ in $(seq 1 30); do curl -fs http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
ollama pull qwen2.5:3b || echo "  (model download failed; run: ollama pull qwen2.5:3b)"

say "Installing Claude Code (Batsy's expert brain, runs on your Claude Pro plan)"
if ! command -v claude >/dev/null && [ ! -x "$BIN/claude" ]; then
  curl -fsSL https://claude.ai/install.sh | bash \
    || { sudo dnf install -y nodejs npm && npm install -g --prefix "$HOME/.local" @anthropic-ai/claude-code; } \
    || echo "  (Claude Code install failed; see https://docs.claude.com/en/docs/claude-code)"
fi
echo "  Log in once with your Pro account: run  claude  in a terminal, then type /login if it asks."
touch "$CONF/env" && chmod 600 "$CONF/env"
[ -f "$CONF/config.toml" ] || cp "$REPO/packaging/config.example.toml" "$CONF/config.toml"

say "Adding the batsy command and autostart"
ln -sf "$VENV/bin/batsy" "$BIN/batsy"
mkdir -p "$HOME/.config/autostart" "$HOME/.local/share/applications"
sed "s|@BATSY@|$VENV/bin/batsy|" "$REPO/packaging/batsy.desktop" > "$HOME/.config/autostart/batsy.desktop"
cp "$HOME/.config/autostart/batsy.desktop" "$HOME/.local/share/applications/batsy.desktop"

say "Checking everything (batsy selftest)"
"$VENV/bin/batsy" selftest || true
cat <<EOF

Next steps (also in ~/batsy/SETUP_AND_TEST.md):
  1. Log out and back in once (Arduino serial permission + PATH).
  2. Run  claude  once and log in with your Claude Pro account.
  3. Run  batsy selftest --all  and fix anything marked FAIL.
  4. Start Batsy:  batsy -v   (it also starts by itself at login). Say "Batsy" and wait for the chime.
EOF
