"""The print queue, printing, and what every print cost.

Confirming an order puts each of its parts in the queue as a job (copies = the part's quantity, per_plate = how
many go on the bed at once). Only you (the admin) print, reorder, reprint and stop. The order of the queue is
yours: drag it, or put something on top. Changing a job's settings re-slices it.

Printing a job slices it first if the G-code doesn't match its settings (or the copies left), then hands it to
Batsy's printer (printer3d.print_now: straight over USB, its own process). A run is recorded for every plate.

The watcher (a thread, every 3 s) follows the print streamer's status file. When a run ends:
  done       the filament it used, the hours it ran, its cost; the copies count up; when every job of an order
             is done, the order is ready for delivery (and your phone hears about it)
  cancelled  (you stopped it) / failed (the printer stopped answering, an error): the filament laid down up to the
             line it reached (batsy/shop/gcode.py), the hours it ran: all of it a loss. The job waits for a reprint.
The active spool of that material is charged with the grams either way.
"""
import logging
import threading
import time
from pathlib import Path

from batsy.shop import costing, db, gcode, quote, sync

log = logging.getLogger("batsy.shop.jobs")
OPEN = ("waiting", "slicing", "ready", "printing", "failed")


def _printer(app):
    return getattr(app, "printer3d", None) if app is not None else None


# --- orders --------------------------------------------------------------------------------------------------------
def create_order(who: str, customer: dict | None = None, title: str = "", due: str = "", notes: str = "",
                 kind: str = "custom") -> dict:
    cid = None
    c = customer or {}
    if c.get("id"):
        cid = int(c["id"])
    elif c.get("name"):
        hit = db.one("SELECT id FROM customers WHERE lower(name) = lower(?) AND coalesce(phone,'') = ?",
                     (c["name"].strip(), (c.get("phone") or "").strip()))
        cid = hit["id"] if hit else db.insert("customers", name=c["name"].strip()[:120],
                                               phone=(c.get("phone") or "").strip()[:40],
                                               email=(c.get("email") or "").strip()[:120], created=time.time())
    now = time.time()
    oid = db.insert("orders", code=db.next_code(), customer_id=cid, title=(title or "Untitled")[:160], kind=kind,
                    status="quote", due=due or None, notes=notes or None, created_by=who, created=now, updated=now)
    db.event(who, "order", f"New quote {db.get('orders', oid)['code']}: {title}", f"order:{oid}")
    return order(oid)


def order(oid: int, money: bool = True) -> dict | None:
    o = db.get("orders", oid)
    if not o:
        return None
    o["customer"] = db.get("customers", o["customer_id"]) if o.get("customer_id") else None
    o["parts"] = [part_view(p, money) for p in db.q("SELECT * FROM parts WHERE order_id = ? ORDER BY id", (oid,))]
    o["jobs"] = [job_view(j) for j in db.q("SELECT * FROM jobs WHERE order_id = ? ORDER BY id", (oid,))]
    o["quote"] = quote.reprice(oid) if money else None
    o["charge"] = o["price_override"] if o.get("price_override") is not None else o["price"]
    if money:
        runs = db.q("SELECT outcome, cost, loss FROM runs WHERE order_id = ?", (oid,))
        o["spent"] = round(sum(r["cost"] or 0 for r in runs), 2)
        o["lost"] = round(sum(r["loss"] or 0 for r in runs), 2)
    else:
        o.pop("price", None), o.pop("price_override", None), o.pop("discount", None)
    return o


def orders(status: str = "", limit: int = 200, who: str = "") -> list[dict]:
    sql, args = "SELECT id FROM orders", []
    cond = []
    if status:
        cond.append("status IN (%s)" % ",".join("?" * len(status.split(","))))
        args += status.split(",")
    if who:
        cond.append("created_by = ?")
        args.append(who)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY id DESC LIMIT ?"
    return [order(r["id"]) for r in db.q(sql, (*args, limit))]


def edit_order(oid: int, who: str, **f) -> dict:
    allowed = {k: v for k, v in f.items() if k in ("title", "due", "notes", "price_override", "discount", "paid", "kind")}
    if "status" in f and f["status"] in ("quote", "confirmed", "printing", "ready", "delivered", "cancelled"):
        allowed["status"] = f["status"]
        if f["status"] == "delivered":
            allowed["delivered"] = time.time()
    if allowed:
        db.update("orders", oid, **allowed, updated=time.time())
        db.event(who, "order_edit", f"{db.get('orders', oid)['code']}: {', '.join(allowed)}", f"order:{oid}")
        sync.later(oid)
    if f.get("customer"):
        o = db.get("orders", oid)
        c = f["customer"]
        if o.get("customer_id"):
            db.update("customers", o["customer_id"], **{k: c[k] for k in ("name", "phone", "email", "notes") if k in c})
    return order(oid)


