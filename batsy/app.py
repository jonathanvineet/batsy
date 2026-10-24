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
from pathlib import Path

from batsy import safety, spotify
from batsy import images
from batsy.overlay import Overlay
from batsy.routines import Routines
from batsy import apps, calc, instant, lifecycle, model3d, system
from batsy.learner import Automator, Learner
from batsy.learning import ACTION, NEGATIVE, SkillBook, claude_cli
from batsy.vocab import get_vocab
from batsy.config import SOCKET_PATH
from batsy.tools import ToolContext, build_registry
from batsy.tools import monitor, music
from batsy.voice.tts import Speaker
from batsy.voice.voiceprint import VoicePrint

log = logging.getLogger("batsy")

FILLERS = ["On it.", "One sec.", "Working on it.", "Give me a moment."]
STOP_WORDS = {"stop", "cancel", "never mind", "nevermind", "shut up", "be quiet", "quiet", "forget it"}
RESET_PHRASES = {"new conversation", "start over", "fresh start", "clear context", "reset conversation"}
# Whisper sometimes "hears" these in silence or background noise; never spend tokens on them.
NOISE_TRANSCRIPTS = {"you", "thank you for watching", "bye", "uh", "um", "hmm", "so", "what the", "mm hmm", "mhm", ""}
END_FOLLOWUP = {"thanks", "thank you", "that's all", "thats all", "nothing", "no", "no thanks", "that's it", "ok", "okay"}


# "Look at this" with nothing else: put the camera on screen and wait for the question.
ATTENTION = re.compile(
    r"^(?:(?:hey|ok|okay|so|now|please|can you|could you|just) )*"
    r"(?:look|see|check|watch|have a look|take a look|i want to show you|let me show you|can you see|do you see)"
    r"(?: (?:at|out))?(?: (?:this|that|here|these|those|it|me|something|this one|this thing))?(?: out)?$")
# Without the wake word, only something shaped like a question or request is for Batsy.
FOR_BATSY = re.compile(
    r"^(?:and |also |then |ok |okay |so |now |please |hey )*(?:what|whats|what's|who|whose|where|when|why|how|which|"
    r"is|are|was|were|can|could|would|will|do|does|did|should|shall|tell|show|open|close|play|pause|stop|skip|set|"
    r"turn|put|give|find|search|read|make|add|remind|take|look|check|start|run|send|call|explain|describe|translate|"
    r"calculate|define|spell|let's|lets|i want|i need|let me)\b")
CLOSE_CAMERA = re.compile(r"\b(close|turn off|stop|hide|shut|exit)\b.*\b(camera|webcam|looking)\b|^stop looking$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", "", text.lower())).strip()


