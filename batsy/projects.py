"""Your projects: Batsy remembers what you're working on, finds it on GitHub and on the laptop from however you
describe it, and gives you updates on it.

- "I'm doing a project on a line following robot" -> tracked, and matched to the repo / folder it most likely is
  (by name, description, topics, README and the files in it), even though you never said its exact name.
- "Give me an update on my robot project" / "how's the portfolio going" -> commits (on GitHub and local, unpushed
  ones too), uncommitted changes, recently edited files, open issues and pull requests, and your own notes.
- "Find my thermodynamics notes" / "where's the STL for the phone stand" -> files found by what they're about,
  not just their exact name.

GitHub: public repos work with just your username ([github] user, or detected from your local clones). For
private repos put GITHUB_TOKEN=... in ~/.config/batsy/env (or have `gh auth login` done). All matching is local;
nothing is sent to an AI unless two candidates are too close to call and Claude is available.
"""
import datetime as dt
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from batsy.config import DATA_DIR, HOME

log = logging.getLogger("batsy.projects")
INDEX_FILE = DATA_DIR / "projects_index.json"
INDEX_MAX_AGE = 6 * 3600
API = "https://api.github.com"

# A folder holding any of these is a project (and isn't searched any deeper).
MARKERS = {".git", "package.json", "pyproject.toml", "setup.py", "requirements.txt", "Cargo.toml", "go.mod",
           "pom.xml", "build.gradle", "CMakeLists.txt", "platformio.ini", "Makefile", "main.tex", "Gemfile",
           "composer.json", "pubspec.yaml", "Project.toml", ".project"}
PROJECT_EXTS = {".ino": "arduino", ".blend": "3d", ".f3d": "3d", ".fcstd": "3d", ".scad": "3d", ".stl": "3d",
                ".3mf": "3d", ".step": "3d", ".stp": "3d", ".obj": "3d", ".kicad_pro": "electronics",
                ".tex": "latex", ".ipynb": "notebook", ".sldprt": "3d", ".fbx": "3d", ".gcode": "3d"}
LANG_EXTS = {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "react", ".jsx": "react",
             ".java": "java", ".c": "c", ".cpp": "c++", ".h": "c", ".rs": "rust", ".go": "go", ".kt": "kotlin",
             ".swift": "swift", ".dart": "flutter", ".html": "web", ".css": "web", ".m": "matlab", ".r": "r",
             ".cs": "c#", ".php": "php", ".rb": "ruby", ".sql": "sql", ".vue": "vue", ".svelte": "svelte"}
SKIP = {"node_modules", "venv", ".venv", "env", "__pycache__", "site-packages", "dist", "build", "target", "snap",
        "flatpak", "Library", "Applications", "Music", "Movies", "Pictures", "Videos", ".cache", ".local", ".npm",
        ".cargo", ".rustup", "go", ".gradle", ".m2", "Trash", ".Trash", "miniconda3", "anaconda3",
        # other people's code vendored into yours (git submodules, Arduino libraries)
        "lib", "libs", "vendor", "third_party", "external", "deps", "libraries", "submodules", "Pods"}
STOP = set("""a an the my our this that these those i im i'm am is are was were be been being of on in for to
with and or about called named it its project projects repo repository code app thing stuff one some new old
working work doing do does did building build making make made on at by from up so just like kind sort
little big called which where what who how""".split())
SYNONYMS = {
    "robot": ["robotics", "bot", "rover", "arm"], "bot": ["robot", "chatbot"], "website": ["site", "web", "webpage"],
    "site": ["website", "web"], "portfolio": ["personal", "website", "resume"], "ml": ["machine", "learning", "model"],
    "ai": ["ml", "llm", "model", "agent"], "game": ["unity", "godot", "pygame"], "3d": ["model", "stl", "blender"],
    "printer": ["print", "3d", "printing"], "drone": ["quadcopter", "uav", "flight"], "car": ["vehicle", "rc"],
    "chat": ["chatbot", "messaging", "bot"], "assistant": ["agent", "bot", "voice"], "voice": ["speech", "audio"],
    "line": ["follower", "following"], "follower": ["line", "following"], "weather": ["forecast", "climate"],
    "notes": ["note", "notebook"], "attendance": ["face", "recognition"], "iot": ["esp32", "arduino", "sensor"],
    "docs": ["documentation", "doc", "documents"], "doc": ["documentation", "docs"], "documentation": ["docs", "doc"],
    "scraper": ["scrapper", "scrape", "scraping", "crawler"], "twitter": ["x", "tweet"], "camera": ["cam", "webcam"],
    "app": ["application", "mobile"], "college": ["licet", "campus", "university"], "booking": ["reservation"],
    "recognition": ["recog", "detection"], "management": ["manager", "system"], "calendar": ["cal", "schedule"],
    "meal": ["food", "meals"], "food": ["meal", "nutrition"], "farm": ["agro", "agriculture"],
    "agriculture": ["agro", "farm", "farming"], "election": ["voting", "vote"], "alumni": ["alumnus", "graduates"],
}

_lock = threading.Lock()
_index: dict | None = None


