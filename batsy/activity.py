"""What Batsy is doing in the background and what it's waiting on you for (shown on the screensaver)."""
import itertools
import threading
import time
from contextlib import contextmanager

_lock = threading.Lock()
_ids = itertools.count(1)
_agents: dict[int, dict] = {}   # running work: Claude Code jobs, learning, picture checks...
_asks: dict[str, dict] = {}     # questions waiting for the user's answer
_done: list[dict] = []          # the last few finished jobs (for the side tab: what it just did, how long it took)
_local = threading.local()      # each thread's open jobs, innermost last (so step() knows which one it's on)
changed = threading.Event()     # set whenever anything changes (the side tab redraws then)


class Job:
    """A job in progress, with its current step: `with activity.agent("Commit & push batsy") as job:
    job.step("Writing the commit message")`. Steps from code deeper down (activity.step) land on it too."""

    def __init__(self, aid: int):
        self.aid = aid

    def step(self, text: str, pct: float | None = None) -> None:
        with _lock:
            a = _agents.get(self.aid)
            if a is not None:
                if text != a.get("step"):
                    a["step"], a["step_since"] = text, time.time()
                a["pct"] = pct
        changed.set()

    def result(self, text: str) -> None:
        """How it ended ("Pushed 4 files"), shown with it once it's done."""
        with _lock:
            if self.aid in _agents:
                _agents[self.aid]["result"] = text


@contextmanager
def agent(label: str, kind: str = "claude"):
    aid = next(_ids)
    with _lock:
        _agents[aid] = {"label": label, "kind": kind, "since": time.time(), "step": "", "pct": None}
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    job = Job(aid)
    stack.append(job)
    changed.set()
    ok = True
    try:
        yield job
    except BaseException:
        ok = False
        raise
    finally:
        stack.remove(job)
        with _lock:
            a = _agents.pop(aid, None)
            if a:
                _done.insert(0, {"label": a["label"], "kind": a["kind"], "secs": int(time.time() - a["since"]),
                                 "ok": ok, "result": a.get("result") or a.get("step") or "", "t": time.time()})
                del _done[8:]
        changed.set()


@contextmanager
def work(label: str, kind: str = "batsy", step_text: str = ""):
    """A step of the job this thread is already doing, or (if it isn't doing one) a job of its own."""
    stack = getattr(_local, "stack", None)
    if stack:
        stack[-1].step(step_text or label)
        yield stack[-1]
    else:
        with agent(label, kind) as job:
            if step_text:
                job.step(step_text)
            yield job


def step(text: str, pct: float | None = None) -> None:
    """The current step of whatever job this thread is doing (nothing, if it isn't in one)."""
    stack = getattr(_local, "stack", None)
    if stack:
        stack[-1].step(text, pct)


def asking(key: str, question: str) -> None:
    with _lock:
        _asks[key] = {"question": question, "since": time.time()}
    changed.set()


def answered(key: str) -> None:
    with _lock:
        _asks.pop(key, None)
    changed.set()


def snapshot() -> dict:
    from batsy.tools import monitor, shell

    now = time.time()
    with _lock:
        agents = [{"label": a["label"], "kind": a["kind"], "secs": int(now - a["since"]), "step": a.get("step", ""),
                   "step_secs": int(now - a.get("step_since", a["since"])), "pct": a.get("pct")}
                  for a in _agents.values()]
        asks = [{"question": a["question"], "mins": int((now - a["since"]) / 60)} for a in _asks.values()]
        done = [dict(d, ago=int(now - d["t"])) for d in _done if now - d["t"] < 1800]
    for jid, j in list(shell._jobs.items()):
        if j["exit_code"] is None:
            agents.append({"label": f"job: {j['label']}", "kind": "shell", "secs": int(now - j["started"])})
    watchers = [i for i in monitor._items.values() if i.get("kind") == "monitor"]
    if watchers:
        agents.append({"label": f"{len(watchers)} monitor{'s' if len(watchers) > 1 else ''} watching", "kind": "monitor",
                       "secs": 0})
    return {"agents": agents, "asks": asks, "done": done}
