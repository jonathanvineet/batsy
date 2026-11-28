"""The bridge to the team's PrintDesk Pro (DreamForge) data in Supabase.

The team's site keeps its orders, revenue and price list in Supabase; Batsy keeps the printing (files, slices, the
queue, runs). They're linked by order: a Batsy order's external_id is the Supabase order's id.

  link(row)       a Supabase order -> its Batsy order (made once; "Slice with Batsy" on an order card)
  push(oid)       Batsy -> Supabase, after anything that matters (confirm, print, a run ending, delivery):
                    status            quote/confirmed -> pending, printing, ready -> ready_for_delivery, delivered,
                                      cancelled
                    filament_grams, print_hours   from the parts (sliced numbers when there are)
                    production_cost   what it costs to make (Batsy's quote) + every failed run's loss
                    failed_prints     the team's own entries kept; Batsy's failed/stopped runs added
                                      ({date, filament_wasted, hours_wasted, cost, batsy_run, reason})
                    quoted_price      the agreed price, when Batsy's order is confirmed
  a Batsy order with no link gets a Supabase order when it's confirmed, so it shows in the team's queue/revenue.
  rates           the team's pricing_rates row is Batsy's price list (read every 5 minutes): one price list.

How: through PrintDesk's database gateway on this workstation (http://127.0.0.1:3000/api/db, db-gateway.mjs in
printdesk-pro), the same way the site's browsers go. It knows Batsy (127.0.0.1 is the admin) and caches everything:
reads are answered from its copy of the tables when Supabase is down, writes are queued there and sent when it's
back. If the site isn't running, Batsy talks to Supabase directly with DATA_DIR/shop/supabase.json {"url", "key"}
(the service-role key: it bypasses the team's RLS, so it stays on this workstation), set with
`batsy shop supabase --env ~/elco/printdesk-pro/.env.local`. BATSY_SHOP_SITE overrides the site's address.

Everything here is best effort: Supabase being away never stops a print. An update that couldn't go out by either
way is remembered (DATA_DIR/shop/sync_pending.json) and retried every minute until it does.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from batsy.shop import db

log = logging.getLogger("batsy.shop.sync")
CREDS = db.HOME / "supabase.json"
PENDING = db.HOME / "sync_pending.json"
SITE = os.environ.get("BATSY_SHOP_SITE", "http://127.0.0.1:3000").rstrip("/")
_site = {"t": 0.0, "up": False}
_pending_lock = threading.Lock()
STATUS = {"quote": "pending", "confirmed": "pending", "printing": "printing", "ready": "ready_for_delivery",
          "delivered": "delivered", "cancelled": "cancelled"}
_rates_at = 0.0


def creds() -> dict | None:
    try:
        c = json.loads(CREDS.read_text())
        return c if c.get("url") and c.get("key") else None
    except (OSError, ValueError):
        return None


def set_creds(url: str, key: str) -> None:
    db.HOME.mkdir(parents=True, exist_ok=True)
    CREDS.write_text(json.dumps({"url": url.rstrip("/"), "key": key}))
    os.chmod(CREDS, 0o600)


def site_up() -> bool:
    """Is PrintDesk's database gateway running here? (Asked at most every 30 s.)"""
    if time.time() - _site["t"] < 30:
        return _site["up"]
    try:
        with urllib.request.urlopen(f"{SITE}/api/db/_status", timeout=3) as r:
            _site["up"] = bool(json.loads(r.read()).get("configured"))
    except (urllib.error.URLError, OSError, ValueError):
        _site["up"] = False
    _site["t"] = time.time()
    return _site["up"]