# --- text matching ------------------------------------------------------------------------------------------------
def words(text: str) -> list[str]:
    """'LineFollower_v2-bot' -> ['line', 'follower', 'v2', 'bot']"""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    out = []
    for w in re.findall(r"[a-z0-9+#]+", text.lower()):
        out.append(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def query_terms(description: str) -> list[str]:
    return list(dict.fromkeys(w for w in words(description) if w not in STOP and len(w) > 1))


def _fields(c: dict) -> list[tuple[float, set[str]]]:
    return [(3.0, set(words(c.get("name", "")))),
            (2.0, set(words(" ".join([c.get("description") or "", " ".join(c.get("topics") or [])])))),
            (1.2, set(words(" ".join(c.get("files") or [])))),
            (1.0, set(words(c.get("readme") or ""))),
            (0.8, set(words(" ".join(c.get("tags") or []) + " " + (c.get("language") or ""))))]


def _hit(t: str, bag: set[str], substr: bool) -> float:
    """1 exact, 0.9 inside a joined-up name ("election" in "licetalumnielection"), 0.75 a close form, else 0."""
    if t in bag:
        return 1.0
    if substr and len(t) >= 4 and any(len(w) > len(t) and t in w for w in bag):
        return 0.9
    if len(t) >= 4 and any(len(w) >= 4 and (w.startswith(t) or t.startswith(w) or
                                            difflib.SequenceMatcher(None, t, w).ratio() >= 0.86) for w in bag):
        return 0.75
    return 0.0


def score(terms: list[str], c: dict) -> float:
    """0..~3: how well a candidate matches what you said. Name hits count most, then description, files, README;
    matching more of what you said beats one strong word ("voice assistant that runs my laptop" is batsy, whose
    README says all of it, not AgroVoiceAI, whose name says "voice")."""
    if not terms:
        return 0.0
    fields = _fields(c)
    names = fields[0][1]
    best = []
    for t in terms:
        b = 0.0
        for i, (weight, bag) in enumerate(fields):
            h = _hit(t, bag, substr=i == 0)
            if not h:
                h = 0.5 * max((_hit(syn, bag, substr=i == 0) for syn in SYNONYMS.get(t, [])), default=0.0)
            b = max(b, weight * h)
        best.append(b)
    # "flight controller" -> "fc", "enterprise resource planning" -> "erp": initials that are a word of the name
    for size in (2, 3):
        for i in range(len(terms) - size + 1):
            if "".join(t[0] for t in terms[i:i + size]) in names:
                for j in range(i, i + size):
                    best[j] = max(best[j], 3.0 * 0.9)
    covered = sum(1 for b in best if b > 0) / len(terms)
    s = sum(best) / len(terms) * (0.4 + 0.6 * covered)
    age = time.time() - (c.get("touched") or 0)
    return s + (0.15 if age < 14 * 86400 else 0.05 if age < 60 * 86400 else 0.0)  # what you're on lately


# --- the local side -------------------------------------------------------------------------------------------------
def _git(path: str | Path, *args: str, timeout: int = 8) -> str:
    if not shutil.which("git"):
        return ""
    try:
        p = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _readme(path: Path, limit: int = 1500) -> str:
    for name in ("README.md", "readme.md", "README.MD", "README.txt", "README", "README.rst"):
        f = path / name
        if f.is_file():
            try:
                return f.read_text(errors="replace")[:limit]
            except OSError:
                return ""
    return ""


def _describe_folder(path: Path, entries: list[os.DirEntry]) -> dict:
    files, tags, langs, newest = [], set(), {}, 0.0
    for e in entries[:400]:
        name = e.name
        try:
            newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
        except OSError:
            pass
        if name.startswith("."):
            continue
        files.append(name)
        ext = os.path.splitext(name)[1].lower()
        if ext in PROJECT_EXTS:
            tags.add(PROJECT_EXTS[ext])
        if ext in LANG_EXTS:
            langs[LANG_EXTS[ext]] = langs.get(LANG_EXTS[ext], 0) + 1
    for sub in ("src", "app", "lib", "models", "sketches"):  # a peek one level down: that's where the code is
        d = path / sub
        if d.is_dir():
            try:
                for e in list(os.scandir(d))[:80]:
                    files.append(e.name)
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext in LANG_EXTS:
                        langs[LANG_EXTS[ext]] = langs.get(LANG_EXTS[ext], 0) + 1
                    if ext in PROJECT_EXTS:
                        tags.add(PROJECT_EXTS[ext])
            except OSError:
                pass
    remote = _git(path, "config", "--get", "remote.origin.url") if (path / ".git").exists() else ""
    language = max(langs, key=langs.get) if langs else ""
    return {"source": "local", "name": path.name, "path": str(path), "files": files[:60], "tags": sorted(tags),
            "language": language, "readme": _readme(path), "remote": remote, "touched": newest,
            "repo": _repo_from_remote(remote)}


def _repo_from_remote(url: str) -> str:
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else ""


# Folders that hold projects rather than being one.
CONTAINERS = {"documents", "desktop", "downloads", "projects", "code", "dev", "src", "repos", "git", "github",
              "workspace", "work", "arduino", "libraries", "my documents", "onedrive", "dropbox", "google drive",
              "icloud drive", "college", "university", "school", "personal", "temp", "tmp", "old", "archive"}


def _looks_like_project(d: Path, entries: list[os.DirEntry], names: set[str]) -> bool:
    if names & MARKERS:
        return True
    if d.name.lower() in CONTAINERS or d.name.lower().startswith("documents"):
        return False
    files = [e.name for e in entries if not e.name.startswith(".") and e.is_file(follow_symlinks=False)]
    own = [f for f in files if os.path.splitext(f)[1].lower() in PROJECT_EXTS or os.path.splitext(f)[1].lower() in LANG_EXTS]
    return bool(own) and any(os.path.splitext(f)[1].lower() in PROJECT_EXTS for f in own) and len(own) >= 0.25 * len(files)


def scan_local(roots: list[Path] | None = None, max_depth: int = 5, budget_s: float = 15.0) -> list[dict]:
    """Project folders under your home: anything with a .git, package.json, pyproject, .ino, .blend, .tex...
    Repos inside other repos (a folder of all your clones that is itself a repo) are found too."""
    roots = roots or [HOME]
    found, seen, t0 = [], set(), time.time()

    def walk(d: Path, depth: int, inside: bool) -> None:
        if time.time() - t0 > budget_s or depth > max_depth:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        names = {e.name for e in entries}
        project = depth > 0 and (".git" in names if inside else _looks_like_project(d, entries, names))
        if project:
            real = os.path.realpath(d)
            if real not in seen:
                seen.add(real)
                found.append(_describe_folder(d, entries))
        if project and ".git" not in names:
            return  # a plain project folder: its subfolders are its own business
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP:
                walk(Path(e.path), depth + 1, inside or project)  # inside a repo, only nested repos count

    for r in roots:
        if r.is_dir():
            walk(r, 0, False)
    # a repo that mostly wraps other repos (~/code as one big repo) is a container, not a project
    paths = [c["path"] for c in found]
    return [c for c in found if sum(1 for p in paths if p != c["path"] and p.startswith(c["path"] + os.sep)) < 3]


# --- GitHub ---------------------------------------------------------------------------------------------------------
def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not tok and shutil.which("gh"):
        try:
            tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            tok = ""
    return tok


def _get(path: str, raw: bool = False, timeout: int = 8):
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "User-Agent": "batsy", "X-GitHub-Api-Version": "2022-11-28"})
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return body.decode(errors="replace") if raw else json.loads(body)


def github_user(cfg=None, local: list[dict] | None = None) -> str:
    """Your GitHub username: from the config, your token, or the owner of most of your local clones."""
    if cfg is not None:
        try:
            if cfg.get("github.user"):
                return cfg.get("github.user")
        except KeyError:
            pass
    if _token():
        try:
            return _get("/user").get("login", "")
        except (urllib.error.URLError, OSError, ValueError):
            pass
    owners: dict[str, int] = {}
    for c in local or []:
        if c.get("repo"):
            o = c["repo"].split("/")[0]
            owners[o] = owners.get(o, 0) + 1
    if shutil.which("git"):  # your git identity beats counting clones (you clone friends' repos too)
        for key in ("github.user", "user.name"):
            name = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True).stdout.strip()
            if name and " " not in name and (key == "github.user" or name.lower() in (o.lower() for o in owners)):
                return next((o for o in owners if o.lower() == name.lower()), name)
    return max(owners, key=owners.get) if owners else ""


