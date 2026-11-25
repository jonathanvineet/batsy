"""Batsy controlling itself, by voice or typed (no AI, never improvised):

  mute yourself / go quiet for an hour / text only      its own voice off (answers still show on screen and phone)
  unmute yourself / talk again / you can speak now      its voice back
  speak louder / quieter / set your volume to 40        its own voice, not the laptop's volume
  stop listening / mute your mic for 30 minutes         the microphone ignored (type, the phone or the website still
                                                        work; "start listening" there turns it back on)
  start listening / unmute your mic                     the microphone back
  restart the phone app / reopen the batsy app          force-stop and reopen the app on the phone (over USB / adb)
  reconnect the phone                                   the USB link again (adb reverse), right now
  restart your screen / the overlay                     the full-screen view and the corner bat, restarted
  restart claude remote control                         the Remote Control session (batsy/remote.py)
  how are you set up / what's muted                     what's off right now

Mute states survive a restart (selfctl.json); timed ones end by themselves.
"""
import json
import logging
import re
import threading
import time

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.selfctl")
STATE = DATA_DIR / "selfctl.json"

YOU = r"(?:yourself|your (?:voice|audio|sound|speaker)|batsy|your own (?:voice|audio|sound))"
FOR = r"(?: for (?P<for>[a-z0-9 ]+?))?"
MUTE = re.compile(rf"^(?:please )?(?:(?:mute|silence|quiet) {YOU}|(?:go|be|stay) (?:quiet|silent|mute)|text only|"
                  rf"switch to text|only show (?:text|answers)|don'?t (?:talk|speak) out loud){FOR}$")
UNMUTE = re.compile(rf"^(?:please )?unmute {YOU}$|^(?:talk|speak) (?:again|out loud|to me again)$|"
                    r"^you can (?:talk|speak)(?: again| now)?$|^(?:turn |switch )?(?:your )?voice (?:back )?on$")
VOLUME = re.compile(rf"^(?:set )?(?:your|batsy'?s) (?:own )?(?:voice |speaking )?volume (?:to )?(?P<n>\d{{1,3}})(?: percent)?$|"
                    r"^(?P<dir>speak|talk) (?P<how>louder|quieter|softer|up|down)$|"
                    r"^(?:be )?(?P<how2>louder|quieter|softer)$")
MIC_OFF = re.compile(rf"^(?:please )?(?:stop listening|don'?t listen|mute (?:your )?(?:mic|microphone)|"
                     rf"turn off (?:your )?(?:mic|microphone|ears)|stop hearing me){FOR}$")
MIC_ON = re.compile(r"^(?:please )?(?:start listening(?: again)?|listen again|unmute (?:your )?(?:mic|microphone)|"
                    r"turn on (?:your )?(?:mic|microphone|ears))$")
PHONE_APP = re.compile(r"^(?:please )?(?:restart|reopen|relaunch|reload|refresh|reset) (?:the |my )?(?:batsy )?"
                       r"(?:phone apps?|apps? on (?:my |the )?phone|mobile app|android app|phone|widgets?(?: app)?)$")
PHONE_LINK = re.compile(r"^(?:please )?(?:reconnect|re-?link|fix the connection to) (?:the |my )?phone$")
OVERLAY = re.compile(r"^(?:please )?(?:restart|reload|reset|fix) (?:your |the )?(?:screen|overlay|full ?screen|display|"
                     r"corner bat|bat|dock)$")
RC = re.compile(r"^(?:please )?(?:restart|start|stop) (?:the )?claude remote(?: control)?(?: session)?$")
STATUS = re.compile(r"^(?:what'?s muted|are you muted|how are you set up|what'?s (?:off|turned off)|"
                    r"(?:are you|is your mic) (?:listening|muted))$")


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        STATE.write_text(json.dumps(d))
    except OSError:
        pass


def _seconds(text: str | None) -> float | None:
    if not text:
        return None
    from batsy import instant

    return instant._duration(text.replace("an hour", "1 hour").replace("a minute", "1 minute"))


def _until(text: str | None) -> float:
    """0 = forever; else when it ends."""
    secs = _seconds(text)
    return time.time() + secs if secs else 0.0


def _span(secs: float) -> str:
    mins = round(secs / 60)
    return f"{mins} minute{'s' if mins != 1 else ''}" if mins < 90 else f"{mins / 60:.1f}".rstrip("0").rstrip(".") + " hours"


def restore(app) -> None:
    """At start: put back what was muted (and hasn't expired)."""
    d = _load()
    now = time.time()
    sp = app.speaker
    if d.get("voice_muted") and (not d.get("voice_until") or d["voice_until"] > now):
        sp.muted_until = d.get("voice_until") or float("inf")
    if d.get("volume"):
        sp.volume = float(d["volume"])
    if d.get("mic_muted") and (not d.get("mic_until") or d["mic_until"] > now):
        app.muted_mic_until = d.get("mic_until") or float("inf")


