"""/api/shop/... on Batsy's server (port 8765, the phone hub's): what PrintDesk Pro, the phone app and the
cockpit use.

Who's asking (the tailnet decides, nobody signs in):
  - the paired phone app (its Bearer token): the admin
  - PrintDesk Pro's server on this workstation, from 127.0.0.1 with the proxy key (shop/proxy.key, readable only
    by you), passing the visitor's own address in X-Shop-Client-IP: that address is looked up below
  - anyone else straight from a tailnet address (100.64.0.0/10, fd7a:115c:a1e0::/48)
  An address is `tailscale whois`-ed to a login: this workstation's own login (or someone you made an admin) is
  the admin, logins on the staff list are staff, anyone else gets a 403 that names their login so you can add them.
  127.0.0.1 behind the proxy is you at the workstation: the admin.

Staff make quotes and orders, upload models, change their settings, confirm (that queues them) and see the queue.
Only the admin prints, stops, reorders, reprints, sets the price list, and sees costs, losses and reports.
"""
import gzip
import ipaddress
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from batsy.shop import brain, costing, db, jobs, quote, reports, sync

log = logging.getLogger("batsy.shop.api")
TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
KEY_FILE = db.HOME / "proxy.key"
_who: dict[str, tuple[float, dict | None]] = {}


def proxy_key() -> str:
    import os

    db.HOME.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        KEY_FILE.write_text(secrets.token_urlsafe(32))
        os.chmod(KEY_FILE, 0o600)
    return KEY_FILE.read_text().strip()


class Denied(Exception):
    def __init__(self, code: int, msg: str, login: str = ""):
        super().__init__(msg)
        self.code, self.login = code, login


def identify(app, h) -> dict:
    from batsy import inbox, phone

    owner = app.cfg.get("user.name") or "Jonathan"
    got = h.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if got and secrets.compare_digest(got, phone.token()):
        return {"login": "phone", "name": owner, "role": "admin"}
    ip = h.client_address[0]
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise Denied(403, "Unknown address.")
    if addr.is_loopback:
        k = h.headers.get("X-Shop-Proxy", "")
        if not (k and secrets.compare_digest(k, proxy_key())):
            raise Denied(403, "Local requests need PrintDesk's proxy key.")
        ip = h.headers.get("X-Shop-Client-IP", "").strip() or "127.0.0.1"
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            raise Denied(403, "Bad client address.")
        if addr.is_loopback:  # you, at the workstation
            return {"login": "local", "name": owner, "role": "admin"}
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
            ip = str(addr)
    if not any(addr in net for net in TAILNET):
        raise Denied(403, "Open PrintDesk over Tailscale.")
    hit = _who.get(ip)
    if hit and time.time() - hit[0] < 300:
        if hit[1] is None:
            raise Denied(403, "Not on the team.", _who.get("login:" + ip, (0, ""))[1] or "")
        return hit[1]
    w = inbox.whois(ip)
    login = w[0] if w else ""
    p = None
    if login and login == inbox.my_login():
        p = {"login": login, "name": owner, "role": "admin", "device": w[1]}
    elif login:
        s = db.one("SELECT * FROM staff WHERE login = ?", (login,))
        if s:
            p = {"login": login, "name": s["name"] or login, "role": s["role"] or "staff", "device": w[1]}
    _who[ip] = (time.time(), p)
    _who["login:" + ip] = (time.time(), login)
    if p is None:
        raise Denied(403, f"You're {login or 'unknown'} on Tailscale. Ask {owner} to add you to PrintDesk.", login)
    return p


def _admin(p: dict) -> None:
    if p["role"] != "admin":
        raise Denied(403, "Only the admin can do that.")


def serve(h, app, method: str) -> bool:
    """On the hub's handler: True if the path was ours (answered)."""
    url = urlparse(h.path)
    if not url.path.startswith("/api/shop"):
        return False
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    try:
        p = identify(app, h)
    except Denied as e:
        h._send(e.code, {"error": str(e), "login": e.login})
        return True
    try:
        if method == "POST" and url.path == "/api/shop/upload":
            r = _upload(h, p, q)
        else:
            body = h._body() if method == "POST" else {}
            r = route(app, p, method, url.path, q, body, h)
    except Denied as e:
        r = None
        h._send(e.code, {"error": str(e)})
    except (ValueError, KeyError, TypeError) as e:
        r = None
        h._send(400, {"error": str(e)[:300]})
    except Exception as e:
        log.exception("shop %s %s", method, url.path)
        r = None
        h._send(500, {"error": str(e)[:300]})
    if r is not None:
        h._send(200, r)
    return True


