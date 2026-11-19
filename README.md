# Batsy

A voice assistant for your Fedora laptop, powered by Claude. Say **"Batsy"** and wait for the chime, or say it all at once ("Batsy, what's eating my CPU?"). Batsy can run commands, read and write files, watch the system, see your screen, program Arduino boards, set timers and monitors, remember things about you, and write new skills for itself.

## Install (Fedora)

```bash
cd batsy
./install.sh          # asks for your sudo password; installs the local AI (no API key needed)
batsy -v              # start it now; it also starts automatically at login
```

Log out and back in once so the Arduino serial-port permission (`dialout` group) applies. Run `claude` once and log in with your Claude Pro account. Then run `batsy selftest --all`, which checks every part and says how to fix anything that fails. **The full step-by-step setup and test checklist is in [SETUP_AND_TEST.md](SETUP_AND_TEST.md).**

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
| "Batsy, remind me in 20 minutes to check the oven" / "remind me tomorrow at 9 to submit the assignment" | reminder (survives restarts, no tokens) |
| "Batsy, I'm doing a project on a line following robot" | remembers it and finds its GitHub repo / folder from the description |
| "Batsy, give me an update on my robot project" | commits, unpushed and uncommitted work, issues and your notes |
| "Batsy, how much is the Raspberry Pi 5 8GB?" / "find me a 55 inch TV under 50000" | prices compared across Amazon, Flipkart, Robu, Croma and more |
| "Batsy, send this to my Mac" / a file arrives from the Mac | Tailscale file sharing both ways |
| "Batsy, create a Python script that converts PNGs to JPG" | Claude Code writes and tests it in `~/Batsy/code/`, then opens it |
| "Batsy, remember my ESP32 is on ttyUSB0" | long-term memory |
| "Batsy, make yourself a skill that summarises my unread GitHub notifications" | writes the Python, tests it, saves it for next time |
| "Batsy, new conversation" | clears the conversation (long-term memory is kept) |
| "Batsy, stop" | stops talking |

- **Follow-ups:** after Batsy answers, it listens for about 6 seconds, so you can just keep talking without saying "Batsy" again.
- **Memory is local.** Everything is stored in SQLite on your laptop. Each request sends Claude only a short context block: your saved facts, the last 6 exchanges with one-line notes of what Batsy did, and up to 3 older exchanges found by a local full-text search. The full conversation is never sent, so requests stay small and fast however long you use it.
- **Interrupting:** say "Batsy" while it's talking.
- **Risky actions** (`rm -rf`, `dnf remove`, shutdown, disk tools, force-push, etc.) are confirmed by voice first. Root commands go through `pkexec`, which shows the normal GNOME password dialog.
- **Typing:** `batsy chat` (or `batsy ask "..."`) talks to the running assistant through a local socket.

## Keys, typing and the wake word

| Key / phrase | What it does |
|---|---|
| **Super+B** | push-to-talk: Batsy listens straight away, no wake word (same as clicking the bat in the corner) |
| **Super+T** | type to Batsy: a small box opens; Enter sends; the reply is the same as if you'd said it |
| **~** | stop: Batsy stops talking, cancels what it's doing and stops listening (also cuts off a recording in progress) |
| "type hello world" · "press enter" · "press control shift t" | Batsy types into the app you're in (ydotool; "set up typing" once, with your password) |
| "Batsy, train my wake word" | records you saying "Batsy" 8 times and builds a wake model of *your* voice (switches on only if it tells you apart from saved noise) |
| "Batsy, learn my voice" | 5 longer takes, so videos and other people don't wake it |

Typed replies to a yes/no question ("yes" in the Super+T box or the phone app) answer it. Filler noises ("uh", "mm-hmm") are never taken as requests.

## Lights (ESP32 on the network)

"Turn on the light" · "lights off" · "is the light on?" · **"night mode"** (the night light **on**) · "it's too dark". Batsy reads the light's real state back after every switch. Said messily ("Kachi turn off the light", "light offs") it still works, and "don't turn off the light" doesn't.

**Good night:** after "good night" and your answer ("that's it"), the light goes off the moment Batsy says "Sleep well".
```toml
[home.lights]
light = "http://192.168.1.70"     # an ESP32 page with /on, /off and "Light status: ON/OFF"
```

## Your desktop PC (over SSH)

"PC" or "desktop" always means the PC; "laptop" means this laptop.

| You say | What happens |
|---|---|
| "Is the PC on?" / "how's the PC?" | uptime, CPU, memory, disk, temperature |
| "Open YouTube on the PC" / "open Files on the PC" / "close Firefox on the PC" | apps and websites on the PC's screen |
| "PC volume to 30" / "mute the PC" / "pause on the PC" | sound and media |
| "Shut down / restart / sleep the PC" (asks first) · "wake up the PC" · "lock the PC" | power (Wake-on-LAN for waking) |
| "Run df -h on the PC" · "open a terminal on the PC" · "update the PC" | commands, an SSH terminal here, apt updates in a terminal |
| "On the PC, find my biggest files" | the AI does it with the `run_on_pc` tool |

One SSH connection stays open, so each command takes about 0.1 s. **"Set up the PC"** opens a terminal where you type the PC's password once: it allows shutdown, restart and updates without a password and turns on Wake-on-LAN.
```toml
[pc]
host = "bob@10.10.10.3"
mac = "94:de:80:fb:d7:a0"     # for Wake-on-LAN
broadcast = "10.10.10.255"
```

## 3D printer (Ender-5 Pro on USB)

