"""Memory tools. Storage is local SQLite (batsy.memory_store); relevant memories are attached to each
request automatically, so these are only for saving, deliberate lookups and forgetting."""
import time

from batsy.memory_store import _clip, get_store


def register(reg):
    @reg.tool(
        "remember",
        "Save a lasting fact about the user, their setup, projects or preferences "
        "(e.g. 'Their Uno is usually on /dev/ttyACM0'). Use when they tell you something worth knowing next week, "
        "or say 'remember'. Keep it one short sentence.",
        {"fact": {"type": "string"}},
        ["fact"],
    )
    def remember(ctx, fact):
        return f"Remembered ({get_store().add_fact(fact)})."

    @reg.tool(
        "recall",
        "Search local memory (facts and every past conversation) when the attached context doesn't cover it.",
        {"query": {"type": "string"}},
        ["query"],
    )
    def recall(ctx, query):
        store = get_store()
        facts = store.search_facts(query, 8)
        talks = store.search_exchanges(query, 5, None)
        lines = [f"fact ({f['id']}): {f['fact']}" for f in facts]
        lines += [f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['t']))} user: {_clip(r['user'], 160)} | "
                  f"you: {_clip(r['reply'], 200)}" + (f" [did: {_clip(r['actions'], 160)}]" if r["actions"] else "")
                  for r in talks]
        return "\n".join(lines) or "Nothing found."

    @reg.tool(
        "forget",
        "Delete a remembered fact by its id.",
        {"id": {"type": "string"}},
        ["id"],
    )
    def forget(ctx, id):
        return "Forgotten." if get_store().forget_fact(id) else f"No fact {id}."

    @reg.tool(
        "new_conversation",
        "Start a fresh conversation after this reply (nothing is deleted; old talk stays searchable). "
        "Use when the user says 'new conversation', 'start over', 'clear context'.",
        {},
    )
    def new_conversation(ctx):
        ctx.flags["new_conversation"] = True
        return "Fresh conversation after this reply."