def scan_github(user: str) -> list[dict]:
    if not user and not _token():
        return []
    try:
        if _token():
            repos = _get("/user/repos?per_page=100&sort=pushed&affiliation=owner,collaborator")
        else:
            repos = _get(f"/users/{user}/repos?per_page=100&sort=pushed")
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("GitHub repos unavailable: %s", e)
        return []
    out = []
    for r in repos if isinstance(repos, list) else []:
        pushed = r.get("pushed_at") or ""
        try:
            touched = dt.datetime.fromisoformat(pushed.replace("Z", "+00:00")).timestamp()
        except ValueError:
            touched = 0
        out.append({"source": "github", "name": r.get("name", ""), "repo": r.get("full_name", ""),
                    "description": r.get("description") or "", "topics": r.get("topics") or [],
                    "language": (r.get("language") or "").lower(), "url": r.get("html_url", ""),
                    "private": r.get("private", False), "branch": r.get("default_branch") or "main",
                    "touched": touched, "readme": "", "fork": r.get("fork", False)})
    return out


def github_readme(repo: str) -> str:
    try:
        return _get(f"/repos/{repo}/readme", raw=True, timeout=4)[:1500]
    except (urllib.error.URLError, OSError, ValueError):
        return ""


# --- the index --------------------------------------------------------------------------------------------------------
def _merge(local: list[dict], remote: list[dict]) -> list[dict]:
    """One candidate per project: a local clone and its GitHub repo become one entry with both. Second clones and
    "name_copy_2025..." backups are left out."""
    by_repo = {c["repo"].lower(): c for c in remote if c.get("repo")}
    out, clones = [], set()
    for c in local:
        if c.get("repo"):
            if c["repo"].lower() in clones:
                continue
            clones.add(c["repo"].lower())
        if re.search(r"(?:[ _-]copy(?:[ _-]?\d+)*|[ _-]backup|[ _-]old| \(\d+\))$", c["name"], re.I):
            continue
        r = by_repo.pop(c["repo"].lower(), None) if c.get("repo") else None
        if r:
            c = {**r, **{k: v for k, v in c.items() if v}, "source": "both", "url": r.get("url"),
                 "description": r.get("description"), "topics": r.get("topics"),
                 "touched": max(r.get("touched") or 0, c.get("touched") or 0)}
        out.append(c)
    out += [r for r in by_repo.values() if not r.get("fork")]
    return out


def build_index(cfg=None, save: bool = True) -> dict:
    local = scan_local(_roots(cfg))
    user = github_user(cfg, local)
    remote = scan_github(user)
    old = {c.get("repo"): c.get("readme") for c in (_index or {}).get("candidates", []) if c.get("repo")}
    idx = {"t": time.time(), "user": user, "candidates": _merge(local, remote)}
    for c in idx["candidates"]:  # READMEs fetched earlier from GitHub are kept across refreshes
        if not c.get("readme") and old.get(c.get("repo")):
            c["readme"] = old[c["repo"]]
    if save:
        _save(idx)
    log.info("project index: %d candidates (GitHub user %s)", len(idx["candidates"]), user or "unknown")
    return idx


def _save(idx: dict) -> None:
    try:
        INDEX_FILE.write_text(json.dumps(idx))
    except OSError:
        pass


def _roots(cfg) -> list[Path] | None:
    try:
        roots = cfg.get("projects.roots") if cfg is not None else None
    except KeyError:
        roots = None
    return [Path(r).expanduser() for r in roots] if roots else None


def index(cfg=None, wait: bool = True) -> dict:
    """The cached index; refreshed in the background when it's old (built now only if there's none at all)."""
    global _index
    with _lock:
        if _index is None and INDEX_FILE.exists():
            try:
                _index = json.loads(INDEX_FILE.read_text())
            except (OSError, ValueError):
                _index = None
        stale = _index is None or time.time() - _index.get("t", 0) > INDEX_MAX_AGE
    if _index is None:
        if not wait:
            return {"candidates": [], "user": ""}
        idx = build_index(cfg)
        with _lock:
            _index = idx
    elif stale:
        refresh_soon(cfg)
    return _index


_refreshing = threading.Event()


def refresh_soon(cfg=None) -> None:
    if _refreshing.is_set():
        return
    _refreshing.set()

    def go():
        global _index
        try:
            idx = build_index(cfg)
            with _lock:
                _index = idx
        except Exception:
            log.exception("project index refresh")
        finally:
            _refreshing.clear()

    threading.Thread(target=go, daemon=True, name="project-index").start()


def spoken_names(limit: int = 40) -> list[str]:
    """Project names as you'd say them ("line follower", "Gotham UI"), for speech recognition hotwords."""
    names = [t["title"] for t in Store().all()]
    try:
        idx = json.loads(INDEX_FILE.read_text()) if INDEX_FILE.exists() else {}
    except (OSError, ValueError):
        idx = {}
    cands = sorted(idx.get("candidates", []), key=lambda c: c.get("touched") or 0, reverse=True)
    names += [" ".join(words(c["name"])) for c in cands]
    out = []
    for n in names:
        n = " ".join(w for w in n.split() if not re.fullmatch(r"v?\d+", w))
        if 3 <= len(n) <= 30 and n.lower() not in (o.lower() for o in out):
            out.append(n)
    return out[:limit]


# --- identifying "the project I mean" ---------------------------------------------------------------------------------
def identify(description: str, cfg=None, ask_claude=None, wait: bool = True) -> dict:
    """{"best": candidate|None, "confident": bool, "options": [top candidates]} for a vague description.
    wait=False never builds the index on the spot (a first scan takes a few seconds): nothing found instead."""
    idx = index(cfg, wait=wait)
    terms = query_terms(description)
    cands = idx.get("candidates", [])
    if not terms or not cands:
        return {"best": None, "confident": False, "options": []}
    ranked = sorted(((score(terms, c), c) for c in cands), key=lambda x: x[0], reverse=True)
    if not _confident(ranked):  # READMEs of the closest GitHub-only repos (fetched once, kept) can settle it
        need = [c for _, c in ranked[:5] if c["source"] == "github" and not c.get("readme")]
        if need:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(len(need)) as pool:
                for c, text in zip(need, pool.map(lambda c: github_readme(c["repo"]), need)):
                    c["readme"] = text or " "
            _save(idx)
        ranked = sorted(((score(terms, c), c) for _, c in ranked), key=lambda x: x[0], reverse=True)
    top = [(s, c) for s, c in ranked if s >= 0.45][:5]
    if not top:
        return {"best": None, "confident": False, "options": [c for _, c in ranked[:3] if _ > 0.2]}
    best_s, best = top[0]
    confident = _confident(top)
    if not confident and ask_claude and len(top) > 1:
        pick = _claude_pick(description, [c for _, c in top], ask_claude)
        if pick is not None:
            return {"best": pick, "confident": True, "options": [c for _, c in top]}
    return {"best": best, "confident": confident, "options": [c for _, c in top]}