**Slicing** uses OrcaSlicer (flatpak `com.orcaslicer.OrcaSlicer`) from the command line, with **your presets**:
- Send Batsy a printer bundle from OrcaSlicer or Creality Print (`.orca_printer` / `.creality_printer`, or a zip of process presets) and it becomes your printer. Your presets only list changes, so Batsy completes them from OrcaSlicer's own profiles (`~/.local/share/batsy/orca-profiles/`, downloaded from OrcaSlicer's GitHub: the flatpak keeps them packed).
- The machine is OrcaSlicer's **Creality Ender-5 Pro (2019) 0.4 nozzle** (Bowden retraction, 500 mm/s² limits); your process and filament presets go on top. The nozzle temperature is the one your temperature tower picked (`nozzle_temp` in `~/.local/share/batsy/printer/settings.json`, now 190 °C, first layer +5).
- **Every 3D file you send is sliced automatically**, and Batsy says the time and grams. Each slice gets a **preview** (a picture, and a **3D view** you can turn, zoom and scrub through the layers) on the laptop and in the phone app.

| You say | What happens |
|---|---|
| "Slice it" · "slice it with keychain" · "slice the iPhone stand" | your default (nicee) or a named preset |
| "Make it strong" · "fast but good quality" · "fine and smooth" · "use less filament" · "40% infill" · "5 walls" · "no supports" · "add a brim" | adjusts the preset and says what changed |
| "It has to hold a heavy bag" (anything else) | Claude picks the settings, only from a safe list and within printable limits |
| "Save it as strong" | keeps the tweak as a new preset |
| "Which print profiles do I have?" · "how long will it take to print?" · "show me the slicing" | |
| **"Print it"** · "print the iPhone stand with fast" | slices if needed, asks "Is the bed clear?", then prints |
| "Print the temperature tower again" · "reprint it" | from the print library, straight away |
| "How's the printer?" · "pause / resume / stop the print" · "preheat the printer (for PETG)" · "cool down" · "home the printer" · "lower the bed 10 mm" · "turn off the motors" | |
| **"Print a temperature tower"** · "temperature tower from 230 to 190" | OrcaSlicer's own tower, cut to your range, 5 °C per 10 mm block |

**Finding things to print** (Printables and MakerWorld, no accounts; Thingiverse with a free key under `[models] thingiverse_token`):
- "**Find me a phone stand to print**" · "search printables for a cable clip" · "find a 3d model of a headphone hook": all sites at once in about 4 s, as **numbered photo cards** on the laptop's screen and in the phone app, best first.
- "**The second one**" / "the geared one", or tap a card: Batsy fetches just the file and shows a **quick preview** (its outline, its size in mm, whether it fits the 220 × 220 × 300 bed, the volume and how many parts) on the laptop and the phone, then asks "**Want me to slice it?**". Yes (or **Slice it** on the phone) slices it with your presets, as usual. MakerWorld downloads need your Bambu login, so its page opens instead.
- Every model you pick gets a card in the brain (`04 Resources/Models`).

**Printing goes straight over USB**, no copying to the card: a separate process (`batsy/printstream.py`) owns the port for the whole print, so Batsy restarting doesn't touch it, and the laptop is kept awake. Every print is saved in `~/.local/share/batsy/prints/` to print again. Progress and time left come from the slicer's own per-layer estimates. You get told when a print finishes, fails or pauses by itself (and on your iPhone).

Notes for this printer (Marlin 1.1.6, 2019): the USB port is set to `-hupcl` so reconnecting never restarts the board; it reports "TF printing" for card prints (a print stopped on its screen keeps saying it, which Batsy recognises); it has no bed PID and gives only 8.3 file names.

## Music that follows the mood

The endless radio after "play X" carries on **the mood and era** of the song, not just the same composer: Claude tags every song in your library and the catalogue once (mood, energy, decade, language) using **your taste profile**, built from your Spotify history ("learn my music taste"). "Play something sad" · "old feel-good Tamil songs" · "some 90s kuthu" start from the moods. Correct it any time: "this song is an old feel-good song", "Raja Raja Chozhan is an 80s feel-good song". Corrections always win and teach the tagging. The profile is a template: whoever links their Spotify gets their own (`~/.local/share/batsy/music_profile.json`). The radio never adds to Spotify's queue, and when you start a playlist yourself it stays out of the way.

## Full-screen mode

When you say "Batsy", the screen fades to a dark Batsy view, drawn by the GPU (three.js inside Qt WebEngine):
- **While you talk,** a glowing line ripples with your voice, particles gather into it, and your words appear live. This is local and uses no tokens.
- **While it works,** a pulse sweeps along the line and the particles stream through it; a status pill says what it's doing ("Asking Claude · 12s").
- **Saying just "Batsy"** gives a chime and the listening line, nothing spoken, so it never talks over you. Pause as long as you like (up to 8 s) before the request.
- **When it answers,** the reply appears as big captions while it speaks. Each exchange is its own block: what you said, then Batsy's reply. The newest is at full size at the bottom, and earlier ones dim and scroll off the top, so a follow-up never runs into the previous answer. When the overlay folds back into the corner bat, its bubble shows your last question and the reply to it.
- **Pictures without special phrases.** Ask in any words: "what does a capybara look like", "who's the CEO of Nvidia", "how big is a blue whale", "I wanna see that bridge in Prague". Claude decides when a picture helps and adds a hidden `[[show: …]]` note to its reply. Batsy cuts the note out of the speech, finds one picture (Wikipedia, then Openverse) and puts it on screen while still talking. There's no extra request to Claude, and Claude never has to see the picture.
- **Details** (lists, steps, numbers) appear on a text card, from a hidden `[[card: Title | text]]` note, instead of being read aloud.