def _send(base: str, key: str | None, method: str, path: str, body, prefer: str):
    h = {"Content-Type": "application/json"}
    if key:
        h.update({"apikey": key, "Authorization": f"Bearer {key}"})
    if prefer:
        h["Prefer"] = prefer
    req = urllib.request.Request(f"{base}/rest/v1/{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def _req(method: str, path: str, body=None, prefer: str = ""):
    """Through the site's gateway (cached, queued when Supabase is down), else straight to Supabase."""
    if site_up():
        try:
            return _send(f"{SITE}/api/db", None, method, path, body, prefer)
        except urllib.error.HTTPError:
            raise  # the gateway answered: that's Supabase's (or the cache's) answer
        except (urllib.error.URLError, OSError):
            _site.update(t=time.time(), up=False)  # it just went away: try Supabase itself
    c = creds()
    if not c:
        raise urllib.error.URLError("neither PrintDesk's gateway nor Supabase credentials are available")
    return _send(c["url"], c["key"], method, path, body, prefer)


def enabled() -> bool:
    return creds() is not None or site_up()


def _pending(add: int | None = None, drop: int | None = None) -> list[int]:
    with _pending_lock:
        try:
            ids = set(json.loads(PENDING.read_text()))
        except (OSError, ValueError):
            ids = set()
        if add is not None or drop is not None:
            if add is not None:
                ids.add(add)
            if drop is not None:
                ids.discard(drop)
            db.HOME.mkdir(parents=True, exist_ok=True)
            PENDING.write_text(json.dumps(sorted(ids)))
        return sorted(ids)


def retry_pending() -> None:
    """Updates that couldn't reach the site or Supabase earlier: send them now (each is the order's latest state)."""
    for oid in _pending():
        if not enabled():
            return
        push(oid)


# --- Supabase -> Batsy ---------------------------------------------------------------------------------------------
def link(row: dict, who: str) -> dict:
    """The Batsy order for a team order (the row as the site has it). Made once, found after."""
    from batsy.shop import jobs

    sid = str(row.get("id") or "")
    if not sid:
        raise ValueError("Which order?")
    hit = db.one("SELECT id FROM orders WHERE external_id = ?", (sid,))
    if hit:
        return jobs.order(hit["id"])
    o = jobs.create_order(who, {"name": row.get("customer_name") or "Customer", "phone": row.get("customer_phone") or "",
                                "email": row.get("customer_email") or ""},
                          row.get("item_description") or "Order", row.get("due_date") or "", row.get("notes") or "",
                          row.get("type") or "custom")
    upd = {"external_id": sid}
    if row.get("quoted_price"):
        upd["price_override"] = float(row["quoted_price"])  # the price the team already gave the customer
    db.update("orders", o["id"], **upd)
    db.event(who, "linked", f"{o['code']} linked to the site's order {sid[:8]}", f"order:{o['id']}")
    return jobs.order(o["id"])


def pull_rates(force: bool = False) -> None:
    global _rates_at
    if not enabled() or (not force and time.time() - _rates_at < 300):
        return
    _rates_at = time.time()
    try:
        rows = _req("GET", "pricing_rates?select=*&order=updated_at.desc&limit=1") or []
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.info("supabase rates: %s", e)
        return
    if rows:
        from batsy.shop import costing

        keep = {k: v for k, v in rows[0].items() if k in costing.DEFAULT_RATES and isinstance(v, (int, float))}
        cur = costing.rates()
        if any(abs(float(cur.get(k, 0)) - float(v)) > 1e-9 for k, v in keep.items()):
            costing.set_rates(keep)
            log.info("price list from the site: %s", keep)


# --- Batsy -> Supabase ---------------------------------------------------------------------------------------------
def _payload(oid: int) -> dict:
    from batsy.shop import jobs, quote

    o = jobs.order(oid)
    parts = o["parts"]
    g = sum((p["numbers"]["grams"] or 0) * p["qty"] for p in parts)
    h = sum((p["numbers"]["hours"] or 0) * p["qty"] for p in parts)
    runs = db.q("SELECT * FROM runs WHERE order_id = ? AND outcome IN ('failed','cancelled') ORDER BY id", (oid,))
    fails = [{"date": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r["started"])),
              "filament_wasted": round(r["grams"] or 0, 1), "hours_wasted": round(r["hours"] or 0, 2),
              "cost": round(r["loss"] or 0, 2), "batsy_run": r["id"], "reason": r["reason"] or r["outcome"]}
             for r in runs]
    q = o.get("quote") or {}
    body = {"status": STATUS.get(o["status"], "pending"), "filament_grams": round(g, 1), "print_hours": round(h, 2),
            "production_cost": round((q.get("manufacturing") or 0) + sum(f["cost"] for f in fails), 2)}
    if o["status"] != "quote":
        body["quoted_price"] = round(o["charge"] or 0, 2)
    return {"body": body, "fails": fails, "order": o}


def push(oid: int) -> None:
    if not enabled():
        _pending(add=oid)  # neither the site nor Supabase credentials right now: send it when one is back
        return
    try:
        p = _payload(oid)
        o, body = p["order"], p["body"]
        sid = o.get("external_id")
        if not sid:
            if o["status"] == "quote":
                return  # not an order yet: the site sees it when it's confirmed
            c = o.get("customer") or {}
            row = {**body, "customer_name": c.get("name") or "Customer", "customer_phone": c.get("phone") or None,
                   "customer_email": c.get("email") or None, "item_description": o["title"], "type": o.get("kind") or "custom",
                   "due_date": o.get("due") or None, "notes": (o.get("notes") or "") + f"\n[Batsy {o['code']}]",
                   "failed_prints": p["fails"]}
            made = _req("POST", "orders", row, prefer="return=representation") or []
            if made:
                db.update("orders", oid, external_id=str(made[0]["id"]))
                log.info("site order %s made for %s", made[0]["id"], o["code"])
                _pending(drop=oid)
            return
        cur = (_req("GET", f"orders?id=eq.{sid}&select=failed_prints") or [{}])
        theirs = [f for f in ((cur[0] if cur else {}).get("failed_prints") or []) if not f.get("batsy_run")]
        body["failed_prints"] = theirs + p["fails"]
        body["production_cost"] = round(body["production_cost"] + sum(float(f.get("cost") or 0) for f in theirs), 2)
        _req("PATCH", f"orders?id=eq.{sid}", body, prefer="return=minimal")
        if oid in _pending():
            _pending(drop=oid)
            log.info("site updated for order %s (it was waiting)", oid)
    except urllib.error.HTTPError as e:
        log.warning("supabase push %s: %s", oid, e)  # refused (a bad row): retrying won't help
        db.event("batsy", "sync_failed", f"The site refused the update for order {oid}: {e}")
        _pending(drop=oid)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        log.warning("supabase push %s: %s (will retry)", oid, e)
        if oid not in _pending():
            db.event("batsy", "sync_failed", f"Couldn't update the site for order {oid}: {e}. Batsy will keep trying.")
        _pending(add=oid)


def later(oid: int | None) -> None:
    """push() in the background: never in the way of printing."""
    if oid:  # the "is the site up" check is a request too: keep it off the caller's thread
        threading.Thread(target=push, args=(oid,), daemon=True, name="shop-sync").start()