def part_view(p: dict, money: bool = True) -> dict:
    n = quote.numbers(p)
    s = p.get("slice") or {}
    out = {**p, "numbers": n, "has_preview": bool(s.get("preview") and Path(s["preview"]).exists()),
           "file_name": Path(p["file"]).name}
    out.pop("file", None)
    if s:
        out["slice"] = {k: v for k, v in s.items() if k not in ("gcode", "preview", "paths")}
    if money:
        o = p.get("options") or {}
        out["cost"] = costing.part_cost(n["grams"], n["hours"], o.get("material", "PLA"), p["qty"],
                                        o.get("design_hours", 0))
    return out


def set_part(part_id: int, who: str, options: dict | None = None, qty: int | None = None) -> dict:
    """Change how a part prints (the "said changes"): re-estimated now, re-sliced in the background, and its jobs
    (if the order is confirmed) re-sliced before they print."""
    p = db.get("parts", part_id)
    if not p:
        raise ValueError("No such part.")
    upd = {"updated": time.time()}
    if options is not None:
        upd["options"] = quote.clean_options({**(p.get("options") or {}), **options})
    if qty is not None:
        upd["qty"] = max(1, int(qty))
    db.update("parts", part_id, **upd)
    quote.estimate(part_id)
    quote.queue_slice(part_id)
    for j in db.q("SELECT * FROM jobs WHERE part_id = ? AND status IN ('waiting','ready','failed')", (part_id,)):
        cp = upd.get("qty", j["copies"])
        pp = int((upd.get("options") or {}).get("per_plate") or j["per_plate"])
        db.update("jobs", j["id"], copies=cp, per_plate=min(pp, cp), gcode_key=None, updated=time.time())
    db.event(who, "part_edit", f"{p['name']}: {options or {}} qty={qty}", f"part:{part_id}")
    return part_view(db.get("parts", part_id))


def remove_part(part_id: int, who: str) -> None:
    p = db.get("parts", part_id)
    if not p:
        return
    db.run("DELETE FROM jobs WHERE part_id = ? AND status NOT IN ('printing')", (part_id,))
    db.run("DELETE FROM parts WHERE id = ?", (part_id,))
    quote.reprice(p["order_id"])
    db.event(who, "part_removed", p["name"], f"order:{p['order_id']}")


