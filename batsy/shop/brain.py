"""The business brain: Batsy's local model first, Claude Code (on this workstation, your Pro plan) when it's hard.

Every question gets a compact picture of the business as it is right now (the queue, today, the last 30 days,
recent failures, the price list, what just happened), built here from business.db: no tools, nothing to look up.

  local   short factual questions about that picture ("what's printing", "how much did we lose this week").
          It's told to answer only from the data and to say ESCALATE when the data doesn't answer it.
  Claude  anything that needs judgement (why, should, plan, price this, draft a message to a customer, compare,
          forecast), anything long, and whatever the local model escalates or can't answer. Claude Code runs in an
          empty folder with web search only: it can't touch files or run anything.

Either may propose changes as lines `ACTION {json}` (reorder the queue, set a price, change a rate). They're never
applied by themselves: they come back as suggestions, and only the admin applies them (api: /api/shop/brain/apply).
Staff get the queue and orders in their picture, not the money.
"""
import json
import logging
import re
import tempfile
import time
from pathlib import Path

from batsy.shop import costing, db, jobs, reports

log = logging.getLogger("batsy.shop.brain")
HARD = re.compile(r"\b(why|should|could we|plan|strategy|forecast|predict|analy[sz]e|analysis|compare|recommend|"
                  r"suggest|advice|advise|improve|optimi[sz]e|reorder|schedule|draft|write|message|email|reply|"
                  r"quote for|price (?:for|this|it)|how much should|negotiat|explain|best way|what if|trend)\b", re.I)

ACTIONS_HELP = """If a change would help, add at the very end one line per change, exactly:
ACTION {"type": "reorder", "ids": [job ids, first to print first]}
ACTION {"type": "price", "order": order id, "price": rupees}
ACTION {"type": "rate", "key": rate name, "value": number}
ACTION {"type": "note", "order": order id, "text": "..."}
Only propose what the data supports. Nothing is applied until the owner taps it."""

LOCAL_SYSTEM = """You are Batsy, running {owner}'s 3D printing company's admin desk. Answer in 1-3 short sentences,
only from the business data below. Money is in rupees (₹). If the data doesn't answer the question, or it needs
judgement or advice, reply with exactly: ESCALATE

{data}"""

CLAUDE_BRIEF = """You are the business brain behind PrintDesk Pro, {owner}'s 3D printing company (one Creality
Ender-5 Pro, 220x220x300 mm bed, 0.4 mm nozzle, Bowden, mostly PLA; OrcaSlicer). {who} is asking. You can search
the web (e.g. market prices), nothing else. Be concrete and brief; use the numbers. Markdown is fine.

{actions}

The business right now:
{data}

Question: {question}"""


def snapshot(money: bool = True) -> str:
    q = jobs.queue_list()
    lines = ["QUEUE (in print order):"]
    for j in q[:15]:
        lines.append(f"- job {j['id']} [{j['status']}] {j['order_code']} {j['part']} x{j['copies'] - j['done_copies']}"
                     f" left, {j['left_hours']} h, {(j['options'] or {}).get('material', 'PLA')}"
                     f"{', due ' + j['due'] if j.get('due') else ''}{', customer ' + j['customer'] if j['customer'] else ''}")
    if not q:
        lines.append("- empty")
    open_orders = db.q("SELECT o.id, o.code, o.title, o.status, o.due, o.price, o.price_override, o.paid, c.name AS cust "
                       "FROM orders o LEFT JOIN customers c ON c.id = o.customer_id "
                       "WHERE o.status NOT IN ('delivered','cancelled') ORDER BY o.id DESC LIMIT 20")
    lines.append("OPEN ORDERS:")
    for o in open_orders:
        price = o["price_override"] if o["price_override"] is not None else o["price"]
        lines.append(f"- order {o['id']} {o['code']} '{o['title']}' {o['status']}"
                     f"{', ' + o['cust'] if o['cust'] else ''}{', due ' + o['due'] if o['due'] else ''}"
                     + (f", ₹{price:.0f}, paid ₹{o['paid'] or 0:.0f}" if money else ""))
    lines.append("TODAY: " + reports.today_line())
    if money:
        p = reports.pnl(30)
        lines.append("LAST 30 DAYS: " + json.dumps({k: v for k, v in p.items() if k not in ("from", "to")}))
        r = costing.rates()
        lines.append("PRICE LIST: " + json.dumps(r))
        f = reports.failures(6)
        if f:
            lines.append("RECENT FAILED/STOPPED PRINTS:")
            for x in f:
                lines.append(f"- {time.strftime('%d %b %H:%M', time.localtime(x['started']))} {x['code']} {x['part']}: "
                             f"{x['outcome']} at {x['progress'] or 0:.0f}%, lost {x['grams'] or 0:.0f} g / ₹{x['loss'] or 0:.0f}"
                             f"{', reason: ' + x['reason'] if x.get('reason') else ''}")
        sp = reports.spools()
        if sp:
            lines.append("SPOOLS: " + "; ".join(f"{s['material']} {s['color'] or ''} {s['left_g']:.0f} g left"
                                                f"{' (active)' if s['active'] else ''}" for s in sp[:6]))
    ev = db.events(8)
    if ev:
        lines.append("JUST HAPPENED:")
        for e in ev:
            lines.append(f"- {time.strftime('%d %b %H:%M', time.localtime(e['t']))} {e['text']}")
    return "\n".join(lines)


