"""Batsy Brain: an Obsidian vault (~/Batsy/Brain) that Batsy writes, reads and keeps in sync.

Structure (PARA-style, linked with [[wikilinks]], every note has frontmatter properties for Obsidian Bases):
  Home.md, 00 Inbox, 01 Daily/<year>, 02 Projects, 03 Areas, 04 Resources/{Files,Prints,Shopping},
  05 People, 06 Memory, 07 Archive, Batsy/{Templates,Bases}, _attachments

Ownership: Batsy only ever rewrites what's inside its marker blocks

    %% batsy:<name> %%
    ...
    %% /batsy %%

(Obsidian hides %% comments %% in reading view) or appends lines inside them. Everything outside belongs to
Jonathan and is never touched. If he deletes a block, Batsy adds it back at the end of the note.

All writes go through one writer thread (`submit`), so a capture never slows a reply and two never collide.
A sync thread commits the vault to Git and pushes it to the private GitHub repo (Obsidian Git on the phone).
"""
import datetime as dt
import logging
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from batsy.config import WORKSPACE

log = logging.getLogger("batsy.vault")

ROOT = WORKSPACE / "Brain"
DIRS = {"inbox": "00 Inbox", "daily": "01 Daily", "projects": "02 Projects", "areas": "03 Areas",
        "files": "04 Resources/Files", "prints": "04 Resources/Prints", "models": "04 Resources/Models", "shopping": "04 Resources/Shopping",
        "people": "05 People", "memory": "06 Memory", "archive": "07 Archive", "weekly": "01 Daily/Weekly",
        "templates": "Batsy/Templates", "bases": "Batsy/Bases", "attachments": "_attachments"}
DAY_STARTS_AT = 5  # until 5 AM it's still "last night" (same as routines.day_starts_at)
BLOCK = re.compile(r"%% batsy:(?P<name>[\w-]+) %%\n(?P<body>.*?)%% /batsy %%", re.S)

_cfg = None
_q: "queue.Queue | None" = None
_ours: dict[str, float] = {}   # path -> mtime of Batsy's own last write (the indexer ignores those as edits)
_lock = threading.RLock()


# --- setup -----------------------------------------------------------------------------------------------------
def configure(cfg) -> None:
    global _cfg, ROOT
    _cfg = cfg
    ROOT = Path(os.path.expanduser(cfg.get("vault.path")))


def enabled() -> bool:
    return bool(_cfg is None or _cfg.get("vault.enabled"))


def capture_level() -> str:
    return _cfg.get("vault.capture") if _cfg else "all"


def start(app) -> None:
    """Make the vault if needed, then start the writer, the indexer and Git sync."""
    configure(app.cfg)
    if not enabled():
        return
    fresh = not ROOT.exists()
    ensure()
    global _q
    _q = queue.Queue()
    threading.Thread(target=_writer, daemon=True, name="vault-writer").start()
    if fresh:  # first run: fill it from everything Batsy already knows
        from batsy import vault_seed

        submit(vault_seed.seed, app.cfg)
    from batsy import vault_daily, vault_index

    vault_index.start(app)
    vault_daily.start(app)
    from batsy import vault_music

    vault_music.start(app)  # every song you listen to, from Spotify's history
    if app.cfg.get("vault.git"):
        threading.Thread(target=_sync_loop, daemon=True, name="vault-git").start()


def path(kind: str, name: str = "") -> Path:
    p = ROOT / DIRS[kind]
    return p / f"{safe(name)}.md" if name else p


def safe(name: str) -> str:
    """A file name Obsidian, Git and every OS accept, that still reads like the title."""
    name = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", str(name)).strip(" .")
    return re.sub(r"\s+", " ", name)[:120] or "Untitled"


def link(name: str, label: str = "") -> str:
    name = safe(name)
    return f"[[{name}|{label}]]" if label and label != name else f"[[{name}]]"


# --- writing ---------------------------------------------------------------------------------------------------
def submit(fn, *args, **kw) -> None:
    """Run a write on the writer thread (or right here, when there isn't one: the CLI)."""
    if not enabled():
        return
    if _q is None:
        with _lock:
            fn(*args, **kw)
        return
    _q.put((fn, args, kw))


def flush(timeout: float = 30) -> None:
    """Wait until every queued write is done."""
    if _q is None:
        return
    done = threading.Event()
    _q.put((done.set, (), {}))
    done.wait(timeout)


def _writer() -> None:
    while True:
        fn, args, kw = _q.get()
        try:
            with _lock:
                fn(*args, **kw)
        except Exception:
            log.exception("vault write %s", getattr(fn, "__name__", fn))


