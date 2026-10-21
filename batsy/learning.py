"""Learn once, run locally forever after.

The first time Batsy meets a new kind of *action* ("turn on dark mode", "connect my earbuds"), Claude Code
writes a small Python skill for it. Batsy checks and runs that skill and saves it. After that, the same kind
of request (any phrasing in its TRIGGERS) runs the code directly: no AI, about as fast as the action itself.
Feedback closes the loop: a complaint ("no, that's wrong") or a crash counts as a failure and Claude rewrites
the skill with the complaint; two failures in a row disable it until it's relearned; each clean use counts
as a success.
"""
import ast
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from batsy import safety
from batsy.config import DATA_DIR, SKILLS_DIR, WORKSPACE
from batsy.vocab import compile_template

log = logging.getLogger("batsy.learning")
DB = DATA_DIR / "skills.db"

# Imperative requests worth learning as code. Questions and chat go to the AI as usual.
ACTION = re.compile(
    r"^(?:please |can you |could you )?(open|close|set|turn|switch|enable|disable|start|stop|connect|disconnect|"
    r"increase|decrease|raise|lower|mute|unmute|lock|take|check|run|install|update|upgrade|download|move|copy|"
    r"delete|remove|rename|create|make|change|put|send|clean|clear|empty|toggle|pair|restart|kill|record|"
    r"backup|back up|sync|mount|eject|organi[sz]e|sort|compress|zip|unzip|convert|resize|print|show me my|"
    r"find my|go to|dim|brighten|activate|deactivate)\b")
NEGATIVE = re.compile(r"^(no|nope|wrong|that'?s wrong|that'?s not (it|right|what i (asked|wanted|meant))|"
                      r"(it )?didn'?t work|not working|undo( that)?|you did it wrong|that'?s incorrect|bad)\b")

_RUNNER = r"""
import importlib.util, json, re, subprocess, sys
DANGER = json.loads(sys.argv[3])
def _confirm(reason):
    print("__BATSY_CONFIRM__ " + reason, flush=True)
    return sys.stdin.readline().strip() == "yes"
def sh(cmd, timeout=60):
    for pat, reason in DANGER:
        if re.search(pat, cmd):
            if not _confirm(f"This will {reason}: {cmd[:100]}. Should I go ahead?"):
                raise RuntimeError("The user declined: " + cmd)
            break
    p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {cmd}\n{(p.stderr or p.stdout)[-600:]}")
    return p.stdout.strip()
def notify(text):
    subprocess.Popen(["notify-send", "-a", "Batsy", "Batsy", str(text)])
def show(what):
    print("__BATSY_SHOW__ " + str(what), flush=True)
def open_model(path):
    print("__BATSY_OPEN3D__ " + str(path), flush=True)
spec = importlib.util.spec_from_file_location("skill", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
mod.sh, mod.notify, mod.show, mod.open_model = sh, notify, show, open_model
spec.loader.exec_module(mod)
result = mod.run(**json.loads(sys.argv[2]))
print("__BATSY_RESULT__ " + json.dumps("" if result is None else str(result)), flush=True)
"""

TEACH = """You are teaching Batsy, a voice assistant on the user's Fedora laptop (GNOME, Wayland, PipeWire), to \
handle a kind of request locally next time, with no AI at all.

The user said: "{request}"
{extra}
Skills Batsy already has (name: what it does | some phrasings):
{catalog}

If the user's sentence is just another way of asking for one of those skills, don't write new code. Reply with \
exactly one line: SAME: <skill name> | <template>. The template is the user's sentence, lowercased and without \
punctuation, with that skill's {{slots}} in place of the parts that vary. Only do this if that skill really does \
what they asked: "turn off dark mode" is not the same as "turn on dark mode".

If this is not a repeatable action on the laptop (a question, chat, opinion, something to write or explain, or \
anything whose answer needs thinking each time), reply with "ANSWER:" followed by a spoken answer of at most three \
short sentences, with no markdown.

Otherwise reply with only one Python file in a ```python block:
- A one-line module docstring.
- TRIGGERS: 4-10 lowercase templates for the ways people say this, without punctuation. Use {{slot}} for the parts \
that vary, e.g. "set brightness to {{level}}" or "connect to {{device}}". The user's sentence must match one exactly \
after lowercasing and removing punctuation. Keep templates specific enough not to catch unrelated requests.
- def run(<one keyword argument per slot>) -> str: do the action, then return one short spoken sentence.
- Run commands with sh(cmd): it runs bash, returns stdout and raises on failure. Use notify(text) for a \
desktop notification, show(query) to put a picture on screen, and open_model(path) to open a 3D model file \
(STL, OBJ, GLB, 3MF, STEP...) in Batsy's own 3D viewer (never f3d, meshlab or a browser). Standard library and psutil only. Never call \
subprocess or os.system yourself. Use pkexec for root. Prefer Fedora/GNOME tools: wpctl, brightnessctl, \
playerctl, nmcli, bluetoothctl, gsettings, gtk-launch, xdg-open, loginctl, powerprofilesctl, flatpak, \
gnome-extensions. Validate slot values and give a helpful sentence if something isn't found.
"""

