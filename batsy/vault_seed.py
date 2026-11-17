"""Fill a new brain with everything Batsy already knows, so it starts full, not empty:
memory facts, projects, received files and senders, prints, the printer (settings, presets, what to change),
the music taste profile, and daily notes for the past 30 days rebuilt from conversations, files, prints and plans.
Safe to run again: existing notes are never replaced (timeline lines are de-duplicated)."""
import datetime as dt
import json
import logging
import sqlite3
import time
from pathlib import Path

from batsy import vault, vault_capture as cap
from batsy.config import DATA_DIR, SOURCE_DIR

log = logging.getLogger("batsy.vault")


def _rows(db: str, sql: str, *args) -> list[dict]:
    try:
        c = sqlite3.connect(str(DATA_DIR / db))
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args)]
    except sqlite3.Error:
        return []


def _run(fn, *a, **kw):
    """The capture function itself (not queued: seeding already runs on the writer thread or in the CLI)."""
    try:
        getattr(fn, "__wrapped__", fn)(*a, **kw)
    except Exception:
        log.exception("seed %s", fn.__name__)


def facts() -> int:
    from batsy import vault_index
    from batsy.memory_store import get_store

    st = get_store()
    seen = set()
    for f in st.all_facts():  # duplicates said twice become one memory
        key = f["fact"].strip().lower()
        if key in seen:
            st.forget_fact(f["id"])
        seen.add(key)
    vault_index.write_facts()
    return len(seen)


def projects() -> int:
    n = 0
    for p in _rows("tasks.db", "SELECT t.id, t.title, t.created, l.about, l.repo, l.path, l.url FROM tasks t "
                               "LEFT JOIN project_links l ON l.task_id = t.id WHERE t.kind = 'project' AND t.active = 1"):
        title = p["title"].strip().capitalize()
        _run(cap._project, title, p.get("about") or "", p.get("repo") or "", p.get("path") or "", p.get("url") or "")
        for e in _rows("tasks.db", "SELECT day, t, status, note FROM task_log WHERE task_id = ? AND note != '' ORDER BY t",
                       p["id"]):
            vault.add_to_block(cap.project_path(title), "log", f"- [[{e['day']}]] {e['status']}: {e['note']}", "Log")
        n += 1
    return n


def files() -> int:
    from batsy import received

    rows = sorted(received.all_rows(limit=2000), key=lambda r: r["received"])
    for r in rows:
        _seed_file(r)
    return len(rows)


def _seed_file(r: dict) -> None:
    p = Path(r["path"])
    card = vault.path("files", p.name)
    if card.exists():
        return
    _run(cap.file_received, r["path"], r["sender"], r.get("via") or "", r.get("kind") or "", r.get("size") or 0,
         r["received"])  # the card, person and day lines get the time it really arrived


def prints() -> int:
    try:
        hist = json.loads((DATA_DIR / "prints" / "history.json").read_text())
    except (OSError, ValueError):
        hist = []
    for h in hist:
        _run(cap.print_started, h["job"], h["gcode"], {}, h["t"])
    return len(hist)


def areas() -> None:
    p = vault.ROOT / "03 Areas" / "3D Printing.md"
    try:
        s = json.loads((DATA_DIR / "printer" / "settings.json").read_text())
    except (OSError, ValueError):
        s = {}
    vault.set_block(p, "printer", "\n".join([
        f"- **Machine profile:** {s.get('machine', '?')}",
        f"- **Default process:** {s.get('default', '?')} · **filament:** {s.get('filament', '?')}",
        f"- **Nozzle:** {s.get('nozzle_temp', '?')} °C (from the temperature tower: the top block was best)",
        "- Presets: `~/.local/share/batsy/printer/` · prints: `~/.local/share/batsy/prints/`",
        "- What to change for what: [[Printer knowledge]]"]), "Setup")
    know = SOURCE_DIR / "printer_knowledge.md"
    if know.exists():
        vault.create(vault.ROOT / "03 Areas" / "Printer knowledge.md",
                     vault.frontmatter(type="reference", tags=["3d-printing"]) + know.read_text())
    try:
        prof = json.loads((DATA_DIR / "music_profile.json").read_text())
    except (OSError, ValueError):
        prof = {}
    if prof.get("summary"):
        rules = "\n".join(f"- {r}" for r in prof.get("rules") or [])
        vault.set_block(vault.ROOT / "03 Areas" / "Music.md", "profile",
                        prof["summary"] + (f"\n\n**Your corrections:**\n{rules}" if rules else ""), "Taste profile")


def history(days: int = 30) -> int:
    """Daily notes for the past `days`, from what Batsy logged: conversations, plans (prints/files come above)."""
    since = time.time() - days * 86400
    events = []
    for r in _rows("memory.db", "SELECT t, source, user, reply FROM exchanges WHERE t > ? AND source != 'alert' "
                                "ORDER BY t", since):
        asked = (r["user"] or "").strip()
        if len(asked) < 4 or cap.TRIVIAL.match(asked) or not cap.worth(asked, r["reply"]):
            continue
        events.append((r["t"], cap.line(asked, r["reply"])))
    for t, line in sorted(events):
        cap._timeline(line, t)
    for a in _rows("memory.db", "SELECT day, text FROM agenda ORDER BY t"):
        try:
            day = dt.date.fromisoformat(a["day"])
        except ValueError:
            continue
        if day >= dt.date.today() - dt.timedelta(days=days):
            _run(cap.plan, day, a["text"])
    return len(events)


def seed(cfg=None) -> str:
    vault.ensure()
    out = {"facts": facts(), "projects": projects(), "files": files(), "prints": prints()}
    areas()
    out["moments"] = history()
    vault.daily()  # today
    try:
        from batsy import vault_daily

        vault_daily.refresh_home()
    except Exception:
        log.exception("seed home")
    msg = (f"Brain ready: {out['facts']} memories, {out['projects']} projects, {out['files']} files, "
           f"{out['prints']} prints, {out['moments']} moments over the last 30 days.")
    log.info(msg)
    return msg
