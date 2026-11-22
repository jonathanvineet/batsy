"""Apps Batsy built (~/Batsy/<name>/, e.g. the gold XRF sample UI): "show me the UI for the gold project" finds the
right one, starts it if it isn't running, and opens it on the laptop and the phone.

An app is a folder in ~/Batsy with a README.md whose first lines say how to run it and where, like:

    # Gold XRF (DP5) sample UI + backend
        python3 backend/server.py          # simulated DP5, http://localhost:8790

It's matched by its folder name, its title and anything its README mentions (the research it belongs to). Running
apps listen on the laptop only (127.0.0.1); `tailscale serve` shares that port with your own devices (tailnet only,
https), so the phone can open it without the app being on the Wi-Fi.
"""
import json
import logging
import re
import socket
import subprocess
import time
from pathlib import Path

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.apps")
HOME = Path.home() / "Batsy"
ASK = re.compile(r"^(?:[a-z]+ )?(?:can you |please |could you )?(?:show|open|launch|start|run|bring up|pull up|load)(?: me| up)? "
                 r"(?:the |my |that |this )?(?:sample |demo )?(?:ui|app|application|interface|dashboard|website|site|"
                 r"front ?end|demo|gui|page)(?: (?:for|of|on|from))? (?:the |my )?(?P<what>.+)$|"
                 r"^(?:[a-z]+ )?(?:show|open|launch|start|run|bring up|pull up)(?: me| up)? (?:the |my )?(?P<what2>.+?) "
                 r"(?:ui|app|interface|dashboard|demo|gui)$")


def apps() -> list[dict]:
    out = []
    for d in sorted(HOME.iterdir()) if HOME.is_dir() else []:
        readme = d / "README.md"
        if not d.is_dir() or not readme.is_file():
            continue
        text = readme.read_text(errors="replace")[:4000]
        run = next((ln.split("#")[0].strip() for ln in text.splitlines()
                    if re.match(r"\s+(?:python3?|node|npm|uv|\./)\S*\s", ln) and "--real" not in ln), "")
        port = re.search(r"localhost:(\d{2,5})", text)
        if not run or not port:
            continue
        title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), d.name)
        out.append({"name": d.name, "dir": d, "title": title, "run": run, "port": int(port.group(1)), "text": text.lower()})
    return out


def find(what: str) -> dict | None:
    from rapidfuzz import fuzz

    what = re.sub(r"\b(?:project|research|ui|app|the|my|for|of|uh|um)\b", " ", what.lower())
    what = re.sub(r"\s+", " ", what).strip()
    best, score = None, 0
    for a in apps():
        hay = f"{a['name'].replace('-', ' ')} {a['title'].lower()}"
        s = max(fuzz.token_set_ratio(what, hay), fuzz.partial_ratio(what, hay) if len(what) >= 3 else 0,
                80 if what and all(w in a["text"] for w in what.split()) else 0)  # "gold": in its README
        if s > score:
            best, score = a, s
    return best if score >= 70 else None


def _up(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure(a: dict) -> bool:
    """Running? Start it if not (its own process, its log in ~/.local/share/batsy/apps/)."""
    if _up(a["port"]):
        return True
    logs = DATA_DIR / "apps"
    logs.mkdir(exist_ok=True)
    subprocess.Popen(a["run"], shell=True, cwd=a["dir"], stdin=subprocess.DEVNULL, start_new_session=True,
                     stdout=open(logs / f"{a['name']}.log", "ab"), stderr=subprocess.STDOUT)
    for _ in range(40):
        time.sleep(0.25)
        if _up(a["port"]):
            log.info("started %s on port %d", a["name"], a["port"])
            return True
    return False


def share(a: dict) -> str | None:
    """The tailnet-only https address of the app (for the phone), set up once with `tailscale serve`."""
    try:
        me = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                                       timeout=10).stdout)["Self"]["DNSName"].rstrip(".")
        status = subprocess.run(["tailscale", "serve", "status"], capture_output=True, text=True, timeout=10).stdout
        if f":{a['port']} " not in status and f":{a['port']}\n" not in status:
            r = subprocess.run(["tailscale", "serve", "--bg", f"--https={a['port']}", f"http://127.0.0.1:{a['port']}"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode:
                log.warning("tailscale serve %s: %s", a["name"], r.stderr[-200:])
                return None
        return f"https://{me}:{a['port']}/"
    except Exception as e:
        log.warning("sharing %s: %s", a["name"], e)
        return None


def handle(app, n: str) -> str | None:
    m = ASK.match(n)
    if not m:
        return None
    a = find(m.group("what") or m.group("what2") or "")
    if a is None:
        return None  # not one of ours: "open the spotify app" is the apps handler's
    if not ensure(a):
        return f"{a['title']} didn't start. Its log is in Batsy's apps folder."
    phone_url = share(a)
    subprocess.Popen(["xdg-open", f"http://localhost:{a['port']}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if phone_url:
        app.phone_event(f"{a['title']} is open. Tap to open it here.", url=phone_url)
    return f"Opened {a['title']} on the laptop" + (", and it's on your phone too: tap the card." if phone_url else ".")
