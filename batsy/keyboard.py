"""Batsy typing for you: "type hello world", "press enter", "press control shift t", into whatever app has focus.

GNOME on Wayland doesn't let one app type into another, so this goes through ydotool: a virtual keyboard made
with /dev/uinput. ydotoold runs as your user service (~/.config/systemd/user/ydotoold.service); a udev rule gives
the logged-in user access to /dev/uinput (TAG+="uaccess", no re-login needed). "Set up typing" opens a terminal
that does that once with your password. The PC gets the same from "set up the PC" (batsy/pc.py).
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

SOCKET = Path(f"/run/user/{os.getuid()}/.ydotool_socket")
UDEV_RULE = 'KERNEL=="uinput", TAG+="uaccess", OPTIONS+="static_node=uinput"'
SERVICE = """[Unit]
Description=ydotool virtual keyboard for Batsy

[Service]
ExecStart=/usr/bin/ydotoold --socket-path=%t/.ydotool_socket --socket-perm=0600 --mouse-off
Restart=on-failure

[Install]
WantedBy=default.target
"""
# root once: let the logged-in user use /dev/uinput, then start the user service
SETUP = (f"echo 'Type your password to let Batsy type for you (only this once).'; "
         f"echo '{UDEV_RULE}' | sudo tee /etc/udev/rules.d/80-batsy-uinput.rules >/dev/null && "
         "sudo modprobe uinput && sudo udevadm control --reload && sudo udevadm trigger --name-match=uinput && "
         "sleep 1 && systemctl --user daemon-reload && systemctl --user enable --now ydotoold && sleep 1 && "
         "echo && echo 'Done. Batsy can type now. You can close this.'")

# Linux input key codes (input-event-codes.h)
KEYS = {"ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "windows": 125, "meta": 125,
        "enter": 28, "return": 28, "tab": 15, "escape": 1, "esc": 1, "backspace": 14, "space": 57,
        "delete": 111, "up": 103, "down": 108, "left": 105, "right": 106, "home": 102, "end": 107,
        "page up": 104, "page down": 109, "f11": 87, "f12": 88,
        **{c: n for c, n in zip("qwertyuiop", range(16, 26))}, **{c: n for c, n in zip("asdfghjkl", range(30, 39))},
        **{c: n for c, n in zip("zxcvbnm", range(44, 51))}, **{str(d): 2 + (d - 1) % 10 for d in range(1, 11)},
        **{f"f{i}": 58 + i for i in range(1, 11)}}
SAID_KEYS = {"see": "c", "sea": "c", "vee": "v", "ex": "x", "zed": "z", "zee": "z", "why": "y", "are": "r",
             "pee": "p", "tea": "t", "tee": "t", "queue": "q", "you": "u", "oh": "o", "eye": "i", "jay": "j",
             "kay": "k", "el": "l", "em": "m", "en": "n", "bee": "b", "dee": "d", "gee": "g", "ef": "f",
             "es": "s", "double you": "w", "control": "ctrl", "contra": "ctrl", "controls": "ctrl",
             "alternate": "alt"}

TYPE = re.compile(r"^(?:please )?(?:type|write|type out|write out)(?: down)? (?P<t>.+)$")
PRESS = re.compile(r"^(?:please )?(?:press|hit|tap|push)(?: the)? (?P<k>.+?)(?: key)?(?: button)?$")
SET_UP = re.compile(r"^(?:please )?(?:set ?up|enable|turn on)(?: the)? (?:typing|keyboard|virtual keyboard)"
                    r"(?: for (?:you|batsy))?$")


def ready() -> bool:
    return SOCKET.exists()


def _ydotool(*args: str) -> bool:
    if not shutil.which("ydotool") or not ready():
        return False
    p = subprocess.run(["ydotool", *args], env={**os.environ, "YDOTOOL_SOCKET": str(SOCKET)},
                       capture_output=True, timeout=60)
    return p.returncode == 0


def type_text(text: str) -> bool:
    return _ydotool("type", "--key-delay", "8", "--", text)


def parse_keys(said: str) -> list[int] | None:
    """"control shift t" / "ctrl+c" / "enter" -> key codes, modifiers first."""
    said = said.lower().replace("+", " ").replace("-", " ").strip(" .")
    for k in ("page up", "page down", "double you"):
        said = said.replace(k, k.replace(" ", "_"))
    codes = []
    for w in said.split():
        w = w.replace("_", " ")
        w = SAID_KEYS.get(w, w) or w
        if w in ("and", "then", "plus"):
            continue
        if w not in KEYS:
            return None
        codes.append(KEYS[w])
    return codes or None


def press(codes: list[int]) -> bool:
    return _ydotool("key", *[f"{c}:1" for c in codes], *[f"{c}:0" for c in reversed(codes)])


def _hand_back_focus(app) -> None:
    """Batsy's screen took the focus when it opened: close it so the keys land in your app."""
    app.overlay.hide()
    time.sleep(0.9)


def _raw_text(app, fallback: str) -> str:
    """What you said after "type", with its capitals and punctuation (not the lowercased command)."""
    raw = getattr(app, "last_raw_text", "") or ""
    m = re.search(r"(?i)\b(?:type|write)(?: out| down)?[,:]?\s+(.+)$", raw)
    text = m.group(1).strip() if m else fallback
    return re.sub(r"(?<![.!?])\.$", "", text) if text.count(".") == 1 else text  # the period STT adds


def handle(app, n: str) -> str | None:
    if SET_UP.match(n):
        from batsy.system import _terminal

        Path.home().joinpath(".config/systemd/user").mkdir(parents=True, exist_ok=True)
        Path.home().joinpath(".config/systemd/user/ydotoold.service").write_text(SERVICE)
        return "I've opened a terminal. Type your password there, just this once." if _terminal(SETUP, "Set up typing") \
            else "I couldn't open a terminal."
    t, k = TYPE.match(n), PRESS.match(n)
    if not (t or k):
        return None
    if k:
        codes = parse_keys(k["k"])
        if codes is None:
            return None  # "press the button on the website": not a key
    if not ready():
        return "I can't type yet. Say set up typing once, and type your password in the window that opens."
    _hand_back_focus(app)
    if t:
        return "" if type_text(_raw_text(app, t["t"])) else "Typing didn't work."
    return "" if press(codes) else "That key press didn't work."
