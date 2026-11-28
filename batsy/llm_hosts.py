"""Where the local model runs: the HP laptop on the cable (10.10.10.4) when it's up, this laptop otherwise.

The HP keeps the model loaded all the time and has nothing else to do, so it answers at full speed and this
laptop keeps its RAM (2.6 GB) and stays cooler. If the HP is off or asleep, everything quietly falls back to the
Ollama here: nothing breaks, it's just slower.

  best(cfg)  -> base URL of the first host that answers ("http://10.10.10.4:11434")
  local(cfg) -> True when that's this laptop (so callers can be gentler with its memory)
"""
import logging
import threading
import time
import urllib.request

log = logging.getLogger("batsy.hosts")

_lock = threading.Lock()
_state = {"t": 0.0, "best": None}
CHECK_EVERY = 15


def hosts(cfg) -> list[str]:
    hs = [h.rstrip("/") for h in (cfg.get("local.hosts") or []) if h]
    own = cfg.get("local.url").rstrip("/")
    return hs + [own] if own not in hs else hs


def _up(base: str, model: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as r:
            import json

            names = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
            return any(n == model or n.split(":")[0] == model.split(":")[0] and n.endswith(model.split(":")[-1])
                       for n in names)
    except Exception:
        return False


def best(cfg) -> str:
    """The first host (in order) that's up and has the model. Checked at most every 15 s."""
    now = time.time()
    with _lock:
        if _state["best"] and now - _state["t"] < CHECK_EVERY:
            return _state["best"]
    model = cfg.get("local.model")
    hs = hosts(cfg)
    pick = next((h for h in hs if _up(h, model)), hs[-1])
    with _lock:
        if pick != _state["best"]:
            log.info("local model now on %s", pick)
        _state.update(t=now, best=pick)
    return pick


def local(cfg) -> bool:
    return best(cfg) == cfg.get("local.url").rstrip("/")


def failed(cfg) -> None:
    """A request to the current host just failed: look again next time instead of waiting 15 s."""
    with _lock:
        _state["t"] = 0.0


def open_chat(cfg, body: dict, timeout: int = 300):
    """POST /api/chat to the best host; if the HP fails, once more on this laptop. Returns the open response."""
    import json
    import urllib.error

    base, own = best(cfg), cfg.get("local.url").rstrip("/")
    if "keep_alive" in body:  # the HP keeps it loaded for good; this laptop only for a while (its RAM)
        body = {**body, "keep_alive": -1 if base != own else cfg.get("local.keep_alive")}
    try:
        return urllib.request.urlopen(urllib.request.Request(base + "/api/chat", data=json.dumps(body).encode(),
                                      headers={"Content-Type": "application/json"}), timeout=timeout)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        if base == own:
            raise
        log.warning("local model on %s failed (%s): using this laptop", base, e)
        failed(cfg)
        body = {**body, "keep_alive": cfg.get("local.keep_alive")} if "keep_alive" in body else body
        return urllib.request.urlopen(urllib.request.Request(own + "/api/chat", data=json.dumps(body).encode(),
                                      headers={"Content-Type": "application/json"}), timeout=timeout)


# --- you first: background jobs step aside while you're talking to Batsy ---------------------------------------
# Two cores can't answer you quickly while they're also writing a daily summary (a question's first word went from
# ~1 s to ~21 s in the stress test). So a background job stops the moment a live question starts and starts over
# when Batsy is quiet again.
_live = 0
_quiet = threading.Condition(_lock)


class live:
    """with llm_hosts.live(): ... -- a turn you're waiting on."""

    def __enter__(self):
        global _live
        with _lock:
            _live += 1

    def __exit__(self, *exc):
        global _live
        with _lock:
            _live -= 1
            _quiet.notify_all()


def _wait_quiet(settle: float = 3.0) -> None:
    while True:
        with _lock:
            while _live:
                _quiet.wait()
        time.sleep(settle)  # a follow-up question often comes right after
        with _lock:
            if not _live:
                return


def background_chat(cfg, body: dict, timeout: int = 600, tries: int = 6) -> str:
    """A job nobody is waiting on (summaries, tidying notes): run it when Batsy is quiet, and drop it the moment
    you ask something, to start again afterwards. Returns the answer's text."""
    import json

    import http.client
    import socket
    import urllib.parse

    body = {**body, "stream": True}
    for _ in range(tries):
        _wait_quiet()
        out, cut, done = "", False, False
        base = best(cfg)
        u = urllib.parse.urlsplit(base)
        send = {**body, "keep_alive": -1 if base != cfg.get("local.url").rstrip("/") else cfg.get("local.keep_alive")} \
            if "keep_alive" in body else body
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        stop = threading.Event()

        def watch():
            # you started talking: hang up at once, even while the model is still reading a long prompt (no words
            # come back for a minute then, so checking between words left your question waiting behind it)
            nonlocal cut
            while not stop.wait(0.2):
                if _live:
                    cut = True
                    try:
                        conn.sock.shutdown(socket.SHUT_RDWR)
                    except (OSError, AttributeError):
                        pass
                    return

        try:
            conn.connect()
            threading.Thread(target=watch, daemon=True).start()
            conn.request("POST", "/api/chat", body=json.dumps(send).encode(), headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            for line in r:
                if _live:
                    cut = True
                    break
                part = json.loads(line) if line.strip() else {}
                out += part.get("message", {}).get("content", "")
                done = done or bool(part.get("done"))
        except (OSError, http.client.HTTPException, ValueError) as e:
            if not cut:
                log.info("background job: %s", e)
        finally:
            stop.set()
            conn.close()
        if done:
            return out.strip()
        if not cut:  # the host went away mid-answer: pick again and start over
            failed(cfg)
        log.info("background job paused for a live question")
    raise TimeoutError("kept being interrupted")