def _confident(ranked: list[tuple[float, dict]]) -> bool:
    if not ranked:
        return False
    best = ranked[0][0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    return best >= 0.9 and best - second >= 0.25 or best >= 1.6 and best - second >= 0.12


def _claude_pick(description: str, options: list[dict], ask_claude) -> dict | None:
    lines = [f"{i + 1}. {label(c)}: {(c.get('description') or '')[:120]} | files: {', '.join((c.get('files') or [])[:12])}"
             f" | README: {' '.join((c.get('readme') or '').split())[:200]}" for i, c in enumerate(options)]
    reply = ask_claude(f'The user described one of their projects as: "{description}". Which of these is it? '
                       f"Reply with only the number, or 0 if none fits.\n" + "\n".join(lines))
    m = re.search(r"\d+", reply or "")
    if m and 1 <= int(m.group()) <= len(options):
        return options[int(m.group()) - 1]
    return None


def label(c: dict) -> str:
    """How to say a candidate: its name and where it lives."""
    where = []
    if c.get("repo"):
        where.append(f"GitHub {c['repo']}")
    if c.get("path"):
        where.append(short_path(c["path"]))
    return f"{c['name']} ({', '.join(where)})" if where else c["name"]


def spoken(c: dict) -> str:
    """How to say where a candidate is, out loud: no paths or slashes ("batsy, on GitHub and in your elco folder")."""
    where = []
    if c.get("repo"):
        owner = c["repo"].split("/")[0]
        mine = owner.lower() == ((_index or {}).get("user") or "").lower()
        where.append("on GitHub" if mine else f"on GitHub under {owner}")
    if c.get("path"):
        where.append(f"in your {Path(c['path']).parent.name or 'home'} folder")
    name = " ".join(words(c.get("name", ""))) if re.search(r"[_-]", c.get("name", "")) else c.get("name", "")
    return f"{name}, " + " and ".join(where) if where else name


def short_path(p: str) -> str:
    return "~/" + os.path.relpath(p, HOME) if str(p).startswith(str(HOME)) else str(p)


# --- tracked projects -------------------------------------------------------------------------------------------------
class Store:
    """Tracked projects live in tasks.db as kind="project" tasks (so check-ins, history and "what are my projects"
    all work), plus a link to the repo / folder they turned out to be."""

    def __init__(self, tasks=None):
        from batsy.tasks import Tasks

        class _Cfg:  # Tasks only reads check-in times from the config
            def get(self, key):
                return None

        self.tasks = tasks or Tasks(_Cfg())
        with self.tasks.lock, self.tasks.db:
            self.tasks.db.execute("CREATE TABLE IF NOT EXISTS project_links (task_id INTEGER PRIMARY KEY, about TEXT, "
                                  "repo TEXT, path TEXT, url TEXT, t REAL, reported REAL DEFAULT 0)")

    def all(self) -> list[dict]:
        with self.tasks.lock:
            rows = self.tasks.db.execute(
                "SELECT t.id, t.title, t.schedule, l.about, l.repo, l.path, l.url, l.reported FROM tasks t "
                "LEFT JOIN project_links l ON l.task_id = t.id WHERE t.active = 1 AND t.kind = 'project' "
                "ORDER BY t.id").fetchall()
        return [dict(r) for r in rows]

    def add(self, title: str, about: str, schedule: dict | None = None) -> int:
        existing = self.find(title)
        if existing and existing["title"].lower() == title.lower():
            return existing["id"]
        tid = self.tasks.add(title, "project", schedule or {"days": []})
        with self.tasks.lock, self.tasks.db:
            self.tasks.db.execute("INSERT OR REPLACE INTO project_links (task_id, about, t) VALUES (?,?,?)",
                                  (tid, about, time.time()))
        return tid

    def link(self, tid: int, cand: dict) -> None:
        with self.tasks.lock, self.tasks.db:
            self.tasks.db.execute("INSERT INTO project_links (task_id, t) VALUES (?, ?) ON CONFLICT(task_id) DO NOTHING",
                                  (tid, time.time()))
            self.tasks.db.execute("UPDATE project_links SET repo = ?, path = ?, url = ? WHERE task_id = ?",
                                  (cand.get("repo") or "", cand.get("path") or "", cand.get("url") or "", tid))

    def reported(self, tid: int) -> None:
        with self.tasks.lock, self.tasks.db:
            self.tasks.db.execute("UPDATE project_links SET reported = ? WHERE task_id = ?", (time.time(), tid))

    def find(self, text: str) -> dict | None:
        projects = self.all()
        terms = query_terms(text)
        if not projects or not terms:
            return projects[0] if len(projects) == 1 and not terms else None
        scored = [(score(terms, {"name": p["title"], "description": p.get("about") or "",
                                 "files": [p.get("repo") or "", os.path.basename(p.get("path") or "")]}), p)
                  for p in projects]
        s, p = max(scored, key=lambda x: x[0])
        return p if s >= 0.6 else None


# --- what's happened on a project -----------------------------------------------------------------------------------
def activity(p: dict, days: int = 7) -> dict:
    """Commits, unpushed and uncommitted work, recently edited files and open issues for a tracked project or a
    candidate. Everything is optional: whatever can be found."""
    since = time.time() - days * 86400
    out = {"commits": [], "unpushed": 0, "uncommitted": 0, "edited": [], "issues": None, "prs": None,
           "branch": "", "pushed": None}
    path = p.get("path")
    if path and Path(path).is_dir():
        if (Path(path) / ".git").exists():
            log_ = _git(path, "log", f"--since={days}.days", "--pretty=%h%x1f%ct%x1f%an%x1f%s", "-n", "30")
            for line in log_.splitlines():
                h, ct, author, subject = (line.split("\x1f") + ["", "", "", ""])[:4]
                out["commits"].append({"sha": h, "t": int(ct or 0), "author": author, "msg": subject, "where": "local"})
            out["uncommitted"] = len([ln for ln in _git(path, "status", "--porcelain").splitlines() if ln.strip()])
            out["branch"] = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
            ahead = _git(path, "rev-list", "--count", "@{u}..HEAD")
            out["unpushed"] = int(ahead) if ahead.isdigit() else 0
        out["edited"] = _recent_files(Path(path), since)
    repo = p.get("repo")
    if repo:
        try:
            iso = dt.datetime.fromtimestamp(since, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            seen = {c["sha"][:7] for c in out["commits"]}
            for c in _get(f"/repos/{repo}/commits?since={iso}&per_page=30"):
                sha = c.get("sha", "")[:7]
                if sha in seen:
                    continue
                info = c.get("commit", {})
                when = info.get("author", {}).get("date", "")
                try:
                    t = int(dt.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    t = 0
                out["commits"].append({"sha": sha, "t": t, "author": info.get("author", {}).get("name", ""),
                                       "msg": info.get("message", "").splitlines()[0][:120], "where": "github"})
            issues = _get(f"/repos/{repo}/issues?state=open&per_page=50")
            out["issues"] = sum(1 for i in issues if "pull_request" not in i)
            out["prs"] = sum(1 for i in issues if "pull_request" in i)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.info("GitHub activity for %s unavailable: %s", repo, e)
    out["commits"].sort(key=lambda c: c["t"], reverse=True)
    return out


def _recent_files(root: Path, since: float, limit: int = 5, max_files: int = 4000) -> list[tuple[float, str]]:
    found, n = [], 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP]
        for f in files:
            n += 1
            if n > max_files:
                break
            if f.startswith("."):
                continue
            fp = os.path.join(dirpath, f)
            try:
                m = os.stat(fp).st_mtime
            except OSError:
                continue
            if m >= since:
                found.append((m, os.path.relpath(fp, root)))
    return sorted(found, reverse=True)[:limit]


def _ago(t: float) -> str:
    d = dt.date.today() - dt.date.fromtimestamp(t)
    if d.days == 0:
        return "today"
    if d.days == 1:
        return "yesterday"
    if d.days < 7:
        return "on " + dt.date.fromtimestamp(t).strftime("%A")
    return f"{d.days} days ago"


def summary(p: dict, act: dict, notes: list[dict] | None = None, days: int = 7) -> tuple[str, str]:
    """(spoken sentence or two, card text) for a project's update."""
    title = p.get("title") or p.get("name") or "your project"
    bits, card = [], []
    commits = act["commits"]
    if commits:
        last = commits[0]
        bits.append(f"{len(commits)} commit{'s' if len(commits) != 1 else ''} in the last {days} days, the latest "
                    f"{_ago(last['t'])}: {last['msg'].rstrip('.')}")
        card += [f"- {_ago(c['t']).capitalize()}: {c['msg']}" for c in commits[:5]]
    elif p.get("repo") or (p.get("path") and (Path(p["path"]) / ".git").exists()):
        bits.append(f"no commits in the last {days} days")
    if act["unpushed"]:
        bits.append(f"{act['unpushed']} not pushed yet")
        card.append(f"Unpushed commits: {act['unpushed']}")
    if act["uncommitted"]:
        bits.append(f"{act['uncommitted']} uncommitted change{'s' if act['uncommitted'] != 1 else ''}")
        card.append(f"Uncommitted changes: {act['uncommitted']}")
    if act["edited"] and not commits:
        names = [os.path.basename(f) for _, f in act["edited"][:3]]
        bits.append("you've been editing " + ", ".join(names))
    if act["edited"]:
        card.append("Recently edited: " + ", ".join(os.path.basename(f) for _, f in act["edited"][:5]))
    if act["issues"]:
        bits.append(f"{act['issues']} open issue{'s' if act['issues'] != 1 else ''}")
    if act["issues"] is not None or act["prs"] is not None:
        card.append(f"Open issues: {act['issues'] or 0} · pull requests: {act['prs'] or 0}")
    if notes:
        n = notes[0]
        bits.append(f"your last note, {_ago(n['t'])}: {(n['note'] or n['status']).rstrip('.!')}")
        card += [f"- Note {dt.date.fromtimestamp(x['t']).strftime('%a %d %b')}: {x['note'] or x['status']}"
                 for x in notes[:3]]
    if not bits:
        where = " I haven't found it on GitHub or the laptop yet." if not (p.get("repo") or p.get("path")) else ""
        return f"Nothing new on {title}.{where}", "\n".join(card) or "No activity found yet."
    where = [x for x in (p.get("repo"), short_path(p["path"]) if p.get("path") else "") if x]
    if where:
        card.insert(0, " · ".join(where))
    spoken = f"On {title}: " + "; ".join(bits[:4]) + "."
    return spoken[0].upper() + spoken[1:], "\n".join(card)


# --- finding files by what they're about -------------------------------------------------------------------------------
FIND_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".txt", ".md", ".tex", ".ipynb", ".py", ".js", ".ts",
             ".tsx", ".jsx", ".c", ".cpp", ".h", ".ino", ".java", ".rs", ".go", ".html", ".css", ".json", ".csv",
             ".png", ".jpg", ".jpeg", ".svg", ".stl", ".obj", ".3mf", ".step", ".stp", ".blend", ".f3d", ".fcstd",
             ".scad", ".gcode", ".glb", ".gltf", ".fbx", ".m", ".kicad_pro", ".sql", ".yaml", ".yml", ".toml", ".sh"}
KIND_HINT = {"stl": {".stl"}, "model": {".stl", ".obj", ".3mf", ".step", ".stp", ".blend", ".f3d", ".fcstd", ".glb",
                                       ".gltf", ".fbx", ".scad"}, "3d": {".stl", ".obj", ".3mf", ".step", ".blend", ".f3d"},
             "blend": {".blend"}, "gcode": {".gcode"}, "pdf": {".pdf"}, "notes": {".pdf", ".docx", ".md", ".txt", ".pptx"},
             "slides": {".pptx", ".ppt", ".pdf"}, "presentation": {".pptx", ".ppt", ".pdf"}, "ppt": {".pptx", ".ppt"},
             "document": {".pdf", ".docx", ".doc", ".md", ".txt"}, "doc": {".docx", ".doc", ".pdf"},
             "report": {".pdf", ".docx", ".tex"}, "assignment": {".pdf", ".docx", ".ipynb", ".py", ".tex"},
             "notebook": {".ipynb"}, "sketch": {".ino"}, "code": {".py", ".js", ".ts", ".c", ".cpp", ".ino", ".java",
                                                                 ".rs", ".go", ".tsx", ".jsx", ".m", ".sh"},
             "script": {".py", ".sh", ".js"}, "spreadsheet": {".xlsx", ".csv"}, "image": {".png", ".jpg", ".jpeg", ".svg"},
             "photo": {".png", ".jpg", ".jpeg"}, "picture": {".png", ".jpg", ".jpeg"}, "latex": {".tex"},
             "resume": {".pdf", ".docx", ".tex"}, "cv": {".pdf", ".docx", ".tex"}}
TEXTISH = {".txt", ".md", ".tex", ".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".cpp", ".h", ".ino", ".java", ".rs", ".go",
           ".html", ".css", ".m", ".sql", ".sh", ".scad", ".yaml", ".yml", ".toml", ".csv", ".json"}


def find_files(description: str, limit: int = 5, max_files: int = 60000, budget_s: float = 8.0) -> list[dict]:
    """Files matching what you described: name and folder words first, then (for text and code) what's inside."""
    terms = query_terms(description)
    want = set()
    for w in words(description):
        want |= KIND_HINT.get(w, set())
    terms = [t for t in terms if t not in KIND_HINT and t not in ("file", "folder", "where", "find")]
    if not terms:
        return []
    scored, n, t0 = [], 0, time.time()
    for dirpath, dirs, files in os.walk(HOME):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP]
        if time.time() - t0 > budget_s:
            break
        rel_dir = os.path.relpath(dirpath, HOME)
        dir_words = set(words(rel_dir))
        for f in files:
            n += 1
            if n > max_files:
                break
            ext = os.path.splitext(f)[1].lower()
            if ext not in FIND_EXTS or (want and ext not in want):
                continue
            name_words = set(words(os.path.splitext(f)[0]))
            s = 0.0
            for t in terms:
                if t in name_words:
                    s += 1.0
                elif any(len(w) >= 4 and (w.startswith(t) or t.startswith(w)) for w in name_words if len(t) >= 4):
                    s += 0.7
                elif t in dir_words:
                    s += 0.5
            if s > 0:
                scored.append([s / len(terms), os.path.join(dirpath, f)])
    # look inside the best text/code candidates, plus recently changed ones, for the remaining words
    scored.sort(key=lambda x: x[0], reverse=True)
    t1 = time.time()
    for item in scored[:60]:  # a quick look only: synced folders may download a file just to open it
        p = item[1]
        try:
            st = os.stat(p)
        except OSError:
            continue
        if time.time() - st.st_mtime < 30 * 86400:
            item[0] += 0.1
        if os.path.splitext(p)[1].lower() in TEXTISH and st.st_size < 1_000_000 and time.time() - t1 < 2.0:
            try:
                with open(p, errors="replace") as fh:
                    body = set(words(fh.read(20000)))
                item[0] += 0.3 * sum(1 for t in terms if t in body) / len(terms)
            except OSError:
                pass
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"path": p, "score": round(s, 2)} for s, p in scored[:limit] if s >= 0.5]


