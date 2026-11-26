"""business.db: the company's records, in Batsy's data folder (~/.local/share/batsy/shop/).

Money is in rupees (floats, rounded when shown). Times are Unix seconds. JSON columns hold the settings a part is
printed with (`options`), the virtual slicer's answers (`estimate`, `slice`) and a run's cost breakdown.

Order status:  quote -> confirmed -> printing -> ready -> delivered   (or cancelled at any point)
Job status:    waiting -> slicing -> ready -> printing -> done        (failed / cancelled; a failed job can be reprinted)
Run outcome:   printing -> done | cancelled | failed                  (one row per plate actually started)
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from batsy.config import DATA_DIR

HOME = DATA_DIR / "shop"
FILES = HOME / "files"
DB = HOME / "business.db"
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS staff (login TEXT PRIMARY KEY, name TEXT, role TEXT DEFAULT 'staff', added REAL);
CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, email TEXT,
    notes TEXT, created REAL);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, code TEXT UNIQUE, customer_id INTEGER, title TEXT,
    kind TEXT DEFAULT 'custom', status TEXT DEFAULT 'quote', due TEXT, notes TEXT, price REAL DEFAULT 0,
    price_override REAL, discount REAL DEFAULT 0, paid REAL DEFAULT 0, created_by TEXT, created REAL,
    updated REAL, confirmed REAL, delivered REAL, external_id TEXT);
CREATE TABLE IF NOT EXISTS parts (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, name TEXT, file TEXT,
    qty INTEGER DEFAULT 1, options TEXT DEFAULT '{}', estimate TEXT, slice TEXT, slice_key TEXT,
    status TEXT DEFAULT 'new', error TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL, order_id INTEGER NOT NULL,
    position REAL, status TEXT DEFAULT 'waiting', copies INTEGER DEFAULT 1, per_plate INTEGER DEFAULT 1,
    done_copies INTEGER DEFAULT 0, gcode TEXT, gcode_key TEXT, note TEXT, added REAL, updated REAL);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, job_id INTEGER, order_id INTEGER, plate_copies INTEGER,
    gcode TEXT, started REAL, ended REAL, outcome TEXT DEFAULT 'printing', progress REAL, grams REAL,
    hours REAL, cost REAL, loss REAL DEFAULT 0, breakdown TEXT, reason TEXT, stopped_by TEXT);
CREATE TABLE IF NOT EXISTS spools (id INTEGER PRIMARY KEY, material TEXT, color TEXT, brand TEXT,
    price REAL, weight_g REAL DEFAULT 1000, used_g REAL DEFAULT 0, active INTEGER DEFAULT 0, added REAL,
    finished REAL);
CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY, t REAL, category TEXT, amount REAL, note TEXT,
    who TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, t REAL, who TEXT, kind TEXT, ref TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS jobs_pos ON jobs(status, position);
CREATE INDEX IF NOT EXISTS runs_t ON runs(started);
CREATE INDEX IF NOT EXISTS events_t ON events(t);
"""

JSON_COLS = {"options", "estimate", "slice", "breakdown"}


def connect() -> sqlite3.Connection:
    HOME.mkdir(parents=True, exist_ok=True)
    FILES.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB), timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


_conn: sqlite3.Connection | None = None


def _c() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
    return _conn


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def q(sql: str, args=()) -> list[dict]:
    with _lock:
        return [_row(r) for r in _c().execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    with _lock:
        return _row(_c().execute(sql, args).fetchone())


def run(sql: str, args=()) -> int:
    with _lock:
        c = _c()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def insert(table: str, **cols) -> int:
    cols = {k: (json.dumps(v) if k in JSON_COLS and not isinstance(v, str) else v) for k, v in cols.items()}
    keys = ", ".join(cols)
    return run(f"INSERT INTO {table} ({keys}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))


def update(table: str, rid: int, **cols) -> None:
    if not cols:
        return
    cols = {k: (json.dumps(v) if k in JSON_COLS and v is not None and not isinstance(v, str) else v)
            for k, v in cols.items()}
    sets = ", ".join(f"{k} = ?" for k in cols)
    run(f"UPDATE {table} SET {sets} WHERE id = ?", (*cols.values(), rid))


def get(table: str, rid: int) -> dict | None:
    return one(f"SELECT * FROM {table} WHERE id = ?", (rid,))


# --- settings (the price list lives here) -----------------------------------------------------------------------
def setting(key: str, default=None):
    r = one("SELECT value FROM settings WHERE key = ?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return default


def set_setting(key: str, value) -> None:
    run("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)))


# --- the log: everything that happened, for the brain and for you ------------------------------------------------
def event(who: str, kind: str, text: str, ref: str = "") -> None:
    run("INSERT INTO events (t, who, kind, ref, text) VALUES (?, ?, ?, ?, ?)", (time.time(), who, kind, ref, text))


def events(limit: int = 50, kind: str = "") -> list[dict]:
    if kind:
        return q("SELECT * FROM events WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit))
    return q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


def order_dir(order_id: int) -> Path:
    d = FILES / f"order-{order_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def next_code() -> str:
    """PD-2611-007: year+month and a running number for the month."""
    ym = time.strftime("%y%m")
    r = one("SELECT max(CAST(substr(code, 9) AS INTEGER)) AS n FROM orders WHERE code LIKE ?", (f"PD-{ym}-%",))
    return f"PD-{ym}-{((r or {}).get('n') or 0) + 1:03d}"
