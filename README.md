# Batsy

A voice assistant for your Fedora laptop, powered by Claude. Say **"Batsy"** and wait for the chime, or say it all at once ("Batsy, what's eating my CPU?"). Batsy can run commands, read and write files, watch the system, see your screen, program Arduino boards, set timers and monitors, remember things about you, and write new skills for itself.

## Install (Fedora)

```bash
cd batsy
./install.sh          # asks for your sudo password and your Claude API key
batsy -v              # start it now; it also starts automatically at login
```

Log out and back in once so the Arduino serial-port permission (`dialout` group) applies. Run `batsy doctor` to check the setup.

## Using it

| You say | What happens |
|---|---|
| "Batsy, how's my battery and temperature?" | live readings from `system_status` |
| "Batsy, open Firefox and VS Code" | launches apps in the background |
| "Batsy, set volume to 40 and pause the music" | `wpctl`, `playerctl` |
| "Batsy, write a sketch that blinks pin 13 twice a second and upload it" | writes it, compiles, fixes errors, flashes, reads serial to check |
| "Batsy, what's on my screen?" | takes a screenshot and looks at it |
| "Batsy, tell me when my build finishes" / "when the website is back up" | background job or monitor that speaks up when it happens |
| "Batsy, remind me in 20 minutes to check the oven" | timer (survives restarts) |
| "Batsy, remember my ESP32 is on ttyUSB0" | long-term memory |
| "Batsy, make yourself a skill that summarises my unread GitHub notifications" | writes the Python, tests it, saves it for next time |
| "Batsy, new conversation" | clears the conversation (long-term memory is kept) |
| "Batsy, stop" | stops talking |

- **Follow-ups:** after Batsy answers, it listens for about 6 seconds, so you can just keep talking without saying "Batsy" again. The whole conversation is saved and survives restarts. Long histories are compacted automatically.
- **Interrupting:** say "Batsy" while it's talking.
- **Risky actions** (`rm -rf`, `dnf remove`, shutdown, disk tools, force-push, etc.) are confirmed by voice first. Root commands go through `pkexec`, which shows the normal GNOME password dialog.
- **Typing:** `batsy chat` (or `batsy ask "..."`) talks to the running assistant through a local socket.

## Files

| Path | What |
|---|---|
| `~/.config/batsy/env` | `ANTHROPIC_API_KEY=...` (chmod 600) |
| `~/.config/batsy/config.toml` | settings: model, effort, wake words, voice, thresholds (see `packaging/config.example.toml`) |
| `~/.local/share/batsy/history.json` | the conversation; old ones go to `archive/` |
| `~/.local/share/batsy/memory.json` | long-term facts |
| `~/.local/share/batsy/skills/` | skills Batsy wrote for itself |
| `~/.local/share/batsy/batsy.log` | log: what it heard, said and ran |
| `~/Batsy/` | Batsy's workspace (Arduino sketches in `~/Batsy/arduino/`) |

## How it works

- **Wake word:** Vosk (offline) spots "Batsy" continuously. Whisper then confirms it, which filters out false alarms.
- **Speech-to-text:** faster-whisper `small.en` running locally (falls back to Vosk).
- **Brain:** Claude Opus 5.5 with streaming, adaptive thinking, prompt caching, server-side compaction and refusal fallbacks. Web search and fetch are on.
- **Voice:** Piper `en_GB-alan-medium`, local and neural (falls back to espeak-ng). Replies are spoken sentence by sentence as they stream in.
- **Tools:** `batsy/tools/`: shell and background jobs, files, system status, screen and webcam, Arduino (`arduino-cli`), timers and monitors, memory, and self-written skills.

## Tuning

- Voice replies feel slow? Use `effort = "low"` or `model = "claude-sonnet-5-5"`, and `whisper_model = "base.en"`.
- False wake-ups? Remove `"patsy"` and `"bitsy"` from `wake_words`. Missed wake-ups? Add how Vosk hears you (`batsy -v` logs it).
- Batsy hears itself through the speakers? Use a headset, or turn on PipeWire echo cancellation.
- Want keyboard and mouse control on Wayland? `sudo systemctl enable --now ydotool` (installed by `install.sh`).

## Cost and safety

Every request sends the conversation to the Claude API, billed to your key. Prompt caching keeps repeat turns cheap. Batsy runs commands as you, with your permissions. Read the log if you want to see exactly what it did.
# batsy