The overlay closes by itself a few seconds after the answer, or 30 seconds after showing a picture. Esc, Space or a click closes it straight away, and **cancels whatever Batsy was still doing**: a Claude Code request in progress is stopped, and nothing from it can reopen the screen or speak afterwards. "~" (or saying "stop") does the same and also skips the follow-up listening. Only the wake word, or something Batsy really has to tell you (a reminder, a check-in question, a file that arrived), opens it again. Turn it off with `[overlay] enabled = false`. If GNOME keeps it behind other windows, set `qt_platform = "xcb"`.

**It doesn't crash the assistant.** The overlay is its own process. If the graphics driver makes it crash, Batsy restarts it with software rendering, and after repeated failures it switches to the plain Qt overlay (`[overlay] renderer = "classic"` forces that). Batsy itself runs under a supervisor (`batsy run` restarts the worker if it ever dies, and notifies you if it keeps happening), the voice loop survives any failed request, and the microphone reopens itself after suspend or replugging. Crash details: `~/.local/share/batsy/crash.log`, `overlay.log`.

## Learns once, then does it instantly

- **Opening and closing are instant, with no AI:** "open VS Code / Files / Firefox / YouTube / Downloads / github.com", "close Spotify". Batsy finds your installed apps (including Flatpaks) by name, with a few nicknames like "vs code", "files" and "browser".
- **New actions are learned once.** The first time you ask for something it doesn't know how to do, like "turn on dark mode", "connect my earbuds" or "set brightness to 40", Claude Code writes a small Python skill for it, covering the different ways you might say it. Batsy checks the code, runs it for you, and saves it ("Next time that'll be instant"). After that, any of those phrasings runs the saved code directly: no AI, no Pro usage.
- **No duplicates.** Before writing anything new, Claude sees a short list of the skills you already have. If your request is just a new way of saying one of them ("I'd prefer the dark theme" for "turn on dark mode"), Batsy adds your phrase to that skill and runs it. Next time that phrase is instant too. Opposites like "turn *off* dark mode" are never merged.
- **Writing code is never turned into a skill.** "Create a Python script that…", "build me a Flask API", "write unit tests for my parser" go to Claude Code as a coding job: it makes a folder under `~/Batsy/code/` (or works in the project you named), writes the code, runs it on a small example, fixes errors, then tells you the file name and how to run it and opens it in your editor.
- **Things you ask again and again become skills in the background,** so they're instant next time. To keep this from wasting Claude runs on one-offs, a request has to come up **twice within 7 days** first, there are at most **3 background learning runs a day**, and Claude writes them with the lighter model (`[claude] light_model`, Sonnet). Before this, things like "I don't know" were being turned into skills after a single slow answer.
- **Feedback teaches it.** Say "no, that's wrong" / "didn't work" right after a skill runs, or let it crash, and Claude rewrites it using your complaint. Two failures in a row switch the skill off until it's relearned. Every clean use counts as a success.
- **Safety:** skills can only run commands through Batsy's checked shell, so risky ones (deleting, uninstalling, shutting down…) still ask "Should I go ahead?". Code that tries to get around that is rejected. Questions and chat are never turned into code: Claude just answers them.
- `batsy skills` lists what it has learned, with how often each skill worked or failed. `batsy skills --forget NAME` removes one, or say "Batsy, forget how to …".

## Showing and pulling up things

- **Your usual phrases work instantly, with no AI:** "pull up connections for Nikola Tesla", "pull up the Taj Mahal", "show me a picture of a capybara", "what does a quokka look like". **Pull up** shows the best picture with an accurate facts card and speaks the first line. **Picture** shows just the picture.
- **Say it any other way** ("gimme a quick look at the Golden Gate Bridge", "throw everything on Marie Curie on the screen") and the AI works out what you meant. Batsy then **learns that phrasing**, so next time it's instant. Plain questions like "what is…" or "who's…" are never learned as show phrases. `batsy vocab` lists the phrases, and `batsy vocab --forget "phrase {x}"` removes one.
- **Current pictures and technical diagrams:** Batsy searches the live web too (DuckDuckGo images, no key needed), so "ESP32 pinout", "HC-SR04 wiring diagram" or "latest iPhone" find up-to-date images, not old Wikipedia photos. Results from trusted electronics sites (Random Nerd Tutorials, Adafruit, SparkFun, Espressif…) rank higher. Stock-photo watermarks and image-scraping sites rank lower. For technical images, the best guess appears in about 2 seconds while **Claude Code looks at the top 4 candidates in the background**. If it finds a more correct, readable one, the picture on screen is swapped.
- **The right picture, not the top search hit:** Batsy first works out exactly what you mean, so "jaguar animal" gives the animal and not the car. It then compares the article's editor-chosen image with high-resolution Wikimedia Commons photos. Images Wikipedia rates as featured or quality score highest, and wrong matches, tiny images and odd shapes score lowest. Specifics like "at night" or "interior" are taken into account. The facts come from the encyclopedia summary, not a search snippet.

## 3D models

"Open this 3D model" / "show me the gear STL" opens the most recent model, or the one you named, in **Batsy 3D**, right inside the overlay (its own window if the overlay is off). It's a dark viewer with a floor grid, colour-coded dimension rulers drawn in 3D (X, Y, Z in mm) and a live size readout. It handles STL, OBJ, PLY, GLB/GLTF, 3MF and OFF, plus STEP if the extra converter installs.

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

## Your projects: remembered, found, followed (no tokens)