FORBIDDEN_CALLS = {"system", "popen", "Popen", "run", "call", "check_output", "check_call", "spawn", "execv"}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", "", text.lower())).strip()


def restore_case(slots: dict, request: str) -> dict:
    """Matching happens on lowercase text; hand skills the words as the user said them ("Jonathan")."""
    out = {}
    for k, v in slots.items():
        m = re.search(re.escape(v).replace(r"\ ", r"[\s,.!?]+"), request, re.I)
        out[k] = m.group(0) if m else v
    return out


def norm_template(t: str) -> str:
    """Normalise a trigger like norm() but keep its {slots}."""
    parts = re.split(r"(\{\w+\})", t.lower())
    return re.sub(r"\s+", " ", " ".join(p if p.startswith("{") else norm(p) for p in parts)).strip()


def _extract(reply: str) -> tuple[str, str]:
    """Claude's reply -> ("skill", code), ("same", "name | template") or ("answer", text)."""
    same = re.search(r"^\s*SAME:\s*(.+)$", reply, re.M)
    if same and "```" not in reply:
        return "same", same.group(1).strip()
    m = re.search(r"```(?:python)?\n(.*?)```", reply, re.S)
    if m:
        return "skill", m.group(1)
    text = reply.strip()
    if text.upper().startswith("ANSWER:"):
        text = text[7:].strip()
    return "answer", text


