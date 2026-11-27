"""The company by voice (besides the admin page / PrintDesk Pro):

  "add the flute to the company"            a company order for that model, priced with your formula (a suggestion)
  "the flute print failed" / "...was a loss" the last print of it goes on the books as a loss (filament + machine time)
  "what's the quote for the flute"          the suggested price, and how it was worked out

The model is found the way slicing finds it: by name ("the flute" -> SingleFlute_F_Sharp_MAJOR) or who sent it.
"""
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("batsy.shop")
COMPANY = r"(?:the |my |our )?(?:[a-z]+ )?(?:company|companies|business|shop|kada company)"
ADD = re.compile(rf"\b(?:add|ads|put|move|make)\b (?P<what>.+?) (?:to|into|under|for|in|as) {COMPANY}(?: order| job)?\b|"
                 rf"\b(?P<what2>.+?) (?:is|was) (?:for|a job for|an order for) {COMPANY}\b")
FAILED = re.compile(r"\b(?:the )?(?P<what>.+?) print (?:failed|was a loss|broke|spaghettied|came off|got knocked)\b|"
                    r"\b(?:record|log|mark) (?:the )?(?P<what2>.+?)(?: print)? as (?:a )?(?:failed|loss)\b")
QUOTE = re.compile(r"\b(?:what(?:'?s| is)|how much is|give me) (?:the )?(?:quote|price) (?:for|of) (?:the )?(?P<what>.+)$")
HEARD = {"flew": "flute", "flu": "flute", "flutes": "flute", "floot": "flute"}  # how the mic hears it


def _model(app, what: str) -> Path | None:
    from batsy import slicer

    what = " ".join(HEARD.get(w, w) for w in what.split())
    if what in ("it", "this", "that", "the last one", "this one"):
        last = getattr(app, "slice_last", None) or {}
        m = Path(last["model"]) if last.get("model") else None
    else:
        m = slicer.resolve_model(f"slice {what}")
    if m is None:
        return None
    orig = m.with_name(re.sub(r" (?:laid|turned)$", "", m.stem) + m.suffix)  # the file as sent, not a turned copy
    return orig if orig.exists() else m


def _order_for(model: Path) -> dict | None:
    from batsy.shop import db

    name = re.sub(r" (?:laid|turned)$", "", model.stem).replace("_", " ")
    return db.one("SELECT * FROM orders WHERE title = ? AND status != 'cancelled' ORDER BY id DESC LIMIT 1", (name,))


def add(app, model: Path, who: str = "Jonathan") -> str:
    from batsy.shop import jobs, quote

    name = re.sub(r" (?:laid|turned)$", "", model.stem).replace("_", " ")
    existing = _order_for(model)
    if existing:
        return f"{name} is already in the company as order {existing['code']}."
    sender = model.parent.name if model.parent.name not in ("Jonathan", "") else ""
    o = jobs.create_order(who, customer={"name": sender} if sender else None, title=name,
                          notes=f"From {sender or 'you'}: {model.name}")
    quote.add_part(o["id"], model.name, src=model)
    log.info("company: %s added as %s", name, o["code"])
    return o["code"]


def record_failure(app, model: Path, reason: str = "") -> str | None:
    """The newest print of that model, on the books as a failed run (everything it used is a loss)."""
    from batsy import slicer
    from batsy.config import DATA_DIR
    from batsy.shop import costing, db

    stem = re.sub(r" (?:laid|turned)$", "", model.stem)
    try:
        hist = json.loads((DATA_DIR / "prints" / "history.json").read_text())
    except (OSError, ValueError):
        hist = []
    pick = next((h for h in reversed(hist) if Path(h["gcode"]).stem.startswith(stem)), None)
    if not pick:
        return None
    o = _order_for(model)
    if db.one("SELECT id FROM runs WHERE gcode = ? AND outcome = 'failed'", (pick["gcode"],)):
        return "already"
    est = slicer._estimates(Path(pick["gcode"])) if Path(pick["gcode"]).exists() else {}
    grams, hours = float(est.get("grams") or 0), slicer.hours(est.get("time") or "")
    c = costing.run_cost(grams, hours)
    db.insert("runs", order_id=o["id"] if o else None, gcode=pick["gcode"], started=pick["t"],
              ended=pick["t"] + hours * 3600, outcome="failed", progress=100, grams=grams, hours=hours,
              cost=c["total"], loss=c["total"], breakdown=json.dumps(c), reason=reason or "failed", stopped_by="Jonathan")
    db.event("Jonathan", "run", f"Failed print of {stem.replace('_', ' ')}: ₹{c['total']:.0f} lost", f"order:{o['id']}" if o else "")
    return f"₹{c['total']:.0f}"


