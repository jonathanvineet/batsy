"""Long-term memory: facts about the user that outlive any one conversation."""
import json
import time
import uuid

from batsy.config import DATA_DIR

MEMORY_FILE = DATA_DIR / "memory.json"


def load() -> list[dict]:
    if not MEMORY_FILE.exists():
        return []
    try:
        return json.loads(MEMORY_FILE.read_text())
    except ValueError:
        return []


def _save(facts: list[dict]) -> None:
    MEMORY_FILE.write_text(json.dumps(facts, indent=1))


def as_prompt() -> str:
    facts = load()
    if not facts:
        return "(nothing remembered yet)"
    return "\n".join(f"- [{f['id']}] {f['fact']}" for f in facts)


def register(reg):
    @reg.tool(
        "remember",
        "Save a lasting fact about the user, their setup, projects or preferences "
        "(e.g. 'My Uno is usually on /dev/ttyACM0', 'Call me boss'). Do this whenever they tell you something worth "
        "knowing next week, or say 'remember'.",
        {"fact": {"type": "string"}},
        ["fact"],
    )
    def remember(ctx, fact):
        facts = load()
        entry = {"id": uuid.uuid4().hex[:5], "fact": fact, "saved": time.strftime("%Y-%m-%d")}
        facts.append(entry)
        _save(facts)
        return f"Remembered [{entry['id']}]."

    @reg.tool(
        "recall",
        "Search long-term memory (empty query lists everything).",
        {"query": {"type": "string"}},
    )
    def recall(ctx, query=""):
        words = query.lower().split()
        hits = [f for f in load() if all(w in f["fact"].lower() for w in words)]
        return "\n".join(f"[{f['id']}] ({f['saved']}) {f['fact']}" for f in hits) or "Nothing matching."

    @reg.tool(
        "forget",
        "Delete a remembered fact by id.",
        {"id": {"type": "string"}},
        ["id"],
    )
    def forget(ctx, id):
        facts = load()
        kept = [f for f in facts if f["id"] != id]
        _save(kept)
        return "Forgotten." if len(kept) < len(facts) else f"No fact {id}."

    @reg.tool(
        "new_conversation",
        "Archive the current conversation and start fresh after this reply (long-term memory is kept). "
        "Use when the user says 'new conversation', 'start over', 'clear context'.",
        {},
    )
    def new_conversation(ctx):
        ctx.flags["new_conversation"] = True
        return "The conversation will be archived after this reply."