# --- the queue -----------------------------------------------------------------------------------------------------
def _fit_per_plate(p: dict) -> int:
    o = p.get("options") or {}
    if o.get("per_plate"):
        return min(int(o["per_plate"]), p["qty"])
    e = p.get("estimate") or {}
    x, y, _z = (e.get("size_mm") or [0, 0, 0])
    if not x or not y:
        return 1
    fit = int((quote.BED[0] - 10) // (x + 8)) * int((quote.BED[1] - 10) // (y + 8))  # 8 mm apart, 5 mm margin
    return max(1, min(p["qty"], fit, 12))


def confirm(oid: int, who: str) -> dict:
    o = db.get("orders", oid)
    if not o:
        raise ValueError("No such order.")
    if o["status"] not in ("quote", "cancelled"):
        return order(oid)
    parts = db.q("SELECT * FROM parts WHERE order_id = ?", (oid,))
    if not parts:
        raise ValueError("Add a model to the order first.")
    bad = [p["name"] for p in parts if p["status"] == "failed"]
    if bad:
        raise ValueError(f"Couldn't read {', '.join(bad)}: replace or remove it first.")
    quote.reprice(oid)
    top = db.one("SELECT max(position) AS m FROM jobs")
    pos = (top["m"] or 0) if top else 0
    now = time.time()
    for p in parts:
        pos += 1
        pp = _fit_per_plate(p)
        jid = db.insert("jobs", part_id=p["id"], order_id=oid, position=pos, status="waiting", copies=p["qty"],
                        per_plate=pp, added=now, updated=now)
        _prepare(jid)
    db.update("orders", oid, status="confirmed", confirmed=now, updated=now)
    db.event(who, "confirmed", f"{o['code']} confirmed: {len(parts)} part(s) queued", f"order:{oid}")
    sync.later(oid)
    return order(oid)


def _plate_now(j: dict) -> int:
    return max(1, min(j["per_plate"], j["copies"] - j["done_copies"]))


def _prepare(jid: int) -> None:
    """Make sure the job's G-code matches its settings and the copies on this plate; slice if not."""
    j = db.get("jobs", jid)
    p = db.get("parts", j["part_id"])
    if not p.get("file"):
        db.update("jobs", jid, status="waiting", note="No model file: add one to the order to print it here.",
                  updated=time.time())
        return
    plate = _plate_now(j)
    key = quote.slice_key(p, plate)
    if j.get("gcode_key") == key and j.get("gcode") and Path(j["gcode"]).exists():
        if j["status"] in ("waiting", "slicing"):
            db.update("jobs", jid, status="ready", updated=time.time())
        return
    s = p.get("slice") or {}
    if plate == 1 and s.get("key") == key and s.get("gcode") and Path(s["gcode"]).exists():  # the quote's slice
        db.update("jobs", jid, gcode=s["gcode"], gcode_key=key, status="ready", updated=time.time())
        return
    db.update("jobs", jid, status="slicing", updated=time.time())
    quote.queue_slice(p["id"], plate, for_job=jid)


def job_view(j: dict) -> dict:
    p = db.get("parts", j["part_id"]) or {}
    o = db.get("orders", j["order_id"]) or {}
    c = db.get("customers", o["customer_id"]) if o.get("customer_id") else None
    n = quote.numbers(p) if p else {"grams": 0, "hours": 0}
    plate = _plate_now(j)
    s = p.get("slice") or {}
    fresh = bool(j.get("gcode_key") and p and j["gcode_key"] == quote.slice_key(p, plate))
    return {**j, "part": p.get("name"), "options": p.get("options") or {}, "order_code": o.get("code"),
            "order_title": o.get("title"), "due": o.get("due"), "customer": c["name"] if c else "",
            "plate": plate, "plate_hours": round(n["hours"] * plate, 2), "plate_grams": round(n["grams"] * plate, 1),
            "left_hours": round(n["hours"] * (j["copies"] - j["done_copies"]), 2), "sliced": fresh,
            "has_preview": bool(s.get("preview") and Path(s["preview"]).exists()), "part_status": p.get("status")}


def queue_list() -> list[dict]:
    rows = db.q("SELECT * FROM jobs WHERE status IN (%s) ORDER BY CASE status WHEN 'printing' THEN 0 ELSE 1 END, "
                "position" % ",".join("?" * len(OPEN)), OPEN)
    out, t = [], 0.0
    for j in rows:
        v = job_view(j)
        v["starts_in_h"] = round(t, 2)  # if printed back to back, in this order
        t += v["left_hours"]
        out.append(v)
    return out


def reorder(ids: list[int], who: str) -> list[dict]:
    for i, jid in enumerate(ids):
        db.update("jobs", int(jid), position=float(i + 1))
    db.event(who, "reorder", f"Queue reordered: {ids[:12]}")
    return queue_list()


def move(jid: int, where: str, who: str) -> list[dict]:
    ids = [j["id"] for j in queue_list() if j["status"] != "printing"]
    if jid not in ids:
        return queue_list()
    i = ids.index(jid)
    ids.pop(i)
    k = {"top": 0, "bottom": len(ids), "up": max(0, i - 1), "down": min(len(ids), i + 1)}.get(where, i)
    ids.insert(k, jid)
    return reorder(ids, who)


def set_job(jid: int, who: str, per_plate: int | None = None, note: str | None = None) -> dict:
    j = db.get("jobs", jid)
    upd = {"updated": time.time()}
    if per_plate:
        upd["per_plate"] = max(1, min(int(per_plate), j["copies"]))
    if note is not None:
        upd["note"] = note[:300]
    db.update("jobs", jid, **upd)
    if per_plate:
        _prepare(jid)
    return job_view(db.get("jobs", jid))


def cancel_job(jid: int, who: str) -> None:
    j = db.get("jobs", jid)
    if j and j["status"] != "printing":
        db.update("jobs", jid, status="cancelled", updated=time.time())
        db.event(who, "job_cancelled", f"Job {jid} removed from the queue", f"job:{jid}")


def reprint(jid: int, who: str) -> dict:
    """A failed plate back in the queue, on top."""
    j = db.get("jobs", jid)
    if not j or j["status"] not in ("failed", "cancelled", "done"):
        raise ValueError("That job isn't waiting for a reprint.")
    if j["status"] == "done":  # one more copy (a customer's spare, a bad copy found later)
        db.update("jobs", jid, copies=j["copies"] + 1)
    top = db.one("SELECT min(position) AS m FROM jobs WHERE status IN ('waiting','ready','slicing','failed')")
    db.update("jobs", jid, status="waiting", position=((top["m"] or 1) - 1) if top else 0, updated=time.time())
    _prepare(jid)
    db.event(who, "reprint", f"Job {jid} back on top of the queue", f"job:{jid}")
    return job_view(db.get("jobs", jid))


# --- printing -------------------------------------------------------------------------------------------------------
def print_job(app, jid: int, who: str) -> dict:
    pr = _printer(app)
    if pr is None:
        return {"ok": False, "say": "No printer on this machine."}
    s = pr.snapshot()
    if not s.get("connected"):
        return {"ok": False, "say": "The printer isn't connected. Is it on, with the USB cable in?"}
    if s.get("printing") or s.get("uploading") is not None or s.get("streaming"):
        return {"ok": False, "say": "The printer is busy."}
    if db.one("SELECT id FROM runs WHERE outcome = 'printing'"):
        return {"ok": False, "say": "A shop print is still running."}
    j = db.get("jobs", jid)
    if not j or j["status"] not in ("ready", "waiting", "failed"):
        return {"ok": False, "say": "That job can't print now."}
    p = db.get("parts", j["part_id"])
    plate = _plate_now(j)
    if j.get("gcode_key") != quote.slice_key(p, plate) or not j.get("gcode") or not Path(j["gcode"]).exists():
        _prepare(jid)
        j = db.get("jobs", jid)
        if j["status"] != "ready":
            return {"ok": False, "say": j.get("note") or "Slicing it with its settings first: press print again when it's ready."}
    o = db.get("orders", j["order_id"])
    left = j["copies"] - j["done_copies"]
    title = f"{o['code']} {p['name']}" + (f" ({plate} of {left} left)" if j["copies"] > 1 else "")
    said = pr.print_now(Path(j["gcode"]), title)
    if not said.startswith("Printing"):
        return {"ok": False, "say": said}
    from batsy.config import DATA_DIR

    started = pr.state.get("started") or time.time()
    rid = db.insert("runs", job_id=jid, order_id=j["order_id"], plate_copies=plate,
                    gcode=str(DATA_DIR / "prints" / Path(j["gcode"]).name), started=started, outcome="printing")
    db.update("jobs", jid, status="printing", updated=time.time())
    if o["status"] in ("confirmed", "quote"):
        db.update("orders", o["id"], status="printing", updated=time.time())
    db.event(who, "print", f"Printing {title}", f"run:{rid}")
    sync.later(o["id"])
    return {"ok": True, "say": said, "run": rid}


def stop(app, who: str, reason: str = "") -> dict:
    pr = _printer(app)
    r = db.one("SELECT * FROM runs WHERE outcome = 'printing' ORDER BY id DESC LIMIT 1")
    if r:
        db.update("runs", r["id"], stopped_by=who, reason=reason or None)
    said = pr.cancel() if pr is not None else "No printer."
    return {"ok": True, "say": said}


def pause(app) -> dict:
    pr = _printer(app)
    return {"ok": pr is not None, "say": pr.pause() if pr else "No printer."}


def resume(app) -> dict:
    pr = _printer(app)
    return {"ok": pr is not None, "say": pr.resume() if pr else "No printer."}


def set_reason(rid: int, reason: str, who: str) -> None:
    db.update("runs", rid, reason=reason[:300])
    db.event(who, "failure_reason", f"Run {rid}: {reason}", f"run:{rid}")
    r = db.get("runs", rid)
    if r and r["outcome"] in ("failed", "cancelled"):
        sync.later(r["order_id"])  # the reason shows on the site's failed prints too


# --- the watcher ---------------------------------------------------------------------------------------------------
def _stream_status() -> dict:
    import json

    from batsy import printstream

    try:
        return json.loads(printstream.STATUS.read_text())
    except (OSError, ValueError):
        return {}


def _alive(pid) -> bool:
    import os

    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def watch(app) -> None:
    def loop():
        while True:
            try:
                check(app)
            except Exception:
                log.exception("shop watcher")
            time.sleep(3)

    threading.Thread(target=loop, daemon=True, name="shop-watch").start()


def check(app=None) -> None:
    r = db.one("SELECT * FROM runs WHERE outcome = 'printing' ORDER BY id DESC LIMIT 1")
    if not r:
        return
    st = _stream_status()
    ours = st and st.get("gcode") == r["gcode"] and abs((st.get("started") or 0) - r["started"]) < 180
    if not ours:
        if time.time() - r["started"] > 300:  # the stream never started (or another print took over)
            close(r["id"], "failed", None, error="The print never started streaming.", app=app)
        return
    total = st.get("total") or 0
    db.update("runs", r["id"], progress=round(100 * (st.get("line") or 0) / total, 1) if total else None)
    state = st.get("state")
    if state in ("done", "failed", "cancelled"):
        close(r["id"], state, st, app=app)
    elif not _alive(st.get("pid")) and time.time() - r["started"] > 30:
        close(r["id"], "failed", st, error="The print process ended without finishing.", app=app)


def close(rid: int, outcome: str, st: dict | None, error: str = "", app=None) -> dict:
    r = db.get("runs", rid)
    j = db.get("jobs", r["job_id"]) or {}
    p = db.get("parts", j.get("part_id")) or {}
    o = db.get("orders", r["order_id"]) or {}
    mat = (p.get("options") or {}).get("material", "PLA")
    ended = (st or {}).get("finished") or time.time()
    hours = max(0.0, (ended - r["started"]) / 3600)
    try:
        if outcome == "done":
            use = gcode.used(r["gcode"], fraction=1.0, material=mat)
        else:
            use = gcode.used(r["gcode"], line=(st or {}).get("line") or 0, material=mat)
        grams = use["grams"]
    except OSError:
        use, grams = {}, 0.0
    cost = costing.run_cost(grams, hours, mat)
    loss = 0.0 if outcome == "done" else cost["total"]
    reason = r.get("reason") or error or (st or {}).get("error") or ("Stopped" if outcome == "cancelled" else "")
    db.update("runs", rid, outcome=outcome, ended=ended, grams=grams, hours=round(hours, 3), cost=cost["total"],
              loss=loss, breakdown={**cost, "use": use}, reason=reason or None,
              progress=100.0 if outcome == "done" else r.get("progress"))
    sp = db.one("SELECT id FROM spools WHERE active = 1 AND upper(material) = ? ORDER BY id DESC LIMIT 1", (mat.upper(),))
    if sp and grams:
        db.run("UPDATE spools SET used_g = used_g + ? WHERE id = ?", (grams, sp["id"]))
    label = f"{o.get('code')} {p.get('name') or o.get('title') or ''}".strip()
    if outcome == "done" and not j:  # printed with "print it" / the phone, not from the queue: the order is ready
        msg = f"Printed {label}: {grams:.0f} g, {hours:.1f} h, cost ₹{cost['total']:.0f}."
        if o and o.get("status") in ("quote", "confirmed", "printing"):
            db.update("orders", o["id"], updated=time.time())
    elif outcome == "done":
        done = (j.get("done_copies") or 0) + (r.get("plate_copies") or 1)
        db.update("jobs", j["id"], done_copies=done, status="done" if done >= j["copies"] else "waiting",
                  gcode_key=j.get("gcode_key") if done >= j["copies"] or _same_plate(j, done) else None,
                  updated=time.time())
        if done < j["copies"]:
            _prepare(j["id"])
        msg = f"Printed {label}: {grams:.0f} g, {hours:.1f} h, cost ₹{cost['total']:.0f}."
        left = db.one("SELECT COUNT(*) AS n FROM jobs WHERE order_id = ? AND status NOT IN ('done','cancelled')",
                      (o["id"],))
        if left and left["n"] == 0:
            db.update("orders", o["id"], status="ready", updated=time.time())
            msg += f" Order {o.get('code')} is ready for delivery."
    else:
        if j:
            db.update("jobs", j["id"], status="failed", updated=time.time())
        pct = (use.get("fraction") or 0) * 100
        msg = (f"{'Stopped' if outcome == 'cancelled' else 'Failed'}: {label} at {pct:.0f}%. "
               f"Lost {grams:.0f} g and {hours:.1f} h: ₹{loss:.0f}. It's waiting for a reprint.")
    db.event("batsy", f"run_{outcome}", msg, f"run:{rid}")
    sync.later(r["order_id"])
    log.info("shop: %s", msg)
    try:
        from batsy import push

        push.notify("PrintDesk", msg, tag="shop")
    except Exception:
        pass
    _spool_warning(mat)
    return db.get("runs", rid)


def _same_plate(j: dict, done: int) -> bool:
    return max(1, min(j["per_plate"], j["copies"] - done)) == max(1, min(j["per_plate"], j["copies"] - j["done_copies"]))


def _spool_warning(mat: str) -> None:
    sp = db.one("SELECT * FROM spools WHERE active = 1 AND upper(material) = ? ORDER BY id DESC LIMIT 1", (mat.upper(),))
    if not sp:
        return
    left = (sp["weight_g"] or 0) - (sp["used_g"] or 0)
    need = db.one("SELECT 1 FROM jobs WHERE status IN ('waiting','ready')")
    if left < 150 and need:
        db.event("batsy", "spool_low", f"{sp['material']} {sp['color'] or ''} spool has about {left:.0f} g left.")