def adopt_print(gcode: Path, keep: Path, started: float) -> None:
    """A print started with "print it" / the phone's Print button, of a part that's in a company order: tracked like
    a queue print, so a stop, a failure or a finish goes on the books with the laptop's real time and filament
    (stopped half way: the grams up to that line and the hours it ran, all a loss)."""
    from batsy.shop import db

    if db.one("SELECT id FROM runs WHERE outcome = 'printing'"):
        return  # the company queue started this one itself
    stem = re.sub(r" (?:laid|turned)$", "", gcode.stem.rsplit(" - ", 1)[0])
    o = _order_for(Path(stem + ".stl"))
    if not o:
        return
    db.insert("runs", order_id=o["id"], job_id=None, plate_copies=1, gcode=str(keep), started=started,
              outcome="printing", reason=None)
    db.event("batsy", "run_started", f"Printing {o['code']} {o['title']}", f"order:{o['id']}")
    log.info("company: tracking the print of %s (%s)", o["title"], o["code"])


def suggested(model: Path) -> str:
    from batsy.shop import quote

    o = _order_for(model)
    if not o:
        return ""
    pr = quote.reprice(o["id"])
    if not pr or not pr.get("price"):
        return f"I'm still working out {o['title']}'s numbers; ask me again in a minute."
    line = (pr.get("lines") or [{}])[0]
    return (f"Suggested price for {o['title']}: ₹{pr['price']:.0f}, from {line.get('grams', 0):.0f} grams and "
            f"{line.get('hours', 0):.1f} hours at {pr.get('formula', 'your rates')}. The final price is yours.")


def handle(app, n: str) -> str | None:
    m = ADD.search(n)
    if m:
        what = (m.group("what") or m.group("what2") or "").strip()
        what = re.sub(r"^(?:the|this|that|my) ", "", what) if what not in ("it", "this", "that") else what
        model = _model(app, what)
        if model is None:
            return f"I can't find a model called {what}. Send it to me first, or say its name again."
        code = add(app, model)
        if " " in code:  # already there
            return code
        lost = record_failure(app, model, "it failed earlier") if re.search(r"\b(?:fail|loss|lost)", n) else None
        name = model.stem.replace("_", " ")
        return (f"Added {name} to the company as order {code}." + (f" The failed print is on the books as a {lost} loss."
                                                                   if lost and lost != "already" else "")
                + " I'll work out a suggested price from its slice.")
    m = FAILED.search(n)
    if m:
        what = (m.group("what") or m.group("what2") or "").strip()
        model = _model(app, re.sub(r"^(?:the|this|that|my) ", "", what))
        if model is None:
            return None
        lost = record_failure(app, model, "failed print")
        if lost is None:
            return f"I haven't printed {model.stem.replace('_', ' ')} yet."
        if lost == "already":
            return "That failed print is already on the books."
        return f"Recorded: the failed {model.stem.replace('_', ' ')} print cost {lost}, all of it a loss."
    m = QUOTE.search(n)
    if m:
        model = _model(app, re.sub(r"^(?:the|this|that|my) ", "", m.group("what").strip()))
        return (suggested(model) or None) if model else None
    return None