def validate(code: str) -> tuple[list[str], str | None]:
    """Static checks. Returns (TRIGGERS, problem or None)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [], f"syntax error: {e}"
    triggers, has_run = [], False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TRIGGERS" for t in node.targets):
            try:
                triggers = [str(t) for t in ast.literal_eval(node.value)]
            except ValueError:
                return [], "TRIGGERS must be a literal list of strings"
    if not has_run:
        return [], "missing def run(...)"
    if not triggers:
        return [], "missing TRIGGERS"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            if any(n.split(".")[0] in ("subprocess", "pty", "ctypes") for n in names):
                return [], "do not import subprocess/pty/ctypes; use sh()"
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALLS and \
                isinstance(node.value, ast.Name) and node.value.id in ("os", "subprocess"):
            return [], f"do not call {node.value.id}.{node.attr}; use sh()"
    return triggers, None


class SkillBook:
    def __init__(self, ask_claude, show=None):
        self.ask_claude = ask_claude      # (prompt) -> reply text, via Claude Code on the Pro plan
        self.show = show                  # (query) -> put a picture on screen
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(DB), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS skills (name TEXT PRIMARY KEY, successes INTEGER DEFAULT 0, "
                            "failures INTEGER DEFAULT 0, streak INTEGER DEFAULT 0, disabled INTEGER DEFAULT 0, "
                            "learned_from TEXT, t REAL)")
        self.reload()

    # --- index -------------------------------------------------------------
    def reload(self) -> None:
        index = []
        for path in sorted(SKILLS_DIR.glob("*.py")):
            triggers, problem = validate(path.read_text())
            if problem:
                continue
            for t in triggers:
                index.append((compile_template(norm_template(t)), path.stem, t))
        index.sort(key=lambda x: len(x[2]), reverse=True)  # most specific first
        self.index = index

    def stats(self, name: str) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()

    def match(self, n: str) -> tuple[str, dict] | None:
        for rx, name, _ in self.index:
            m = rx.match(n)
            if m:
                st = self.stats(name)
                if st and st["disabled"]:
                    continue
                return name, {k: v.strip() for k, v in m.groupdict().items()}
        return None

    # --- running -----------------------------------------------------------
    def run(self, name: str, slots: dict, confirm, timeout: int = 90) -> tuple[str, bool]:
        """Run a skill. Returns (sentence or error, ok)."""
        path = SKILLS_DIR / f"{name}.py"
        danger = json.dumps([[p, r] for p, r in safety._DANGEROUS])
        proc = subprocess.Popen([sys.executable, "-c", _RUNNER, str(path), json.dumps(slots), danger],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=str(Path.home()))
        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        result = None
        try:
            for line in proc.stdout:
                if line.startswith("__BATSY_CONFIRM__ "):
                    ok = confirm(line[18:].strip())
                    proc.stdin.write("yes\n" if ok else "no\n")
                    proc.stdin.flush()
                elif line.startswith("__BATSY_SHOW__ ") and self.show:
                    threading.Thread(target=self.show, args=(line[15:].strip(),), daemon=True).start()
                elif line.startswith("__BATSY_OPEN3D__ "):
                    from batsy.model3d import VIEWER  # the daemon's viewer, so "rotate it" works afterwards

                    reply = VIEWER.open(Path(line[17:].strip()))
                    if "error" in reply:
                        proc.kill()
                        return f"the 3D viewer couldn't open it: {reply['error']}", False
                elif line.startswith("__BATSY_RESULT__ "):
                    result = json.loads(line[17:])
            proc.wait()
        finally:
            killer.cancel()
        err = proc.stderr.read().strip()
        if proc.returncode != 0 or result is None:
            return (err.splitlines()[-1] if err else "it stopped unexpectedly")[:300], False
        return result, True

    def record(self, name: str, ok: bool) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO skills (name, t) VALUES (?, ?)", (name, time.time()))
            if ok:
                self.db.execute("UPDATE skills SET successes = successes + 1, streak = 0 WHERE name = ?", (name,))
            else:
                self.db.execute("UPDATE skills SET failures = failures + 1, streak = streak + 1, "
                                "disabled = CASE WHEN streak + 1 >= 2 THEN 1 ELSE 0 END WHERE name = ?", (name,))

    # --- learning ----------------------------------------------------------
    def learn(self, request: str, confirm, execute: bool = True, happened: str = "", previous: str | None = None,
              complaint: str | None = None, name: str | None = None, attempts: int = 3) -> dict:
        """Ask Claude to write (or fix) a skill for this request.
        Returns {"kind": "answer", "text"} or {"kind": "skill", "name", "spoken", "ok"}."""
        n = norm(request)
        extra = ""
        if happened:
            extra += f"\nHere is what was done to fulfil it this time (turn this into code):\n{happened[:1500]}\n"
        if previous:
            extra += f"\nThe current skill for this:\n```python\n{previous}\n```\n"
        if complaint:
            extra += f"\nIt went wrong. The user said / the error was: {complaint}\nFix it.\n"
        catalog = self.catalog(exclude=name)
        for attempt in range(attempts):
            reply = self.ask_claude(TEACH.format(request=request, extra=extra, catalog=catalog))
            kind, body = _extract(reply)
            if kind == "answer":
                return {"kind": "answer", "text": body}
            if kind == "same":
                problem, slots, existing = self._alias(body, n)
                if problem:
                    extra += f"\nYour SAME reply was rejected: {problem}. Reply again.\n"
                    continue
                if not execute:
                    return {"kind": "skill", "name": existing, "spoken": "", "ok": True}
                spoken, ok = self.run(existing, restore_case(slots, request), confirm)
                return {"kind": "skill", "name": existing, "alias": True, "spoken": spoken if ok else
                        "I tried my existing skill for that, but it failed.", "ok": ok}
            triggers, problem = validate(body)
            rxs = [compile_template(norm_template(t)) for t in triggers]
            m = next((rx.match(n) for rx in rxs if rx.match(n)), None)
            if not problem and m is None:
                problem = f'none of the TRIGGERS match the user\'s sentence "{n}"'
            if problem:
                extra += f"\nYour previous attempt was rejected: {problem}. Reply again with the full corrected file.\n"
                continue
            skill = name or self._name_for(triggers[0])
            path = SKILLS_DIR / f"{skill}.py"
            old = path.read_text() if path.exists() else None
            path.write_text(body)
            if not execute:  # already done this time (by the local AI): first real use is the test
                self._saved(skill, request)
                return {"kind": "skill", "name": skill, "spoken": "", "ok": True}
            spoken, ok = self.run(skill, restore_case({k: v.strip() for k, v in m.groupdict().items()}, request), confirm)
            if ok:
                self._saved(skill, request)
                return {"kind": "skill", "name": skill, "spoken": spoken, "ok": True}
            # failed on the real request: put the old version back and let Claude fix it
            if old is not None:
                path.write_text(old)
            else:
                path.unlink(missing_ok=True)
            previous, extra = body, extra + f"\nRunning it failed with: {spoken}\nReply with the full corrected file.\n"
        self.reload()
        return {"kind": "skill", "name": name or "", "spoken": "I couldn't work out how to do that reliably.",
                "ok": False}

    def catalog(self, exclude: str | None = None, limit: int = 60) -> str:
        """One short line per existing skill, so Claude can spot a new phrasing of something already learned."""
        lines = []
        for path in sorted(SKILLS_DIR.glob("*.py"))[:limit]:
            if path.stem == exclude:
                continue
            code = path.read_text()
            triggers, problem = validate(code)
            if problem:
                continue
            try:
                doc = (ast.get_docstring(ast.parse(code)) or "").splitlines()[0]
            except (SyntaxError, IndexError):
                doc = ""
            lines.append(f"- {path.stem}: {doc} | " + "; ".join(f'"{t}"' for t in triggers[:3]))
        return "\n".join(lines) or "(none yet)"

    def _alias(self, body: str, n: str) -> tuple[str | None, dict, str]:
        """Check a 'SAME: name | template' reply. On success, add the template to that skill's TRIGGERS."""
        name, _, template = (x.strip() for x in body.partition("|"))
        path = SKILLS_DIR / f"{name}.py"
        if not path.exists():
            return f"there is no skill named {name}", {}, name
        template = norm_template(template.strip('"'))
        m = compile_template(template).match(n)
        if not m:
            return f'the template "{template}" does not match the sentence "{n}"', {}, name
        code = path.read_text()
        tree = ast.parse(code)
        run = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == "run")
        params = {a.arg for a in run.args.args + run.args.kwonlyargs}
        slots = {k: v.strip() for k, v in m.groupdict().items()}
        if not set(slots) <= params and run.args.kwarg is None:
            return f"slots {sorted(set(slots) - params)} are not parameters of {name}.run", {}, name
        node = next(x for x in tree.body if isinstance(x, ast.Assign)
                    and any(getattr(t, "id", "") == "TRIGGERS" for t in x.targets))
        triggers = list(ast.literal_eval(node.value))
        if template not in triggers:
            triggers.append(template)
            lines = code.splitlines(keepends=True)
            new = "TRIGGERS = [\n" + "".join(f"    {t!r},\n" for t in triggers) + "]\n"
            path.write_text("".join(lines[:node.lineno - 1]) + new + "".join(lines[node.end_lineno:]))
            self.reload()
            log.info("taught skill %s a new phrasing: %r", name, template)
        return None, slots, name

    def _name_for(self, trigger: str) -> str:
        base = "_".join(w for w in re.sub(r"\{\w+\}", "", trigger).split() if w not in ("the", "a", "my"))[:40] or "skill"
        base = re.sub(r"\W", "_", base).strip("_") or "skill"
        name, i = base, 2
        while (SKILLS_DIR / f"{name}.py").exists():
            name, i = f"{base}_{i}", i + 1
        return name

    def _saved(self, name: str, request: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO skills (name, learned_from, t) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE "
                            "SET disabled = 0, streak = 0, learned_from = excluded.learned_from",
                            (name, request, time.time()))
        self.reload()
        log.info("learned skill %s from %r", name, request)

    def relearn(self, name: str, request: str, complaint: str, confirm) -> dict:
        path = SKILLS_DIR / f"{name}.py"
        previous = path.read_text() if path.exists() else None
        return self.learn(request, confirm, previous=previous, complaint=complaint, name=name)

    def forget(self, name: str) -> bool:
        path = SKILLS_DIR / f"{name}.py"
        existed = path.exists()
        path.unlink(missing_ok=True)
        with self.lock, self.db:
            self.db.execute("DELETE FROM skills WHERE name = ?", (name,))
        self.reload()
        return existed

    def listing(self) -> list[dict]:
        out = []
        for path in sorted(SKILLS_DIR.glob("*.py")):
            triggers, problem = validate(path.read_text())
            st = self.stats(path.stem)
            out.append({"name": path.stem, "triggers": triggers, "problem": problem,
                        "successes": st["successes"] if st else 0, "failures": st["failures"] if st else 0,
                        "disabled": bool(st and st["disabled"])})
        return out


def claude_cli(prompt: str, timeout: int = 180) -> str:
    """Teaching call (Claude only writes text here)."""
    from batsy import claude_code

    out = claude_code.run(prompt, "learning a new skill", cwd=WORKSPACE, timeout=timeout)
    return out if out.strip() else "ANSWER: Claude didn't answer."
