"""Commit and push, done by Batsy itself (Claude never stages, commits or pushes: Jonathan's rule).

  "commit and push the batsy folder" · "commit my robot project" · "push batsy" · "commit and push it"
    1. finds the folder (the project index: your local clones and project folders; "it" = the last one)
    2. stages everything that changed (git add -A)
    3. the free local model writes the message from the diff (a summary line + a few bullets; no AI credit)
    4. reads it out and asks "push it?"; on yes: commits, then pushes (push only when asked to)
"""
import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.gitops")
PRE = r"^(?:(?:can you|could you|please|hey|ok|okay|now|so|batsy)\s+)*"
REPO = r"(?:(?:the|my|our) )?(?P<what>.+?)(?: (?:folder|repo|repository|project|code|codebase))?"
COMMIT = re.compile(PRE + r"(?:stage(?: and)? )?commit(?: everything| all| the changes| my changes)?"
                          r"(?P<push>(?:,? and| then)? push(?: it| them)?)?(?: (?:in|for|of|on|to) " + REPO + r")?"
                          r"(?: and push(?: it)?)?$")
COMMIT_ALT = re.compile(PRE + r"(?:stage(?: and)? )?commit(?: and push)? " + REPO + r"(?P<push2> and push(?: it)?)?$")
ANY_GIT = re.compile(r"\b(?:commit|stage the changes|stage and commit)\b|\bpush\b.*\b(?:changes|github|repo|folder|code|main)\b")
PUSH = re.compile(PRE + r"push (?:the changes (?:in|of|for) )?" + REPO + r"(?: to (?:github|main|origin))?$")
_last: dict = {}


def _git(path: Path, *args, timeout: float = 60) -> tuple[int, str]:
    import os

    # your whole session's environment: the GitHub login lives in GNOME's keyring, reached over D-Bus
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return r.returncode, (r.stdout + r.stderr).strip()


# How speech-to-text mishears "batsy" ("commit and push the patchy folder"): the wake word's own variants and a
# few more. Only for finding the folder, nothing else.
BATSY = {"patchy", "batchy", "patsy", "bats", "bat see", "bats see", "betsy", "batsie", "bitsy", "batsi", "batty"}


def _batsy(w: str) -> str:
    try:
        from batsy.config import Config

        names = {x.lower() for x in Config.load().get("voice.wake_words") or []}
    except Exception:
        names = set()
    w = re.sub(r"\s+", " ", w).strip()
    return "batsy" if w in BATSY | names else w


def find_repo(what: str | None) -> Path | None:
    """'batsy' / 'the robot project' / 'it' -> the git folder."""
    w = re.sub(r"\b(?:the|my|our|folder|repo|repository|project|changes|in|it'?s|one|please|uh|um)\b", " ",
               (what or "").lower().strip(" .!?"))
    w = _batsy(re.sub(r"[^a-z0-9 -]", "", w).strip())
    if not w or w in ("it", "that", "this", "everything", "all"):
        return Path(_last["path"]) if _last.get("path") else None
    try:
        cands = [c for c in json.loads((DATA_DIR / "projects_index.json").read_text())["candidates"] if c.get("path")]
    except (OSError, ValueError, KeyError):
        cands = []
    from rapidfuzz import fuzz, process

    hit = process.extractOne(w, [c["name"] for c in cands], scorer=fuzz.WRatio)
    if hit and hit[1] >= 80:
        p = Path(cands[hit[2]]["path"])
        if (p / ".git").exists():
            return p
    p = Path.home() / w.replace(" ", "-")
    return p if (p / ".git").exists() else None


def _named_in(n: str) -> str | None:
    """A project folder named anywhere in the sentence ("…the changes in the batsy folder and then…")."""
    try:
        names = [c["name"] for c in json.loads((DATA_DIR / "projects_index.json").read_text())["candidates"]
                 if c.get("path")]
    except (OSError, ValueError, KeyError):
        return None
    n = re.sub(r"\b(?:" + "|".join(sorted(map(re.escape, BATSY), key=len, reverse=True)) + r")\b", "batsy", n)
    hits = [x for x in names if len(x) > 2 and re.search(rf"\b{re.escape(x.lower())}\b", n)]
    return max(hits, key=len) if hits else None


