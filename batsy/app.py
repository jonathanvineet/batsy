"""Batsy runtime: wires voice, brain, tools, monitors and the local control socket together."""
import json
import logging
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time

from batsy import safety, spotify
from batsy import images
from batsy.brain import Brain
from batsy.overlay import Overlay
from batsy.routines import Routines
from batsy.vocab import get_vocab
from batsy.config import SOCKET_PATH
from batsy.tools import ToolContext, build_registry
from batsy.tools import monitor, music
from batsy.voice.tts import Speaker

log = logging.getLogger("batsy")

FILLERS = ["On it.", "One sec.", "Working on it.", "Give me a moment."]
STOP_WORDS = {"stop", "cancel", "never mind", "nevermind", "shut up", "be quiet", "quiet", "forget it"}
RESET_PHRASES = {"new conversation", "start over", "fresh start", "clear context", "reset conversation"}
# Whisper sometimes "hears" these in silence or background noise; never spend tokens on them.
NOISE_TRANSCRIPTS = {"you", "thank you for watching", "bye", "uh", "um", "hmm", "so", ""}
END_FOLLOWUP = {"thanks", "thank you", "that's all", "thats all", "nothing", "no", "no thanks", "that's it", "ok", "okay"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", "", text.lower())).strip()


class Batsy:
    def __init__(self, cfg, voice: bool = True, speak: bool = True):
        self.cfg = cfg
        self.voice = voice
        self.speaker = Speaker(cfg, enabled=speak)
        self.ctx = ToolContext(cfg, announce=self.announce, confirm=self._confirm)
        self.registry = build_registry(self.ctx)
        if cfg.get("brain.engine") == "claude":
            self.brain = Brain(cfg, self.registry)
        else:
            from batsy.local_brain import LocalBrain

            self.brain = LocalBrain(cfg, self.registry)
        self.turn_lock = threading.Lock()
        self.confirm_handler = self._confirm_console
        self.confirm_request: dict | None = None
        self.ears = None
        self.stt = None
        self.overlay = Overlay(cfg, on_dismiss=self._overlay_dismissed, enabled=voice)
        self.overlay_dismissed = False
        if self.overlay.enabled:
            self.ctx.display = self.overlay.send
        self.brain.visual = self.show_visual
        self.routines = Routines(self)
        self.vocab = get_vocab()
        self._turn_markers: list[tuple[str, str]] = []
        self.saver_on = False
        self.overlay.on_saver_exit = self._saver_exited
        if self.overlay.enabled and cfg.get("overlay.screensaver_minutes") > 0:
            threading.Thread(target=self._saver_loop, daemon=True, name="screensaver").start()
        self.last_raw_text = ""
        monitor.start_background(self.ctx)

    def _saver_exited(self) -> None:
        self.saver_on = False

    def _saver_loop(self) -> None:
        """Show the token-usage screensaver while the user is away and Batsy has nothing to do."""
        from batsy import idle, usage
        from batsy.memory_store import get_store

        after_ms = self.cfg.get("overlay.screensaver_minutes") * 60_000
        last_refresh = 0.0
        while True:
            time.sleep(5)
            away = idle.idle_ms()
            if away is None:
                continue
            busy = self.turn_lock.locked() or not self.speaker.idle() or \
                time.time() - self.overlay.last_active < self.cfg.get("overlay.linger_media_seconds") + 5
            if self.saver_on and away < 3000:
                self.saver_on = False
                self.overlay.hide()
            elif not busy and away >= after_ms and (not self.saver_on or time.time() - last_refresh > 30):
                items = get_store().agenda_for(time.strftime("%Y-%m-%d"))
                nxt = ("Today: " + " · ".join(i["text"].rstrip(".") for i in items[:3])) if items else ""
                self.overlay.saver(usage.stats(), nxt)
                self.saver_on, last_refresh = True, time.time()

    def _overlay_dismissed(self) -> None:
        self.overlay_dismissed = True
        self.speaker.stop()

    def show_visual(self, kind: str, arg: str) -> str | None:
        """Put a picture ([[show: query]]) or a text card ([[card: Title | text]]) on screen.
        Returns the picture's caption, or None if nothing was found."""
        self._turn_markers.append((kind, arg))
        if kind == "pull":
            return self.pull_up(arg, speak=False)
        if kind == "card":
            title, _, body = arg.partition("|")
            if self.overlay.enabled:
                self.overlay.card(title.strip(), body.strip())
            else:
                print(f"\n[{title.strip()}]\n{body.strip()}")
            return title.strip()
        try:
            path, caption = images.find_image(arg, on_better=self._better_picture)
        except Exception as e:
            log.warning("no picture for %r: %s", arg, e)
            return None
        if self.overlay.enabled:
            self.overlay.image(path, caption)
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return caption

    def _better_picture(self, path, caption: str) -> None:
        """Claude looked at the candidates and preferred another one: swap it in."""
        log.info("Claude picked a better picture: %s", caption)
        if self.overlay.enabled:
            self.overlay.image(path, caption)
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def pull_up(self, subject: str, speak: bool = True) -> str | None:
        """Picture + accurate facts card for a person/place/thing. Returns the resolved name."""
        try:
            d = images.pull_up(subject, on_better=self._better_picture)
        except Exception as e:
            log.warning("pull up %r failed: %s", subject, e)
            if speak:
                self._reply(f"I couldn't find {subject}.")
            return None
        body = "\n".join(filter(None, [d["description"], d["facts"]]))
        if self.overlay.enabled:
            if d["image"]:
                self.overlay.image(d["image"], d["caption"])
            if body:
                self.overlay.card(d["title"], body)
        else:
            print(f"\n[{d['title']}]\n{body}\n{d['url']}")
            if d["image"]:
                subprocess.Popen(["xdg-open", d["image"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if speak:
            self._reply(d["spoken"])
        return d["title"]

    def speak_brief(self, prompt: str) -> None:
        """Run a short Claude request from a routine and speak the answer (costs tokens)."""
        self.run_turn(prompt, "routine", self._reply, threading.Event(), self._confirm_console)

    def _reply(self, text: str) -> None:
        """Speak and caption a locally generated reply."""
        self.speaker.say(text)
        self.overlay.say(text)

    # --- shared plumbing --------------------------------------------------
    def announce(self, text: str) -> None:
        log.info("announce: %s", text)
        self.brain.note_announcement(text)
        self.speaker.say(text)
        try:
            subprocess.Popen(["notify-send", "-a", "Batsy", "Batsy", text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass
        if not self.voice:
            print(f"\n[Batsy] {text}")

    def _confirm(self, question: str) -> bool:
        return self.confirm_handler(question)

    def _confirm_console(self, question: str) -> bool:
        try:
            return safety.is_yes(input(f"\n[Batsy asks] {question} (yes/no) "))
        except EOFError:
            return False

    def _confirm_voice(self, question: str) -> bool:
        # Called on the brain thread; the main voice loop owns the mic and answers it.
        req = {"question": question, "done": threading.Event(), "answer": False}
        self.confirm_request = req
        req["done"].wait(timeout=60)
        self.confirm_request = None
        return req["answer"]

    def _serve_voice_confirm(self) -> None:
        req = self.confirm_request
        self.speaker.say(req["question"] + " Say yes or no.")
        self.speaker.wait_idle()
        self.ears.drain()
        audio = self.ears.record_utterance(start_timeout=8)
        answer = self.stt.transcribe(audio) if audio is not None else ""
        req["answer"] = safety.is_yes(answer)
        if not req["answer"]:
            self.speaker.say("Okay, I won't.")
        req["done"].set()
        self.confirm_request = None

    def run_turn(self, text: str, source: str, say, abort: threading.Event, confirm) -> str:
        """Run one request through the brain, then honour any restart/reset the tools asked for."""
        spoke = []

        def on_tool(name: str) -> None:
            if not spoke and self.speaker.idle() and source == "voice":
                self.speaker.say(random.choice(FILLERS))
            spoke.append(name)

        with self.turn_lock:
            self.confirm_handler = confirm
            self._turn_markers = []
            try:
                reply = self.brain.ask(text, source, say, abort, on_tool)
            finally:
                self.confirm_handler = self._confirm_console
            self._learn_phrase(text)
        self._apply_flags()
        return reply

    def _learn_phrase(self, text: str) -> None:
        """If the AI showed something for a phrasing we didn't know, remember that phrasing."""
        shows = [(k, a) for k, a in self._turn_markers if k in ("show", "pull")]
        n = _norm(text)
        if len(shows) != 1 or self.vocab.match(n):
            return
        kind, subject = shows[0]
        learned = self.vocab.learn(n, "pullup" if kind == "pull" else "picture", subject)
        if learned:
            log.info("learned new phrase: %r -> %s", learned, kind)

    def _apply_flags(self) -> None:
        flags = self.ctx.flags
        if flags.pop("new_conversation", False):
            self.brain.reset()
        if flags.pop("restart", False):
            self.speaker.wait_idle(15)
            self.restart()

    def restart(self) -> None:
        log.info("restarting")
        if self.ears:
            self.ears.close()
        SOCKET_PATH.unlink(missing_ok=True)
        os.execv(sys.executable, [sys.executable, "-m", "batsy", *sys.argv[1:]])

    def local_command(self, text: str) -> bool:
        """Handle common phrases instantly, without calling Claude (zero tokens). Returns True if handled."""
        from batsy.tools import screen

        n = _norm(text)
        self.last_raw_text = text.strip()
        if n in STOP_WORDS:
            self.speaker.stop()
            self.overlay.hide()
            self.routines.capture = None
            return True
        if self.routines.handle(n):
            return True
        m = re.match(r"(?:hey |ok )?ask (?:claude|cloud|clawed|claud)(?: to| about| if| whether)?[,\s]+(.+)", text.strip(" .!?"), re.I)
        if m:  # the only path that touches the Pro plan: explicit, one request
            from batsy.local_brain import ask_claude

            self._reply("Asking Claude.")
            answer = ask_claude(m.group(1))
            self.brain.memory.add_exchange("claude", text, answer, ["asked Claude Code"])
            self._reply(answer)
            return True
        if n in RESET_PHRASES:
            self.brain.reset()
            self._reply("Fresh start. I still remember the long-term stuff.")
            return True
        if re.fullmatch(r"(please )?(open|start|show|turn on)( up)?( the| my)? (camera|webcam)( please)?", n):
            self._reply(screen.open_camera(self.cfg))
            return True
        if re.fullmatch(r"(please )?(close|stop|hide|turn off)( the| my)? (camera|webcam)( please)?", n):
            self._reply(screen.close_camera())
            return True
        hit = self.vocab.match(n)  # your phrases for "show me" / "pull up", including learned ones
        if hit:
            intent, subject = hit
            if intent == "pullup":
                self.pull_up(subject)
            else:
                caption = self.show_visual("show", subject)
                self._reply(f"Here's {caption}." if caption else f"I couldn't find a picture of {subject}.")
            return True
        if re.fullmatch(r"what('s| is) the time( now)?|what time is it( now)?", n):
            self._reply(time.strftime("It's %-I:%M %p."))
            return True
        if re.fullmatch(r"what('s| is) (the date|today's date)|what day is (it|today)", n):
            self._reply(time.strftime("It's %A, %B %-d."))
            return True
        m = re.fullmatch(r"(set )?(the )?volume (to )?(\d{1,3})( percent)?", n)
        if m:
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{min(int(m.group(4)), 100)}%"])
            return True
        if n in ("mute", "unmute", "toggle mute"):
            subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
            return True
        sp = music.get_spotify(self.cfg)
        if sp.ready():
            try:
                reply = spotify.handle_local(sp, n)
            except spotify.SpotifyError as e:
                reply = str(e)
            except Exception as e:  # network hiccups etc. must not kill the voice loop
                log.exception("spotify")
                reply = f"Spotify had a problem: {e}"
            if reply is not None:
                if reply:
                    self._reply(reply)
                return True
        if n in ("pause", "play", "pause music", "play music", "resume", "resume music"):
            subprocess.run(["playerctl", "play-pause"], stderr=subprocess.DEVNULL)
            return True
        if n in ("next", "next song", "next track", "skip"):
            subprocess.run(["playerctl", "next"], stderr=subprocess.DEVNULL)
            return True
        return False

    # --- voice loop -------------------------------------------------------
    def run_voice(self) -> None:
        from batsy.voice.ears import Ears, strip_wake_word
        from batsy.voice.stt import Transcriber

        self.ears = Ears(self.cfg)
        self.stt = Transcriber(self.cfg, self.ears.model)
        ControlServer(self).start()
        self.ears.start()
        self.speaker.say("Batsy online.")
        log.info("listening for wake word")

        pending_text: str | None = None
        follow_up = False
        while True:
            if pending_text is not None:
                text, pending_text = pending_text, None
            elif follow_up:
                follow_up = False
                self.ears.drain()
                wait = self.routines.follow_up_seconds or self.cfg.get("voice.follow_up_seconds")
                audio = self._record(wait, self.ears.preroll(0.3), fresh=False)
                if audio is None:
                    self.overlay.done()
                    continue
                text = self.stt.transcribe(audio)
                ignore = NOISE_TRANSCRIPTS if self.routines.capturing else END_FOLLOWUP | NOISE_TRANSCRIPTS
                if _norm(text) in ignore:
                    self.overlay.done()
                    continue
                self.overlay.heard(text)
            else:
                frame = self.ears.read(0.5)
                if frame is None or not self.ears.wake_hit(frame):
                    continue
                text = self._listen_after_wake(strip_wake_word)
                if text is None:
                    continue
                if not text:
                    self._reply("Yes?")
                    self.speaker.wait_idle()
                    follow_up = True
                    continue

            if self.local_command(text):
                self.speaker.wait_idle()
                self.overlay.done()
                follow_up = (self.cfg.get("voice.follow_up_seconds") > 0 or self.routines.capturing) \
                    and _norm(text) not in STOP_WORDS
                continue
            pending_text, aborted = self._voice_turn(text, strip_wake_word)
            follow_up = pending_text is None and not aborted and self.cfg.get("voice.follow_up_seconds") > 0

    def _record(self, start_timeout: float, preroll: list[bytes], fresh: bool):
        """Record an utterance while animating the overlay: orb follows your voice, words appear live.
        fresh=True (after the wake word) opens the overlay at once; follow-ups open it only once you speak."""
        live = self.ears.live_recognizer() if self.overlay.enabled and self.cfg.get("overlay.live_words") else None
        state = {"shown": False, "partial": "", "n": 0}
        if fresh:
            self.saver_on = False  # the wake word takes over the screen from the screensaver
            self.overlay_dismissed = False
            self.overlay.listen(fresh=True)
            state["shown"] = True

        def on_frame(frame, level, speaking):
            if not self.overlay.enabled:
                return
            if not state["shown"]:
                if not speaking or self.overlay_dismissed:
                    return
                self.overlay.listen()
                state["shown"] = True
            state["n"] += 1
            if state["n"] % 2 == 0:  # ~10 updates/s is plenty
                self.overlay.level(min(1.0, level))
            if live is not None:
                live.AcceptWaveform(frame)
                text = json.loads(live.PartialResult()).get("partial", "")
                if text and text != state["partial"]:
                    state["partial"] = text
                    self.overlay.partial(text)

        return self.ears.record_utterance(start_timeout=start_timeout, preroll=preroll, on_frame=on_frame)

    def _listen_after_wake(self, strip_wake_word) -> str | None:
        """Record the command that follows the wake word. None = false alarm, '' = wake word only."""
        self.speaker.stop()
        self.speaker.chime()
        preroll = self.ears.preroll(2.0)
        audio = self._record(5, preroll, fresh=True)
        if audio is None:
            # they said just the wake word and waited; check the pre-roll really contained it
            import numpy as np

            audio = np.frombuffer(b"".join(preroll), dtype=np.int16)
        text = self.stt.transcribe(audio)
        found, rest = strip_wake_word(text, self.cfg.get("voice.wake_words"))
        if _norm(rest) in NOISE_TRANSCRIPTS:
            rest = ""
        if not found and self.stt.engine == "whisper":
            log.info("wake word not confirmed by whisper (%r); ignoring", text)
            self.overlay.hide()
            return None
        if rest.strip():
            self.overlay.heard(rest.strip())
        return rest.strip()

    def _voice_turn(self, text: str, strip_wake_word) -> tuple[str | None, bool]:
        """Run a turn while still listening for barge-in. Returns (interrupting command or None, aborted)."""
        abort = threading.Event()
        done = threading.Event()

        def say(sentence: str) -> None:
            if not abort.is_set():
                self.speaker.say(sentence)
                self.overlay.say(sentence)

        def work():
            try:
                self.run_turn(text, "voice", say, abort, self._confirm_voice)
            except Exception:
                log.exception("turn failed")
                self.speaker.say("Something went wrong on my side. It's in the log.")
            finally:
                done.set()

        self.overlay.think()
        threading.Thread(target=work, daemon=True, name="turn").start()
        self.ears.drain()
        barge_in = self.cfg.get("voice.barge_in")
        while not (done.is_set() and self.speaker.idle()):
            if self.confirm_request:
                self._serve_voice_confirm()
                self.ears.drain()
                continue
            frame = self.ears.read(0.1)
            if frame is None or not barge_in:
                continue
            if self.ears.wake_hit(frame, strict=not self.speaker.idle()):
                new_text = self._listen_after_wake(strip_wake_word)
                if new_text is None:
                    continue
                abort.set()
                self.speaker.stop()
                done.wait()
                if not new_text or self.local_command(new_text):
                    return None, True
                return new_text, True
        self.overlay.done()
        return None, abort.is_set()


class ControlServer(threading.Thread):
    """Unix socket so `batsy ask` / `batsy chat` can type to the running voice daemon.

    Protocol: one JSON line in {"text", "speak"}; JSON lines out: {"say"}, {"confirm"}, {"done"}.
    """

    def __init__(self, app: Batsy):
        super().__init__(daemon=True, name="control")
        self.app = app

    def run(self) -> None:
        SOCKET_PATH.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(4)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rw") as f:
            try:
                req = json.loads(f.readline())
            except ValueError:
                return

            def send(obj):
                f.write(json.dumps(obj) + "\n")
                f.flush()

            def say(text):
                send({"say": text})
                if req.get("speak"):
                    self.app.speaker.say(text)

            def confirm(question):
                send({"confirm": question})
                return safety.is_yes(f.readline())

            text = req.get("text", "").strip()
            try:
                if text and not self.app.local_command(text):
                    self.app.run_turn(text, "typed", say, threading.Event(), confirm)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:
                log.exception("socket turn failed")
                send({"say": f"Error: {e}"})
            send({"done": True})


def ask_daemon(text: str, speak: bool = False) -> bool:
    """Send text to a running daemon and print its reply. Returns False if no daemon is running."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(SOCKET_PATH))
    except OSError:
        return False
    with s, s.makefile("rw") as f:
        f.write(json.dumps({"text": text, "speak": speak}) + "\n")
        f.flush()
        for line in f:
            msg = json.loads(line)
            if "say" in msg:
                print(f"Batsy: {msg['say']}")
            elif "confirm" in msg:
                ans = input(f"[Batsy asks] {msg['confirm']} (yes/no) ")
                f.write(ans + "\n")
                f.flush()
            elif msg.get("done"):
                break
    return True