def write(p: Path, text: str) -> None:
    """Atomic write; remembers it was Batsy's own (so it isn't mistaken for an edit of Jonathan's)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(text)
    tmp.replace(p)
    _ours[str(p)] = p.stat().st_mtime


def read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def is_ours(p: Path) -> bool:
    try:
        return abs(_ours.get(str(p), -1) - p.stat().st_mtime) < 1e-6
    except OSError:
        return False


def create(p: Path, text: str) -> bool:
    """Write a new note; never replaces an existing one. True if it made it."""
    if p.exists():
        return False
    write(p, text)
    return True


def frontmatter(**props) -> str:
    lines = ["---"]
    for k, v in props.items():
        if v is None or v == "":
            continue
        if isinstance(v, (list, tuple)):
            lines.append(f"{k}:" + ("".join(f"\n  - {_yaml(x)}" for x in v) if v else " []"))
        else:
            lines.append(f"{k}: {_yaml(v)}")
    return "\n".join(lines + ["---", ""])


def _yaml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"' if re.search(r"[:#\[\]{}&*!|>'\"%@`,]|^\s|\s$|^-", s) \
        or s.lower() in ("yes", "no", "true", "false", "null") else s


def props(p: Path) -> dict:
    """The note's frontmatter, simply parsed (key: value and key: lists)."""
    m = re.match(r"---\n(.*?)\n---", read(p), re.S)
    out, key = {}, None
    for line in (m.group(1).splitlines() if m else []):
        if re.match(r"\s+- ", line) and key:
            out.setdefault(key, []).append(line.split("- ", 1)[1].strip().strip('"'))
        elif ":" in line:
            key, v = (x.strip() for x in line.split(":", 1))
            out[key] = v.strip('"') if v else []
    return out


def set_props(p: Path, **kv) -> None:
    """Change (or add) frontmatter properties, leaving the rest of the note alone."""
    text = read(p)
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return
    lines = m.group(1).splitlines()
    for k, v in kv.items():
        new = frontmatter(**{k: v})[4:-5]  # "k: v" (lists span lines)
        i = next((i for i, x in enumerate(lines) if x.split(":", 1)[0] == k), None)
        if i is None:
            lines += new.splitlines()
            continue
        j = i + 1
        while j < len(lines) and lines[j].startswith("  - "):
            j += 1
        lines[i:j] = new.splitlines()
    new_text = "---\n" + "\n".join(lines) + "\n---\n" + text[m.end():]
    if new_text != text:
        write(p, new_text)


def block(name: str, body: str = "") -> str:
    body = body.rstrip("\n")
    return f"%% batsy:{name} %%\n{body + chr(10) if body else ''}%% /batsy %%"


def get_block(p: Path, name: str) -> str | None:
    for m in BLOCK.finditer(read(p)):
        if m.group("name") == name:
            return m.group("body")
    return None


def set_block(p: Path, name: str, body: str, heading: str = "") -> None:
    """Replace what's inside a block (the rest of the note is left exactly as it is)."""
    text = read(p)
    for m in BLOCK.finditer(text):
        if m.group("name") == name:
            new = text[:m.start()] + block(name, body) + text[m.end():]
            if new != text:
                write(p, new)
            return
    write(p, text.rstrip("\n") + "\n\n" + (f"## {heading}\n" if heading else "") + block(name, body) + "\n")


def add_to_block(p: Path, name: str, line: str, heading: str = "", dedupe: bool = True, newest_first: bool = False) -> None:
    body = get_block(p, name)
    lines = [x for x in (body or "").splitlines() if x.strip()]
    if dedupe and line in lines:
        return
    lines = [line] + lines if newest_first else lines + [line]
    set_block(p, name, "\n".join(lines), heading)


# --- days --------------------------------------------------------------------------------------------------------
def today(t: float | None = None) -> dt.date:
    """The logical day: 2 AM still belongs to yesterday."""
    return (dt.datetime.fromtimestamp(t or time.time()) - dt.timedelta(hours=DAY_STARTS_AT)).date()


def daily_name(day: dt.date) -> str:
    return day.isoformat()


def daily_path(day: dt.date) -> Path:
    return ROOT / DIRS["daily"] / str(day.year) / f"{daily_name(day)}.md"


def daily(day: dt.date | None = None) -> Path:
    """Today's (or that day's) daily note, made from the template the first time it's needed."""
    day = day or today()
    p = daily_path(day)
    if not p.exists():
        prev, nxt = day - dt.timedelta(days=1), day + dt.timedelta(days=1)
        week = f"{day.isocalendar().year}-W{day.isocalendar().week:02d}"
        create(p, frontmatter(type="daily", date=day.isoformat(), week=week, tags=["daily"]) +
               f"# {day:%A %-d %B %Y}\n[[{daily_name(prev)}|← {prev:%a %-d}]] · [[Home]] · [[{week}]] · "
               f"[[{daily_name(nxt)}|{nxt:%a %-d} →]]\n\n"
               f"## Plan\n{block('plan')}\n\n## Summary\n{block('summary', '_Batsy writes this tonight._')}\n\n"
               f"## Timeline\n{block('timeline')}\n\n## Files\n{block('files')}\n\n## Prints\n{block('prints')}\n\n"
               f"## Music\n{block('music')}\n\n## Notes\n- \n")
    return p


