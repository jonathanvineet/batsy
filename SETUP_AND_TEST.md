# Batsy: setup and full test checklist (Fedora)

Work through this top to bottom. Tick each box as you go. Every test says **what to say or run**, **what should happen**, and **what to check if it doesn't**.

> Logs, for when anything goes wrong: `~/.local/share/batsy/batsy.log` (what Batsy heard, said and ran), plus `overlay.log`, `crash.log`, `viewer3d.log` and `camera.log` in the same folder. Start Batsy with `batsy -v` to see the log live in the terminal.

---

## 0. Before you start

- [ ] Fedora Workstation (GNOME) 41 or newer, logged in to a normal desktop session
- [ ] 8 GB RAM or more, about 6 GB free disk, internet connection
- [ ] A microphone and speakers (a headset works best: Batsy won't hear itself)
- [ ] Lenovo webcam plugged in (for the camera tests)
- [ ] Claude Pro account (for Claude Code)
- [ ] Optional: Spotify Premium plus the iPad with Spotify open; an Arduino board and USB cable; a few STL files and PDFs to try

---

## 1. Install (one command)

- [ ] Copy `batsy-install.sh` to the laptop (USB, Google Drive, email…) and run:
  ```bash
  bash ~/Downloads/batsy-install.sh
  ```
  **Expect:** it asks for your password once, then works through about 15 steps for 10–20 minutes (mostly downloads: the local AI is about 2 GB, the 3D libraries about 100 MB). It ends with a `batsy selftest` summary.
  **If a step prints "(skipped …)" or "failed":** keep going. The self-test below shows exactly what's missing and how to fix it.

- [ ] **Log out and back in.** This activates Arduino serial access and the `batsy` command in new terminals.

- [ ] **Log in to Claude Code once:**
  ```bash
  claude
  ```
  Sign in with your Claude Pro account (type `/login` if it asks), then quit with `/exit`.

## 2. Self-test (automatic)

- [ ] Quick check, takes seconds and uses no Claude:
  ```bash
  batsy selftest
  ```
  **Expect:** mostly PASS. SKIP is fine for optional parts (Spotify). Every FAIL line says how to fix it. Fix them, then run it again.

- [ ] Full check. It opens windows, records 2 s from your mic, speaks, compiles an Arduino sketch, and sends one tiny Claude request:
  ```bash
  batsy selftest --all
  ```
  **Expect, in order:**
  - "Speak now" and a level reading.
  - Batsy's voice saying "Batsy self test".
  - The overlay flashing a card, then a screensaver with a clock.
  - The 3D viewer opening a 40×20×10 mm box, rotating, cutting and exploding it, then closing.
  - "blink compiles for Uno".
  - "Claude Code answers … tokens counted".
  - **Goal: 0 failed.**

- [ ] `batsy doctor` gives a second, simpler overview of the same things.

## 3. First start

- [ ] In a terminal: `batsy -v`
  **Expect:** "Batsy online." spoken. The log shows `local model qwen2.5:3b ready` within about 30 s.
  *(After the next login it starts by itself. That's the autostart entry.)*

---

## 4. Feature tests

Talk normally. Say **"Batsy"**, wait for the chime, then speak. Or say it all in one go: "Batsy, what time is it?"

### A. Voice basics
- [ ] "Batsy" → chime → the full-screen overlay appears: a glowing line that ripples with your voice, particles drifting into it, and your words appear live as you speak.
- [ ] While it thinks, a pulse sweeps along the line and a status pill shows what it's doing with a seconds counter.
- [ ] "Batsy, what time is it?" → the answer comes in about 1.5–2.5 s.
- [ ] **Follow-up without the wake word:** right after an answer, say "and what's the date?" → it answers.
- [ ] **Interrupt:** ask something long, then say "Batsy" while it talks → it stops and listens.
- [ ] "Batsy, stop" → silence, overlay closes.
- [ ] Say "Batsy" on its own → chime only (nothing spoken) → pause a second or two → speak your request → it answers. It must not talk over you.
- [ ] "Batsy, ... open YouTube" with a pause in the middle → still one request (the pause after "Batsy" doesn't cut you off).
- [ ] Talk normally near the laptop **without** "Batsy" → nothing happens. If it wakes by itself, remove `"patsy"` and `"bitsy"` from `wake_words` in `~/.config/batsy/config.toml`.

### B. Instant commands (no AI at all)
- [ ] "Set volume to 30" → laptop volume changes.
- [ ] "Mute" / "Unmute"
- [ ] "Pause" / "Play" / "Next song" (with Spotify set up these go to the iPad; otherwise to local media)

### C. Local AI (free, offline)
- [ ] "How's my battery?" → spoken battery level (about 5–8 s).
- [ ] "Tell me a joke" → a short joke.
- [ ] Disconnect Wi-Fi, then "Batsy, what's 15 times 12?" → still answers (local brain).

### D. Instant open and close
- [ ] "Open Firefox" / "Open VS Code" / "Open Files" / "Open Downloads" / "Open YouTube" → opens in about 2 s.
- [ ] "Close Firefox" → it closes.

### E. Claude (Pro): automatic for hard things
- [ ] "Explain why the sky is blue in detail" → "…" and a precise answer from Claude in about 8–20 s.
- [ ] "Ask Claude what a MOSFET is" → forced to Claude.
- [ ] **Works anywhere:** "Ask Claude to list what's in my batsy folder and tell me the biggest file" → it reads `~/batsy` (not only `~/Batsy`) and answers.
- [ ] **Guarded:** make a test folder (`mkdir ~/junk-test`), then "Ask Claude to delete the junk-test folder in my home" → Batsy asks "Claude wants to delete files recursively… Should I let it?" → say "yes" → it's gone. Try again with "no" → Claude doesn't do it.
- [ ] "How much Claude have I used?" → percentages of your session and week budgets.
- [ ] "Set my session Claude limit to 2 million" → confirmed.
- [ ] *(Optional)* Set a tiny limit ("set my session Claude limit to 1000"), then ask a hard question → "I've used this session's Claude budget…", answered locally instead. Then "ask Claude anyway what a MOSFET is" → still goes through. **Set the limit back afterwards.**

### F. Learn once, then instant
- [ ] "Turn on dark mode" → "Give me a moment, I'm learning how to do that." → dark mode turns on → "Next time that'll be instant."
- [ ] "Turn off dark mode" → learned the same way (a separate skill).
- [ ] "Turn on dark mode" again → instant, with no "learning" message.
- [ ] A new phrasing: "Switch to the dark theme" → no new skill; it's added to the existing one and runs.
- [ ] **Feedback:** after a skill runs, say "no, that's wrong" → "Sorry. Let me relearn that." → Claude fixes it.
- [ ] `batsy skills` → lists the skills with how often each worked or failed.
- [ ] "Forget how to turn on dark mode" → removed.

### G. Memory
- [ ] "Remember that my Uno is on ttyACM0" → saved.
- [ ] Restart Batsy (`Ctrl+C`, then `batsy -v`) → "Which port is my Uno on?" → it remembers.
- [ ] Ask something, then a follow-up that only makes sense with context ("and how about tomorrow?") → it understands.
- [ ] "New conversation" → "Fresh start…" (long-term facts are kept).

### H. Pictures and pull-ups
- [ ] "Show me a picture of the Eiffel Tower at night" → a night photo appears full screen.
- [ ] "Pull up connections for Nikola Tesla" → photo plus a facts card ("Serbian-American engineer…"), and the first line spoken.
- [ ] "What does a capybara look like?" → picture.
- [ ] **A new phrasing gets learned:** "Gimme a quick look at the Golden Gate Bridge" → picture (the AI understood it). Then "gimme a quick look at the Colosseum" → instant. `batsy vocab` shows the learned phrase.
- [ ] **Technical:** "Show me the ESP32 pinout diagram" → a pinout in about 2 s. A few seconds later it may switch to a better one (Claude checked the top candidates).
- [ ] **Natural requests:** "How big is a blue whale?" → the spoken answer also puts a picture on screen.
- [ ] Esc, Space or a click closes the overlay.

### Robustness (do these once)
- [ ] `pkill -f batsy.overlay_web` while it's idle → the next "Batsy" brings the overlay back (`overlay.log` shows the restart).
- [ ] `pkill -f "batsy run --worker"` → within a few seconds Batsy says "Batsy online." again (the supervisor restarted it).
- [ ] Suspend the laptop, wake it, say "Batsy" → it still hears you (the log may show "microphone reopened").

### I. Camera (Lenovo webcam)
- [ ] "Open the camera" → a "Batsy camera" window shows the live feed.
- [ ] Hold up an object: "What am I holding?" → the frame flashes yellow and Claude describes it (about 8–15 s).
- [ ] "Which component is this?" (hold an Arduino or resistor) → identified.
- [ ] "Close the camera" → window closes.
- [ ] Wrong camera? `batsy doctor` lists the cameras; set `[camera] device = N` in the config.

### J. Screen questions
- [ ] Put an error message on screen: "Read this error on my screen" → Claude explains it.

### K. Documents and pictures (any file)
- [ ] Open a PDF in the document viewer, then "Circle all the dates in this PDF" → "Looking at …" → a marked-up copy opens. It's in `~/Batsy/docs/…-marked.pdf` and the original is unchanged.
- [ ] Follow-up: "Now highlight the headings too" → applied to the same file.
- [ ] "Highlight the formulas in my physics notes PDF" → found by name.
- [ ] Open a photo or screenshot: "Label the components in this picture" → labelled copy.
- [ ] "Learn from this PDF", then later "What did that PDF say about …?" → answered from memory.
- [ ] Other jobs: "Turn the table in this PDF into a spreadsheet", "Redact my phone number in this document", "Crop this image to the face".

### L. 3D models
- [ ] "Open this 3D model" (after opening or downloading an STL), or "Show me the gear STL" → the model appears inside the overlay with yellow rulers in mm, and the size is spoken. Drag to orbit, scroll to zoom.
- [ ] "Rotate it 90 degrees around X" · "Top view" · "Zoom in" · "Spin it" · "Stop spinning"
- [ ] "How tall is it?" · "How much will it weigh in PETG?" · "Is it watertight?"
- [ ] "Make it 50 mm tall" → the rulers update.
- [ ] "Show a cross-section along Y at 30 percent" → cut view plus the cut size spoken.
- [ ] "Explode it" (multi-part model) → parts separate.
- [ ] "Measure" → click two points → the distance is spoken.
- [ ] "X-ray mode" · "Colour it red" · "Reset"
- [ ] "Save it as an OBJ" → saved in `~/Batsy/3d/`.
- [ ] **Claude:** "Is this printable?" → checks plus a card. "Add two M3 mounting holes 30 mm apart" → a new model loads next to the original. "Design a phone stand 80 mm wide" → a new model.

### M. Arduino
- [ ] Plug in the board. "Which Arduino boards are connected?" → port and board name.
- [ ] "Write a sketch that blinks the LED twice a second and upload it" → Claude writes it, it compiles and uploads, and the LED blinks.
- [ ] "Read the serial port for 5 seconds" → output spoken or summarised.

### N. Spotify on the iPad (optional)
- [ ] One-time setup: README section "Spotify on the iPad" (client ID → `batsy spotify-login`).
- [ ] Spotify open on the iPad: "Play Blinding Lights by The Weeknd" → plays on the iPad.
- [ ] "Pause" · "Next song" · "What's playing?" · "Like this song" · "Play something chill for coding" · "Queue Levitating"
- [ ] If it says it can't see the iPad: tap the Spotify app on the iPad and try again.

### O. Good morning / good night / presets
- [ ] "Good night" → "Anything you have on <the right day>?" Answer one item at a time, then "that's it" → "All saved. Sleep well."
  - [ ] **The 3 AM rule:** at night before 5 AM it must ask about **today's** date (the day you'll wake into).
- [ ] "Good morning" → greeting plus that day's plans and tracked chores, with a card on screen.
- [ ] "Make a routine: when I say study mode, play my lofi playlist and say good luck" → then "Study mode" runs it instantly.

### P. Chores, projects, habits
- [ ] "Every weekend I work on my robot project" → "Got it. I'll keep track…"
- [ ] "I do laundry every Sunday at 6 PM" · "Gym Monday, Wednesday and Friday at 6" · "Water the plants every 3 days"
- [ ] "What are my chores?" → list plus card.
- [ ] **Check-in:** on a due day after the check-in time (or temporarily set `checkin_chore = "00:00"` under `[tasks]`), Batsy asks "Did you get laundry done?" while you're at the laptop. Answer "yes" without the wake word → "Nice, logged."
- [ ] For a project: answer with progress ("finished the chassis") → "I've noted that." Then "How's my robot project going?" → last update plus history card.
- [ ] "I finished the laundry" → logged anytime. "Stop tracking gym" → removed.

### Q. Screensaver
- [ ] "Show me the screensaver" → animated dashboard: clock, Claude session and week bars, running jobs, waiting questions, coming up. Move the mouse → it closes.
- [ ] Leave the laptop alone for 3 minutes → it appears by itself. *(If the screen goes black first, raise Settings → Power → Screen Blank.)*
- [ ] Voice edits:
  - "Add the weather in Chennai to the screensaver"
  - "Put system stats on the screensaver"
  - "Add a countdown to my exam on 12 November to the screensaver"
  - "Make the screensaver blue stars"
  - "Make the screensaver minimal / detailed"
- [ ] **Claude redesign:** "Make the screensaver look like a cyberpunk terminal" → new look.
- [ ] **Auto-adapt:**
  - While a check-in question is unanswered, "Waiting for you" glows at the top.
  - While music plays, "Now playing" appears.
  - After 11 PM it's dim.

### R. Timers, monitors, background jobs
- [ ] "Remind me in 2 minutes to stretch" → spoken plus a notification after 2 minutes.
- [ ] "Tell me when google.com stops responding" → a monitor (it shows under "Running now" on the screensaver).
- [ ] "Run a system update in the background" → a password dialog, then an announcement when it finishes.

### S. Safety
- [ ] "Delete the folder ~/Batsy/test" (after creating it) → "This command will delete files… Should I go ahead? Say yes or no." Say "no" → "Okay, I won't."
- [ ] Root actions use the GNOME password dialog, never a hidden sudo.

### T. Typing instead of talking
- [ ] `batsy chat` → type requests; replies print. It talks to the running Batsy.
- [ ] `batsy ask "what's my battery?"`

---

## 5. Useful commands

| Command | What it does |
|---|---|
| `batsy -v` | run with live log |
| `batsy selftest [--all]` | test everything |
| `batsy doctor` | quick setup overview |
| `batsy usage` | tokens today, this week and all time |
| `batsy skills` / `--forget NAME` | learned skills |
| `batsy vocab` / `--forget "phrase {x}"` | learned "show me" phrasings |
| `batsy spotify-login` | connect Spotify |
| `batsy reset` | fresh conversation |
| `batsy saver get` | screensaver layout |
| `batsy 3d open FILE` | open a model in the viewer |
| `batsy doc text FILE` | read any file's text (OCR included) |

## 6. Where things live

| Path | What |
|---|---|
| `~/.config/batsy/config.toml` | all settings |
| `~/.local/share/batsy/` | memory, skills, tasks, usage, logs, models |
| `~/Batsy/` | outputs: `docs/` (marked-up files), `3d/` (models, screenshots), `arduino/` (sketches) |
| `~/batsy/` | the source code (Batsy can edit itself here) |

## 7. Troubleshooting

| Symptom | Check |
|---|---|
| Doesn't react to "Batsy" | `batsy -v` and watch for "wake word heard"; check mic input level in Settings → Sound; run `batsy selftest --voice` |
| Hears itself or wakes randomly | use a headset; trim `wake_words` in the config |
| Slow replies | `[local] model = "qwen2.5:1.5b"`, `[voice] whisper_model = "tiny.en"` |
| "My local brain isn't running" | `sudo systemctl restart ollama`, then `ollama list` |
| Claude never used | `claude` (log in again); `batsy selftest --claude`; "how much Claude have I used?" (budget reached?) |
| Overlay stays behind windows | `[overlay] qt_platform = "xcb"` |
| Overlay black, flickers or keeps restarting | `overlay.log`; it falls back by itself, or set `[overlay] renderer = "classic"` |
| Batsy restarted by itself | `crash.log` and the end of `batsy.log` (send me both) |
| 3D viewer won't open | `overlay.log` (inside the overlay) or `viewer3d.log` (own window) |
| Camera busy or black | close other camera apps; `[camera] device = N` |
| Spotify "can't see the iPad" | tap Spotify on the iPad; Premium needed |
| Arduino upload denied | log out and back in (dialout group), then replug the board |

## 8. Uninstall

```bash
rm -rf ~/.local/share/batsy ~/.config/batsy ~/batsy ~/.local/bin/batsy \
       ~/.config/autostart/batsy.desktop ~/.local/share/applications/batsy.desktop
# keeps ~/Batsy (your outputs). Ollama: sudo dnf remove ollama
```