Tell Batsy what you're working on in your own words. You don't need the repo's exact name:
- "I'm doing a project on the alumni election website" · "I started a new project, it's a portfolio site" · "I'm building a drone for my college project" · "my final year project is about attendance with face recognition"

Batsy says "Got it", then works out **which repo or folder it is**. It matches what you said against the names, descriptions, topics, READMEs and files of your GitHub repos and of the project folders on the laptop (anything with a `.git`, `package.json`, `pyproject.toml`, `.ino`, `.blend`, `.scad`, `.tex`…, including repos inside other repos). Joined-up names (`licetalumnielection`) and initials ("flight controller" → `fc_documentation`) are understood. If two projects are too close to call it asks "Is it humanrecog or humanrecog web?" (answer "the second one", or the name). With Claude Code set up, a quick one-line check usually settles it without asking.

**Then ask anytime:**
- "Give me an update on my robot project" / "how's the portfolio going" / "what did I do on the weather app this week": commits on GitHub and locally (unpushed ones too), uncommitted changes, recently edited files, open issues and PRs, and your own notes. Spoken in a sentence or two, with details on a card.
- "Update on my robot project: motors are wired" logs a note. "Any updates?" / "how are my projects going" covers all of them. "What are my projects?" lists them.
- "Where is my gazebo world editor project?" · "open my portfolio project" (opens the folder in VS Code, or the GitHub page).
- Check-ins and "good morning" mention new commits ("I saw 2 commits on it today. How did it go?").

**Finding files by what they're about:** "find my thermodynamics notes" · "where's the STL for the phone stand" · "find my arduino sketch for the servo". Batsy names the best match and asks "Want me to open it?"; say "yes", "the second one" or "no".

**GitHub access:** public repos work straight away. Batsy uses `[github] user`, or works your username out from your git identity and your clones. For private repos, put `GITHUB_TOKEN=...` (a fine-grained read-only token) in `~/.config/batsy/env`, or run `gh auth login`. Check what it sees with `batsy projects --scan`, `batsy projects --find "the drone docs"` and `batsy projects --update robot`. The index refreshes in the background every 6 hours. `[projects] roots` limits where it looks.

## Files between your Mac and Batsy (Tailscale)

**Mac → Batsy:** right-click a file → **Share → Tailscale** → this laptop, or run `tailscale file cp FILE <this laptop's name>:`. Batsy collects it into `~/Batsy/inbox`, puts it on screen and says *"datasheet.pdf just arrived. Want me to open it?"* (answer "yes", "the second one" or "no"). PDFs, documents and images are read in straight away, so "summarise what I just sent" or "circle the pin numbers in the file I sent" work. STL, 3MF and STEP files open in the 3D viewer. If you're away from the laptop it just announces the file and shows a notification. Files dropped into the inbox any other way (LocalSend, `scp`) are noticed too.

**From friends, split by who sent it:** every sender has a folder, `~/Batsy/inbox/<name>/`. Taildrop only works between your own devices, so whatever it delivers goes in your folder. Anyone else on the tailnet opens `http://workstation:8766` (a page with a file picker that works on phones too) or runs `curl -T FILE http://workstation:8766/`. Batsy asks Tailscale who is connecting (`tailscale whois`), so the folder can't be faked. Logins missing from `[files.senders]` are refused:
```toml
[files.senders]               # Tailscale login -> folder name
"cvineetjonathan@gmail.com" = "Jonathan"
"rehaanjohn@passkey" = "Rehaan"
"rkarancindae@gmail.com" = "Karan"
"rehaanrafael.john@gmail.com" = "Rehaan"   # a second login of the same person

[files.pc_users]              # folder -> the user on the PC whose Inbox gets a copy
"Jonathan" = "batman"         # /batman/Inbox (the PC's 500 GB drive)
"Karan" = "nightwing"         # /home/nightwing/Inbox
"Rehaan" = "superman"         # /home/superman/Inbox
```
Batsy then says *"Karan sent wiring.pdf"* and copies it to that person's **Inbox on the PC** (queued if the PC is off). "Set up the inboxes on the PC" creates them once: private to each user; bob can only drop files in.

**Batsy remembers every file it received** (`~/.local/share/batsy/received.db`): "who sent the last file?" · "what did Karan send yesterday?" · "open the audit report" · "open Karan's last PDF" · "where is the human values assignment?" · "list everything I've received". The AI can search it too.

**Batsy → Mac:** "send this to my Mac" (the file Batsy just made, opened, found or received) · "send the marked-up PDF to my Mac" · "send my arduino sketch for the servo to my Mac" · "send the portfolio project to my Mac" (a folder is zipped first, without `.git`, `node_modules` or virtualenvs). It arrives in the Mac's Downloads folder. Also: "what's in my inbox?", "open my inbox", "open the file I just sent".

**One-time setup on Fedora:**
```bash
sudo dnf install tailscale && sudo systemctl enable --now tailscaled
sudo tailscale up                       # sign in with the same account as your Mac
sudo tailscale set --operator=$USER     # lets Batsy collect received files without root
```
"My Mac" means the Tailscale device `c` (change it, or add `ipad` / `phone`, under `[files] devices`). Taildrop only sends between devices signed in to your own account. `batsy doctor` checks all of this.

## Shopping: compare prices across stores

