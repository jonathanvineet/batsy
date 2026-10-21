"""Token accounting from usage.jsonl (one row per Claude request) for `batsy usage` and the screensaver."""
import json
import time

from batsy.config import DATA_DIR

USAGE_FILE = DATA_DIR / "usage.jsonl"
STARTED = time.time()  # "this session" = since Batsy started

# $ per million tokens: input, output, cache read. Cache writes are priced at 1.25x (5 min) / 2x (1 h)
# input; we use 1.6x as a blend since Batsy writes both.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
}


def _cost(r: dict) -> float:
    if r.get("model", "").startswith(("local:", "claude-code")):
        return 0.0  # the laptop is free; Claude Code is covered by the Pro subscription
    pin, pout, pread = PRICES.get(r.get("model", ""), PRICES["claude-opus-5-5"])
    return (r["in"] * pin + r["out"] * pout + r["cache_read"] * pread + r["cache_write"] * pin * 1.6) / 1e6


def _rows() -> list[dict]:
    if not USAGE_FILE.exists():
        return []
    rows = []
    for line in USAGE_FILE.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _sum(rows: list[dict]) -> dict:
    tot = {k: sum(r[k] for r in rows) for k in ("in", "out", "cache_read", "cache_write")}
    tot["tokens"] = sum(tot.values())
    tot["requests"] = len(rows)
    tot["cost"] = round(sum(_cost(r) for r in rows), 4)
    return tot


def stats() -> dict:
    rows = _rows()
    now = time.time()
    midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    return {
        "session": _sum([r for r in rows if r["t"] >= STARTED]),
        "today": _sum([r for r in rows if r["t"] >= midnight]),
        "week": _sum([r for r in rows if r["t"] >= now - 7 * 86400]),
        "all": _sum(rows),
    }