def message(path: Path, cfg) -> str:
    """The commit message, written by the local model from what's staged (never by Claude)."""
    _, stat = _git(path, "diff", "--cached", "--stat=100")
    _, diff = _git(path, "diff", "--cached", "--unified=0", "--no-color")
    files = [l.split("|")[0].strip() for l in stat.splitlines() if "|" in l]
    # Small but telling: the model reads ~15 words a second, so 5000 characters of raw diff took 2-3 minutes.
    # Per file, the functions that changed (git names them in each hunk header) and a few of the added lines.
    brief, cur = [], None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
            brief.append(f"\n{cur}:")
        elif line.startswith("@@") and cur:
            fn = line.split("@@")[-1].strip()
            if fn and fn[:60] not in "".join(brief[-6:]):
                brief.append(f"  in {fn[:60]}")
        elif line.startswith("+") and not line.startswith("+++") and len(line.strip()) > 12 and \
                sum(1 for b in brief if b.startswith("    +")) < 24:
            brief.append("    " + line[:90])
    brief_text = "\n".join(brief)[:1800]
    fallback = f"Update {', '.join(Path(f).name for f in files[:4])}" + (f" and {len(files) - 4} more" if len(files) > 4 else "")
    try:
        from batsy import vault_daily

        vault_daily._cfg = vault_daily._cfg or cfg
        out = vault_daily.local_model(
            "Write a git commit message for these changes. First line: a summary in the imperative, at most 70 "
            "characters. Then a blank line and 2 to 5 short '- ' bullet lines saying what changed. Plain text only, "
            f"no quotes, no AI credit.\n\nFiles:\n{stat[-600:]}\n\nWhat changed (functions, some added lines):"
            f"{brief_text}", 140)
        msg = out.strip().strip("`").strip()
    except Exception as e:
        log.info("local model couldn't write the message: %s", e)
        msg = ""
    lines = [l.strip() for l in msg.splitlines()
             if not re.search(r"co-authored-by|generated with|claude|anthropic", l, re.I)]
    msg = "\n".join(lines).strip()
    if not msg or len(msg.splitlines()[0]) > 100:
        msg = fallback
    return msg


def run(app, path: Path, push: bool) -> None:
    from batsy import activity

    with activity.agent(f"{'Commit & push' if push else 'Commit'} {path.name}", "git") as job:
        _run(app, path, push, job)


def _run(app, path: Path, push: bool, job) -> None:
    """Stage, write the message, commit, push: on a thread (the local model takes a moment)."""
    _last["path"] = str(path)
    name = path.name
    job.step("Checking what changed")
    code, status = _git(path, "status", "--porcelain")
    if code:
        app.announce(f"{name} isn't a git folder I can use: {status[:120]}")
        return
    if not status and not push:
        app.announce(f"Nothing to commit in {name}.")
        return
    if status:
        job.step("Staging the changes")
        _git(path, "add", "-A")
        n = len([l for l in _git(path, "diff", "--cached", "--name-only")[1].splitlines() if l.strip()])
        app.announce(f"Staged {n} file{'s' if n != 1 else ''} in {name}. Writing the commit message.")
        job.step(f"Writing the commit message for {n} file{'s' if n != 1 else ''} (local AI)")
        msg = message(path, app.cfg)
        first = msg.splitlines()[0].rstrip(".")
        # you said commit (and push): no "are you sure?" - just say what it's called ("undo the commit" takes it back)
        app.announce(f"Committing {n} file{'s' if n != 1 else ''} as: {first}.")
        job.step(f"Committing: {first}")
        code, out = _commit(path, msg)
        if code:
            app.announce(f"The commit failed: {out[-200:]}")
            return
    if push:
        job.step("Pushing to GitHub")
        code, out = _git(path, "push", "-q", "origin", "HEAD", timeout=180)
        if code:
            app.announce(f"Committed, but the push failed: {out[-200:]}")
            return
        job.result("Committed and pushed" if status else "Pushed")
        app.announce(f"Committed and pushed {name}." if status else f"Pushed {name}.")
    else:
        app.announce(f"Committed {name}. Say push it when you want it on GitHub.")