Ask about anything you might buy, in any words:
- **A specific product:** "how much is the Raspberry Pi 5 8GB" · "compare prices for PETG filament 1kg" · "where can I buy an ESP32 DevKit V1" · "price of the iPhone 16 128GB". It's checked on every store that fits, all at once, in about 5 seconds:
  - electronics and maker parts: Amazon, Flipkart, Robu, Robocraze, ElectronicsComp, ThinkRobotics, Quartz Components, Evelta;
  - gadgets and appliances: Amazon, Flipkart, Croma, Reliance Digital, Vijay Sales, Tata CLiQ, JioMart;
  - fashion and beauty: also Myntra, AJIO, Meesho and Nykaa.

  Batsy says the cheapest in-stock price and the next few ("Cheapest is Amazon at 22,989 rupees. Quartz Components has it for 17,500, but it's out of stock."). The full table (store, price, listing name, stock, link) goes on screen and into a page in `~/Batsy/shopping/`.
- **Not sure what to get:** "find me a 55 inch TV under 50000" · "best earbuds under 2000" · "a microcontroller for a drone". Claude picks a few current models that fit, using web search (one request, about a minute), and each is priced across the stores.
- **Then:** "open the cheapest one" · "open the Flipkart one" · "open the comparison".

**How it reads prices (no store offers an API):**
- Amazon's and Flipkart's own search results.
- Shopify stores' product search.
- Elsewhere, the store's own page for the product, through the product data shops publish for Google.
- Croma, Tata CLiQ and JioMart fill their prices in with JavaScript, so their price comes from a search listing (marked "approx.") or just the link.

**How it matches:** a different variant never counts as the product you asked for:
- 4GB for 8GB, 43" for 55", iPhone 16 **Plus** for iPhone 16, ESP32-**C5** for ESP32;
- an accessory: a case, power supply, or a "relay board *for* ESP32";
- a starter kit, unless nothing else matches.

Prices are as listed, before delivery charges; stores change their pages, so a store sometimes drops out.

## Phone app (Android and iPhone)