def _upload(h, p: dict, q: dict) -> dict:
    n = int(h.headers.get("Content-Length") or 0)
    oid = int(q.get("order") or 0)
    if not oid or not db.get("orders", oid):
        raise ValueError("Which order?")
    opts = json.loads(unquote(q["options"])) if q.get("options") else {}
    part = quote.add_part(oid, unquote(q.get("name", "model.stl")), h.rfile, n, options=opts, qty=int(q.get("qty") or 1))
    db.event(p["name"], "upload", f"{part['name']} added to {db.get('orders', oid)['code']}", f"part:{part['id']}")
    return {"ok": True, "part": jobs.part_view(part)}


def _file(h, path: str | None, ctype: str, zipped: bool = False) -> None:
    if not path or not Path(path).exists():
        h._send(404, {"error": "not there"})
        return
    raw = Path(path).read_bytes()
    if zipped and "gzip" in (h.headers.get("Accept-Encoding") or ""):
        z = gzip.compress(raw, 5)
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Encoding", "gzip")
        h.send_header("Content-Length", str(len(z)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(z)
        return
    h._send(200, body=raw, ctype=ctype)


def overview(app, p: dict) -> dict:
    pr = getattr(app, "printer3d", None)
    out = {"me": p, "queue": jobs.queue_list(), "printer": pr.snapshot() if pr else None,
           "today": reports.today_line(), "events": db.events(15),
           "running": db.one("SELECT * FROM runs WHERE outcome = 'printing' ORDER BY id DESC LIMIT 1"),
           "counts": {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}}
    if p["role"] == "admin":
        out["month"] = reports.pnl(30)
        out["spools"] = reports.spools()
        # quotes waiting for your price (the formula's suggestion; yours is final), and what failed prints cost lately
        out["quotes"] = [{"id": o["id"], "code": o["code"], "title": o["title"], "price": o.get("price") or 0,
                          "override": o.get("price_override")}
                         for o in db.q("SELECT * FROM orders WHERE status = 'quote' ORDER BY updated DESC LIMIT 5")]
        out["losses"] = [{"title": (db.get("orders", r["order_id"]) or {}).get("title", "print"), "loss": r["loss"] or 0,
                          "grams": r["grams"] or 0, "hours": r["hours"] or 0, "outcome": r["outcome"], "t": r["ended"]}
                         for r in db.q("SELECT * FROM runs WHERE outcome IN ('failed','cancelled') AND loss > 0 "
                                       "ORDER BY ended DESC LIMIT 3")]
    return out


def route(app, p: dict, method: str, path: str, q: dict, b: dict, h):
    who = p["name"]
    admin = p["role"] == "admin"
    post = method == "POST"
    a = b.get("action", "")

    if path == "/api/shop/me":
        return p
    if path == "/api/shop/overview":
        return overview(app, p)
    if path == "/api/shop/presets":
        from batsy import slicer

        return {"processes": slicer.processes(), "default": slicer.settings().get("default") or slicer.DEFAULT_PROCESS,
                "materials": list(costing.rates()["materials"]), "slicer_ready": slicer.ready(), "bed": quote.BED}

    # orders
    if path == "/api/shop/orders":
        if post:
            return jobs.create_order(who, b.get("customer"), b.get("title", ""), b.get("due", ""), b.get("notes", ""),
                                     b.get("kind", "custom"))
        return {"orders": jobs.orders(q.get("status", ""), int(q.get("limit", 200)))}
    if path == "/api/shop/order":
        oid = int(b.get("id") or q.get("id") or 0)
        if not post:
            return jobs.order(oid) or {"error": "No such order."}
        if a == "confirm":
            return jobs.confirm(oid, who)
        if a == "edit":
            f = {k: v for k, v in b.items() if k not in ("id", "action")}
            if not admin:
                for k in ("price_override", "discount", "paid", "status"):
                    f.pop(k, None)
            return jobs.edit_order(oid, who, **f)
        if a in ("deliver", "cancel"):
            if a == "cancel":
                for j in db.q("SELECT id FROM jobs WHERE order_id = ? AND status != 'printing'", (oid,)):
                    jobs.cancel_job(j["id"], who)
            return jobs.edit_order(oid, who, status="delivered" if a == "deliver" else "cancelled")
        if a == "pay":
            _admin(p)
            o = db.get("orders", oid)
            return jobs.edit_order(oid, who, paid=(o["paid"] or 0) + float(b.get("amount") or 0))
        if a == "reprice":
            _admin(p)
            pr = quote.reprice(oid)
            db.update("orders", oid, price=pr["price"], updated=time.time())
            return jobs.order(oid)
        if a == "delete":
            _admin(p)
            if db.one("SELECT 1 FROM jobs WHERE order_id = ? AND status = 'printing'", (oid,)):
                raise ValueError("It's printing.")
            for t in ("jobs", "parts"):
                db.run(f"DELETE FROM {t} WHERE order_id = ?", (oid,))
            db.run("DELETE FROM orders WHERE id = ?", (oid,))
            return {"ok": True}
        raise ValueError(f"Unknown action {a}")

    if path == "/api/shop/link" and post:  # an order on the team's site -> its Batsy order (made once)
        return jobs.order(sync.link(b.get("order") or {}, who)["id"])
    if path == "/api/shop/sync":
        return {"enabled": sync.enabled()}

    # parts (the virtual slicer's results)
    if path == "/api/shop/part":
        pid = int(b.get("id") or q.get("id") or 0)
        if not post:
            part = db.get("parts", pid)
            return jobs.part_view(part) if part else {"error": "No such part."}
        if a == "set":
            return jobs.set_part(pid, who, b.get("options"), b.get("qty"))
        if a == "remove":
            jobs.remove_part(pid, who)
            return {"ok": True}
        if a == "manual":  # a part without a file: your grams and hours
            part = quote.add_manual_part(int(b["order"]), b.get("name", ""), b.get("grams", 0), b.get("hours", 0),
                                         b.get("qty", 1), b.get("options"))
            return jobs.part_view(part)
        if a == "reslice":
            quote.queue_slice(pid)
            return {"ok": True}
        raise ValueError(f"Unknown action {a}")
    if path == "/api/shop/part/preview":
        part = db.get("parts", int(q.get("id", 0))) or {}
        _file(h, (part.get("slice") or {}).get("preview"), "image/png")
        return None
    if path == "/api/shop/part/paths":
        part = db.get("parts", int(q.get("id", 0))) or {}
        _file(h, (part.get("slice") or {}).get("paths"), "application/json", zipped=True)
        return None
    if path == "/api/shop/part/model":
        part = db.get("parts", int(q.get("id", 0))) or {}
        f = quote._mesh_file(part) if part else None
        _file(h, str(f) if f else None, "model/stl", zipped=True)
        return None

    # the queue and the printer
    if path == "/api/shop/queue":
        if not post:
            return {"queue": jobs.queue_list()}
        _admin(p)
        jid = int(b.get("id") or 0)
        if a == "reorder":
            return {"queue": jobs.reorder([int(i) for i in b.get("ids") or []], who)}
        if a == "move":
            return {"queue": jobs.move(jid, str(b.get("where", "top")), who)}
        if a == "print":
            return jobs.print_job(app, jid, who)
        if a == "reprint":
            return jobs.reprint(jid, who)
        if a == "cancel":
            jobs.cancel_job(jid, who)
            return {"queue": jobs.queue_list()}
        if a == "set":
            return jobs.set_job(jid, who, b.get("per_plate"), b.get("note"))
        if a == "stop":
            return jobs.stop(app, who, str(b.get("reason", "")))
        if a == "pause":
            return jobs.pause(app)
        if a == "resume":
            return jobs.resume(app)
        raise ValueError(f"Unknown action {a}")
    if path == "/api/shop/printer":
        pr = getattr(app, "printer3d", None)
        return {"printer": pr.snapshot() if pr else None,
                "running": db.one("SELECT * FROM runs WHERE outcome = 'printing' ORDER BY id DESC LIMIT 1")}

    if path == "/api/shop/ask" and post:  # staff too (their picture has no money in it)
        return ask_route(app, p, b)

    # everything below is the admin's
    _admin(p)
    if path == "/api/shop/runs":
        return {"runs": reports.runs(int(q.get("limit", 50)))}
    if path == "/api/shop/failures":
        return {"failures": reports.failures(int(q.get("limit", 30)))}
    if path == "/api/shop/run" and post:
        jobs.set_reason(int(b["id"]), str(b.get("reason", "")), who)
        return {"ok": True}
    if path == "/api/shop/rates":
        return {"rates": costing.set_rates(b.get("rates") or {}) if post else costing.rates(),
                "defaults": costing.DEFAULT_RATES}
    if path == "/api/shop/price":  # a quick price for numbers alone (the pricing page's calculator)
        return costing.order_price([b] if post else [{"grams": float(q.get("grams", 0)),
                                                     "hours": float(q.get("hours", 0)),
                                                     "material": q.get("material", "PLA")}])
    if path == "/api/shop/reports":
        days = int(q.get("days", 30))
        return {"pnl": reports.pnl(days), "daily": reports.daily(min(days, 90)), "failures": reports.failures(10)}
    if path == "/api/shop/spools":
        if post:
            sid = int(b.get("id") or 0)
            if a == "add":
                sid = db.insert("spools", material=str(b.get("material", "PLA")).upper(), color=b.get("color", ""),
                                brand=b.get("brand", ""), price=float(b.get("price") or 0),
                                weight_g=float(b.get("weight_g") or 1000), used_g=0, active=0, added=time.time())
                a = "activate" if b.get("active") else ""
            if a == "activate":
                s = db.get("spools", sid)
                db.run("UPDATE spools SET active = 0 WHERE upper(material) = ?", (s["material"].upper(),))
                db.update("spools", sid, active=1)
            elif a == "finish":
                db.update("spools", sid, active=0, finished=time.time())
            elif a == "edit":
                db.update("spools", sid, **{k: b[k] for k in ("color", "brand", "price", "weight_g", "used_g") if k in b})
        return {"spools": reports.spools()}
    if path == "/api/shop/expenses":
        if post:
            if a == "delete":
                db.run("DELETE FROM expenses WHERE id = ?", (int(b["id"]),))
            else:
                db.insert("expenses", t=float(b.get("t") or time.time()), category=b.get("category", "other"),
                          amount=float(b.get("amount") or 0), note=b.get("note", ""), who=who)
        return {"expenses": db.q("SELECT * FROM expenses ORDER BY t DESC LIMIT 200")}
    if path == "/api/shop/customers":
        if post and a == "edit":
            db.update("customers", int(b["id"]), **{k: b[k] for k in ("name", "phone", "email", "notes") if k in b})
        rows = db.q("SELECT c.*, COUNT(o.id) AS orders, "
                    "SUM(CASE WHEN o.status='delivered' THEN coalesce(o.price_override, o.price) ELSE 0 END) AS spent "
                    "FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id ORDER BY spent DESC")
        return {"customers": rows}
    if path == "/api/shop/staff":
        if post:
            login = str(b.get("login", "")).strip().lower()
            if a == "remove":
                db.run("DELETE FROM staff WHERE login = ?", (login,))
            elif login:
                db.run("INSERT INTO staff (login, name, role, added) VALUES (?, ?, ?, ?) ON CONFLICT(login) DO "
                       "UPDATE SET name = excluded.name, role = excluded.role",
                       (login, b.get("name") or login, "admin" if b.get("role") == "admin" else "staff", time.time()))
            _who.clear()
        return {"staff": db.q("SELECT * FROM staff ORDER BY name")}
    if path == "/api/shop/events":
        return {"events": db.events(int(q.get("limit", 100)), q.get("kind", ""))}
    if path == "/api/shop/brain/apply" and post:
        return brain.apply(b.get("action") or {}, who)
    raise ValueError("No such thing.")


def start(app) -> None:
    """From the app, with the hub: the database, the proxy key for PrintDesk, the print watcher."""
    import threading

    db.connect()
    proxy_key()
    jobs.watch(app)

    def rates_loop():  # the team's price list (Supabase pricing_rates) is Batsy's
        while True:
            try:
                sync.pull_rates()
            except Exception:
                log.exception("shop: rates from the site")
            time.sleep(300)

    threading.Thread(target=rates_loop, daemon=True, name="shop-rates").start()


def ask_route(app, p: dict, b: dict) -> dict:
    return brain.ask(str(b.get("question", "")), p["name"], p["role"] == "admin",
                     app.cfg.get("user.name") or "Jonathan", bool(b.get("claude")))