class Batsy:
    def __init__(self, cfg, voice: bool = True, speak: bool = True):
        self.cfg = cfg
        self.voice = voice
        self.speaker = Speaker(cfg, enabled=speak)
        self.ctx = ToolContext(cfg, announce=self.announce, confirm=self._confirm)
        self.registry = build_registry(self.ctx)
        if cfg.get("brain.engine") == "claude":  # optional: Claude API with your own key (pay per use)
            from batsy.brain import Brain

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
            from batsy.camera_view import FRAME_PATH
            from batsy.tools import screen

            screen.on_camera = lambda on: self.overlay.send("camera", on=on, path=str(FRAME_PATH))
        self.brain.visual = self.show_visual
        if self.overlay.enabled:
            self.brain.status = lambda text: self.overlay.send("status", text=text)
        self.routines = Routines(self)
        from batsy import claude_code
        from batsy.tasks import TaskTalk

        claude_code.CFG, claude_code.announce = cfg, self.announce
        self.listen_requested = threading.Event()  # Batsy asked something: listen without the wake word
        self.tasks = TaskTalk(self)
        from batsy.projects import ProjectTalk

        self.projects = ProjectTalk(self)  # "I'm doing a project on...", updates from GitHub/the laptop, finding files
        self.last_file = None  # the file last made, opened or received: what "send this to my Mac" means
        from batsy.inbox import Inbox

        self.inbox = Inbox(self)  # files sent over Tailscale land in ~/Batsy/inbox; "send it to my Mac"
        from batsy.shopping import ShopTalk

        self.shop = ShopTalk(self)  # "how much is X", "compare prices for X", "find me a TV under 50k"
        self.vocab = get_vocab()
        self.skills = SkillBook(claude_cli, show=lambda q: self.show_visual("show", q))
        self.learner = Learner()  # logs every request, remembers answers, turns repeats into code
        self.automator = Automator(self, self.learner)
        self.last_turn = 0.0
        self.last_skill: dict | None = None  # the skill that just ran, awaiting feedback
        if hasattr(self.brain, "skills"):
            self.brain.skills = self.skills
        threading.Thread(target=apps.INDEX.refresh, daemon=True, name="app-index").start()
        model3d.VIEWER.on_event = self._viewer_event
        if self.overlay.enabled:  # the 3D viewer lives inside the overlay: one process, controlled by voice
            model3d.VIEWER.host = self._host_3d
            self.overlay.on_viewer_event = self._viewer_event
            self.overlay.on_dock_open = self._dock_clicked
            self.overlay.on_stop = self.end_conversation  # "~" pressed in the overlay
            self.speaker.on_idle = lambda: self.overlay.send("quiet")  # stopped talking: stop showing "Speaking"
        self._turn_markers: list[tuple[str, str]] = []
        self.saver_on = False
        self.saver_preview_until = 0.0
        self._saver_dirty = False
        self._saver_wake = threading.Event()
        self.overlay.on_saver_exit = self._saver_exited
        if self.overlay.enabled and cfg.get("overlay.screensaver_minutes") > 0:
            threading.Thread(target=self._saver_loop, daemon=True, name="screensaver").start()
        self.last_raw_text = ""
        monitor.start_background(self.ctx)

    def _host_3d(self) -> bool:
        """Start the 3D viewer inside the overlay; False if this overlay can't (then it opens its own window)."""
        if not self.overlay.hosts_3d:
            return False
        self.overlay.send("model3d")
        return True

    def _saver_exited(self) -> None:
        self.saver_on = False
        self.saver_preview_until = 0.0

    def _saver_loop(self) -> None:
        """Show the screensaver dashboard while you're away and Batsy has nothing to do (or on request)."""
        from batsy import idle, saver

        after_ms = self.cfg.get("overlay.screensaver_minutes") * 60_000
        last_refresh = 0.0
        while True:
            self._saver_wake.wait(5)
            self._saver_wake.clear()
            previewing = time.time() < self.saver_preview_until
            away = idle.idle_ms()
            if away is None and not previewing:
                continue
            busy = self.turn_lock.locked() or not self.speaker.idle() or \
                time.time() - self.overlay.last_active < self.cfg.get("overlay.linger_media_seconds") + 5
            if self.saver_on and not previewing and away is not None and away < 3000:
                self.saver_on = False
                self.overlay.hide()
            elif previewing or (not busy and away >= after_ms):
                if not self.saver_on or time.time() - last_refresh > 20 or self._saver_dirty:
                    try:
                        self.overlay.saver_spec(saver.payload(self))
                    except Exception:
                        log.exception("screensaver payload")
                    self.saver_on, last_refresh, self._saver_dirty = True, time.time(), False

    def preview_saver(self, seconds: int = 120) -> None:
        """Show the screensaver now (it closes on any key or mouse movement)."""
        self.saver_preview_until = time.time() + seconds
        self._saver_dirty = True
        self._saver_wake.set()

    def refresh_saver(self) -> None:
        self._saver_dirty = True
        if self.saver_on or time.time() < self.saver_preview_until:
            self._saver_wake.set()
        else:
            self.preview_saver(30)  # show what changed

    def _dock_clicked(self) -> None:
        """The standby bat was clicked: open up and listen, as if you'd said "Batsy"."""
        self.overlay.listen(fresh=True)
        self.listen_requested.set()

    def _close_thing(self, n: str) -> bool:
        """"Close it / that / the file / the PDF / the video / the model / what you opened"."""
        if not re.fullmatch(r"(?:please )?(?:close|shut|hide|get rid of)(?: up)? (?:it|that|this|(?:the|my|that|this) "
                            r"(?:file|pdf|document|doc|video|movie|picture|photo|image|model|3d model|3d|viewer|window|"
                            r"app)|what you (?:just )?opened|the thing you opened)(?: please| now)?", n):
            return False
        self._route = "command"
        if model3d.VIEWER.is_open() and not re.search(r"\b(pdf|video|movie|document|doc|app|window)\b", n):
            try:
                model3d.VIEWER.send({"cmd": "close"})
            except OSError:
                pass
            self.overlay.hide()
            self._reply("Closed it.")
            return True
        opened = getattr(self, "_opened", [])
        for path in reversed(opened):  # the most recent thing Batsy opened that's still open in some app
            if not path.startswith("/"):
                continue
            p = subprocess.run(["pkill", "-f", "--", path], capture_output=True)
            if p.returncode == 0:
                self._opened.remove(path)
                self._reply(f"Closed {os.path.basename(path)}.")
                return True
        if self.overlay.has_media:  # a picture or card on Batsy's screen
            self.overlay.hide()
            self._reply("Closed it.")
            return True
        self._reply("I don't have anything open to close.")
        return True

    def end_conversation(self) -> None:
        """"~" in the overlay, "stop", or `batsy stop`: stop talking, cancel what's being worked on, don't listen
        for a follow-up, and close the screen."""
        self.conversation_ended = time.time()
        abort = getattr(self, "_turn_abort", None)
        if abort is not None:
            abort.set()
        from batsy import claude_code

        claude_code.cancel()  # stop the Claude Code run itself, not just its voice
        self.overlay.muted = True
        self.listen_requested.clear()
        self.speaker.stop()
        self._close_camera()
        self.routines.capture = None
        self.overlay.hide()

    def _overlay_dismissed(self, user: bool = False) -> None:
        """The screen closed. You closing it (Esc, click) means you're done: the request is cancelled too, or it
        would reopen the screen and start talking when its answer arrived. Timing out after an answer is not that."""
        self.overlay_dismissed = True
        if user:
            self.end_conversation()
            return
        self.speaker.stop()
        self._close_camera()

    @staticmethod
    def _close_camera() -> None:
        from batsy.tools import screen

        if screen.camera_pid():
            screen.close_camera()

    # --- learned skills ---------------------------------------------------
    def _confirm_now(self, question: str) -> bool:
        """Yes/no from whichever context we're in (voice main loop, turn thread, or console)."""
        if self.ears and threading.current_thread() is threading.main_thread():
            self.speaker.say(question + " Say yes or no.")
            self.speaker.wait_idle()
            self.ears.drain()
            audio = self.ears.record_utterance(start_timeout=8)
            ok = safety.is_yes(self.stt.transcribe(audio) if audio is not None else "")
            if not ok:
                self.speaker.say("Okay, I won't.")
            return ok
        return self._confirm(question)

    def _run_skill(self, name: str, slots: dict, request: str) -> None:
        from batsy.learning import restore_case

        self._route = "skill"
        slots = restore_case(slots, request)  # matching is lowercase; skills get "Jonathan", not "jonathan"
        self.overlay.send("status", text=f"Running the {name.replace('_', ' ')} skill")
        spoken, ok = self.skills.run(name, slots, self._confirm_now)
        if ok:
            if spoken:
                self._reply(spoken)
            self.last_skill = {"name": name, "request": request, "t": time.time()}
            self.brain.memory.add_exchange("skill", request, spoken, [f"ran learned skill {name}"])
            return
        log.warning("skill %s failed: %s", name, spoken)
        self.skills.record(name, False)
        self._reply("That didn't work. Let me fix it.")
        self._after_learning(self.skills.relearn(name, request, f"error: {spoken}", self._confirm_now), request)

    def _skill_feedback(self, n: str, text: str) -> bool:
        """A complaint right after a learned skill ran = failure + relearn. Anything else = success."""
        last, self.last_skill = self.last_skill, None
        if not last:
            return False
        if time.time() - last["t"] < 90 and NEGATIVE.match(n) and not self.routines.capturing:
            self.skills.record(last["name"], False)
            self._reply("Sorry. Let me relearn that.")
            out = self.skills.relearn(last["name"], last["request"], text, self._confirm_now)
            self._after_learning(out, last["request"])
            return True
        self.skills.record(last["name"], True)
        return False

    def _after_learning(self, out: dict, request: str) -> None:
        if out["kind"] == "answer":
            self._reply(out["text"])
            return
        if out.get("spoken"):
            self._reply(out["spoken"])
        if out.get("ok") and out.get("name"):
            self.last_skill = {"name": out["name"], "request": request, "t": time.time()}

    def show_visual(self, kind: str, arg: str) -> str | None:
        """Put a picture ([[show: query]]) or a text card ([[card: Title | text]]) on screen.
        Returns the picture's caption, or None if nothing was found."""
        self._turn_markers.append((kind, arg))
        if kind == "open":
            return self.open_result(arg)
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
            self._open(str(path))
        return caption

    def _tool(self, argv: list[str]) -> None:
        """Run a desktop helper (wpctl, playerctl...); say so instead of crashing if it isn't installed."""
        try:
            subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except FileNotFoundError:
            self._reply(f"I need {argv[0]} for that. Run batsy doctor to see what's missing.")

    def _open(self, path) -> None:
        """Open a file or URL in its default app (and remember it, for "close it")."""
        self._opened = [p for p in getattr(self, "_opened", []) if p != str(path)][-9:] + [str(path)]
        for opener in (["xdg-open"], ["gio", "open"]):
            try:
                subprocess.Popen([*opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except FileNotFoundError:
                continue
        log.warning("no xdg-open/gio to open %s", path)

    def ask_user(self, question: str) -> None:
        """Ask something out of the blue and listen for the answer without needing the wake word."""
        self.last_said = question
        self.speaker.say(question)
        self.overlay.say(question, force=True)  # worth reopening the screen for, even after you closed it
        if self.overlay.enabled:
            self.overlay.done()
        self.listen_requested.set()
        if not self.voice:
            print(f"\n[Batsy asks] {question}")

    def _claude_budget_command(self, n: str) -> bool:
        from batsy import claude_code

        m = re.fullmatch(r"set (?:my |the )?(weekly|week|session) (?:claude )?(?:limit|budget) to (\d+(?:\.\d+)?) ?"
                         r"(million|m|thousand|k)?(?: tokens)?", n)
        if m:
            mult = {"million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}.get(m.group(3) or "", 1)
            which = "weekly" if m.group(1).startswith("week") else "session"
            claude_code.set_limit(which, int(float(m.group(2)) * mult))
            self._reply(f"Your {which} Claude budget is now {float(m.group(2)):g} {m.group(3) or ''} tokens.".replace("  ", " "))
            return True
        if re.fullmatch(r"(how much )?claude (usage|budget|left)|how much claude (have i used|is left|do i have left)|"
                        r"how much of my claude (budget|limit) (have i used|is left)", n):
            b = claude_code.budget(self.cfg)
            parts = []
            for key in ("session", "week"):
                x = b[key]
                if x["limit"]:
                    parts.append(f"{x['pct']:.0f} percent of this {key}'s budget")
            self._reply("You've used " + " and ".join(parts) + "." if parts else "No Claude budget is set.")
            return True
        return False

    def _viewer_event(self, ev: dict) -> None:
        if ev.get("event") == "measured":
            self._reply(f"{ev['mm']:.1f} millimetres.")

    def open_result(self, path_str: str) -> str | None:
        """A file Batsy made (marked-up PDF, edited image...): open it and preview it on screen."""
        from pathlib import Path

        from batsy import docs

        path = Path(path_str.strip().strip('"').strip("'")).expanduser()
        if not path.exists():
            log.warning("result file missing: %s", path)
            return None
        self.last_file = path
        if path.suffix.lower() in model3d.MODEL_EXTS:  # a model Claude made or edited: show it in the viewer
            model3d.VIEWER.open(path)
            return path.name
        self._open(str(path))
        if self.overlay.enabled and path.suffix.lower() in docs.PDF | docs.IMAGES:
            try:
                self.overlay.image(docs.render(path, 1) if path.suffix.lower() in docs.PDF else path, path.name)
            except Exception as e:
                log.warning("preview failed: %s", e)
        return path.name

    def _better_picture(self, path, caption: str) -> None:
        """Claude looked at the candidates and preferred another one: swap it in."""
        log.info("Claude picked a better picture: %s", caption)
        if self.overlay.enabled:
            self.overlay.image(path, caption)
        else:
            self._open(str(path))

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
                self._open(d["image"])
        if speak:
            self._reply(d["spoken"])
        return d["title"]

    def speak_brief(self, prompt: str) -> None:
        """Run a short Claude request from a routine and speak the answer (costs tokens)."""
        self.run_turn(prompt, "routine", self._reply, threading.Event(), self._confirm_console)

    def _reply(self, text: str) -> None:
        """Speak and caption a locally generated reply."""
        self.last_said = text
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
        abort = getattr(self, "_turn_abort", None)
        if abort is not None and abort.is_set():
            return False  # the request was cancelled: never ask (or do) anything more for it
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
            if not spoke and self.speaker.idle() and source == "voice" and not abort.is_set():
                self.speaker.say(random.choice(FILLERS))
            spoke.append(name)

        n, t0 = _norm(text), time.time()
        self.last_turn = t0
        known = self.learner.recall(n)  # asked before and answered by a model: instant
        if known:
            for sentence in re.split(r"(?<=[.!?])\s+", known):
                say(sentence)
            self.learner.record(text, n, "cache", time.time() - t0, known)
            self.brain.memory.add_exchange(source, text, known, ["answered from memory"])
            return known
        with self.turn_lock:
            self.confirm_handler = confirm
            self._turn_markers = []
            self.brain.route, self.brain.last_actions = "full", []
            try:
                reply = self.brain.ask(text, source, say, abort, on_tool)
            finally:
                self.confirm_handler = self._confirm_console
            route = getattr(self.brain, "route", "full")
            secs = time.time() - t0
            self.learner.record(text, n, route, secs, reply)
            if route in ("quick", "full", "claude") and secs > 6 and not abort.is_set():
                self.automator.learn_soon(text, n)  # slow once is enough: have Claude make it instant
            plain = not [a for a in getattr(self.brain, "last_actions", []) if a != "answered by Claude"]
            if route in ("quick", "claude") and plain and not abort.is_set():
                self.learner.remember(text, n, reply)
            self._learn_phrase(text)
            learned = getattr(self.brain, "last_skill_run", None)
            if learned:
                self.last_skill = {"name": learned, "request": text, "t": time.time()}
                self.brain.last_skill_run = None
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

    def restart(self, reason: str = "restart") -> None:
        """Replace this process with a fresh Batsy. The new run reads the reason (same PID, so it can't tell
        from the process itself) and says it's back."""
        log.info("restarting (%s)", reason)
        os.environ["BATSY_RESTART_REASON"] = reason
        if self.ears:
            self.ears.close()
        SOCKET_PATH.unlink(missing_ok=True)
        os.execv(sys.executable, [sys.executable, "-m", "batsy", *sys.argv[1:]])

    def local_command(self, text: str) -> bool:
        """Handle common phrases instantly, without any AI. Returns True if handled (and logs how)."""
        t0, self._route = time.time(), "command"
        handled = self._local_command(text)
        if handled:
            self.last_turn = time.time()
            self.learner.record(text, _norm(text), self._route, time.time() - t0)
        return handled

    def _local_command(self, text: str) -> bool:
        from batsy.tools import screen

        n = _norm(text)
        self.last_raw_text = text.strip()
        if n in STOP_WORDS:
            self.end_conversation()
            return True
        if self._self_command(n):
            return True
        said = system.handle(self, n)  # terminal, system update, restart / shut down: built in, never improvised
        if said is not None:
            self._route = "command"
            if said:
                self._reply(said)
            return True
        sp = music.get_spotify(self.cfg)
        if sp.ready() and (sp.pending or sp.last):  # Batsy just asked "Is it X, or Y?", or just played something
            try:
                said = sp.choose(n)  # "yes", "the second one", "no", "not that one"
            except spotify.SpotifyError as e:
                said = str(e)
            if said is not None:
                self._route = "command"
                self._reply(said)
                return True
        if self._claude_budget_command(n):
            return True
        if self._voiceprint_command(n) or self._wake_training(n):
            return True
        said = calc.answer(text) or instant.answer(self, n, text)  # maths, time, weather, small talk...: no AI
        if said:
            self._route = "instant"
            self._reply(said)
            return True
        if self._learner_command(n):
            return True
        if self._close_thing(n):
            return True
        if CLOSE_CAMERA.search(n):
            self._close_camera()
            self._reply("Okay.")
            return True
        if ATTENTION.match(n):
            said = screen.open_camera(self.cfg)
            self._reply("Show me." if said.endswith("open.") else said)
            return True
        from batsy import saver

        said = saver.handle(n, self)  # "add the weather to the screensaver", "make it minimal"... (no AI)
        if said is not None:
            if said:
                self._reply(said)
            return True
        try:
            if self.inbox.handle(n, text):  # "send this to my Mac", "what's in my inbox"
                self._route = "command"
                return True
        except Exception:
            log.exception("inbox")
        try:
            if self.shop.handle(n, text):  # prices across Amazon, Flipkart, Robu, Croma...
                self._route = "command"
                return True
        except Exception:
            log.exception("shopping")
            self._reply("Something went wrong checking prices. It's in the log.")
            return True
        try:
            if self.projects.handle(n, text):  # "I'm doing a project on...", "update on my robot project", "find my notes"
                self._route = "command"
                return True
        except Exception:
            log.exception("projects")
        if self.tasks.handle(n, text):  # chores/projects: check-in answers, "I do laundry every Sunday"...
            return True
        if self._skill_feedback(n, text):
            return True
        if self.routines.handle(n):
            return True
        try:
            said = model3d.handle(n)  # 3D viewer: open models, rotate, views, dimensions... (no AI)
        except Exception as e:
            log.exception("3d viewer")
            said = f"The 3D viewer had a problem: {e}"
        if said is not None:
            if said:
                self._reply(said)
            return True
        m = re.match(r"(?:(?:please|can you|could you) )?(open|launch|start|run) (.+)", n)
        if m and not re.search(r"\b(camera|webcam)\b", m.group(2)):
            said = apps.open_target(m.group(2))
            if said:
                self._reply(said)
                return True
        m = re.match(r"(?:(?:please|can you|could you) )?(close|quit|exit|kill) (.+)", n)
        if m and not re.search(r"\b(camera|webcam)\b", m.group(2)):
            said = apps.close_app(m.group(2))
            if said:
                self._reply(said)
                return True
        m = re.match(r"forget how to (.+)", n)
        if m:
            hit = self.skills.match(m.group(1))
            if hit and self.skills.forget(hit[0]):
                self._reply("Forgotten. I'll relearn it if you ask again.")
            else:
                self._reply("I don't have a learned skill for that.")
            return True
        hit = self.skills.match(n)  # something Batsy learned before: run its code, no AI
        if hit:
            self._run_skill(hit[0], hit[1], text)
            return True
        if re.match(r"(?:hey |ok )?ask (?:claude|cloud|clawed|claud)\b", text.strip(" .!?"), re.I):
            return False  # a full turn (status on screen, barge-in, voice confirmations), not a local command
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
            self._tool(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{min(int(m.group(4)), 100)}%"])
            return True
        if n in ("mute", "unmute", "toggle mute"):
            self._tool(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
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
            self._tool(["playerctl", "play-pause"])
            return True
        if n in ("next", "next song", "next track", "skip"):
            self._tool(["playerctl", "next"])
            return True
        return False

    # --- voice loop -------------------------------------------------------
    def run_voice(self) -> None:
        from batsy.voice.ears import Ears, strip_wake_word
        from batsy.voice.stt import Transcriber

        self.ears = Ears(self.cfg)
        self.stt = Transcriber(self.cfg, self.ears.model)
        self.voiceprint = VoicePrint(self.cfg)
        ControlServer(self).start()
        self.ears.start()
        self.speaker.say(lifecycle.started())  # "Batsy online." or "I'm back. Updated: 23 files changed."
        log.info("listening for wake word")
        self.automator.start()  # in the background: turn repeated model requests into instant code

        pending_text: str | None = None
        follow_up = False
        wake_only = False  # heard just "Batsy": keep listening for the command, without talking over you
        errors: list[float] = []
        while True:
            try:
                if self.listen_requested.is_set():  # Batsy just asked a question: hear the answer, no wake word
                    self.listen_requested.clear()
                    self.speaker.wait_idle()
                    follow_up = True
                if pending_text is not None:
                    text, pending_text = pending_text, None
                elif follow_up and time.time() - getattr(self, "conversation_ended", 0) < 5:
                    follow_up = False  # you just ended the conversation: back to waiting for "Batsy"
                    continue
                elif follow_up:
                    follow_up = False
                    from batsy.tools import screen

                    if wake_only:  # keep what you already started saying: no drain, nothing spoken
                        wait = 8
                    else:
                        self.ears.drain()
                        wait = self.routines.follow_up_seconds or (15 if self.tasks.store.pending else None) or \
                            (30 if screen.camera_pid() or model3d.VIEWER.active() else None) or \
                            self.cfg.get("voice.follow_up_seconds")  # live camera / 3D model: keep talking to it
                    was_wake_only, wake_only = wake_only, False
                    audio = self._record(wait, self.ears.preroll(0.3), fresh=False,
                                         min_speech=0.6 if was_wake_only else 0.0)
                    if audio is None or not self.voiceprint.is_owner(audio):
                        self.overlay.done()
                        continue
                    self.overlay.think()  # you've stopped talking: the screen shows it's working straight away
                    self.overlay.send("status", text="Transcribing")
                    text = self.stt.transcribe(audio, live=self._live)
                    if was_wake_only:  # "Batsy ... Batsy, open YouTube"
                        text = strip_wake_word(text, self.cfg.get("voice.wake_words"))[1] or text
                    awaiting = self.routines.capturing or self.last_skill or self.tasks.store.pending or \
                        self.projects.pending  # "no" is an answer
                    # Without the wake word, a short "mm-hmm" / "they got this" is you talking to someone else,
                    # unless Batsy just asked you something ("Is it X, or Y?" -> "no" is an answer, not goodbye).
                    asked = was_wake_only or getattr(self, "last_said", "").rstrip().endswith("?")
                    ignore = NOISE_TRANSCRIPTS if awaiting or asked else END_FOLLOWUP | NOISE_TRANSCRIPTS
                    for_me = text.rstrip().endswith("?") or FOR_BATSY.match(_norm(text)) or \
                        (model3d.VIEWER.active() and model3d.parse(_norm(text)) is not None)  # "rotate it", "top view"
                    if _norm(text) in ignore or not (awaiting or asked or for_me):
                        log.info("follow-up ignored: %r", text)
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
                    if not text:  # just "Batsy": the chime and the screen already say "listening"
                        self.overlay.listen()
                        follow_up = wake_only = True
                        continue

                if self.local_command(text):
                    self.speaker.wait_idle()
                    self.overlay.done()
                    follow_up = (self.cfg.get("voice.follow_up_seconds") > 0 or self.routines.capturing) \
                        and _norm(text) not in STOP_WORDS
                    continue
                pending_text, aborted = self._voice_turn(text, strip_wake_word)
                if pending_text == "":  # interrupted with just "Batsy": listen for the new command
                    pending_text, follow_up, wake_only = None, True, True
                    self.overlay.listen()
                    continue
                follow_up = pending_text is None and not aborted and self.cfg.get("voice.follow_up_seconds") > 0
            except Exception:  # one bad turn must never stop Batsy listening
                log.exception("voice loop")
                pending_text, follow_up, wake_only = None, False, False
                self.overlay.done()
                now = time.time()
                errors = [t for t in errors if now - t < 60] + [now]
                if len(errors) == 1:
                    self.speaker.say("Sorry, that went wrong. I'm still listening.")
                if len(errors) >= 5:  # something keeps failing: maybe the microphone; reopen it
                    self.ears.reopen()
                    errors.clear()
                time.sleep(0.2)


    def _self_command(self, n: str) -> bool:
        """Built in, never improvised by a model: "update yourself" (a real git pull) and "restart yourself"."""
        update = re.search(r"\b(git pull|pull (the )?(latest|updates?|new code)|(update|upgrade) (yourself|batsy)|"
                           r"get the latest (code|version))\b", n)
        restart = re.search(r"\b(restart|reboot|reload|relaunch) (yourself|batsy)\b|^restart$", n)
        if not update and not restart:
            return False
        self._route = "command"
        reason = "restart"
        if update:
            repo = Path(__file__).resolve().parent.parent
            self.overlay.send("status", text="Pulling the latest code")
            p = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"], capture_output=True, text=True,
                               timeout=120)
            out = (p.stdout + p.stderr).strip()
            log.info("git pull: %s", out[-500:])
            if p.returncode != 0:
                first = next((ln for ln in out.splitlines() if ln.strip()), "unknown error")
                self._reply(f"The git pull failed: {first[:160]}")
                return True
            if "Already up to date" in out:
                reason = "update: already up to date"
                self._reply("Already up to date." + (" Restarting anyway." if restart else ""))
                if not restart:
                    return True
            else:
                changed = re.search(r"(\d+) files? changed", out)
                self._reply(f"Pulled {changed.group(1) + ' changed files' if changed else 'the update'}. Restarting.")
                reason = f"update: {changed.group(1)} files changed" if changed else "update"
                if "pyproject.toml" in out:  # dependencies may have changed
                    self.overlay.send("status", text="Installing dependencies")
                    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(repo)], timeout=900)
        else:
            self._reply("Restarting.")
        self.speaker.wait_idle(15)
        self.restart(reason)
        return True

    def _learner_command(self, n: str) -> bool:
        """"That's wrong" forgets a remembered answer; "learn from what I ask" automates repeats now;
        "what have you learned" sums it up."""
        if re.fullmatch(r"(no |nope )?(that'?s|thats|that is) (wrong|not right|incorrect)|wrong answer|forget that answer", n) \
                and self.learner.last_answer:
            self.learner.forget_last()
            self._route = "command"
            self._reply("Okay, I've forgotten that answer. Ask me again and I'll work it out fresh.")
            return True
        if re.fullmatch(r"(learn from (what|everything) i (ask|say)|improve yourself|get faster|automate (my|what i) "
                        r"(ask|requests|questions))", n):
            self._reply("Looking at what you ask most. I'll tell you what I manage to make instant.")

            def go():
                learned = self.automator.run_once()
                self._reply(("Done. Now instant: " + "; ".join(t.strip(" .?!") for t in learned) + ".") if learned
                            else "Nothing new to automate yet. Everything you repeat is already instant or needs thinking.")

            threading.Thread(target=go, daemon=True).start()
            return True
        if re.fullmatch(r"what (have you|did you) learn(ed|t)?( so far)?|how fast (have you been|are you)|"
                        r"(show|tell me) (your|my) (stats|statistics)", n):
            st = self.learner.stats()
            routes = st["routes"]
            fast = sum(c for r, (c, _) in routes.items() if r in ("instant", "command", "skill", "cache"))
            slow = sum(c for r, (c, _) in routes.items() if r in ("quick", "full", "claude", "look", "3d", "doc"))
            self._reply(f"This week, {fast} requests were instant and {slow} needed a model. I remember "
                        f"{st['remembered']} answers and have turned {st['automated']} repeated requests into code.")
            return True
        return False

    def _wake_training(self, n: str) -> bool:
        """"Train my wake word": record you saying "Batsy" a few times, then tune the keyword spotter to you."""
        if not re.fullmatch(r"(train|learn|calibrate|tune)( my| the| your)? wake ?word|learn how i say (your name|batsy)", n):
            return False
        from batsy.voice import wake

        if not wake.available() or self.ears is None:
            self._reply("I don't have the keyword spotting model, so there's nothing to train.")
            return True
        clips = []
        self.speaker.say("After each beep, say Batsy, the way you normally would.")
        for i in range(5):
            self.speaker.wait_idle()
            self.speaker.chime()
            self.ears.drain()
            audio = self.ears.record_utterance(start_timeout=6, max_seconds=4)
            if audio is not None:
                clips.append(audio)
                wake.save_sample(audio, "real", "train")
            if i < 4:
                self.speaker.say(["Again.", "Once more.", "And again.", "Last one."][i])
        if len(clips) < 3:
            self._reply("I didn't hear enough. Say train my wake word to try again.")
            return True
        self._reply("Got it. Tuning myself to your voice, give me a minute.")

        def tune():
            r = wake.tune(clips)
            self.ears.spotter = wake.Spotter()
            self._reply(f"Done. I now catch {r['caught']} of your {r['of']} Batsys"
                        + (f", with {r['false']} false alarms in {r['negatives']} saved noises." if r["negatives"]
                           else "."))

        threading.Thread(target=tune, daemon=True, name="wake-tune").start()
        return True

    def _voiceprint_command(self, n: str) -> bool:
        """"learn my voice" records you so Batsy ignores other voices (music, TV, itself); "forget my voice" undoes it."""
        vp = getattr(self, "voiceprint", None)
        if vp is None or not re.search(r"\b(learn|remember|forget)\b.* my voice\b", n):
            return False
        if not vp.available:
            self._reply("I don't have the voice recognition model yet, so I can't learn your voice.")
            return True
        if "forget" in n:
            vp.forget()
            self._reply("Okay, I'll answer any voice again.")
            return True
        prompts = ["After the beep, tell me what you did today, for a few seconds.",
                   "Now say something you'd usually ask me.",
                   "Last one. Count from one to ten."]
        clips = []
        for prompt in prompts:
            self.speaker.say(prompt)
            self.speaker.wait_idle()
            self.speaker.chime()
            self.ears.drain()
            audio = self.ears.record_utterance(start_timeout=8)
            if audio is not None and audio.size >= 16000 * 2:
                clips.append(audio)
        if len(clips) < 2:
            self._reply("I didn't hear enough to learn your voice. Say learn my voice to try again.")
            return True
        vp.enroll(clips)
        self._reply("Got it. From now on I'll only answer your voice. Say forget my voice to undo that.")
        return True

    def _record(self, start_timeout: float, preroll: list[bytes], fresh: bool, min_speech: float = 0.0):
        """Record an utterance while animating the overlay: orb follows your voice, words appear live.
        fresh=True (after the wake word) opens the overlay at once; follow-ups open it only once you speak."""
        live = self.ears.live_recognizer() if self.overlay.enabled and self.cfg.get("overlay.live_words") else None
        state = {"shown": False, "partial": "", "n": 0}
        # transcribe while you talk: when you stop, only the last stretch is left (stt.LiveTranscript)
        self._live = self.stt.live(preroll) if self.stt.engine == "parakeet" else None
        if fresh:
            self.saver_on = False  # the wake word takes over the screen from the screensaver
            self.overlay_dismissed = False
            self.overlay.listen(fresh=True)
            state["shown"] = True

        def on_frame(frame, level, speaking):
            if self._live is not None:
                self._live.feed(frame, level > 1 / 6)  # level is relative to the speech threshold x6
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

        return self.ears.record_utterance(start_timeout=start_timeout, preroll=preroll, on_frame=on_frame,
                                          min_speech=min_speech)

    def _listen_after_wake(self, strip_wake_word) -> str | None:
        """Record the command that follows the wake word. None = false alarm, '' = wake word only."""
        import numpy as np

        from batsy.voice import wake

        if self.ears.wake_source == "kws":  # the dedicated spotter is precise: answer at once, no check
            preroll = self.ears.preroll(2.0)
        else:
            # Vosk often fires on similar sounds. Check the last couple of seconds with the real recogniser
            # first (~0.3 s), so a false alarm never opens the screen or chimes.
            for _ in range(8):  # 0.4 s more (kept in the pre-roll), so a "Batsy" still being said is complete
                self.ears.read(0.2)
            preroll = self.ears.preroll(2.4)
            clip = np.frombuffer(b"".join(preroll), dtype=np.int16)
            check = self.stt.transcribe(clip, quiet=True)
            real = strip_wake_word(check, self.cfg.get("voice.wake_words"))[0]
            wake.save_sample(clip, "real" if real else "false", "vosk")  # examples for "train my wake word"
            if not real and self.stt.engine != "vosk":
                log.info("false wake (%r): staying hidden", check)
                return None
        self.speaker.stop()
        self.speaker.chime()
        audio = self._record(5, preroll, fresh=True, min_speech=1.0)  # don't stop at the pause after "Batsy"
        if audio is None:
            # they said just the wake word and waited; check the pre-roll really contained it
            audio = np.frombuffer(b"".join(preroll), dtype=np.int16)
        if not self.voiceprint.is_owner(audio):
            self.overlay.hide()
            return None
        self.overlay.think()  # you've stopped talking: the screen shows it's working straight away
        self.overlay.send("status", text="Transcribing")
        text = self.stt.transcribe(audio, live=self._live)  # it checks the audio matches what it heard
        found, rest = strip_wake_word(text, self.cfg.get("voice.wake_words"))
        if not found:
            # The quick check above already heard "Batsy", so the full transcript can't veto it ("Bachy, how
            # are you?"). Drop a first word that's a mangled "Batsy" and keep the rest as the command.
            import difflib

            first, _, after = text.strip().partition(" ")
            if difflib.SequenceMatcher(None, re.sub(r"[^a-z]", "", first.lower()), "batsy").ratio() >= 0.45:
                rest = after
            else:
                rest = text
            log.info("wake word heard earlier; full transcript %r, command %r", text, rest)
        if _norm(rest) in NOISE_TRANSCRIPTS:
            rest = ""
        if rest.strip():
            self.overlay.heard(rest.strip())
        return rest.strip()

    def _voice_turn(self, text: str, strip_wake_word) -> tuple[str | None, bool]:
        """Run a turn while still listening for barge-in. Returns (interrupting command or None, aborted)."""
        abort = threading.Event()
        self._turn_abort = abort  # so end_conversation() can cancel it
        done = threading.Event()

        def say(sentence: str) -> None:
            if not abort.is_set():
                self.last_said = sentence
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
                if not new_text:
                    return "", True
                if self.local_command(new_text):
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

            if "confirm_only" in req:  # batsy.guard: Claude Code wants to run something risky
                yes = False
                try:
                    yes = self.app._confirm(str(req["confirm_only"]))
                finally:
                    send({"yes": bool(yes)})
                return
            text = req.get("text", "").strip()
            if text:
                self.app.overlay.heard(text)  # typed turns are their own exchange on screen too
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