The same app runs on both. Its pages are served by the laptop (`batsy/phone_ui/`, `/app/` on port 8765), so changes reach the phone without reinstalling. It shows Batsy's state and an Ask box, the **3D printer** (a liquid fill rising with the progress, name, grams, time left, finish time, controls), the **latest slice** (preview, 3D view with a filling-bat loader, Print), the **lights and PC**, **files received**, **laptop, PC and HP** specs (the HP card shows that it runs Batsy's AI, the model, and how fast it starts speaking; "AI on standby" when the laptop has taken over), **music** (with the album art's colours as the theme) and **Claude usage**. Confirmations appear as frosted glass over the widget you tapped.

**Android (`android/`, `android/dist/batsy-widgets.apk`): StandBy.** Landscape and full screen, like iPhone StandBy: two panels of widgets you scroll up and down, a big clock and Now Playing to swipe to, dimmed at night, the screen kept on. It connects over the **USB cable** (`adb reverse`) first, then Tailscale. "Batsy, install the widget app on my phone" installs and pairs it over USB. Rebuild with `android/build.sh` (needs a JDK and the Android SDK; this laptop has both in `~/.local/share/jdk` and `~/Android/Sdk`).

**iPhone: a home-screen web app (PWA), free, never expires.**
1. Tailscale HTTPS certificates on (admin › DNS), and `tailscale serve --bg --https=443 http://127.0.0.1:8765` (tailnet only).
2. On the iPhone, with Tailscale connected: Safari › `https://workstation.taila566eb.ts.net/app/` › Share › **Add to Home Screen**.
3. Open it from the home screen, say **"Batsy, pair my iPhone"** and type the code.
4. Tap the **bell** › Allow: **push notifications** (Apple's web push, signed with Batsy's own key, `~/.local/share/batsy/vapid.pem`) for the 3D printer and reminders, even when the app is closed.

On the iPhone the app is a **dashboard** (one scrolling page of cards, no clock). Updates arrive by themselves; only a new icon needs the app re-added.

**Your layout, your way:** long-press any widget.
- **iPhone:** the widgets wiggle; drag one by **≡** to move it, **−** hides it, **＋ Add widgets** brings hidden ones back, **Reset** restores the original order.
- **Android:** an **Edit widgets** sheet opens with the left stack, right stack and hidden widgets side by side: drag by **≡** (between stacks too), − to hide, ＋ to add back.
- The layout is saved on each phone.

**Files open where you are:** tap one at home (the phone on the same Wi-Fi as the laptop, which Tailscale's direct connection shows, or on the USB cable) and you can open it on the laptop, with a filling-bat loader while it opens. Away, it opens inside the app on the phone: pictures, video, text and PDFs, with Share for Save to Files, and the same filling bat while it downloads. Both choices are always on the card.

**Batman drive: your PC's 458 GB drive as cloud storage.** The **Batman drive** widget shows the space used and your 6 newest files with previews (photo thumbnails, first page of PDFs). **All files ›** opens the whole drive: folders, search by name, **＋** to upload from the phone (several at once, with progress) and a button for new folders.
- **Cached on the phone:** folder listings show at once, even with the PC asleep; thumbnails are made once per file version; files you open are kept (about 400 MB, oldest dropped first) so they reopen instantly, offline too.
- **One-time setup:** say **"Batsy, set up the batman drive"** and type the PC's password in the terminal that opens. It lets the laptop's SSH key sign in as `batman`. Files opened on the laptop are copied to `~/Batsy/batman/`.

## Talk to Batsy through the phone

In the phone app (Android and iPhone), next to **Ask Batsy…**:
- **🎙 Hold to talk:** hold it, speak, let go. The laptop turns it into words with Whisper and handles it exactly as if you'd said it after "Batsy". Batsy answers **out loud from the laptop's speakers**, as usual; the phone is only the microphone, and shows the answer as text.
- **👂 Phone as Batsy's ears:** tap it and the phone listens for "**Batsy, …**" (or "Batsy", a beep, then your request) for as long as it's on and the app is open. Meanwhile the laptop's own mic stops waking Batsy. Tap again, or leave the app, and the laptop's mic takes over again.

The Android app (version 4.0+) asks for the microphone the first time. The iPhone app asks through Safari. Background noise (music in the room) lowers accuracy: hold the phone near you.

## Batsy Brain: your Obsidian second brain

`~/Batsy/Brain` is an Obsidian vault that Batsy **writes as your day happens and reads back when it answers**. It's made the first time Batsy starts, already filled from everything Batsy knew: memories, projects, every file you've received, prints, your printer setup and presets, your music taste, and daily notes for the past 30 days rebuilt from your conversations.

**How it's organised:**

| Folder | What's in it |
|---|---|
| `Home.md` | the dashboard: today, plans, projects, inbox, recent notes (live tables via Obsidian Bases) |
| `00 Inbox` | quick notes ("note that…", the ＋ in the phone app) |
| `01 Daily` | a note per day: **Plan**, **Summary** (written each night by the free local AI), **Timeline** (what you asked, received, printed, were reminded of), **Files**, **Prints**, **Music**, and your own **Notes**. **Weekly reviews** every Sunday night, written by Claude: wins, project progress, open loops, next week |
| `02 Projects` | a note per project you told Batsy about: repo, folder, a log of every update |
| `03 Areas` | 3D Printing (setup, presets, every slice and print), Music (taste profile, plus **Listening**: this week and month, top artists, songs on repeat, latest plays), College, Home & Devices |
| `04 Resources` | a card for every file received (who sent it, a link, its opening lines), every print (preview, grams, time, result), every price comparison |
| `05 People` | who sent you what |
| `06 Memory` | **About Jonathan**: everything Batsy remembers. **Edit it in Obsidian**: change a line to correct a memory, delete one to make Batsy forget, add one to teach it |

**Batsy never overwrites your writing.** It only rewrites text between `%% batsy:… %%` and `%% /batsy %%` (hidden when reading), and adds lines inside those blocks. Everything else in every note is yours. Notes link to each other (days ↔ projects ↔ files ↔ people), so the graph view is meaningful. Folders have colours in the graph, and the accent is Batsy yellow.

**Say it** (instant, no tokens):
- "Note that the bed needs levelling" · "jot down: buy PETG" · "add to my robot notes: the driver overheats"
- "What did I do yesterday?" · "…today / this week / on Monday" · "summarise my day"
- "What do my notes say about PLA?" · "search my brain for supports" (the local AI answers from your notes)
- **Batsy Discovery:** every Monday Claude picks about 25 songs you've **never played** that fit your taste profile and listening (your eras, composers, moods; remixes and covers skipped), and they replace the tracks of your private Spotify playlist **Batsy Discovery**. "Play my discovery playlist" / "play something new" plays it; "make my discovery playlist" / "find me new songs" makes a fresh one now. The list is in Music.md (Discovery), and each one you play gets ticked ▶. Needs `batsy spotify-login` once (playlist permission).
- "What did I listen to yesterday?" · "…today / this week": **every song you play on Spotify** (any device, playlist or radio) goes into that day's Music, with its time, artists, album and mood
- "Open my brain" · "open today's note" · "open the robot note" (in Obsidian) · "sync my brain"
- "Remember that…" still goes to memory, which now also shows up in About Jonathan

**Batsy uses it:** questions that match your notes get a short "[From your notes]" snippet as context. Both AIs have tools to search, read and add to the brain, and Claude Code knows the vault is there.

**Phone app:** a **Brain** widget shows today (summary, or the latest moments), your plan and the inbox count, with a box to jot a note. Tap it or **Open ›** to read any note, follow its links and search the whole brain.

**Sync to the iPhone and Mac (free):** Batsy commits the vault to Git every 10 minutes, so you get the full history, and pushes it to a private GitHub repo.
1. On github.com, create an **empty private repository named `brain`** (no README).
2. Batsy pushes to it within 10 minutes (or say "sync my brain").
3. On the iPhone/Mac: install **Obsidian**, add the community plugin **Obsidian Git**, and clone `https://github.com/jonathanvineet/brain` (a GitHub personal access token as the password). Edits made there are pulled back to the laptop.

`batsy vault seed | search WORDS | daily [DATE] | weekly [DATE] | sync | open` · settings under `[vault]` (`path`, `capture = "all" | "important" | "off"`, `git`, `git_remote`, `git_every_min`).

## Research assistant

Several research topics at once, kept apart, all written into your Obsidian brain by Batsy (`~/Batsy/Brain/08 Research/<Name>/`). You never write there yourself.

- **Just say it.** The local AI works out what you mean, so there's no list of phrases to learn:
  - "I want to start a research" (Batsy asks what it's about), "can we look into how bats fly"
  - "switch to my drone research", "what research am I doing?"
  - "note: …", "what did we find about cooling?", "what are the open questions?", "what should I read next?"
  - "find papers on …", then "keep the second one"; "summarise it"
- **While a research is on** (for 2 hours after you last touched it), anything you say that isn't a normal command is a conversation about it. The local AI acts as a thinking partner:
  - it asks what you mean and points out assumptions
  - it gives the strongest counter-argument, offers another angle, and suggests a next step
  - it uses your own notes and sources, saying which one
  - questions that aren't about the research ("what's 35k euros in INR?") still go to the normal commands.
- **Files you send** (Tailscale or the upload page) are read and understood:
  - Batsy reads every page: PDFs, docx, text, and photos and scans by OCR.
  - It works out what each file is and files research material into the right research. If it can't tell, it asks which one; for a new topic it asks "start a research on it?".
  - It tells you what it understood, out loud, on a laptop card and on the phone.
- **Papers** come from OpenAlex (free, about 250M works, citation counts), arXiv, and Semantic Scholar as a backup. "Keep the second one" saves it, and Batsy downloads the open-access PDF and reads it.
- **Claude only when you ask:** "think hard about this", "go deeper", "find other ways", "challenge this".
  - Claude only *finds* the documents and hands them to Batsy (`batsy research fetch`).
  - Batsy downloads them and **reads and understands every one itself** with the local AI, writing a Source note for each: what it is, key points, how it relates to your question. The full text is kept, so later questions use all of it.
  - These deep dives are the only part that uses credits.
- **In each research folder:**
  - the hub note: question, living summary, findings, open questions, ideas, next steps, sources, log
  - `Notes/`: your words plus a clean version
  - `Sources/`: one note per paper or document
  - `Files/`: the PDFs and pages themselves
  - `Conversations/`: written after 5 quiet minutes, with what was discussed, ideas, decisions and new questions, which also go onto the hub
- **Filing notes and files into the right research** uses `nomic-embed-text` on the HP (meaning, not just words). When the HP is off, it falls back to word matching.
- `batsy research list|show|search|papers|fetch|add-finding|add-note`: the same, from a terminal. It's also Claude's tool.

## Reminders (no tokens)

- "Remind me in 20 minutes to take the print off the bed" · "remind me at 5 pm to call mom" · "remind me tomorrow at 9 to submit the DBMS assignment" · "remind me on Friday at 4:30 to push the code" · "remind me tonight to charge the batteries" · "remind me at half past five to eat" · "set a reminder for 7 to go to the gym"
- "What are my reminders?" · "cancel the reminder about the gym" · "cancel all reminders" · after one goes off: "snooze it" / "remind me again in 5 minutes"
- **"Remember that…"** saves a note to long-term memory straight away; "what did I ask you to remember?" reads them back.
- Said messily ("Pati, remind me at uh 11 30…") they still work; "remind me about X today" with no time asks what time.
- Reminders survive restarts. If the laptop was asleep when one was due, it still goes off and says when it was due. Anything with "every…" ("remind me every Sunday to do laundry") becomes a tracked chore instead (see above).

## Speaks your language: developer, 3D and college vocabulary

Parakeet (the speech recogniser) was trained on general speech, so it used to write "Bamboo Studio", "Cubectil", "Ladek" and "red apology". `batsy/lexicon.py` fixes that in two ways. It **biases Parakeet** (and Whisper) toward the words you actually use: tools, file formats, materials and course terms, plus **your own project names**. It also **repairs the usual mishearings** afterwards ("fusion three sixty" → Fusion 360, "pet g" → PETG, "cube control" → kubectl, "mat lab" → MATLAB, "nsam" → end-sem). On a test of 10 jargon-heavy sentences, recognition went from 17 to 30 of 30 technical words. Ordinary speech is left alone: only distinctive forms are rewritten.

- Domains: `[voice] lexicon = ["developer", "3d", "student"]`. Drop one you don't use, or add your own words to `hotwords`.
- Both brains also know who you are, from `[user] about` (by default a college student who codes and does 3D design and printing). They pitch answers at that level and use your jargon.

## Claude budget (your Pro plan)

Every Claude Code request Batsy makes is counted exactly, using the token numbers Claude Code reports. Pro's own limits can't be read from outside the official app, so Batsy keeps **its own budgets**:
- **Session:** a 5-hour window, like Pro's.
- **Week:** resets every Monday.

Batsy warns you at 80%. At the limit it switches to the local AI until the budget resets. "Ask Claude anyway…" goes through regardless. Set the budgets by voice ("set my weekly Claude limit to 5 million", "set my session limit to 800k") or under `[claude_budget]` in the config. Ask "how much Claude have I used?" to hear where you are.

## Screensaver: a live dashboard Batsy can redesign

After 3 minutes with no keyboard or mouse activity, as long as Batsy isn't doing anything, the screen turns into an animated dashboard. Say "show me the screensaver" to see it now. Any key or mouse movement closes it, or say "Batsy".

- **Backgrounds:** aurora, stars, waves, orb or minimal, in any accent colour. Everything drifts slowly, so nothing burns into the screen.
- **Pieces it can show:**
  - the clock
  - Claude budget (session and week, with reset times) and local AI tokens
  - running jobs and questions waiting for you
  - chores, projects and habits coming up, and today's plan
  - live laptop stats (CPU, RAM, temperature, battery) and the song playing on Spotify
  - weather for any city, countdowns, notes and quotes
  - a photo slideshow from a folder, one project's update history, or **any command's output** (so it can show anything)
- **Change it by voice, instantly and free:** "add the weather in Chennai to the screensaver" · "put system stats on the screensaver" · "add a countdown to my exam on 12 November to the screensaver" · "make the screensaver blue stars" · "remove the local tokens from the screensaver" · "add a note saying buy resistors to the screensaver" · "make the screensaver minimal / detailed / focus / default".
- **Anything fancier** ("make it look like a cyberpunk terminal", "show my GitHub notifications on it") goes to Claude Code. Claude rewrites the layout file `~/.local/share/batsy/screensaver.json` with `batsy saver set`, and can add custom command panels.
- **It adapts on its own:**
  - Near your Claude limit, the budget glows and moves up.
  - Questions waiting for you appear at the top.
  - Music that's playing shows up.
  - Running jobs appear while they run.
  - After 11 PM it goes dim and calm.

  Turn this off with "turn off auto on the screensaver".

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
| `~/Batsy/inbox/<sender>/sliced/` | G-code, previews and 3D paths of each slice |
| `~/.local/share/batsy/printer/` | your printer presets (from the bundles you sent) and `settings.json` |
| `~/.local/share/batsy/prints/` | every print, to print again (`history.json`) |
| `~/.local/share/batsy/calibration/` | calibration towers (OrcaSlicer's tower converted to STL in `orca/`) |
| `~/.local/share/batsy/received.db` | every file received: who, when, where its copies are |
| `~/.local/share/batsy/music_moods.json` · `music_profile.json` | song moods and your taste profile |
| `~/.local/share/batsy/push_subs.json` · `vapid.pem` | phones with notifications on, and the key that signs them |
| `~/Batsy/Brain/` | your Obsidian brain (a Git repo) |
| `~/.local/share/batsy/cloud_thumbs/` | previews of Batman drive files |
| `~/Batsy/batman/` | Batman drive files you opened on the laptop |

## The brain: local and free (with Claude on request)

- **Default: a local AI.** `qwen2.5:3b` runs in Ollama on the laptop. It's free, works offline and has no limits. It's tuned for 8 GB of RAM and real back-and-forth conversation: the model stays loaded, its instructions are tiny (about 200 tokens), memory is short, tool output is trimmed, and replies are spoken as they stream. On a typical laptop CPU, expect the first words about 1–3 seconds after you stop talking.
- **Complex requests go to Claude automatically,** through Claude Code on your **Claude Pro** plan. That covers code, Arduino sketches, "explain why…", comparisons, plans, writing and long questions. The local AI also hands off anything it isn't sure about. Everything else stays local and free. Saying "ask Claude…" still forces it. **Claude can work anywhere on the laptop:** any folder (including Batsy's own code in `~/batsy`), any command, and the web. Risky commands (deleting recursively, uninstalling, shutdown, disk tools, force-push…) are confirmed with you by voice first, through a Claude Code hook (`batsy/guard.py`); root goes through `pkexec`, so you get the normal password dialog. Turn the automatic hand-off off with `[local] auto_claude = false`. One-time setup: `npm install -g @anthropic-ai/claude-code`, then run `claude` and log in.
- **Faster but simpler:** `[local] model = "qwen2.5:1.5b"`. **Smarter but slower:** `"llama3.2:3b"` or a 7B model if you get more RAM.
- **Have a Claude API key?** Set `[brain] engine = "claude"` to use the full Claude agent instead.
- The no-token local commands (music, routines, camera, time, volume, "show me a picture of X") work the same with either brain.

## Two laptops: the HP runs the AI

The local AI runs on the **HP laptop** (`jonathanlen`, i3-8130U, 8 GB, Ubuntu), wired to this one at `10.10.10.4`. This laptop does the listening, speech recognition, voice, screen and apps. It keeps the 2.6 GB the model would take, and stays cooler.

- **Always ready.** On the HP the model never unloads, so there's no "One moment". The HP's Ollama is a user service (`~/.config/systemd/user/ollama.service`, `~/.local/ollama`). It's bound to the cable address only, starts at boot without a login (linger), and keeps 3 prompt caches: quick answers, full answers with tools, and background jobs.
- **Falls back by itself.** `batsy/llm_hosts.py` checks the HP every 15 s. If it's off, everything uses the Ollama on this laptop: slower, but nothing breaks. When it's back, Batsy switches back. The hosts list is `[local] hosts` in the config.
- **The prompt cache is the big win.** Prompts start with the parts that stay the same (the instructions, then the conversation), so the model only reads the new words. When you say "Batsy", the HP pre-reads the conversation while your words are being transcribed.
- **Measured** with `python -m batsy.tools.bench HOST`:

  | | HP | This laptop |
  |---|---|---|
  | Writing the answer | 6.1 words/s | 3.9 words/s (hot, throttled) |
  | New conversation | 62 s | 55 s |
  | Same conversation, new question | **12 s** | 17 s |

  Real quick answers on the HP take about **5 s, with the first word in about 4 s** (they used to take 12.6 s at the median). With the HP off: about 20 s.
- **Room talk never reaches a model.** Overheard conversation after a false "Batsy" ("such a thank you mm-hmm") used to be half of everything sent to the AI. It's now recognised and ignored, with no reply and no CPU. This is checked only after instant commands and skills, and only for speech, never for typed text.
- **`batsy speed`** shows, per route, how long the first word and the whole answer take, where the AI is running, and which jobs use Claude's tokens.

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
- **Remembered answers match the meaning, not the letters.** "What's the capital of Japan" reuses the saved answer for "what is the capital of Japan". "What is a resistor" no longer gets the saved answer for "transistor".
- **The lighter Claude model for plain questions.** "Ask Claude what the difference between PLA and PETG is" uses Sonnet. Code, files, git and real work use the full model.
- **Caching.** The system prompt and tools (about 3.3K tokens) are cached for an hour, so repeat questions only pay for the new part.
- **No false triggers.** Whisper must confirm "Batsy" before anything is sent. Background chatter heard during the follow-up window is ignored.
- **`batsy usage`** shows how many tokens you've used today, this week and all time. Each request is logged to `~/.local/share/batsy/usage.jsonl`.

## Cost and safety

Every request sends the conversation to the Claude API, billed to your key. Prompt caching keeps repeat turns cheap. Batsy runs commands as you, with your permissions. Read the log if you want to see exactly what it did.
