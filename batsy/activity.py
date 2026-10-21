"""What Batsy is doing in the background and what it's waiting on you for (shown on the screensaver)."""
import itertools
import threading
import time
from contextlib import contextmanager

_lock = threading.Lock()
_ids = itertools.count(1)
_agents: dict[int, dict] = {}   # running work: Claude Code jobs, learning, picture checks...
_asks: dict[str, dict] = {}     # questions waiting for the user's answer


@contextmanager
def agent(label: str, kind: str = "claude"):
    aid = next(_ids)
    with _lock:
        _agents[aid] = {"label": label, "kind": kind, "since": time.time()}
    try:
        yield
    finally:
        with _lock:
            _agents.pop(aid, None)


def asking(key: str, question: str) -> None:
    with _lock:
        _asks[key] = {"question": question, "since": time.time()}


def answered(key: str) -> None:
    with _lock:
        _asks.pop(key, None)


def snapshot() -> dict:
    from batsy.tools import monitor, shell

    now = time.time()
    with _lock:
        agents = [{"label": a["label"], "kind": a["kind"], "secs": int(now - a["since"])} for a in _agents.values()]
        asks = [{"question": a["question"], "mins": int((now - a["since"]) / 60)} for a in _asks.values()]
    for jid, j in list(shell._jobs.items()):
        if j["exit_code"] is None:
            agents.append({"label": f"job: {j['label']}", "kind": "shell", "secs": int(now - j["started"])})
    watchers = [i for i in monitor._items.values() if i.get("kind") == "monitor"]
    if watchers:
        agents.append({"label": f"{len(watchers)} monitor{'s' if len(watchers) > 1 else ''} watching", "kind": "monitor",
                       "secs": 0})
    return {"agents": agents, "asks": asks}