# --- talking about projects (local, no AI) ------------------------------------------------------------------------------
_LEAD = (r"(?:i'?m|i am|im|we'?re|we are|i'?ve been|ive been|i have been|i'?ve (?:just )?started|i (?:just )?started|"
         r"i'?m starting|i am starting|i have|i'?ve got|i got|i'?ll be|i will be|i want to|i wanna|i need to|"
         r"i'?m going to|i'?m gonna)")
_DOING = r"(?:doing|working on|work on|building|build|making|make|developing|creating|starting|on|do)"
REGISTER = [
    # "i'm doing a project on a line following robot", "i started a new project called gotham ui"
    re.compile(rf"^(?:(?:hey|so|ok|okay|well|fyi|btw|by the way|remember|remember that|note that|just so you know)\s+)*"
               rf"{_LEAD}\s+(?:currently |now |also |actually )?{_DOING}?\s*(?:on )?(?:a |an |my |the |this |our |one )?"
               rf"(?:new |big |small |little |cool )?(?:[a-z]+ ){{0,3}}?projects?\b"
               rf"(?:\s+(?:it'?s|its|it is|is|called|named|on|about|for|where|which|that|to|of|in|with|which is|"
               rf"that is|where i|and it'?s|and its))*\s+(?P<about>.{{3,}})$"),
    # "my project is a weather app", "my final year project is about attendance with face recognition"
    re.compile(r"^(?:so |ok |okay |well )?(?:my|our) (?:new |current |latest |[a-z]+ ){0,3}projects? "
               r"(?:is going to be|is about|is on|is called|will be|is)\s+(?P<about>.{3,})$"),
    # "i'm building a drone for my college project"
    re.compile(rf"^{_LEAD}\s+{_DOING}\s+(?:a |an |the |my )?(?P<about>.+?)\s+(?:for|as) (?:my|a|our|the) "
               rf"(?:[a-z]+ ){{0,3}}projects?$"),
]
UPDATE = [
    re.compile(r"^(?:(?:hey|so|ok|okay|please|can you|could you|batsy)\s+)*(?:give me|get me|can i get|i want|i need|"
               r"tell me|any|what are|whats|what's|what is|show me)?\s*(?:an? |the |some )?(?:quick |latest |recent )?"
               r"(?:updates?|progress|status|news) (?:on|about|for|of|with) (?:my |the |our )?(?P<what>.+?)$"),
    re.compile(r"^how(?:'s| is| are|s) (?:my |the |our )?(?P<what>.+?) (?:going|coming along|doing|looking|progressing)"
               r"(?: so far| lately| this week)?$"),
    re.compile(r"^what(?:'s| is|s) (?:new|happening|going on|the status|the latest|up) (?:with|on|in|for) "
               r"(?:my |the |our )?(?P<what>.+?)$"),
    re.compile(r"^what (?:have i|did i|have we|did we) (?:done|do|get done|pushed|push|committed|commit|worked on|work on|"
               r"changed|change) (?:on|for|in|with) (?:my |the |our )?(?P<what>.+?)(?: today| this week| lately| recently|"
               r" so far)?$"),
    re.compile(r"^(?P<what>(?:any )?(?:project )?updates?|how are my projects (?:going|doing)|"
               r"what(?:'s| is|s) new (?:on|with) my projects)$"),
]
NOTE = re.compile(r"^(?:quick )?(?:update|progress|note) (?:on|for) (?:my |the )?(?P<what>.+?) projects? (?P<note>.{4,})$")
LIST = re.compile(r"^(?:what|which) projects? (?:am i|are we|do i have|have i got) ?(?:working on|doing|going)?|"
                  r"(?:list|tell me|what are) (?:all )?my projects?$")
