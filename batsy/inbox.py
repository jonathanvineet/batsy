"""Files in and out of this laptop, over Tailscale.

In: each sender gets a folder, ~/Batsy/inbox/<name>, named in files.senders (Tailscale login -> name).
  - Taildrop (right-click > Share > Tailscale on your Mac, or `tailscale file cp FILE <this laptop>:`) only works
    between your own devices, so whatever it delivers is yours: it goes in your folder.
  - Anyone else on the tailnet (and you, from any device) uploads to http://<this laptop>:8765, a page with a file
    picker, or `curl -T FILE http://<this laptop>:8765/`. Tailscale says who is connecting (`tailscale whois`), so
    the folder can't be faked; logins not in files.senders are turned away.
Batsy says what arrived and from whom, shows it, reads it into memory (so "summarise what Karan sent" works), and
asks whether to open it. Anything else that drops files in the inbox (LocalSend, scp) is noticed too.

Out: "send this to my Mac" / "send the marked-up PDF to my Mac" / "send my thermodynamics notes to my iPad" runs
`tailscale file cp`. On a Mac it arrives in Downloads. A folder is zipped first.

Linux keeps received Taildrop files in a holding area until something collects them: Batsy runs
`tailscale file get --loop` for that. Without root it needs one-time permission:
  sudo tailscale set --operator=$USER
"""
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from batsy import received
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
FROM_LIST = re.compile(r"^(?:what|which files?) (?:did|has) (?P<who>[a-z]+) (?:send|sent|shared?)(?: me| you)?"
                       r"(?: today)?$|^(?:show me|list|check) (?:the )?files? from (?P<who2>[a-z]+)$")
OPEN_FROM = re.compile(r"^open (?P<who>[a-z]+)'?s? (?:inbox |files? )?folder$|"
                       r"^open (?:the )?(?:inbox |files? )?folder (?:for|from) (?P<who2>[a-z]+)$")
STAGING = ".taildrop"  # Taildrop lands here first, then moves into your folder
# --- questions about what was received, answered from batsy/received.py ---
_ANY = r"(?:file|files|thing|things|one|pdf|pdfs|photo|photos|picture|pictures|image|video|document|doc|model|zip)"
WHO_SENT = re.compile(rf"^who (?:sent|shared|gave me|dropped) (?:me )?(?:the |that |this |those |these |my )?"
                      rf"(?:last |latest |newest |most recent |new )?(?:{_ANY}|it|that|this|those|these)?"
                      r"(?: (?:just )?now| today| just| earlier)?$|"
                      rf"^who (?:was|is|were) (?:the |that |this )?(?:last |latest )?(?:{_ANY} )?(?:that )?from$|"
                      rf"^(?:where|who) did (?:the |that |this )?(?:last |latest )?{_ANY}? ?come from$")
WHO_SENT_NAMED = re.compile(r"^who (?:sent|shared|gave me) (?:me )?(?:the |that |my )?(?P<what>.+)$")
LAST_FILE = re.compile(rf"^what(?:'s| is| was|s) (?:the |my )?(?:last|latest|newest|most recent) (?:{_ANY})"
                       r"(?: (?:i|you) (?:got|received))?$|^what did (?:i|you) (?:just )?(?:get|receive)$|"
                       r"^what (?:just )?(?:came|arrived)(?: in)?$")
EVERYTHING = re.compile(r"^(?:list|show me|show|what are) (?:all |everything |all the )?(?:the )?(?:files )?"
                        r"(?:i'?ve|i have|you'?ve|you have) (?:got|received|gotten)(?: so far)?$|"
                        r"^(?:list|show me|show) (?:all )?(?:the )?(?:received files|everything (?:i|you) (?:got|received))$")
WHERE = re.compile(r"^where (?:is|did you (?:save|put)|are) (?:the |my |that )?(?P<what>.+?)(?: saved| stored| kept)?$")
OPEN_FILE = re.compile(r"^(?:open|show me|show|pull up|bring up) (?P<rest>.+)$")
WHEN = ("today", "yesterday", "this week", "this month")
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