def _remember(app) -> None:
    sp, now = app.speaker, time.time()
    _save({"voice_muted": sp.muted_until > now, "voice_until": 0 if sp.muted_until == float("inf") else sp.muted_until,
           "volume": sp.volume,
           "mic_muted": mic_muted(app), "mic_until": 0 if app.muted_mic_until == float("inf") else app.muted_mic_until})


def mic_muted(app) -> bool:
    return time.time() < getattr(app, "muted_mic_until", 0.0)


def _show(app, text: str) -> None:
    """Said while muted: on screen and on the phone, not out loud."""
    app.overlay.say(text, force=True)
    app.last_said = text


def handle(app, n: str) -> str | None:
    """What to say back (may be "" when it was shown instead), or None if n isn't about Batsy itself."""
    sp = app.speaker
    m = MUTE.match(n)
    if m:
        until = _until(m.group("for"))
        sp.stop()
        sp.muted_until = until or float("inf")
        _remember(app)
        _show(app, f"Muted for {_span(until - time.time())}. I'll only show my answers." if until else
              "Muted. I'll only show my answers. Say or type “unmute yourself” to hear me again.")
        return ""
    if UNMUTE.match(n):
        sp.muted_until = 0.0
        _remember(app)
        return "I'm back."
    m = VOLUME.match(n)
    if m:
        if m.group("n"):
            sp.volume = max(0.1, min(1.5, int(m.group("n")) / 100))
        else:
            up = (m.group("how") or m.group("how2")) in ("louder", "up")
            sp.volume = max(0.1, min(1.5, round(sp.volume + (0.2 if up else -0.2), 2)))
        _remember(app)
        return f"This loud? My voice is at {round(sp.volume * 100)} percent."
    m = MIC_OFF.match(n)
    if m:
        until = _until(m.group("for"))
        app.muted_mic_until = until or float("inf")
        _remember(app)
        how = f" for {_span(until - time.time())}" if until else ""
        return (f"Okay, I'm not listening{how}. Type to me, or use the phone or the website; "
                f"“start listening” there turns my ears back on.")
    if MIC_ON.match(n):
        app.muted_mic_until = 0.0
        _remember(app)
        return "Listening again."
    if PHONE_APP.match(n):
        return restart_phone_app(app)
    if PHONE_LINK.match(n):
        hub = getattr(app, "hub", None)
        where = hub._phone_on() if hub else None
        if not where:
            return "I can't see the phone over USB. Plug it in, with USB debugging allowed."
        hub._sh(where, f"adb reverse tcp:{hub.port} tcp:{hub.port}", 8)
        return f"Reconnected the phone through the {'laptop' if where == 'laptop' else 'PC'}."
    if OVERLAY.match(n):
        threading.Thread(target=restart_overlay, args=(app,), daemon=True).start()
        return "Restarting my screen."
    m = RC.match(n)
    if m:
        from batsy import remote

        action = "stop" if n.split()[0] == "stop" else "start" if n.startswith(("start", "please start")) else "restart"
        return remote.rc(action)
    if STATUS.match(n):
        bits = []
        now = time.time()
        if sp.muted_until > now:
            bits.append("my voice is muted" + ("" if sp.muted_until == float("inf") else
                                               f" for another {_span(sp.muted_until - now)}"))
        if mic_muted(app):
            bits.append("I'm not listening" + ("" if app.muted_mic_until == float("inf") else
                                               f" for another {_span(app.muted_mic_until - now)}"))
        if round(sp.volume, 2) != 1.0:
            bits.append(f"my voice is at {round(sp.volume * 100)} percent")
        text = ("Right now " + " and ".join(bits) + ".") if bits else "Nothing's muted. I'm listening and talking."
        if sp.muted_until > now:
            _show(app, text)
            return ""
        return text
    return None


def restart_phone_app(app) -> str:
    """Force-stop the Batsy app on the phone and open it again (over adb: phone plugged into the laptop or PC)."""
    from batsy import phone

    hub = getattr(app, "hub", None)
    where = hub._phone_on() if hub else None
    if not where:
        return ("I can't reach the phone over USB, so I can't restart the app. Plug it in with USB debugging on, "
                "or close and reopen the app on the phone.")
    pkg = phone.PACKAGE
    code, out = hub._sh(where, f"adb shell am force-stop {pkg} && adb reverse tcp:{hub.port} tcp:{hub.port} && "
                               f"adb shell am start -n {pkg}/.MainActivity", 20)
    if code != 0:
        return f"Restarting the phone app failed: {out.strip().splitlines()[-1][:120] if out.strip() else 'no reason given'}"
    return "Restarted the app on your phone."


def restart_overlay(app) -> None:
    """The full-screen view and the corner bat: stopped and started fresh (if the screen looks stuck or wrong)."""
    ov = app.overlay
    if not ov.enabled:
        return
    ov.close()
    time.sleep(1)
    ov.next_start = 0
    with ov.lock:
        ov._start()
    if ov.cfg.get("overlay.dock"):
        import os

        if os.environ.get("DISPLAY"):
            ov._start_dock()