FIND_PROJECT = re.compile(r"^(?:(?:can you|could you|please)\s+)*(?P<verb>find|where(?:'s| is|s)|locate|open|pull up|"
                          r"show me|go to) (?:my |the |our )?(?P<what>.+?) (?:project|repo|repository|codebase|"
                          r"project folder|source code|code)(?: folder| on github| on the laptop)?$")
FIND_FILE = re.compile(r"^(?:(?:can you|could you|please|help me)\s+)*(?:find|where(?:'s| is| are|s)|locate|search for|"
                       r"look for|get) (?:me )?(?:my |the |that |a |an |those )?(?P<what>.+?\b(?:files?|notes|pdfs?|"
                       r"stls?|models?|slides|presentations?|ppts?|documents?|docs?|reports?|assignments?|notebooks?|"
                       r"sketch(?:es)?|code|scripts?|spreadsheets?|images?|photos?|pictures?|resume|cv|gcode|"
                       r"blend(?: file)?|drawings?|cad|latex|record))(?: (?:about|for|on|with|of|from|where|that|which|"
                       r"called|named) (?P<about>.+))?$")
YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|correct|right|exactly|that one|thats it|that's it|the first(?: one)?|first"
                 r"(?: one)?|number one|1|one)\b")
SECOND = re.compile(r"^(?:the )?(?:second|2nd|number two|two|other)(?: one)?\b")
THIRD = re.compile(r"^(?:the )?(?:third|3rd|number three|three)(?: one)?\b")
NO = re.compile(r"^(?:no|nope|nah|neither|none|not that|not those|none of them|wrong)\b")
OPEN_IT = re.compile(r"^(?:yes|yeah|yep|sure|ok|okay|please)?\s*(?:open|show)(?: it| that| that one| the first one| this)?"
                     r"(?: please)?$|^(?:yes|yeah|yep|sure)(?: please)?$")
PROJECT_WORDS = re.compile(r"\b(projects?|repos?|repository|codebase|github)\b")
CONNECT = re.compile(r"\s+(?:using|with|that|which|where|so that|to|in|on|for|and it|because|so i)\s+")


