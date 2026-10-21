# Batsy

A voice assistant for your Fedora laptop, powered by Claude. Say **"Batsy"** and wait for the chime, or say it all at once ("Batsy, what's eating my CPU?"). Batsy can run commands, read and write files, watch the system, see your screen, program Arduino boards, set timers and monitors, remember things about you, and write new skills for itself.

## Install (Fedora)

```bash
cd batsy
./install.sh          # asks for your sudo password; installs the local AI (no API key needed)
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

## Full-screen mode

When you say "Batsy", the screen fades to a dark Batsy view:
- **While you talk,** a glowing orb follows your voice and your words appear live. This is local and uses no tokens.
- **While it works,** the orb spins.
- **When it answers,** the reply appears as big captions while it speaks.
- **Pictures without special phrases.** Ask in any words: "what does a capybara look like", "who's the CEO of Nvidia", "how big is a blue whale", "I wanna see that bridge in Prague". Claude decides when a picture helps and adds a hidden `[[show: …]]` note to its reply. Batsy cuts the note out of the speech, finds one picture (Wikipedia, then Openverse) and puts it on screen while still talking. There's no extra request to Claude, and Claude never has to see the picture.
- **Details** (lists, steps, numbers) appear on a text card, from a hidden `[[card: Title | text]]` note, instead of being read aloud.

The overlay closes by itself a few seconds after the answer, or 30 seconds after showing a picture. Esc, Space or a click closes it straight away. Turn it off with `[overlay] enabled = false`. If GNOME keeps it behind other windows, set `qt_platform = "xcb"`.

## Learns once, then does it instantly

- **Opening and closing are instant, with no AI:** "open VS Code / Files / Firefox / YouTube / Downloads / github.com", "close Spotify". Batsy finds your installed apps (including Flatpaks) by name, with a few nicknames like "vs code", "files" and "browser".
- **New actions are learned once.** The first time you ask for something it doesn't know how to do, like "turn on dark mode", "connect my earbuds" or "set brightness to 40", Claude Code writes a small Python skill for it, covering the different ways you might say it. Batsy checks the code, runs it for you, and saves it ("Next time that'll be instant"). After that, any of those phrasings runs the saved code directly: no AI, no Pro usage.
- **No duplicates.** Before writing anything new, Claude sees a short list of the skills you already have. If your request is just a new way of saying one of them ("I'd prefer the dark theme" for "turn on dark mode"), Batsy adds your phrase to that skill and runs it. Next time that phrase is instant too. Opposites like "turn *off* dark mode" are never merged.
- **Things the local AI already did** (for example "what's my battery") are turned into skills in the background, so they're instant next time too.
- **Feedback teaches it.** Say "no, that's wrong" / "didn't work" right after a skill runs, or let it crash, and Claude rewrites it using your complaint. Two failures in a row switch the skill off until it's relearned. Every clean use counts as a success.
- **Safety:** skills can only run commands through Batsy's checked shell, so risky ones (deleting, uninstalling, shutting down…) still ask "Should I go ahead?". Code that tries to get around that is rejected. Questions and chat are never turned into code: Claude just answers them.
- `batsy skills` lists what it has learned, with how often each skill worked or failed. `batsy skills --forget NAME` removes one, or say "Batsy, forget how to …".

## Showing and pulling up things

- **Your usual phrases work instantly, with no AI:** "pull up connections for Nikola Tesla", "pull up the Taj Mahal", "show me a picture of a capybara", "what does a quokka look like". **Pull up** shows the best picture with an accurate facts card and speaks the first line. **Picture** shows just the picture.
- **Say it any other way** ("gimme a quick look at the Golden Gate Bridge", "throw everything on Marie Curie on the screen") and the AI works out what you meant. Batsy then **learns that phrasing**, so next time it's instant. Plain questions like "what is…" or "who's…" are never learned as show phrases. `batsy vocab` lists the phrases, and `batsy vocab --forget "phrase {x}"` removes one.
- **Current pictures and technical diagrams:** Batsy searches the live web too (DuckDuckGo images, no key needed), so "ESP32 pinout", "HC-SR04 wiring diagram" or "latest iPhone" find up-to-date images, not old Wikipedia photos. Results from trusted electronics sites (Random Nerd Tutorials, Adafruit, SparkFun, Espressif…) rank higher. Stock-photo watermarks and image-scraping sites rank lower. For technical images, the best guess appears in about 2 seconds while **Claude Code looks at the top 4 candidates in the background**. If it finds a more correct, readable one, the picture on screen is swapped.
- **The right picture, not the top search hit:** Batsy first works out exactly what you mean, so "jaguar animal" gives the animal and not the car. It then compares the article's editor-chosen image with high-resolution Wikimedia Commons photos. Images Wikipedia rates as featured or quality score highest, and wrong matches, tiny images and odd shapes score lowest. Specifics like "at night" or "interior" are taken into account. The facts come from the encyclopedia summary, not a search snippet.

## 3D models

"Open this 3D model" / "show me the gear STL" opens the most recent model, or the one you named, in **Batsy 3D**. It's a dark viewer window with a floor grid, colour-coded dimension rulers drawn in 3D (X, Y, Z in mm) and a live size readout. It handles STL, OBJ, PLY, GLB/GLTF, 3MF and OFF, plus STEP if the extra converter installs.

**Instant voice controls, no AI:**
- **Rotate and view:** "rotate it 90 degrees around X" · "turn it right" · "flip it over" · "spin it" / "stop spinning" · "top / front / side / back / bottom / isometric view" · "zoom in/out" · "reset"
- **Move and resize:** "move it up 5 mm" · "make it 50 mm tall" · "scale it 150 percent" · "mirror it along Y" · "centre it" · "put it on the bed"
- **Measure:**
  - "show / hide the dimensions" · "how big / tall / wide is it?"
  - "how much will it weigh in PETG?" (also PLA, ABS, resin, aluminium and more) · "what's the volume?" · "is it watertight?"
  - "measure": click two points and Batsy says the distance
- **Look inside:** "cross-section along Y at 30 percent" (shows the cut size) · "explode it" (separates the parts) · "wireframe" / "x-ray" / "show edges" · "colour it red"
- **Save:** "save it as an OBJ" writes to `~/Batsy/3d/` with your changes applied.

**Everything else goes to Claude Code**, which can see the viewer through screenshots, control it, and create or edit geometry in Python (trimesh and manifold3d, in millimetres). For example:
- "Is this printable?" checks watertightness, walls, overhangs and fit on a 220 × 220 × 250 mm bed.
- "What is this part?"
- "Add two M3 mounting holes 30 mm apart."
- "Design a phone stand 80 mm wide."

New or edited models are saved next to the original and shown in the viewer. Your original file is never changed.

## Files: PDFs, images, documents

Talk about any file on the laptop: PDF, PNG/JPG/other images, DOCX, text or code.
- "See this PDF" / "learn from my physics notes"
- "Circle all the dates in this PDF" / "highlight the formulas" / "box the pin 13 description in the Arduino datasheet"
- "Label the resistor values in this picture" / "redact my phone number" / "crop this image" / "turn these tables into a spreadsheet"

**What happens:**
1. **Batsy finds the file and says its name**, so you know it picked the right one. It uses the name you gave, or otherwise the file you opened most recently, or something new in Downloads, Desktop or Screenshots.
2. **It reads the file on the laptop** (PDF text, OCR for images and scans) and stores it in local memory. Later questions like "when was the exam date in those notes?" can then use it.
3. **Claude Code does what you asked.** It reads the file, finds exact text positions, renders pages with a coordinate grid to place circles and arrows, and marks up a copy. Then it **looks at its own result and fixes anything misplaced**. For anything else (convert, crop, rotate, extract, fill, merge…) it writes and runs Python.
4. **The result opens** in your viewer and appears on screen, saved in `~/Batsy/docs/` as `<name>-marked.pdf` or `.png`. **Your original is never changed.**

Follow-ups work too: "now underline the second one as well" applies to the same file. Each file job is one Claude Code request on your Pro plan. Bigger files and more marks take longer, usually 20–60 seconds.

## Camera and screen questions

The live camera window is local: OpenCV shows the feed and nothing streams anywhere. The local AI can't see. So when you ask something visual, like "what am I holding?", "what's this?", "which component is this?", "how do I look?" or "read this error on my screen", Batsy saves **one frame** (768 px) or a screenshot. Claude Code then looks at that image and answers. Each look uses a little of your Pro allowance, roughly one image plus the question. It isn't continuous video: every question is a fresh snapshot.

## Chores, projects and habits (tracked locally, no tokens)

Tell Batsy what you do regularly, in your own words:
- "Every weekend I work on my robot project" · "I do laundry every Sunday at 6 PM" · "Gym Monday, Wednesday and Friday at 6" · "Water the plants every 3 days" · "Add a habit: read for 20 minutes every night" · "Remind me to pay rent every month"

**On the days they're due, Batsy:**
- reminds you 15 minutes before anything with a time
- **checks in afterwards:** "Did you get the laundry done?" or, for projects, "How did the robot project go today? Any progress?" It waits until you're at the laptop and listens for your answer without the wake word. Unanswered questions stay on the screensaver.
- logs what you say: done, skipped, or your progress update. It keeps streaks and mentions today's chores and projects in "good morning".

**Ask anytime:** "what are my projects?" · "what do I have to do today?" · "how's my robot project going?" (shows the update history) · "I finished the laundry" · "update on robot project: motors wired" · "stop tracking gym".

## Claude budget (your Pro plan)

Every Claude Code request Batsy makes is counted exactly, using the token numbers Claude Code reports. Pro's own limits can't be read from outside the official app, so Batsy keeps **its own budgets**:
- **Session:** a 5-hour window, like Pro's.
- **Week:** resets every Monday.

Batsy warns you at 80%. At the limit it switches to the local AI until the budget resets. "Ask Claude anyway…" goes through regardless. Set the budgets by voice ("set my weekly Claude limit to 5 million", "set my session limit to 800k") or under `[claude_budget]` in the config. Ask "how much Claude have I used?" to hear where you are.

## Screensaver: token usage

After 3 minutes with no keyboard or mouse activity, as long as Batsy isn't doing anything, the screen fades to a dark screensaver showing:
- a big clock and the date
- **Claude · this session** and **Claude · this week**: tokens used against your budget, with a colour bar (cyan, then orange, then red) and when each resets
- **Local AI · today**: tokens used (free)
- **Running now**: Claude jobs (file work, 3D, learning a skill, checking pictures), background shell jobs and active monitors, with how long each has been running
- **Waiting for you**: questions Batsy asked that you haven't answered yet
- **Coming up**: your chores, projects and habits for the next 7 days, plus today's plan

Move the mouse or press a key to close it, or just say "Batsy". The layout drifts slowly so nothing burns into the screen. Change the delay with `[overlay] screensaver_minutes` (0 turns it off). GNOME blanks the screen after 5 minutes by default, so raise that in Settings → Power if you want the screensaver to stay up longer.

These are Batsy's own API tokens, the same numbers `batsy usage` prints. Your claude.ai plan limits aren't available through the API.

## Routines (no tokens)

- **"Good morning"**: Batsy says good morning and reads you that day's plan. The plan also appears as a card on screen.
- **"Good night"**: Batsy asks *"Anything you have on Monday, October 5?"*. Answer in your own words, one thing at a time. Say "no" or "that's it" when you're done, and the plans are saved for that day. **Times before 5 AM still count as the night before.** Going to bed at 3 AM on 5 October asks about 5 October, the day you'll wake into. Change the cutoff with `[routines] day_starts_at`.
- **Anytime:** "add dentist on Friday at 4" or "what do I have this week?" works too. Claude uses the same local agenda for these, so they do cost tokens.
- **Your own presets:** say "Batsy, make a routine: when I say *study mode*, pause notifications, play my lofi playlist and say good luck". Or add one to `config.toml`:
  ```toml
  [routines.custom.study]
  phrases = ["study mode", "focus time"]
  say = "Study mode on. Good luck."
  commands = ["gsettings set org.gnome.desktop.notifications show-banners false"]
  spotify = "my Lofi playlist"
  ```
  After that, saying the phrase runs the preset instantly with no tokens.
- Optional: `morning_extra = "today's weather in Chennai in one line"` adds a Claude answer to the morning briefing.

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

## The brain: local and free (with Claude on request)

- **Default: a local AI.** `qwen2.5:3b` runs in Ollama on the laptop. It's free, works offline and has no limits. It's tuned for 8 GB of RAM and real back-and-forth conversation: the model stays loaded, its instructions are tiny (about 200 tokens), memory is short, tool output is trimmed, and replies are spoken as they stream. On a typical laptop CPU, expect the first words about 1–3 seconds after you stop talking.
- **Complex requests go to Claude automatically,** through Claude Code on your **Claude Pro** plan. That covers code, Arduino sketches, "explain why…", comparisons, plans, writing and long questions. The local AI also hands off anything it isn't sure about. Everything else stays local and free. Saying "ask Claude…" still forces it. Turn the automatic hand-off off with `[local] auto_claude = false`. One-time setup: `npm install -g @anthropic-ai/claude-code`, then run `claude` and log in.
- **Faster but simpler:** `[local] model = "qwen2.5:1.5b"`. **Smarter but slower:** `"llama3.2:3b"` or a 7B model if you get more RAM.
- **Have a Claude API key?** Set `[brain] engine = "claude"` to use the full Claude agent instead.
- The no-token local commands (music, routines, camera, time, volume, "show me a picture of X") work the same with either brain.

## How it works

- **Wake word:** Vosk (offline) spots "Batsy" continuously. Whisper then confirms it, which filters out false alarms.
- **Speech-to-text:** faster-whisper `small.en` running locally (falls back to Vosk).
- **Brain:** a local Ollama model by default; Claude Code (Pro) when you say "ask Claude"; or the Claude API agent (Opus 5.5) if you have a key.
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
