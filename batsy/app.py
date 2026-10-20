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

from batsy import safety
from batsy.brain import Brain
from batsy.config import SOCKET_PATH
from batsy.tools import ToolContext, build_registry
from batsy.tools import monitor
from batsy.voice.tts import Speaker

log = logging.getLogger("batsy")

FILLERS = ["On it.", "One sec.", "Working on it.", "Give me a moment."]
STOP_WORDS = {"stop", "cancel", "never mind", "nevermind", "shut up", "be quiet", "quiet", "forget it"}
RESET_PHRASES = {"new conversation", "start over", "fresh start", "clear context", "reset conversation"}
END_FOLLOWUP = {"thanks", "thank you", "that's all", "thats all", "nothing", "no", "no thanks", "that's it", "ok", "okay"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z' ]", "", text.lower()).strip()


class Batsy:
    def __init__(self, cfg, voice: bool = True, speak: bool = True):
        self.cfg = cfg
        self.voice = voice
        self.speaker = Speaker(cfg, enabled=speak)
        self.ctx = ToolContext(cfg, announce=self.announce, confirm=self._confirm)
        self.registry = build_registry(self.ctx)
        self.brain = Brain(cfg, self.registry)
        self.turn_lock = threading.Lock()
        self.confirm_handler = self._confirm_console
        self.confirm_request: dict | None = None
        self.ears = None
        self.stt = None
        monitor.start_background(self.ctx)

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
            try:
                reply = self.brain.ask(text, source, say, abort, on_tool)
            finally:
                self.confirm_handler = self._confirm_console
        self._apply_flags()
        return reply

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
        """Handle a few phrases instantly without calling Claude. Returns True if handled."""
        n = _norm(text)
        if n in STOP_WORDS:
            self.speaker.stop()
            return True
        if n in RESET_PHRASES:
            self.brain.reset()
            self.speaker.say("Fresh start. I still remember the long-term stuff.")
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
                audio = self.ears.record_utterance(start_timeout=self.cfg.get("voice.follow_up_seconds"),
                                                   preroll=self.ears.preroll(0.3))
                if audio is None:
                    continue
                text = self.stt.transcribe(audio)
                if not text or _norm(text) in END_FOLLOWUP:
                    continue
            else:
                frame = self.ears.read(0.5)
                if frame is None or not self.ears.wake_hit(frame):
                    continue
                text = self._listen_after_wake(strip_wake_word)
                if text is None:
                    continue
                if not text:
                    self.speaker.say("Yes?")
                    self.speaker.wait_idle()
                    follow_up = True
                    continue

            if self.local_command(text):
                continue
            pending_text, aborted = self._voice_turn(text, strip_wake_word)
            follow_up = pending_text is None and not aborted and self.cfg.get("voice.follow_up_seconds") > 0

    def _listen_after_wake(self, strip_wake_word) -> str | None:
        """Record the command that follows the wake word. None = false alarm, '' = wake word only."""
        self.speaker.stop()
        self.speaker.chime()
        preroll = self.ears.preroll(2.0)
        audio = self.ears.record_utterance(start_timeout=5, preroll=preroll)
        if audio is None:
            # they said just the wake word and waited; check the pre-roll really contained it
            import numpy as np

            audio = np.frombuffer(b"".join(preroll), dtype=np.int16)
        text = self.stt.transcribe(audio)
        found, rest = strip_wake_word(text, self.cfg.get("voice.wake_words"))
        if not found and self.stt.engine == "whisper":
            log.info("wake word not confirmed by whisper (%r); ignoring", text)
            return None
        return rest.strip()

    def _voice_turn(self, text: str, strip_wake_word) -> tuple[str | None, bool]:
        """Run a turn while still listening for barge-in. Returns (interrupting command or None, aborted)."""
        abort = threading.Event()
        done = threading.Event()

        def say(sentence: str) -> None:
            if not abort.is_set():
                self.speaker.say(sentence)

        def work():
            try:
                self.run_turn(text, "voice", say, abort, self._confirm_voice)
            except Exception:
                log.exception("turn failed")
                self.speaker.say("Something went wrong on my side. It's in the log.")
            finally:
                done.set()

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
