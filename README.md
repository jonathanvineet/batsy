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
| "Batsy, open the camera" | opens a live window showing your Lenovo webcam (no Claude call, no tokens) |
| "Batsy, what am I holding?" / "what colour is this?" | grabs one small frame from that window and answers |
| "Batsy, close the camera" | closes the window |
| "Batsy, play Blinding Lights by The Weeknd" / "play my Gym playlist" / "play something chill" | plays on your iPad's Spotify (no tokens) |
| "Batsy, pause" / "next song" / "what's playing?" / "like this song" / "queue Levitating" | Spotify control (no tokens) |
| "Batsy, play three upbeat songs for my workout, then shuffle" | Claude works it out with the `spotify` tool |
| "Batsy, what's on my screen?" | takes a screenshot and looks at it |
| "Batsy, tell me when my build finishes" / "when the website is back up" | background job or monitor that speaks up when it happens |
| "Batsy, remind me in 20 minutes to check the oven" | timer (survives restarts) |
| "Batsy, remember my ESP32 is on ttyUSB0" | long-term memory |
| "Batsy, make yourself a skill that summarises my unread GitHub notifications" | writes the Python, tests it, saves it for next time |
| "Batsy, new conversation" | clears the conversation (long-term memory is kept) |
| "Batsy, stop" | stops talking |

- **Follow-ups:** after Batsy answers, it listens for about 6 seconds, so you can just keep talking without saying "Batsy" again.
- **Memory is local.** Everything is stored in SQLite on your laptop. Each request sends Claude only a short context block: your saved facts, the last 6 exchanges with one-line notes of what Batsy did, and up to 3 older exchanges found by a local full-text search. The full conversation is never sent, so requests stay small and fast however long you use it.
- **Interrupting:** say "Batsy" while it's talking.
- **Risky actions** (`rm -rf`, `dnf remove`, shutdown, disk tools, force-push, etc.) are confirmed by voice first. Root commands go through `pkexec`, which shows the normal GNOME password dialog.
- **Typing:** `batsy chat` (or `batsy ask "..."`) talks to the running assistant through a local socket.

## Spotify on the iPad (one-time setup)

Batsy controls the Spotify app on your iPad through Spotify Connect, using the Spotify Web API. The iPad app can't be controlled directly over the local network, so commands go through Spotify's servers. Being on the same Wi-Fi doesn't matter. **Spotify Premium is required.**

1. Go to <https://developer.spotify.com/dashboard> and click **Create app**. Give it any name, choose **Web API**, and set the redirect URI to `http://127.0.0.1:8888/callback`.
2. Copy the app's **Client ID** into `~/.config/batsy/config.toml`:
   ```toml
   [spotify]
   client_id = "your-client-id"
   device = "ipad"
   ```
3. Run `batsy spotify-login` on the laptop and approve access in the browser.
4. Restart Batsy.

**Things to know:**
- Spotify has to be open on the iPad. iPadOS suspends it after a while when nothing is playing; if Batsy says it can't see the iPad, tap the Spotify app once.
- iPads don't allow remote volume control. Use the iPad's buttons, or say "volume 40" to change the laptop's volume.

## Files

| Path | What |
|---|---|
| `~/.config/batsy/env` | `ANTHROPIC_API_KEY=...` (chmod 600) |
| `~/.config/batsy/config.toml` | settings: model, effort, wake words, voice, thresholds (see `packaging/config.example.toml`) |
| `~/.local/share/batsy/memory.db` | local memory (SQLite): every exchange, the actions taken, and remembered facts |
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

- Voice replies feel slow? Use `model = "claude-sonnet-5-5"` and `whisper_model = "base.en"`. For a long coding session, temporarily raise `effort` to `"medium"`.
- Wrong camera? `batsy doctor` lists the cameras. Set `[camera] name = "..."` or `device = 2` in the config.
- False wake-ups? Remove `"patsy"` and `"bitsy"` from `wake_words`. Missed wake-ups? Add how Vosk hears you (`batsy -v` logs it).
- Batsy hears itself through the speakers? Use a headset, or turn on PipeWire echo cancellation.
- Want keyboard and mouse control on Wayland? `sudo systemctl enable --now ydotool` (installed by `install.sh`).

## Keeping token use low

- **The camera never streams to Claude.** The window just shows the feed. Batsy sends a single 768px frame (about 450 tokens) only when you ask a question that needs it.
- **Instant local commands** use no tokens at all: open/close camera, time, date, "volume 40", mute, stop, new conversation, and Spotify commands (play X, pause, next, previous, what's playing, like, queue, shuffle).
- **Local memory.** Each request carries a few hundred tokens of memory, not the whole history. Tool outputs and images are never re-sent after the request that used them; only a one-line note of each action is kept.
- **Lean context.** Effort defaults to `low`. Tool output is capped. Web search and fetch are limited per question.
- **Caching.** The system prompt and tools (about 3.3K tokens) are cached for an hour, so repeat questions only pay for the new part.
- **No false triggers.** Whisper must confirm "Batsy" before anything is sent. Background chatter heard during the follow-up window is ignored.
- **`batsy usage`** shows how many tokens you've used today, this week and all time. Each request is logged to `~/.local/share/batsy/usage.jsonl`.

## Cost and safety

Every request sends the conversation to the Claude API, billed to your key. Prompt caching keeps repeat turns cheap. Batsy runs commands as you, with your permissions. Read the log if you want to see exactly what it did.
# batsy