def _commit(path: Path, msg: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", str(path), "commit", "-q", "-F", "-"], input=msg, capture_output=True, text=True,
                       timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


UNDO = re.compile(PRE + r"(?:undo|revert|take back|cancel) (?:the |that |my )?(?:last )?commit(?: (?:in|on|for) " + REPO + r")?$")


def undo(path: Path) -> str:
    """The last commit, taken back (its changes stay in the folder) - unless it's already on GitHub."""
    code, ahead = _git(path, "rev-list", "--count", "@{u}..HEAD")
    if code == 0 and ahead.strip() == "0":
        return "That commit is already pushed to GitHub, so I'll leave it."
    code, out = _git(path, "reset", "--soft", "HEAD~1")
    return f"Undone: the changes are back in {path.name}, uncommitted." if code == 0 else f"I couldn't undo it: {out[-120:]}"


# Batsy just asked "Which folder?": the next short reply is the answer ("the batsy folder"), not a new request
# (it went to the local AI, which ran git in the wrong place).
_asked = {"t": 0.0, "push": False, "undo": False, "tries": 0}


def _ask(push: bool = False, undo_: bool = False, text: str = "Which folder? Say, for example, commit and push the "
         "batsy folder.") -> str:
    _asked.update(t=time.time(), push=push, undo=undo_, tries=_asked["tries"] + 1 if _asked["t"] else 1)
    return text


def _answer(app, n: str) -> str | None:
    """The folder, in reply to "Which folder?". None if this isn't that."""
    if time.time() - _asked["t"] > 90 or len(n.split()) > 8:
        return None
    if re.match(r"(?:never ?mind|cancel|stop|forget it|no)\b", n):
        _asked["t"] = 0.0
        return "Okay."
    path = find_repo(n)
    if path is None:
        if _asked["tries"] >= 2:
            _asked.update(t=0.0, tries=0)
            return "I couldn't find that folder."
        try:
            from rapidfuzz import process

            names = [c["name"] for c in json.loads((DATA_DIR / "projects_index.json").read_text())["candidates"]
                     if c.get("path") and (Path(c["path"]) / ".git").exists()]
            near = [x[0] for x in process.extract(n, names, limit=2)]
        except Exception:
            near = []
        return _ask(_asked["push"], _asked["undo"], f"Which one, {' or '.join(near)}?" if near else "Which folder?")
    # what you say now counts: "commit and push the batsy folder" as the answer still means push
    push, undo_ = _asked["push"] or bool(re.search(r"\bpush", n)), _asked["undo"]
    _asked.update(t=0.0, tries=0)
    if undo_:
        return undo(path)
    threading.Thread(target=run, args=(app, path, push), daemon=True, name="git").start()
    return f"On it: {'committing and pushing' if push else 'committing'} {path.name}."


def handle(app, n: str) -> str | None:
    # "comment and push the batsy folder": how the mic hears "commit" in a git command
    n = re.sub(r"\bcomm?ents?\b(?=.*\b(?:push|folder|repo|changes|batsy)\b)", "commit", n)
    said = _answer(app, n)
    if said:
        return said
    m = UNDO.match(n)
    if m:
        path = find_repo(m.group("what") or "it")
        return undo(path) if path else _ask(undo_=True, text="Which folder?")
    m = COMMIT.match(n) or COMMIT_ALT.match(n)
    push = bool(m and (m.groupdict().get("push") or m.groupdict().get("push2") or " push" in n))
    what = m.group("what") if m else None
    if not m:
        m = PUSH.match(n)
        if m:
            push, what = True, m.group("what")
        elif ANY_GIT.search(n):  # a long sentence about committing: Batsy's git, never Claude or the local model
            push = bool(re.search(r"\bpush", n))
            what = _named_in(n) or "it"
        else:
            return None
    path = find_repo(what)
    if path is None:
        return _ask(push)
    threading.Thread(target=run, args=(app, path, push), daemon=True, name="git").start()
    return f"On it: {'committing and pushing' if push else 'committing'} {path.name}."