# --- opening in Obsidian ---------------------------------------------------------------------------------------------
def open_note(p: Path | None = None) -> bool:
    """Open the vault (or one note) in Obsidian; the default app for .md files if Obsidian isn't there."""
    from urllib.parse import quote

    target = p or (ROOT / "Home.md")
    url = f"obsidian://open?path={quote(str(target))}"
    for cmd in (["flatpak", "run", "md.obsidian.Obsidian", url], ["obsidian", url], ["xdg-open", str(target)]):
        try:
            if cmd[0] == "flatpak" and subprocess.run(["flatpak", "info", "md.obsidian.Obsidian"],
                                                       capture_output=True).returncode:
                continue
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except FileNotFoundError:
            continue
    return False


# --- Git: history and the sync to the phone ------------------------------------------------------------------------
# Use the GitHub login saved in the keyring, but never change it: a failed pull/push (the repo doesn't exist yet)
# makes git's normal helper DELETE the saved token, so every git push on the laptop asked for it again.
READ_ONLY_LOGIN = ["-c", "credential.helper=", "-c", "credential.helper=!f() { test \"$1\" = get && "
                   "exec /usr/libexec/git-core/git-credential-libsecret get; }; f"]
REMOTE = {"pull", "push", "fetch", "ls-remote"}


def git(*args, timeout: float = 60) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k != "SSH_ASKPASS"}  # (git used it to pop up a GitHub login)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")  # never ask: no login saved = fail quietly
    pre = READ_ONLY_LOGIN if args and args[0] in REMOTE else []
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *pre, *args], capture_output=True, text=True, timeout=timeout,
                           env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


_remote_off_until = 0.0  # GitHub said no (no repo yet / no login): stop asking it every 10 minutes


def sync(push: bool = True, force: bool = False) -> str:
    """Commit what changed; pull the phone's edits (rebase); push. Returns a short sentence. While GitHub keeps
    saying no, only every 6 hours (or when you say "sync my brain": force)."""
    global _remote_off_until
    flush()
    with _lock:
        if not (ROOT / ".git").exists():
            git("init", "-b", "main")
            git("config", "user.name", "Batsy")
            git("config", "user.email", "batsy@localhost")
        remote = _cfg.get("vault.git_remote") if _cfg else ""
        if remote and git("remote", "get-url", "origin")[0]:
            git("remote", "add", "origin", remote)
        git("add", "-A")
        changed = git("diff", "--cached", "--quiet")[0] != 0
        if changed:
            n = len(git("diff", "--cached", "--name-only")[1].splitlines())
            git("commit", "-q", "-m", f"Batsy: {n} note{'s' if n != 1 else ''} {dt.datetime.now():%Y-%m-%d %H:%M}")
        if not (push and remote):
            return "Saved the brain's history." if changed else "Nothing new to save."
        if not force and time.time() < _remote_off_until:
            return "Saved here; GitHub isn't set up for the brain yet."
        code, out = git("pull", "--rebase", "--autostash", "-q", "origin", "main", timeout=60)
        if code and "couldn't find remote ref" not in out:
            if _refused(out):
                _remote_off_until = time.time() + 6 * 3600
                return "The GitHub repo for the brain doesn't exist yet. Make a private repo called brain."
            git("rebase", "--abort")
            log.warning("vault pull failed: %s", out[-300:])
        code, out = git("push", "-q", "-u", "origin", "main", timeout=60)
        if code:
            log.warning("vault push failed: %s", out[-300:])
            if _refused(out):
                _remote_off_until = time.time() + 6 * 3600
            return "Saved here, but I couldn't push it to GitHub." + (
                " The repo doesn't exist yet: make a private repo called brain." if "not found" in out.lower() else "")
        _remote_off_until = 0.0
        return "The brain is synced with GitHub."


def _refused(out: str) -> bool:
    o = out.lower()
    return any(w in o for w in ("not found", "invalid username", "authentication failed", "could not read username",
                                "terminal prompts disabled", "403", "401"))


def _sync_loop() -> None:
    time.sleep(90)
    last = ""
    while True:
        try:
            said = sync()
            if said != last and "GitHub" in said:
                log.info("brain sync: %s", said)  # once per change of state (e.g. until the repo exists)
            last = said
        except Exception:
            log.exception("vault sync")
        time.sleep(max(60, 60 * int(_cfg.get("vault.git_every_min"))))


# --- the vault itself ------------------------------------------------------------------------------------------------
def ensure() -> None:
    """Folders, Home, templates, Bases, Obsidian settings: everything that makes it a good vault on day one."""
    from batsy import vault_layout

    for d in DIRS.values():
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    vault_layout.install(ROOT)