def title_from(about: str) -> str:
    """'a line following robot using esp32 and pid' -> 'line following robot'"""
    t = about.strip(" .,!?")
    # "my college on a human recognition system" -> "human recognition system"
    t = re.sub(r"^(?:(?:for|in) )?(?:my |our |the |a )?(?:college|school|class|course|uni|university|semester|lab|team|"
               r"final year|mini|major|minor|group|hackathon)(?: projects?)?\s+(?:on|about|for|where|which is|that is|"
               r"called|named)\s+", "", t)
    for _ in range(2):
        t = re.sub(r"^(?:a|an|the|my|our|this|some|it'?s|its|is|called|named|about|on|for|of)\s+", "", t)
    whole = t
    t = CONNECT.split(" " + t + " ", maxsplit=1)[0].strip()
    if len(t.split()) == 1 and len(whole.split()) > 1:  # "attendance with face recognition": one word says too little
        t = " ".join(CONNECT.split(" " + " ".join(whole.split()[:4]) + " ")[0].split()) \
            if not re.match(r"^\w+ (?:with|in|on)\b", whole) else " ".join(whole.split()[:4])
    t = re.sub(r"\s+(?:project|app|thing|stuff)$", "", t).strip()
    w = t.split()
    return " ".join(w[:5]) if w else about.strip()[:40]


