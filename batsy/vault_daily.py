"""The brain's daily rhythm.

- End of day (the first check after 5 AM, or straight away on "good night"): the free local model reads the day's
  note (plan, timeline, files, prints, music) and writes `## Summary`: a few lines, plus open loops.
- Sunday's day closes the week: Claude (Pro plan) writes `01 Daily/Weekly/<year>-W<nn>.md` from the seven days:
  wins, what moved on each project, open loops, a suggestion or two. If Claude isn't available, the local model does.
- Home.md's "today" and "recent" blocks are refreshed every few minutes.
"""
import datetime as dt
import json
import logging
import re
import threading
import time
import urllib.request

from batsy import vault
from batsy.config import DATA_DIR

log = logging.getLogger("batsy.vault")
STATE = DATA_DIR / "vault_state.json"
_cfg = None


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(**kv) -> None:
    STATE.write_text(json.dumps({**_state(), **kv}))


def local_model(prompt: str, max_tokens: int = 350, label: str = "Local AI", background: bool = False) -> str:
    """One answer from the local Ollama model (free, offline). background: nobody is waiting for it, so it waits
    for Batsy to be quiet and steps aside if you start talking (llm_hosts.background_chat)."""
    from batsy import activity

    with activity.work(label, "local", "Thinking with the local AI"):
        return _local_model(prompt, max_tokens, background)


def _local_model(prompt: str, max_tokens: int = 350, background: bool = False) -> str:
    from batsy import llm_hosts

    body = {"model": _cfg.get("local.model"), "stream": False, "keep_alive": _cfg.get("local.keep_alive"),
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.3, "num_predict": max_tokens, "num_ctx": 4096}}
    if background:
        return llm_hosts.background_chat(_cfg, body)
    with llm_hosts.live(), llm_hosts.open_chat(_cfg, body) as r:
        return json.loads(r.read())["message"]["content"].strip()


def _day_material(day: dt.date) -> str:
    p = vault.daily_path(day)
    parts = []
    for name, title in (("plan", "Plan"), ("timeline", "Timeline"), ("files", "Files"), ("prints", "Prints"),
                        ("music", "Music")):
        body = (vault.get_block(p, name) or "").strip()
        if body:
            parts.append(f"{title}:\n{body}")
    mine = re.search(r"## Notes\n(.*)", vault.read(p), re.S)
    if mine and mine.group(1).strip(" -\n"):
        parts.append("Jonathan's notes:\n" + mine.group(1).strip())
    text = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", "\n\n".join(parts))
    return text[-6000:]


def summarise(day: dt.date) -> bool:
    """Write the day's Summary. False if there was nothing to summarise."""
    material = _day_material(day)
    if len(material) < 40:
        return False
    out = local_model(
        f"Here is Jonathan's day ({day:%A %d %B}), as logged by his assistant Batsy:\n\n{material}\n\n"
        "Write a short summary in Markdown for his journal, in second person ('You…'):\n"
        "- 2 to 4 bullet points: what he actually did or achieved (be specific, use names of projects/files/prints)\n"
        "- then a line '**Open loops:**' followed by bullets for anything started but not finished, or planned "
        "and not done (or 'none').\nUse only facts from the log. No intro, no headings.", background=True)
    out = re.sub(r"^#+ .*\n", "", out).strip()
    vault.submit(lambda: vault.set_block(vault.daily(day), "summary", out, "Summary"))  # the model ran out here
    log.info("brain: summarised %s", day)
    return True


