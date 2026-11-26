"""`batsy shop ...`: the company from the terminal.

  batsy shop status                 today, the queue, the last 30 days
  batsy shop queue                  the queue in print order
  batsy shop ask "question"         the business brain (local model, Claude Code when it's hard)
  batsy shop key                    PrintDesk Pro's proxy key (its .env.local BATSY_SHOP_KEY)
  batsy shop staff add LOGIN NAME   let a tailnet login use PrintDesk (staff); `staff admin LOGIN NAME`; `staff rm LOGIN`
  batsy shop import-supabase        old PrintDesk orders and price list, from Supabase (SUPABASE_URL and
                                    SUPABASE_SERVICE_ROLE_KEY in the environment, or --env path/to/.env)
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from batsy.shop import api, brain, costing, db, jobs, quote, reports


def _env(path: str | None) -> dict:
    env = dict(os.environ)
    if path:
        for line in Path(path).expanduser().read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def import_supabase(env_path: str | None = None) -> str:
    env = _env(env_path)
    url = (env.get("SUPABASE_URL") or env.get("NEXT_PUBLIC_SUPABASE_URL") or "").rstrip("/")
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        return "Need SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."

    def get(table: str, extra: str = "") -> list:
        req = urllib.request.Request(f"{url}/rest/v1/{table}?select=*{extra}",
                                     headers={"apikey": key, "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def ts(s: str | None) -> float:
        if not s:
            return time.time()
        from datetime import datetime

        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    n = 0
    status = {"pending": "confirmed", "printing": "printing", "ready_for_delivery": "ready", "delivered": "delivered",
              "cancelled": "cancelled"}
    for o in get("orders", "&order=created_at.asc"):
        if db.one("SELECT id FROM orders WHERE external_id = ?", (o["id"],)):
            continue
        cust = {"name": o.get("customer_name") or "Unknown"}
        made = jobs.create_order("import", cust, o.get("item_description") or "", o.get("due_date") or "",
                                 o.get("notes") or "", o.get("type") or "custom")
        quote.add_manual_part(made["id"], o.get("item_description") or "Part", float(o.get("filament_grams") or 0),
                              float(o.get("print_hours") or 0))
        st = status.get(o.get("status"), "confirmed")
        db.update("orders", made["id"], status=st, price=float(o.get("final_price") or 0), external_id=o["id"],
                  created=ts(o.get("created_at")), updated=ts(o.get("updated_at")),
                  delivered=ts(o.get("updated_at")) if st == "delivered" else None,
                  paid=float(o.get("final_price") or 0) if st == "delivered" else 0)
        n += 1
    rates = get("pricing_rates", "&order=updated_at.desc&limit=1")
    if rates:
        costing.set_rates({k: v for k, v in rates[0].items() if k in costing.DEFAULT_RATES})
    return f"Imported {n} orders" + (" and the price list." if rates else ".")


def main(argv: list[str]) -> None:
    db.connect()
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(reports.today_line())
        print(json.dumps(reports.pnl(30), indent=1))
    elif cmd == "queue":
        for j in jobs.queue_list():
            print(f"{j['id']:>4} {j['status']:<9} {j['order_code']} {j['part']} x{j['copies'] - j['done_copies']} "
                  f"{j['left_hours']} h{'  due ' + j['due'] if j.get('due') else ''}")
    elif cmd == "ask":
        r = brain.ask(" ".join(argv[1:]), "terminal", True)
        print(f"[{r['by']}] {r['answer']}")
        for a in r["actions"]:
            print("suggests:", json.dumps(a))
    elif cmd == "key":
        print(api.proxy_key())
    elif cmd == "staff" and len(argv) >= 3:
        if argv[1] in ("rm", "remove"):
            db.run("DELETE FROM staff WHERE login = ?", (argv[2].lower(),))
        else:
            role = "admin" if argv[1] == "admin" else "staff"
            db.run("INSERT INTO staff (login, name, role, added) VALUES (?, ?, ?, ?) ON CONFLICT(login) DO UPDATE SET "
                   "name = excluded.name, role = excluded.role",
                   (argv[2].lower(), " ".join(argv[3:]) or argv[2], role, time.time()))
        print(json.dumps(db.q("SELECT * FROM staff"), indent=1))
    elif cmd == "import-supabase":
        env = argv[argv.index("--env") + 1] if "--env" in argv else None
        print(import_supabase(env))
    else:
        print(__doc__)
        sys.exit(1)
