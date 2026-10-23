"""Files in and out of this laptop, over Tailscale (Taildrop).

In: send a file from your Mac (right-click > Share > Tailscale, or `tailscale file cp FILE <this laptop>:`) and it
lands in ~/Batsy/inbox. Batsy says what arrived, shows it, reads it into memory (so "summarise what I just sent"
works), and asks whether to open it. Anything else that drops files there (LocalSend, scp) is noticed too.

Out: "send this to my Mac" / "send the marked-up PDF to my Mac" / "send my thermodynamics notes to my iPad" runs
`tailscale file cp`. On a Mac it arrives in Downloads. A folder is zipped first.

Linux keeps received Taildrop files in a holding area until something collects them: Batsy runs
`tailscale file get --loop` for that. Without root it needs one-time permission:
  sudo tailscale set --operator=$USER
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from batsy.config import HOME

log = logging.getLogger("batsy.inbox")

PARTIAL = re.compile(r"(\.partial|\.part|\.crdownload|\.tmp|\.download|~)$|^\.", re.I)
THIS = r"(?:this|it|that|this file|that file|the file|this one|that one|the result|what you made|what you did|" \
       r"the last file|the file you made|the last one)"
SEND = re.compile(r"^(?:(?:can you|could you|please|now|ok|okay)\s+)*(?:send|share|taildrop|transfer|push|move|copy)"
                  r"(?: me)? (?P<what>.+?) (?:to|over to|onto|on) (?:my |the )?(?P<dev>[a-z0-9' -]+?)"
                  r"(?: (?:over|with|using|via) tailscale)?(?: please| now)?$")
INBOX_LIST = re.compile(r"^(?:what(?:'s| is|s) in|show me|list|check) (?:my |the )?inbox$|"
                        r"^(?:what|which) files? (?:did i (?:get|receive)|came in|have i (?:got|received))(?: today)?$|"
                        r"^did (?:i get|anything come|something come|a file come)(?: in)?(?: from my mac)?$")
OPEN_INBOX = re.compile(r"^open (?:my |the )?inbox(?: folder)?$")
OPEN_LAST = re.compile(r"^open (?:the |that )?(?:file|thing|one|pdf|picture|image|model)? ?(?:i|you) (?:just )?"
                       r"(?:sent|got|received|shared)(?: me| you)?$")
DEVICE_WORDS = {"mac": "mac", "macbook": "mac", "mac book": "mac", "laptop": "mac", "other laptop": "mac",
                "computer": "mac", "ipad": "ipad", "phone": "phone", "iphone": "phone", "android": "phone",
                "mobile": "phone"}


def tailscale() -> str | None:
    return shutil.which("tailscale")


def _run(args: list[str], timeout: int = 15) -> tuple[int, str]:
    exe = tailscale()
    if not exe:
        return 127, "Tailscale isn't installed."
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "Tailscale took too long."
    except OSError as e:
        return 1, str(e)


def targets() -> list[str]:
    """Devices Taildrop can send to right now (yours, online)."""
    code, out = _run(["file", "cp", "--targets"])
    if code:
        return []
    return [parts[1] for parts in (ln.split() for ln in out.splitlines()) if len(parts) >= 2]


def check() -> tuple[bool, str]:
    """(ok, what's wrong and how to fix it), for `batsy doctor`."""
    if not tailscale():
        return False, "install Tailscale: sudo dnf install tailscale && sudo systemctl enable --now tailscaled"
    code, out = _run(["status"])
    if code:
        return False, "start it: sudo tailscale up"
    if os.geteuid() != 0:  # collecting received files needs root or being Tailscale's operator
        import getpass
        import json

        code, out = _run(["debug", "prefs"])
        try:
            operator = json.loads(out).get("OperatorUser", "") if code == 0 else ""
        except ValueError:
            operator = ""
        if operator != getpass.getuser():
            return False, "let Batsy collect files: sudo tailscale set --operator=$USER"
    return True, ""


class Inbox:
    """Owned by the app: collects Taildrop files, notices new files in the inbox folder, and sends files out."""

    def __init__(self, app):
        self.app, self.cfg = app, app.cfg
        self.dir = Path(self.cfg.get("files.inbox")).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.last: Path | None = None     # the newest file that arrived
        self.problem = ""                 # why Taildrop collection isn't working, if it isn't
        self._known = {p.name for p in self._files()}  # what's already there isn't news
        self._growing: dict[str, tuple[int, float]] = {}
        self._batch: list[Path] = []
        self._batch_t = 0.0
        if self.cfg.get("files.taildrop") and tailscale():
            threading.Thread(target=self._collect, daemon=True, name="taildrop").start()
        threading.Thread(target=self._watch, daemon=True, name="inbox").start()

    # --- in ------------------------------------------------------------------------------------------------
    def _collect(self) -> None:
        """`tailscale file get --loop`: move each file Taildrop receives into the inbox. Restarts if it stops."""
        delay = 10
        while True:
            t0 = time.time()
            try:
                p = subprocess.Popen([tailscale(), "file", "get", "--loop", "--wait", "--conflict=rename", "--verbose",
                                      str(self.dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in p.stdout:
                    line = line.strip()
                    if line:
                        log.info("taildrop: %s", line)
                    if re.search(r"access denied|permission denied|operator", line, re.I):
                        self.problem = "run: sudo tailscale set --operator=$USER"
                        p.kill()
                p.wait()
            except OSError as e:
                log.warning("taildrop collection unavailable: %s", e)
            if self.problem:
                log.warning("Taildrop files can't be collected: %s", self.problem)
            delay = 10 if time.time() - t0 > 120 else min(300, delay * 2)  # back off while it keeps failing
            time.sleep(delay)

    def _files(self) -> list[Path]:
        try:
            return [p for p in self.dir.iterdir() if p.is_file() and not PARTIAL.search(p.name)]
        except OSError:
            return []

    def _watch(self) -> None:
        """Every couple of seconds: a new file whose size has stopped changing has arrived. Several arriving
        together are announced once."""
        while True:
            time.sleep(2)
            try:
                now = time.time()
                for p in self._files():
                    if p.name in self._known:
                        continue
                    size = p.stat().st_size
                    was = self._growing.get(p.name)
                    if was and was[0] == size and now - was[1] >= 2:
                        self._known.add(p.name)
                        self._growing.pop(p.name, None)
                        self._batch.append(p)
                        self._batch_t = now
                    elif not was or was[0] != size:
                        self._growing[p.name] = (size, now)
                if self._batch and now - self._batch_t >= 3:
                    batch, self._batch = self._batch, []
                    self._arrived(batch)
            except Exception:
                log.exception("inbox watch")

    def _arrived(self, files: list[Path]) -> None:
        from batsy import docs

        log.info("arrived: %s", ", ".join(p.name for p in files))
        self.last = files[-1]
        self.app.last_file = files[0]
        first = files[0]
        brain = self.app.brain
        if first.suffix.lower() in docs.SUPPORTED:  # "summarise what I just sent" / "circle the dates in it"
            brain.last_doc = (first, time.time())
            threading.Thread(target=self._learn, args=(first,), daemon=True).start()
        names = [p.name for p in files]
        if len(files) == 1:
            question = f"{names[0]} just arrived. Want me to open it?"
        else:
            listed = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
            question = f"{len(files)} files just arrived: {listed}. Want me to open the first one?"
        brain.note_announcement(question.split(" Want")[0])  # so the AI knows what you mean by "that file"
        self._wait_quiet()
        if self._user_away():
            self.app.announce(question.split(" Want")[0])  # spoken + a desktop notification; nothing to answer
            return
        if self.app.overlay.enabled and first.suffix.lower() in docs.IMAGES:
            self.app.overlay.image(first, first.name)
        self.app.show_visual("card", "Just arrived | " + "\n".join(
            f"- {p.name} · {_size(p)}" for p in files[:6]))
        self.app.projects.pending = {"kind": "open", "files": [str(p) for p in files], "until": time.time() + 120}
        self.app.ask_user(question)

    def _learn(self, path: Path) -> None:
        from batsy import docs

        try:
            docs.learn(path)
        except Exception as e:
            log.info("couldn't read %s: %s", path.name, e)

    def _wait_quiet(self, limit: float = 120) -> None:
        """Don't talk over a conversation: wait until Batsy is idle (at most `limit` seconds)."""
        t0 = time.time()
        while time.time() - t0 < limit:
            if not self.app.turn_lock.locked() and self.app.speaker.idle():
                return
            time.sleep(1)

    @staticmethod
    def _user_away() -> bool:
        from batsy import idle

        away = idle.idle_ms()
        return away is not None and away > 120_000

    # --- talking ------------------------------------------------------------------------------------------
    def handle(self, n: str, raw: str) -> bool:
        m = SEND.match(n)
        if m and self._looks_like_device(m.group("dev")):
            return self._send(m.group("what"), m.group("dev"))
        if INBOX_LIST.match(n):
            return self._list()
        if OPEN_INBOX.match(n):
            self.app._open(str(self.dir))
            self.app._reply("Opening your inbox.")
            return True
        if OPEN_LAST.match(n):
            if not self.last or not self.last.exists():
                self.app._reply("Nothing has arrived recently.")
            else:
                self.app.open_result(str(self.last))
                self.app._reply(f"Opening {self.last.name}.")
            return True
        return False

    def _looks_like_device(self, dev: str) -> bool:
        dev = dev.strip()
        return dev in DEVICE_WORDS or dev in self._devices() or dev.replace(" ", "-") in targets()

    def _devices(self) -> dict:
        try:
            return {k.lower(): v for k, v in (self.cfg.get("files.devices") or {}).items()}
        except (KeyError, AttributeError):
            return {}

    def _device(self, word: str) -> tuple[str | None, str]:
        """'my mac' -> ('c', 'your Mac'). The Tailscale name, and how to say it."""
        word = word.strip()
        devices = self._devices()
        kind = DEVICE_WORDS.get(word, word)
        name = devices.get(word) or devices.get(kind)
        if not name and word.replace(" ", "-") in targets():
            name = word.replace(" ", "-")
        say = {"mac": "your Mac", "ipad": "your iPad", "phone": "your phone"}.get(kind, word)
        return name, say

    def _pick(self, what: str) -> Path | None:
        what = what.strip()
        if re.fullmatch(THIS, what):
            for p in (getattr(self.app, "last_file", None), self.last):
                if p and Path(p).exists():
                    return Path(p)
            last_doc = getattr(self.app.brain, "last_doc", None)
            return Path(last_doc[0]) if last_doc and Path(last_doc[0]).exists() else None
        from batsy import projects

        if re.fullmatch(r"(?:my |the )?(.+?) (?:project|repo|folder)", what):  # a whole project: zipped
            found = projects.Store().find(what) or (projects.identify(what, self.cfg).get("best") or {})
            if found.get("path"):
                return Path(found["path"])
        hits = projects.find_files(what)
        return Path(hits[0]["path"]) if hits else None

    def _send(self, what: str, dev: str) -> bool:
        name, say = self._device(dev)
        if not tailscale():
            self.app._reply("Tailscale isn't installed on this laptop, so I can't send files yet.")
            return True
        if not name:
            self.app._reply(f"I don't know which Tailscale device {say} is. Set it under files devices in my config.")
            return True
        path = self._pick(what)
        if path is None:
            self.app._reply("I'm not sure which file you mean. Say its name, like: send my physics notes to my Mac.")
            return True
        self.app.overlay.send("status", text=f"Sending {path.name} to {say}")
        send, cleanup = path, None
        if path.is_dir():  # Taildrop sends files, not folders
            tmp = Path(tempfile.mkdtemp(prefix="batsy-send-"))
            send = _zip(path, tmp / f"{path.name}.zip")
            cleanup = tmp
        try:
            code, out = _run(["file", "cp", str(send), f"{name}:"], timeout=600)
        finally:
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)
        if code == 0:
            where = " It's in Downloads." if "mac" in say.lower() else ""
            self.app._reply(f"Sent {send.name} to {say}.{where}")
            self.app.last_file = path
            return True
        log.warning("taildrop send to %s failed: %s", name, out)
        if re.search(r"offline|not online|no route|unreachable|timeout|timed out", out, re.I):
            self.app._reply(f"{say[0].upper() + say[1:]} isn't reachable on Tailscale right now. Is it awake and connected?")
        elif re.search(r"unknown|no matching|not found|invalid", out, re.I):
            self.app._reply(f"Tailscale doesn't know a device called {name}. Check the name under files devices in my config.")
        else:
            self.app._reply(f"Sending failed: {out.splitlines()[-1][:120] if out else 'no reason given'}.")
        return True

    def _list(self) -> bool:
        files = sorted(self._files(), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
        if not files:
            self.app._reply("Your inbox is empty.")
            return True
        today = [p for p in files if time.time() - p.stat().st_mtime < 86400]
        if today:
            self.app._reply(f"{len(today)} file{'s' if len(today) != 1 else ''} came in today; the latest is "
                            f"{today[0].name}.")
        else:
            self.app._reply(f"Nothing new today. The latest is {files[0].name}.")
        self.app.show_visual("card", "Inbox | " + "\n".join(f"- {p.name} · {_size(p)}" for p in files))
        if self.problem:
            log.warning("inbox: %s", self.problem)
        return True


def _zip(folder: Path, out: Path) -> Path:
    """A folder as a zip, without what can be rebuilt (.git, node_modules, virtualenvs, build output)."""
    import zipfile

    from batsy.projects import SKIP

    skip = SKIP | {".git", ".venv", "venv", "node_modules", "__pycache__", ".next", ".cache", "dist", "build", "target"}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                if f == ".DS_Store":
                    continue
                full = os.path.join(dirpath, f)
                z.write(full, os.path.join(folder.name, os.path.relpath(full, folder)))
    return out


def _size(p: Path) -> str:
    try:
        b = p.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} GB"