def weekly(day: dt.date) -> None:
    """The week ending on `day` (a Sunday): a review note."""
    iso = day.isocalendar()
    name = f"{iso.year}-W{iso.week:02d}"
    p = vault.path("weekly", name)
    if p.exists():
        return
    days = [day - dt.timedelta(days=i) for i in range(6, -1, -1)]
    material = "\n\n".join(f"### {d:%A %d %b}\n" + (vault.get_block(vault.daily_path(d), "summary") or "")
                           + "\n" + _day_material(d)[:2500] for d in days)
    ask = (f"These are Jonathan's daily notes for the week {name} (from his assistant Batsy):\n\n{material}\n\n"
           "Write his weekly review in Markdown, second person, concise and specific. Sections: "
           "'## Wins', '## Projects' (what moved on each, link project names as [[Name]]), '## Open loops', "
           "'## Next week' (2-3 concrete suggestions based on the open loops). No title, no intro.")
    text = ""
    try:
        from batsy import claude_code

        if claude_code.over_budget(_cfg) is None:
            text = claude_code.run(ask, "weekly review", cfg=_cfg, timeout=240)
    except Exception as e:
        log.info("weekly review by Claude failed (%s); using the local model", e)
    if not text or len(text) < 80:
        text = local_model(ask, 700, background=True)
    links = " · ".join(f"[[{vault.daily_name(d)}|{d:%a}]]" for d in days)
    vault.submit(vault.create, p, vault.frontmatter(type="weekly", week=name, start=days[0].isoformat(), end=day.isoformat(),
                                     tags=["weekly"]) + f"# Week {iso.week}, {iso.year}\n{links}\n\n{text.strip()}\n")
    log.info("brain: weekly review %s", name)


def refresh_home() -> None:
    root = vault.ROOT
    home = root / "Home.md"
    if not home.exists():
        return
    d = vault.today()
    note = vault.daily(d)
    plan = [x for x in (vault.get_block(note, "plan") or "").splitlines() if x.strip()]
    open_plan = [x for x in plan if x.lstrip().startswith("- [ ]")]
    inbox = len(list(vault.path("inbox").glob("*.md")))
    y = d - dt.timedelta(days=1)
    ysum = (vault.get_block(vault.daily_path(y), "summary") or "").strip()
    today = [f"**Today:** [[{vault.daily_name(d)}|{d:%A %-d %B}]] · "
             f"{len(open_plan)} planned · {inbox} in the inbox"]
    if open_plan:
        today += open_plan[:5]
    if ysum and not ysum.startswith("_"):
        today += ["", f"**Yesterday** ([[{vault.daily_name(y)}]]):", ysum]
    vault.set_block(home, "today", "\n".join(today))
    recent = sorted((p for p in root.rglob("*.md") if not p.relative_to(root).as_posix().startswith(
        ("01 Daily", "Batsy", ".", "Home.md"))), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    vault.set_block(home, "recent", "\n".join(
        f"- [[{p.stem}]] · {time.strftime('%d %b %H:%M', time.localtime(p.stat().st_mtime))}" for p in recent))


def end_of_day(day: dt.date | None = None) -> None:
    """Summarise `day` (default: the day that just ended); on Sundays also the week."""
    day = day or vault.today() - dt.timedelta(days=1)
    try:
        summarise(day)
    except Exception:
        log.exception("brain: summary for %s", day)
    if day.weekday() == 6:
        try:
            weekly(day)
        except Exception:
            log.exception("brain: weekly review")
    _save(summarised=day.isoformat())
    vault.submit(refresh_home)


def start(app) -> None:
    global _cfg
    _cfg = app.cfg

    def loop():
        time.sleep(30)
        while True:
            try:
                done = _state().get("summarised", "")
                yesterday = vault.today() - dt.timedelta(days=1)
                if done < yesterday.isoformat():
                    end_of_day(yesterday)  # here: the models are slow; only the writes go to the writer
                vault.submit(refresh_home)
            except Exception:
                log.exception("brain daily loop")
            time.sleep(300)

    threading.Thread(target=loop, daemon=True, name="vault-daily").start()


def good_night() -> None:
    """'Good night': today's summary now, so it's there to read in the morning."""
    if _cfg is not None and vault.enabled():
        threading.Thread(target=end_of_day, args=(vault.today(),), daemon=True, name="vault-goodnight").start()
