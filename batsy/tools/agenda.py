"""Agenda and presets for Claude: the same local store the good-morning / good-night routines use."""
import datetime as dt
import re

from batsy.memory_store import get_store
from batsy.routines import custom_routines, save_routine


def register(reg):
    @reg.tool(
        "agenda",
        "The user's day plans (read by the good-morning routine). add: date (YYYY-MM-DD) + text; list: date or "
        "date..end_date; remove: id. Before 5 AM, 'tomorrow' means the calendar today (they haven't slept yet).",
        {
            "action": {"type": "string", "enum": ["add", "list", "remove"]},
            "date": {"type": "string"},
            "end_date": {"type": "string"},
            "text": {"type": "string"},
            "id": {"type": "integer"},
        },
        ["action"],
    )
    def agenda(ctx, action, date=None, end_date=None, text=None, id=None):
        store = get_store()
        if action == "add":
            if not (date and text):
                return "add needs date and text."
            dt.date.fromisoformat(date)
            return f"Added ({store.add_agenda(date, text)})."
        if action == "remove":
            return "Removed." if id is not None and store.remove_agenda(id) else "No such item."
        start = date or dt.date.today().isoformat()
        rows = store.agenda_range(start, end_date or start)
        return "\n".join(f"({r['id']}) {r['day']}: {r['text']}" for r in rows) or "Nothing planned."

    @reg.tool(
        "make_routine",
        "Create or replace a voice preset that then runs locally with no tokens. phrases: what the user will say. "
        "say: reply to speak. commands: bash commands to run. spotify: what to play (e.g. 'my Focus playlist'). "
        "ask: optional prompt to hand to you each time (costs tokens; only if it needs thinking).",
        {
            "name": {"type": "string"},
            "phrases": {"type": "array", "items": {"type": "string"}},
            "say": {"type": "string"},
            "commands": {"type": "array", "items": {"type": "string"}},
            "spotify": {"type": "string"},
            "ask": {"type": "string"},
        },
        ["name", "phrases"],
    )
    def make_routine(ctx, name, phrases, say=None, commands=None, spotify=None, ask=None):
        spec = {k: v for k, v in dict(phrases=phrases, say=say, commands=commands, spotify=spotify, ask=ask).items() if v}
        save_routine(re.sub(r"\W+", "_", name.lower()), spec)
        return f"Routine '{name}' saved. Existing: {', '.join(custom_routines(ctx.cfg))}."