def _local(question: str, data: str, owner: str) -> str:
    from batsy import claude_code, llm_hosts
    from batsy.config import Config

    CFG = claude_code.CFG or Config.load()
    body = {"model": CFG.get("local.model"), "stream": False, "keep_alive": CFG.get("local.keep_alive"),
            "options": {"temperature": 0.2, "num_ctx": 4096},
            "messages": [{"role": "system", "content": LOCAL_SYSTEM.format(owner=owner, data=data)},
                         {"role": "user", "content": question}]}
    with llm_hosts.live():
        resp = llm_hosts.open_chat(CFG, body, timeout=90)
        out = json.loads(resp.read())
    return str((out.get("message") or {}).get("content") or "").strip()


def _claude(question: str, data: str, owner: str, who: str, admin: bool) -> str:
    from batsy import claude_code

    work = Path(tempfile.mkdtemp(prefix="batsy-shop-"))
    none = work / "no-mcp.json"
    none.write_text('{"mcpServers": {}}')
    prompt = CLAUDE_BRIEF.format(owner=owner, who=who, data=data, question=question,
                                 actions=ACTIONS_HELP if admin else "Don't propose changes.")
    tools = "WebSearch,WebFetch"
    return claude_code.run(prompt, "PrintDesk", cwd=work, timeout=420,
                           args=["--tools", tools, "--allowedTools", tools, "--permission-mode", "dontAsk",
                                 "--strict-mcp-config", "--mcp-config", str(none)])


def split_actions(text: str) -> tuple[str, list[dict]]:
    acts, keep = [], []
    for line in text.splitlines():
        m = re.match(r"^\s*ACTION\s+(\{.*\})\s*$", line)
        if m:
            try:
                a = json.loads(m.group(1))
                if a.get("type") in ("reorder", "price", "rate", "note"):
                    acts.append(a)
                continue
            except ValueError:
                pass
        keep.append(line)
    return "\n".join(keep).strip(), acts


def ask(question: str, who: str = "", admin: bool = True, owner: str = "Jonathan", force_claude: bool = False) -> dict:
    question = (question or "").strip()[:2000]
    if not question:
        return {"answer": "", "by": "", "actions": []}
    data = snapshot(money=admin)
    by, answer = "batsy", ""
    hard = force_claude or bool(HARD.search(question)) or len(question.split()) > 25
    if not hard:
        try:
            answer = _local(question, data, owner)
        except Exception as e:
            log.info("shop brain: local model unavailable (%s)", e)
            answer = ""
        if not answer or "ESCALATE" in answer.upper() or len(answer) < 3:
            answer = ""
    if not answer:
        by = "claude"
        answer = _claude(question, data, owner, who or owner, admin)
    answer, acts = split_actions(answer)
    if not admin:
        acts = []
    db.event(who or "admin", "ask", f"Asked: {question[:120]} -> {by}")
    return {"answer": answer, "by": by, "actions": acts}


def apply(action: dict, who: str) -> dict:
    """Only from the admin, one suggestion at a time."""
    t = action.get("type")
    if t == "reorder":
        return {"ok": True, "queue": jobs.reorder([int(i) for i in action.get("ids") or []], who)}
    if t == "price":
        jobs.edit_order(int(action["order"]), who, price_override=float(action["price"]))
        return {"ok": True}
    if t == "rate":
        return {"ok": True, "rates": costing.set_rates({action["key"]: action["value"]})}
    if t == "note":
        o = db.get("orders", int(action["order"]))
        jobs.edit_order(o["id"], who, notes=((o.get("notes") or "") + "\n" + str(action.get("text", ""))).strip())
        return {"ok": True}
    return {"ok": False, "say": "Unknown suggestion."}
