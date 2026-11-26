"""The money and the machine, over a period: what came in, what printing cost, what failures lost, expenses,
profit, how busy the printer was, and which materials went where.

Revenue counts an order when it's delivered (its charge: your override, else the quoted price). Payments received
are tracked separately (orders.paid), so "owed" is delivered-and-not-fully-paid.
"""
import time

from batsy.shop import db


def _range(days: int | None = None, start: float | None = None, end: float | None = None) -> tuple[float, float]:
    end = end or time.time()
    if start is None:
        start = end - (days or 30) * 86400
    return start, end


def pnl(days: int | None = 30, start: float | None = None, end: float | None = None) -> dict:
    a, b = _range(days, start, end)
    orders = db.q("SELECT * FROM orders WHERE status = 'delivered' AND delivered BETWEEN ? AND ?", (a, b))
    revenue = sum((o["price_override"] if o["price_override"] is not None else o["price"]) or 0 for o in orders)
    runs = db.q("SELECT * FROM runs WHERE started BETWEEN ? AND ? AND outcome != 'printing'", (a, b))
    good = [r for r in runs if r["outcome"] == "done"]
    bad = [r for r in runs if r["outcome"] in ("failed", "cancelled")]
    print_cost = sum(r["cost"] or 0 for r in good)
    loss = sum(r["loss"] or 0 for r in bad)
    exp = db.q("SELECT * FROM expenses WHERE t BETWEEN ? AND ?", (a, b))
    expenses = sum(e["amount"] or 0 for e in exp)
    hours = sum(r["hours"] or 0 for r in runs)
    owed = db.q("SELECT code, price, price_override, paid FROM orders WHERE status = 'delivered'")
    owed_total = sum(max(0, ((o["price_override"] if o["price_override"] is not None else o["price"]) or 0)
                         - (o["paid"] or 0)) for o in owed)
    by_cat: dict = {}
    for e in exp:
        by_cat[e["category"] or "other"] = by_cat.get(e["category"] or "other", 0) + (e["amount"] or 0)
    return {"from": a, "to": b, "orders_delivered": len(orders), "revenue": round(revenue, 2),
            "print_cost": round(print_cost, 2), "failure_loss": round(loss, 2), "expenses": round(expenses, 2),
            "expenses_by_category": {k: round(v, 2) for k, v in by_cat.items()},
            "profit": round(revenue - print_cost - loss - expenses, 2),
            "runs": len(runs), "failed_runs": len(bad),
            "failure_rate": round(len(bad) / len(runs), 3) if runs else 0.0,
            "grams_used": round(sum(r["grams"] or 0 for r in runs), 1),
            "grams_wasted": round(sum(r["grams"] or 0 for r in bad), 1),
            "print_hours": round(hours, 1), "utilisation": round(hours / max((b - a) / 3600, 1), 3),
            "owed": round(owed_total, 2)}


def daily(days: int = 30) -> list[dict]:
    out = []
    today = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    for i in range(days - 1, -1, -1):
        a = today - i * 86400
        p = pnl(start=a, end=a + 86400)
        out.append({"day": time.strftime("%Y-%m-%d", time.localtime(a)), "revenue": p["revenue"],
                    "cost": p["print_cost"], "loss": p["failure_loss"], "hours": p["print_hours"]})
    return out


def failures(limit: int = 30) -> list[dict]:
    rows = db.q("SELECT r.*, o.code, p.name AS part FROM runs r LEFT JOIN jobs j ON j.id = r.job_id "
                "LEFT JOIN parts p ON p.id = j.part_id LEFT JOIN orders o ON o.id = r.order_id "
                "WHERE r.outcome IN ('failed','cancelled') ORDER BY r.id DESC LIMIT ?", (limit,))
    for r in rows:
        r.pop("breakdown", None)
    return rows


def runs(limit: int = 50) -> list[dict]:
    rows = db.q("SELECT r.*, o.code, p.name AS part FROM runs r LEFT JOIN jobs j ON j.id = r.job_id "
                "LEFT JOIN parts p ON p.id = j.part_id LEFT JOIN orders o ON o.id = r.order_id "
                "ORDER BY r.id DESC LIMIT ?", (limit,))
    return rows


def spools() -> list[dict]:
    rows = db.q("SELECT * FROM spools WHERE finished IS NULL ORDER BY active DESC, id DESC")
    for s in rows:
        s["left_g"] = round((s["weight_g"] or 0) - (s["used_g"] or 0), 1)
    return rows


def today_line() -> str:
    """One line for the cockpit and the morning: what's queued, what's running, today's money."""
    from batsy.shop import jobs

    q = jobs.queue_list()
    waiting = [j for j in q if j["status"] in ("waiting", "ready", "slicing")]
    failed = [j for j in q if j["status"] == "failed"]
    p = pnl(days=1)
    bits = [f"{len(waiting)} job{'s' if len(waiting) != 1 else ''} queued ({sum(j['left_hours'] for j in waiting):.1f} h)"]
    if failed:
        bits.append(f"{len(failed)} waiting for a reprint")
    ready = db.one("SELECT COUNT(*) AS n FROM orders WHERE status = 'ready'")
    if ready and ready["n"]:
        bits.append(f"{ready['n']} ready to deliver")
    if p["revenue"] or p["failure_loss"]:
        bits.append(f"today ₹{p['revenue']:.0f} in, ₹{p['failure_loss']:.0f} lost")
    return ", ".join(bits)


def cockpit() -> dict:
    """The cockpit's company panel: small, read-only."""
    from batsy.shop import jobs

    q = jobs.queue_list()
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}
    p = pnl(30)
    run = db.one("SELECT r.*, o.code FROM runs r LEFT JOIN orders o ON o.id = r.order_id "
                 "WHERE r.outcome = 'printing' ORDER BY r.id DESC LIMIT 1")
    printing = next((j for j in q if j["status"] == "printing"), None)
    return {"today": today_line(), "counts": counts,
            "printing": printing and {"part": printing["part"], "code": printing["order_code"],
                                      "customer": printing["customer"], "progress": (run or {}).get("progress")},
            "next": [{"part": j["part"], "code": j["order_code"], "customer": j["customer"], "status": j["status"],
                      "left": j["copies"] - j["done_copies"], "hours": j["left_hours"], "due": j.get("due")}
                     for j in q if j["status"] != "printing"][:4],
            "queue_hours": round(sum(j["left_hours"] for j in q), 1),
            "month": {k: p[k] for k in ("revenue", "profit", "failure_loss", "failed_runs", "runs", "owed")}}