def whois(ip: str) -> tuple[str, str] | None:
    """(login, device) behind a tailnet address, or None if it isn't one."""
    code, out = _run(["whois", "--json", ip], timeout=5)
    if code:
        return None
    try:
        d = json.loads(out)
        return d["UserProfile"]["LoginName"].lower(), d["Node"].get("ComputedName") or d["Node"]["Name"].split(".")[0]
    except (ValueError, KeyError, TypeError):
        return None


def my_login() -> str:
    """Whose tailnet login this laptop is on: Taildrop only delivers from that login's devices."""
    code, out = _run(["status", "--json"])
    try:
        d = json.loads(out) if code == 0 else {}
        return d["User"][str(d["Self"]["UserID"])]["LoginName"].lower()
    except (ValueError, KeyError, TypeError):
        return ""


def my_ip() -> str:
    code, out = _run(["ip", "-4"])
    return out.splitlines()[0].strip() if code == 0 and out else ""


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
    """Owned by the app: collects Taildrop files, receives uploads, files each under its sender, notices new files
    in the inbox, and sends files out."""

    def __init__(self, app):
        import getpass

        self.app, self.cfg = app, app.cfg
        self.dir = Path(self.cfg.get("files.inbox")).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.staging = self.dir / STAGING
        self.senders = {k.lower(): _safe_name(v) for k, v in (self.cfg.get("files.senders") or {}).items()}
        self.me = my_login() if tailscale() else ""  # your Tailscale login
        self.mine = self.senders.get(self.me) or _safe_name(self.cfg.get("user.name") or getpass.getuser().title())
        self.last: Path | None = None     # the newest file that arrived
        self.problem = ""                 # why Taildrop collection isn't working, if it isn't
        self._known = {self._key(p) for p in self._files() if p.parent != self.staging}  # what's there isn't news
        self._taildropped: set[str] = set()  # arrived by Taildrop (the rest came through the upload page)
        try:
            n = received.backfill(self.dir, self.mine, {STAGING})
            if n:
                log.info("remembered %d files already in the inbox", n)
        except Exception:
            log.exception("received files: backfill")
        self._growing: dict[str, tuple[int, float]] = {}
        self._batch: list[Path] = []
        self._batch_t = 0.0
        if self.cfg.get("files.taildrop") and tailscale():
            threading.Thread(target=self._collect, daemon=True, name="taildrop").start()
        if self.cfg.get("files.receive") and tailscale():
            threading.Thread(target=self._serve, daemon=True, name="receive").start()
        threading.Thread(target=self._watch, daemon=True, name="inbox").start()

    def _key(self, p: Path) -> str:
        return str(p.relative_to(self.dir))

    def sender_of(self, p: Path) -> str | None:
        """Whose folder a received file is in (None for one loose in the inbox)."""
        return p.parent.name if p.parent != self.dir and p.parent != self.staging else None

    def sender(self, ip: str) -> str | None:
        """The folder for whoever is at this tailnet address, or None if they may not send files."""
        who = whois(ip)
        if not who:
            return None
        if who[0] in self.senders:
            return self.senders[who[0]]
        if not self.me:
            self.me = my_login()
        return self.mine if who[0] == self.me else None

    def _folder(self, word: str) -> str | None:
        """'karan' / 'my' -> the sender folder it means, if there is one."""
        word = word.lower().strip()
        if word in ("my", "me", "mine", "i"):
            return self.mine
        names = set(self.senders.values()) | {self.mine}
        try:
            names |= {p.name for p in self.dir.iterdir() if p.is_dir() and not p.name.startswith(".")}
        except OSError:
            pass
        return next((n for n in names if n.lower() == word), None)

    # --- in ------------------------------------------------------------------------------------------------
    def _collect(self) -> None:
        """`tailscale file get --loop`: move each file Taildrop receives into staging; the watcher then files it in
        your folder. Restarts if it stops."""
        delay = 10
        self.staging.mkdir(exist_ok=True)
        # a restart (os.execv) leaves the old collector running: only one may collect, or they fight over files
        subprocess.run(["pkill", "-f", f"^{tailscale()} file get --loop .*{re.escape(str(self.staging))}$"],
                       capture_output=True)
        while True:
            t0 = time.time()
            self.problem = None  # fixed since last time (e.g. the operator was just set)? find out afresh
            try:
                p = subprocess.Popen([tailscale(), "file", "get", "--loop", "--wait", "--conflict=rename", "--verbose",
                                      str(self.staging)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
                if not getattr(self, "_told", False):  # say it once, out loud: a log line is easy to never see
                    self._told = True
                    self.app.announce("Files sent to me over Tailscale are stuck: I'm not allowed to collect them. "
                                      "Run sudo tailscale set operator equals your user name, once, in a terminal.")
            # back off while it keeps failing, but not for long: once it's fixed, files arrive within a minute
            delay = 10 if time.time() - t0 > 120 else min(60, delay * 2)
            time.sleep(delay)

    def _files(self, sender: str | None = None) -> list[Path]:
        """Received files: loose in the inbox, in each sender's folder, and waiting in Taildrop's staging folder.
        With `sender`, only that sender's folder."""
        def listed(d: Path) -> list[Path]:
            try:
                return [p for p in d.iterdir() if p.is_file() and not PARTIAL.search(p.name)]
            except OSError:
                return []

        if sender:
            return listed(self.dir / sender)
        out = listed(self.dir)
        try:
            dirs = [p for p in self.dir.iterdir() if p.is_dir() and (not p.name.startswith(".") or p == self.staging)]
        except OSError:
            dirs = []
        for d in dirs:
            out += listed(d)
        return out

    def _watch(self) -> None:
        """Every couple of seconds: a new file whose size has stopped changing has arrived. Taildrop's are moved
        into your folder first. Several arriving together are announced once."""
        while True:
            time.sleep(2)
            try:
                now = time.time()
                for p in self._files():
                    key = self._key(p)
                    if key in self._known:
                        continue
                    size = p.stat().st_size
                    was = self._growing.get(key)
                    if was and was[0] == size and now - was[1] >= 2:
                        self._growing.pop(key, None)
                        if p.parent == self.staging:
                            p = _place(p, self.dir / self.mine)
                            self._taildropped.add(str(p))
                        self._known.add(self._key(p))
                        self._batch.append(p)
                        self._batch_t = now
                    elif not was or was[0] != size:
                        self._growing[key] = (size, now)
                if self._batch and now - self._batch_t >= 3:
                    batch, self._batch = self._batch, []
                    self._arrived(batch)
            except Exception:
                log.exception("inbox watch")

    def _arrived(self, files: list[Path]) -> None:
        from batsy import docs

        log.info("arrived: %s", ", ".join(p.name for p in files))
        from batsy import vault_capture

        # The extras below must never cost you the announcement: each one failing is logged and skipped.
        for p in files:  # remembered for good: "who sent the last file?", "open the audit report"
            via = "taildrop" if str(p) in self._taildropped else "upload page"
            _safely("record", received.record, p, self.sender_of(p) or self.mine, via)
            _safely("vault card", vault_capture.file_received, str(p), self.sender_of(p) or self.mine, via,
                    received.kind_of(p.name), p.stat().st_size if p.exists() else 0)  # a card in the brain
        from batsy import slicer

        _safely("slicer", slicer.on_arrival, self.app, files)  # presets -> your printer; models sliced in background
        fw = getattr(self.app, "pc_forward", None)
        if fw is not None:  # a copy to the sender's Inbox on the PC (Karan -> nightwing, ...)
            _safely("copy to the PC", fw.send, files, self.sender_of)
        self.last = files[-1]
        self.app.last_file = files[0]
        first = files[0]
        brain = self.app.brain
        if first.suffix.lower() in docs.SUPPORTED:  # "summarise what I just sent" / "circle the dates in it"
            brain.last_doc = (first, time.time())
            threading.Thread(target=self._learn, args=(first,), daemon=True).start()
        if any(p.suffix.lower() in docs.SUPPORTED for p in files):
            # documents: read every one, work out what it is, file research material into the right research, and
            # show back what was understood (instead of just "you sent X, want me to open it?")
            self.app.projects.pending = {"kind": "open", "files": [str(p) for p in files], "until": time.time() + 300}
            brain.note_announcement(self._describe(files))

            def understand():
                self._wait_quiet()
                from batsy import research

                try:
                    research.understand_batch(self.app, files)
                except Exception:
                    log.exception("understanding the files")
                    self.app.announce(self._describe(files, spoken=True) + ".")
            threading.Thread(target=understand, daemon=True, name="understand-files").start()
            return
        spoken = self._describe(files, spoken=True)
        question = spoken + (". Want me to open it?" if len(files) == 1 else ". Want me to open the first one?")
        brain.note_announcement(self._describe(files))  # full names, so the AI knows what you mean by "that file"
        self._wait_quiet()
        if self._user_away():
            self.app.announce(spoken)  # spoken + a desktop notification; nothing to answer
            return
        if self.app.overlay.enabled and first.suffix.lower() in docs.IMAGES:
            self.app.overlay.image(first, first.name)
        self.app.show_visual("card", "Just arrived | " + "\n".join(self._line(p) for p in files[:6]))
        self.app.projects.pending = {"kind": "open", "files": [str(p) for p in files], "until": time.time() + 120}
        self.app.ask_user(question)

    def _describe(self, files: list[Path], spoken: bool = False) -> str:
        """'Karan sent report.pdf' / 'Rehaan sent 3 files: a, b and c; you sent d' / 'notes.pdf just arrived'.
        spoken: how a person would say it: 'Karan sent Physics notes, a PDF' / 'you sent 3 copies of Audit report'
        / 'Rehaan sent 4 photos'. Full names stay on the card."""
        groups: dict[str | None, list[Path]] = {}
        for p in files:
            groups.setdefault(self.sender_of(p), []).append(p)
        said = []
        for who, paths in groups.items():
            names = [p.name for p in paths]
            listed = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
            what = names[0] if len(names) == 1 else f"{len(names)} files: {listed}"
            if spoken:
                what = listed = _say_files(paths)
            if who is None:
                said.append(f"{what} just arrived" if len(names) == 1 or spoken else
                            f"{len(names)} files just arrived: {listed}")
            else:
                said.append(f"{'you' if who == self.mine else who} sent {what}")
        text = "; ".join(said)
        return text[0].upper() + text[1:]

    def _line(self, p: Path) -> str:
        who = self.sender_of(p)
        return f"- {p.name} · {_size(p)}" + (f" · from {who}" if who else "")

    # --- receiving uploads ---------------------------------------------------------------------------------
    def _serve(self) -> None:
        """The upload page. Bound to this laptop's Tailscale address only, so nothing off the tailnet can reach
        it. Waits for Tailscale to be up."""
        port = int(self.cfg.get("files.receive_port"))
        while True:
            ip = my_ip()
            if ip:
                try:
                    srv = ThreadingHTTPServer((ip, port), _handler(self))
                    srv.daemon_threads = True
                    log.info("receiving files at http://%s:%d", ip, port)
                    srv.serve_forever()
                except OSError as e:
                    log.warning("can't receive files on %s:%d: %s", ip, port, e)
            time.sleep(30)

    def _learn(self, path: Path) -> None:
        from batsy import docs

        try:
            docs.learn(path)
        except Exception as e:
            log.info("couldn't read %s: %s", path.name, e)
            return
        try:  # the file's card in the brain gets its opening lines
            from batsy import vault_capture
            from batsy.memory_store import get_store

            st = get_store()
            with st.lock:
                rows = st.db.execute("SELECT text FROM doc_chunks WHERE path = ? ORDER BY page, id LIMIT 2",
                                     (str(path),)).fetchall()
            text = " ".join(r["text"] for r in rows).strip()
            if text:
                vault_capture.file_summary(str(path), "**Starts:** " + text[:900])
        except Exception:
            log.exception("brain: file summary")

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
        if self._remembered(n):
            return True
        m = SEND.match(n)
        if m and self._looks_like_device(m.group("dev")):
            return self._send(m.group("what"), m.group("dev"))
        if INBOX_LIST.match(n):
            return self._list()
        m = FROM_LIST.match(n) or OPEN_FROM.match(n)
        if m:
            who = self._folder(m.group("who") or m.group("who2"))
            if not who:
                return False  # "what did you send" isn't about a sender
            if m.re is FROM_LIST:
                return self._list(who)
            (self.dir / who).mkdir(exist_ok=True)
            self.app._open(str(self.dir / who))
            self.app._reply(f"Opening {'your' if who == self.mine else who + chr(39) + 's'} folder.")
            return True
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

    # --- what was received (batsy/received.py) -------------------------------------------------------------
    def _who(self, word: str) -> str | None:
        """'karan' / 'nightwing' / 'me' / 'batman' -> the sender folder."""
        word = word.lower().strip().removesuffix("'s").removesuffix("s'")
        heroes = {v.lower(): k for k, v in (self.cfg.get("files.pc_users") or {}).items()}
        return self._folder(heroes.get(word, word)) or (self._folder(word[:-1]) if word.endswith("s") else None)

    def _say_from(self, r: dict) -> str:
        return "you" if r["sender"] == self.mine else r["sender"] or "someone"

    def _remembered(self, n: str) -> bool:
        if WHO_SENT.match(n) or LAST_FILE.match(n):
            r = received.last()
            if not r:
                self.app._reply("Nothing has been sent to me yet.")
                return True
            self.last = Path(r["path"])
            who = self._say_from(r)
            self.app._reply(f"{who[0].upper() + who[1:]} sent {received.spoken(r['name'])}, a {r['kind']}, "
                            f"{received.ago(r['received'])}" + (" by Taildrop." if r["via"] == "taildrop" else "."))
            return True
        if EVERYTHING.match(n):
            rows = received.all_rows(limit=500)
            if not rows:
                self.app._reply("Nothing has been sent to me yet.")
                return True
            by = {}
            for r in rows:
                by[self._say_from(r)] = by.get(self._say_from(r), 0) + 1
            self.app._reply(f"{len(rows)} files so far: " + ", ".join(f"{c} from {w}" for w, c in by.items()) + ".")
            self.app.show_visual("card", "Everything received | " + "\n".join(
                f"- {r['name']} · {self._say_from(r)} · {received.ago(r['received'])}" for r in rows[:14]))
            return True
        m = WHO_SENT_NAMED.match(n)
        if m and not WHO_SENT.match(n):
            hits = received.search(m["what"])
            if hits and hits[0][0] >= 75:
                r = hits[0][1]
                self.app._reply(f"{self._say_from(r)[0].upper() + self._say_from(r)[1:]} sent {received.spoken(r['name'])}, "
                                f"{received.ago(r['received'])}.")
                return True
        m = WHERE.match(n)
        if m:
            r = self._find(m["what"])
            if r and "none" in r:
                self.app._reply(r["none"])
                return True
            if r:
                whose = "your" if r["sender"] == self.mine else f"{r['sender']}'s"
                said = f"{r['name']} is in {whose} folder in the inbox"
                pc_user = (self.cfg.get("files.pc_users") or {}).get(r["sender"])
                self.app._reply(said + (f", and on the PC in {pc_user}'s Inbox." if r.get("pc_path") and pc_user else "."))
                return True
        m = OPEN_FILE.match(n)
        if m:
            r = self._find(m["rest"])
            if r and "none" in r:
                self.app._reply(r["none"])
                return True
            if r:
                path = Path(r["path"])
                if not path.exists():
                    self.app._reply(f"I remember {r['name']}, but it's been moved or deleted from the inbox.")
                    return True
                self.last = path
                self.app.last_file = path
                self.app.open_result(str(path))
                self.app._reply(f"Opening {received.spoken(r['name'])}, from {self._say_from(r)}.")
                return True
        return False

    def _find(self, said: str) -> dict | None:
        """'the audit report' / 'the file karan sent' / 'karan's last pdf' / 'the photo from yesterday' -> a row.
        None if it isn't clearly a received file (so "open youtube" still opens YouTube)."""
        s = re.sub(r"^(?:the|that|this|my) ", "", said.strip())
        mine = said.strip().startswith("my ")
        since, until = 0.0, None
        for word in WHEN:
            if re.search(rf"\b(?:from |of )?{word}\b", s):
                s = re.sub(rf"\s*\b(?:from |of )?{word}\b", "", s).strip()
                since, until = received.day_range(word)
        sender = self.mine if mine else None
        m = re.match(r"^(?P<who>[a-z]+)'s (?P<rest>.+)$", s) or \
            re.match(r"^(?P<rest>.+?) (?:that |which )?(?P<who>[a-z]+) (?:sent|shared|gave me)(?: me)?$", s) or \
            re.match(r"^(?P<rest>.+?) (?:from|by) (?P<who>[a-z]+)$", s)
        if m and (who := self._who(m["who"])):
            sender, s = who, m["rest"].strip()
        elif re.search(r"\b(?:i|you) (?:just )?(?:sent|got|received)\b", s):
            s = re.sub(r"\s*(?:that |which )?\b(?:i|you) (?:just )?(?:sent|got|received)\b(?: me| you)?", "", s).strip()
        latest = bool(re.match(r"^(?:last|latest|newest|most recent|new) ", s)) or s in ("last one", "latest one")
        s = re.sub(r"^(?:last|latest|newest|most recent|new) ", "", s).strip()
        kind = received.KIND_WORDS.get(s.removesuffix(" file"))
        generic = s in ("file", "files", "one", "thing", "last one", "latest one", "") or kind is not None
        if generic:  # "karan's last pdf", "the file rehaan sent", "the photo from yesterday"
            if not (sender or latest or since or "file" in said):
                return None
            rows = received.all_rows(None if mine and not latest and not since and sender == self.mine and
                                     "file" not in said else sender, since=since, kind=kind, limit=1, until=until)
            if rows:
                return rows[0]
            who = "You haven't" if sender == self.mine else f"{sender} hasn't" if sender else "Nobody has"
            when = {0.0: ""}.get(since, " then")
            return {"none": f"{who} sent {('any ' + kind + 's') if kind else 'anything'}{when}."}
        hits = [h for h in received.search(s, sender) if since <= h[1]["received"] < (until or 1e12)]
        if hits and hits[0][0] >= (70 if sender or since else 86):
            return hits[0][1]
        return None

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
            from batsy import vault_capture

            vault_capture.file_sent(send.name, say)
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

    def _list(self, sender: str | None = None) -> bool:
        """What came in (from everyone, or from one sender), newest first."""
        rows = [r for r in received.all_rows(sender, limit=6) if not r.get("missing")]
        files = [Path(r["path"]) for r in rows] or \
            sorted(self._files(sender), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
        who = "you" if sender == self.mine else sender
        if not files:
            self.app._reply(f"{who.title()} haven't sent anything yet." if who == "you" else
                            f"{who} hasn't sent anything yet." if who else "Your inbox is empty.")
            return True
        today = [p for p in files if time.time() - p.stat().st_mtime < 86400]
        latest = today[0] if today else files[0]
        came = f"from {who} " if who else ""
        if today:
            self.app._reply(f"{len(today)} file{'s' if len(today) != 1 else ''} came in {came}today; the latest is "
                            f"{latest.name}" + (f", from {self.sender_of(latest)}." if not who and
                                                self.sender_of(latest) else "."))
        else:
            self.app._reply(f"Nothing new {came}today. The latest is {latest.name}.")
        title = f"From {sender}" if sender else "Inbox"
        self.app.show_visual("card", f"{title} | " + "\n".join(
            f"- {p.name} · {_size(p)}" if sender else self._line(p) for p in files))
        if self.problem:
            log.warning("inbox: %s", self.problem)
        return True


def _safe_name(name: str) -> str:
    """A file or folder name that stays where it's put: no path parts, no leading dot, no control characters."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", os.path.basename(str(name).replace("\\", "/"))).strip().lstrip(".")
    return name[:200] or "file"


def _safely(what: str, fn, *args) -> None:
    try:
        fn(*args)
    except Exception:
        log.exception("arrival: %s failed", what)


def _place(src: Path, folder: Path, name: str | None = None) -> Path:
    """Move src into folder (as `name`) without overwriting anything: 'a.pdf' becomes 'a (1).pdf' if taken."""
    folder.mkdir(parents=True, exist_ok=True)
    base = Path(name or src.name)
    for i in range(10_000):
        dest = folder / (base.name if i == 0 else f"{base.stem} ({i}){base.suffix}")
        try:
            os.link(src, dest)  # atomic and refuses to overwrite, even with two uploads racing
        except FileExistsError:
            continue
        os.unlink(src)
        return dest
    raise OSError(f"too many files called {base.name}")


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Send files</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 16px;background:#fff;color:#111}
@media(prefers-color-scheme:dark){body{background:#111;color:#eee}}
label{display:block;padding:2.5rem 1rem;border:2px dashed #888;border-radius:12px;text-align:center;cursor:pointer}
label.over{border-color:#3b82f6}input{display:none}ul{padding:0;list-style:none}li{margin:.5rem 0;word-break:break-all}
</style>
<h1>Hi {who}</h1><p>Files you send here land in your folder on {me}'s laptop.</p>
<label id=drop>Tap to choose files, or drop them here<input type=file multiple id=pick></label><ul id=log></ul>
<script>
async function send(files){for(const f of files){const li=document.createElement('li');li.textContent=f.name+' …';
log.append(li);await new Promise(done=>{const x=new XMLHttpRequest();x.open('PUT','/'+encodeURIComponent(f.name));
x.upload.onprogress=e=>{if(e.lengthComputable)li.textContent=f.name+' '+Math.round(100*e.loaded/e.total)+'%'};
x.onload=()=>{li.textContent=f.name+(x.status<300?' ✓ sent':' ✗ '+x.responseText);done()};
x.onerror=()=>{li.textContent=f.name+' ✗ failed, try again';done()};x.send(f)})}}
pick.onchange=()=>{send([...pick.files]);pick.value=''};
drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');send([...e.dataTransfer.files])};
</script>
"""


def _handler(inbox: "Inbox"):
    """GET / is the upload page; PUT /<name> (curl -T FILE http://host:port/) stores a file in the sender's folder.
    Who the sender is comes from Tailscale, never from the request."""
    limit = int(inbox.cfg.get("files.receive_max_mb")) << 20

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # so curl gets "100 Continue" instead of waiting before it uploads
        server_version = "Batsy"

        def log_message(self, fmt, *args):
            log.debug("receive %s: " + fmt, self.client_address[0], *args)

        def _reply(self, code: int, text: str, kind: str = "text/plain; charset=utf-8") -> None:
            body = text.encode()
            self.close_connection = code >= 400  # an unread upload body mustn't be parsed as the next request
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _sender(self) -> str | None:
            who = inbox.sender(self.client_address[0])
            if not who:
                log.info("receive: turned away %s", self.client_address[0])
                self._reply(403, "You're not on the list of people who can send files here.\n")
            return who

        def do_GET(self):
            who = self._sender()
            if who:
                page = PAGE.replace("{who}", html.escape("there" if who == inbox.mine else who))
                self._reply(200, page.replace("{me}", html.escape(inbox.mine)), "text/html; charset=utf-8")

        def do_PUT(self):
            who = self._sender()
            if not who:
                return
            name = _safe_name(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path))
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._reply(411, "Send the file itself (curl -T FILE ...), not a stream.\n")
            if size > limit:
                return self._reply(413, f"That's over the {limit >> 20} MB limit.\n")
            folder = inbox.dir / who
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f".{name}.{threading.get_ident()}.part"  # hidden until complete: the watcher skips it
            try:
                with open(tmp, "wb") as f:
                    left = size
                    while left:
                        chunk = self.rfile.read(min(left, 1 << 20))
                        if not chunk:
                            raise ConnectionError("upload cut off")
                        f.write(chunk)
                        left -= len(chunk)
                dest = _place(tmp, folder, name)
            except (OSError, ConnectionError) as e:
                tmp.unlink(missing_ok=True)
                log.warning("receive %s from %s failed: %s", name, who, e)
                try:
                    self._reply(500, "Couldn't save it, try again.\n")
                except OSError:
                    pass
                return
            log.info("received %s from %s", dest.name, who)
            self._reply(201, f"Got it: {dest.name}\n")

    return Handler


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


KINDS = {".pdf": ("a PDF", "PDFs"), ".docx": ("a Word file", "Word files"), ".doc": ("a Word file", "Word files"),
         ".pptx": ("a slide deck", "slide decks"), ".ppt": ("a slide deck", "slide decks"),
         ".xlsx": ("a spreadsheet", "spreadsheets"), ".csv": ("a spreadsheet", "spreadsheets"),
         ".jpg": ("a photo", "photos"), ".jpeg": ("a photo", "photos"), ".png": ("an image", "images"),
         ".heic": ("a photo", "photos"), ".mp4": ("a video", "videos"), ".mov": ("a video", "videos"),
         ".mp3": ("a song", "songs"), ".zip": ("a zip", "zips"), ".txt": ("a text file", "text files")}


def _title(p: Path) -> str:
    """'Academic_Audit-Report2025-2026 (1).pdf' -> 'Academic Audit Report': what you'd call it out loud."""
    s = re.sub(r"\s*\(\d+\)$", "", p.stem)  # browser copies: "x (1)"
    s = re.sub(r"[_\-.]+", " ", s)
    s = re.sub(r"\b(?:v\d+|final|copy)\b", " ", s, flags=re.I)  # version tags nobody says
    s = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", s)  # "Report2025" -> "Report 2025"
    words = [w for w in s.split() if not re.fullmatch(r"\d{4,}|img|dsc|scan", w, re.I)]
    words = words or s.split()
    if len(words) > 10:  # very long titles: the gist is enough
        words = words[:8]
    out = " ".join(words)
    return out[0].upper() + out[1:] if out else p.name


def _say_files(paths: list[Path]) -> str:
    """One file: 'Physics notes, a PDF'. Copies: '3 copies of Audit report'. Several: '4 photos' / '3 PDFs and a
    Word file' / '5 files'."""
    kinds = [KINDS.get(p.suffix.lower(), ("a file", "files")) for p in paths]
    if len(paths) == 1:
        return f"{_title(paths[0])}, {kinds[0][0]}" if kinds[0][0] != "a file" else _title(paths[0])
    titles = {_title(p).lower() for p in paths}
    if len(titles) == 1:
        return f"{len(paths)} copies of {_title(paths[0])}"
    counts: dict[tuple[str, str], int] = {}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1
    if len(counts) > 2:
        return f"{len(paths)} files"
    parts = [(f"{n} {plural}" if n > 1 else one) for (one, plural), n in counts.items()]
    return " and ".join(parts)


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