class ProjectTalk:
    """Owned by the app. handle(n, raw) -> True if it was about your projects or finding a file."""

    def __init__(self, app):
        self.app, self.cfg = app, app.cfg
        self.store = Store(getattr(getattr(app, "tasks", None), "store", None))
        self.pending: dict | None = None  # waiting for an answer: which candidate, or "open it?"
        refresh_soon(self.cfg)  # build/refresh the GitHub + local index in the background

    # --- helpers ---------------------------------------------------------------------------------------------
    def _say(self, text: str) -> None:
        self.app._reply(text)

    def _status(self, text: str) -> None:
        try:
            self.app.overlay.send("status", text=text)
        except Exception:
            pass

    def _claude(self):
        """A text-only Claude call to settle a close call between candidates (None if Claude isn't available)."""
        try:
            from batsy import claude_code

            if not shutil.which("claude") or claude_code.over_budget(self.cfg):
                return None
            return lambda prompt: claude_code.run(prompt, "which project", timeout=60, args=["--model", "haiku"])
        except Exception:
            return None

    def _card(self, title: str, body: str) -> None:
        if body:
            self.app.show_visual("card", f"{title} | {body}")

    def _remember(self, p: dict) -> None:
        bits = [f"The user is working on a project: {p['title']}"]
        if p.get("about") and p["about"].lower() != p["title"].lower():
            bits.append(f"({p['about'][:160]})")
        where = [x for x in (f"GitHub {p['repo']}" if p.get("repo") else "",
                             f"folder {short_path(p['path'])}" if p.get("path") else "") if x]
        if where:
            bits.append("- " + ", ".join(where))
        try:
            self.app.brain.memory.add_fact(" ".join(bits))
        except Exception:
            log.exception("remember project")

    # --- the conversation ------------------------------------------------------------------------------------
    def handle(self, n: str, raw: str) -> bool:
        if self.pending and time.time() < self.pending["until"]:
            if self._answer(n):
                return True
        else:
            self.pending = None
        m = NOTE.match(n)
        if m and self.store.find(m.group("what")):
            p = self.store.find(m.group("what"))
            self.store.tasks.log(p["id"], "update", m.group("note"))
            self._say(f"Noted for {p['title']}.")
            return True
        for rx in UPDATE:
            m = rx.match(n)
            if m and self._update(m.group("what"), n):
                return True
        if LIST.match(n):
            return self._list()
        m = FIND_PROJECT.match(n)
        if m and not re.search(r"\b(this|that|these|those)\b", m.group("what")):
            return self._find_project(m.group("what"), opening=m.group("verb") in ("open", "pull up", "go to", "show me"))
        m = FIND_FILE.match(n)
        if m and not re.search(r"\b(this|these|those)\b|\bthat (?:pdf|file|image|document)\b", n) \
                and not re.search(r"\bin (?:the|this|that|my) (?:pdf|file|document|image)\b", n):
            return self._find_file(" ".join(x for x in (m.group("what"), m.group("about")) if x))
        for rx in REGISTER:
            m = rx.match(n)
            if m and not n.startswith(("how ", "what ", "when ", "where ", "why ", "which ", "who ", "is ", "are ",
                                      "do ", "does ", "can ", "could ", "should ")):
                return self._register(m.group("about"), n)
        return False

    def _register(self, about: str, n: str) -> bool:
        from batsy.tasks import parse_schedule

        sched, phrase = parse_schedule(n)
        if phrase:
            about = re.sub(re.escape(phrase), " ", about).strip(" ,")
        about = " ".join(about.split())
        if len(about.split()) < 1 or about in ("it", "this", "that", "something", "stuff"):
            return False
        from batsy.tasks import describe

        title = title_from(about)
        tid = self.store.add(title, about, sched)
        p = next((x for x in self.store.all() if x["id"] == tid), {"id": tid, "title": title, "about": about})
        check = f" and check in {describe(sched)}" if sched else ""
        self._say(f"Got it, I'll remember your {title} project{check}.")  # said now: finding it can take a moment
        self._status("Looking for it on GitHub and the laptop")
        res = identify(f"{about} {title}", self.cfg, self._claude())
        if res["best"] and res["confident"]:
            self.store.link(tid, res["best"])
            p.update(repo=res["best"].get("repo"), path=res["best"].get("path"), url=res["best"].get("url"))
            self._remember(p)
            self._say(f"I think it's {spoken(res['best'])}. Ask me for an update on it any time.")
            self._card(f"Project: {title}", "\n".join(x for x in (about, label(res["best"])) if x))
            return True
        if res["options"]:
            opts = res["options"][:3]
            self.pending = {"kind": "which", "tid": tid, "options": opts, "until": time.time() + 120}
            names = [" ".join(words(o["name"])) for o in opts]
            ask = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
            self._say(f"Is it {ask}?")
            self._card(f"Which one is {title}?", "\n".join(f"- {label(o)}" for o in opts))
            return True
        self._remember(p)
        self._say("I couldn't find it on GitHub or the laptop yet; once there's a repo or folder for it I'll pick it up.")
        return True

    def _answer(self, n: str) -> bool:
        p = self.pending
        if p["kind"] == "which":
            opts = p["options"]
            pick = None
            if NO.match(n):
                self.pending = None
                proj = next((x for x in self.store.all() if x["id"] == p["tid"]), None)
                if proj:
                    self._remember(proj)
                self._say("Okay. I'll keep it in mind and look again when you ask for an update.")
                return True
            if SECOND.match(n) and len(opts) > 1:
                pick = opts[1]
            elif THIRD.match(n) and len(opts) > 2:
                pick = opts[2]
            elif YES.match(n):
                pick = opts[0]
            else:
                scored = [(score(query_terms(n), o), o) for o in opts]
                s, o = max(scored, key=lambda x: x[0])
                pick = o if s >= 0.8 else None
            if pick is None:
                return False
            self.pending = None
            self.store.link(p["tid"], pick)
            proj = next((x for x in self.store.all() if x["id"] == p["tid"]), None)
            if proj:
                self._remember(proj)
            self._say(f"Got it, {pick['name']}. I'll follow it from now on.")
            return True
        if p["kind"] == "open":
            files = p["files"]
            target = files[1] if SECOND.match(n) and len(files) > 1 else files[2] if THIRD.match(n) and len(files) > 2 \
                else files[0] if (OPEN_IT.match(n) or YES.match(n)) else None
            if target is None:
                if NO.match(n):
                    self.pending = None
                    self._say("Okay.")
                    return True
                return False
            self.pending = None
            self._open(target)
            self._say(f"Opening {os.path.basename(target)}.")
            return True
        return False

    def _open(self, path: str) -> None:
        if os.path.isdir(path) and shutil.which("code"):  # a project folder: straight into the editor
            subprocess.Popen(["code", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return
        self.app._open(path)

    def _resolve(self, what: str, n: str = "") -> tuple[dict | None, bool]:
        """A tracked project (linked to its repo/folder if we can), or a candidate from the index. (p, tracked)."""
        p = self.store.find(what)
        if p:
            if not (p.get("repo") or p.get("path")):
                res = identify(f"{p.get('about') or ''} {p['title']}", self.cfg)
                if res["best"] and res["confident"]:
                    self.store.link(p["id"], res["best"])
                    p.update(repo=res["best"].get("repo"), path=res["best"].get("path"), url=res["best"].get("url"))
            return p, True
        sure = bool(PROJECT_WORDS.search(n or what))  # clearly about a project: worth waiting for a first scan
        res = identify(what, self.cfg, self._claude() if sure else None, wait=sure)
        if res["best"] and (res["confident"] or len(res["options"]) == 1 and sure):
            c = dict(res["best"])
            c["title"] = c["name"]
            return c, False
        return None, False

    def _update(self, what: str, n: str) -> bool:
        what = re.sub(r"\b(?:projects?|repos?|repository|codebase)\b", "", what).strip()
        generic = what in ("", "any updates", "updates", "update", "any update", "project updates", "it", "that",
                           "everything", "how are my projects going", "how are my projects doing",
                           "whats new on my projects", "what's new on my projects", "my") or \
            re.fullmatch(r"(any )?( )?updates?|how are my\s+(going|doing)|what'?s new (on|with) my", what) is not None
        projects = self.store.all()
        if generic:
            if not projects:
                if "project" in n or "update" in n:
                    self._say("You haven't told me about any projects yet. Say something like: I'm doing a project on "
                              "a line following robot.")
                    return True
                return False
            self._status("Checking your projects")
            if len(projects) == 1:
                return self._report(projects[0], True)
            lines, cards = [], []
            for p in projects[:4]:
                act = activity(p, 7)
                k = len(act["commits"])
                lines.append(f"{p['title']}: {k} commit{'s' if k != 1 else ''} this week" if k else
                             f"{p['title']}: quiet this week")
                cards.append(f"- {p['title']}: " + (f"{k} commits, latest: {act['commits'][0]['msg']}" if k else
                                                     "no commits this week"))
            self._say("; ".join(lines) + ".")
            self._card("Your projects", "\n".join(cards))
            return True
        self._status("Checking GitHub and the laptop")
        p, tracked = self._resolve(what, n)
        if p is None:
            if PROJECT_WORDS.search(n):
                self._say(f"I don't know a project like {what} yet. Tell me about it, like: I'm doing a project on {what}.")
                return True
            return False  # "how's my gym going": a chore or habit, for the task tracker
        return self._report(p, tracked)

    def _report(self, p: dict, tracked: bool) -> bool:
        act = activity(p, 7)
        notes = []
        if tracked:
            notes = [{"t": h["t"], "note": h["note"], "status": h["status"]} for h in self.store.tasks.history(p["id"])
                     if h["status"] in ("update", "done") and h["note"]]
        spoken, card = summary(p, act, notes)
        self._say(spoken)
        self._card(p.get("title") or p.get("name"), card)
        if tracked:
            self.store.reported(p["id"])
        return True

    def _list(self) -> bool:
        projects = self.store.all()
        if not projects:
            self._say("You haven't told me about any projects yet. Say something like: I'm doing a project on a "
                      "weather app.")
            return True
        self._say("You're working on " + ", ".join(p["title"] for p in projects[:-1]) +
                  (" and " if len(projects) > 1 else "") + projects[-1]["title"] + ".")
        self._card("Your projects", "\n".join(
            f"- {p['title']}" + (f" · {p['repo']}" if p.get("repo") else "") +
            (f" · {short_path(p['path'])}" if p.get("path") else "") for p in projects))
        return True

    def _find_project(self, what: str, opening: bool) -> bool:
        self._status("Looking for it")
        p, _ = self._resolve(what, "project")
        if p is None:
            res = identify(what, self.cfg)
            if res["options"]:
                opts = res["options"][:3]
                self._say("I'm not sure which one. It could be " + " or ".join(o["name"] for o in opts) + ".")
                self._card(f"Projects like {what}", "\n".join(f"- {label(o)}" for o in opts))
                return True
            self._say(f"I couldn't find a project like {what} on GitHub or the laptop.")
            return True
        name = p.get("title") or p.get("name")
        if opening:
            if p.get("path"):
                self._open(p["path"])
            elif p.get("url"):
                self.app._open(p["url"])
            self._say(f"Opening {name}.")
            return True
        where = [x for x in (f"GitHub: {p['repo']}" if p.get("repo") else "",
                             f"Folder: {short_path(p['path'])}" if p.get("path") else "") if x]
        self._say(f"It's {spoken(p)}." if where else f"I know {name}, but not where it lives yet.")
        self._card(name, "\n".join(where))
        return True

    def _find_file(self, description: str) -> bool:
        self._status("Searching your files")
        hits = find_files(description)
        if not hits:
            self._say("I couldn't find a file like that.")
            return True
        files = [h["path"] for h in hits[:3]]
        first = files[0]
        self.pending = {"kind": "open", "files": files, "until": time.time() + 120}
        folder = Path(first).parent.name if Path(first).parent != HOME else "home"
        self._say(f"Found {os.path.basename(first)}, in your {folder} folder. Want me to open it?")
        self._card("Found", "\n".join(f"- {os.path.basename(f)} · {os.path.dirname(short_path(f))}" for f in files))
        return True

    # --- used by check-ins and the morning briefing --------------------------------------------------------------
    def checkin_hint(self, task_id: int) -> str:
        """'I saw 2 commits today. ' before "How did the robot project go today?" (empty if nothing to add)."""
        p = next((x for x in self.store.all() if x["id"] == task_id), None)
        if not p or not (p.get("repo") or p.get("path")):
            return ""
        act = activity(p, 1)
        k = len(act["commits"])
        if k:
            return f"I saw {k} commit{'s' if k != 1 else ''} on it today. "
        if act["uncommitted"]:
            return f"You've got {act['uncommitted']} uncommitted changes there. "
        return ""

    def morning_news(self) -> str:
        bits = []
        for p in self.store.all()[:4]:
            if not (p.get("repo") or p.get("path")):
                continue
            act = activity(p, 1)
            if act["commits"]:
                bits.append(f"{p['title']} got {len(act['commits'])} commit{'s' if len(act['commits']) != 1 else ''}")
        return (" Yesterday " + " and ".join(bits) + ".") if bits else ""
