"""Command line entry point.

  batsy            run the voice assistant (wake word "Batsy")
  batsy chat       type to Batsy (talks to the running voice daemon if there is one)
  batsy ask TEXT   one-off request
  batsy doctor     check microphone, models, API key and tools
  batsy reset      archive the conversation and start fresh
  batsy usage      tokens used today / this week / all time
  batsy spotify-login   connect Batsy to your Spotify account (one time)
"""
import argparse
import fcntl
import logging
import os
import shutil
import sys
import threading

from batsy.config import CONFIG_DIR, DATA_DIR, MODELS_DIR, Config


def _setup_logging(verbose: bool) -> None:
    handlers = [logging.FileHandler(DATA_DIR / "batsy.log")]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "anthropic", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _require_key() -> None:
    if Config.load().get("brain.engine") != "claude":
        return  # local brain needs no key
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit(f"No Claude API key. Put ANTHROPIC_API_KEY=sk-ant-... in {CONFIG_DIR / 'env'} (chmod 600).")


def _single_instance():
    fh = open(DATA_DIR / "batsy.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Batsy is already running. Use `batsy chat` to type to it.")
    return fh


def cmd_run(cfg, args) -> None:
    _require_key()
    lock = _single_instance()  # noqa: F841 - held for the life of the process
    from batsy.app import Batsy

    Batsy(cfg, voice=True).run_voice()


def _local_turn(app, text: str) -> None:
    def say(sentence):
        print(f"Batsy: {sentence}")
        app.speaker.say(sentence)

    if not app.local_command(text):
        app.run_turn(text, "typed", say, threading.Event(), app._confirm_console)
    app.speaker.wait_idle()


def cmd_chat(cfg, args) -> None:
    from batsy.app import ask_daemon

    if ask_daemon("", speak=False):
        print("Connected to the running Batsy. Ctrl+D to quit.")
        remote = True
    else:
        _require_key()
        lock = _single_instance()  # noqa: F841
        from batsy.app import Batsy

        app = Batsy(cfg, voice=False, speak=args.speak)
        remote = False
        print("Batsy (text mode). Ctrl+D to quit.")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if remote:
            ask_daemon(text, speak=args.speak)
        else:
            _local_turn(app, text)


def cmd_ask(cfg, args) -> None:
    from batsy.app import ask_daemon

    text = " ".join(args.text)
    if ask_daemon(text, speak=args.speak):
        return
    _require_key()
    lock = _single_instance()  # noqa: F841
    from batsy.app import Batsy

    _local_turn(Batsy(cfg, voice=False, speak=args.speak), text)


def cmd_reset(cfg, args) -> None:
    from batsy.app import ask_daemon
    from batsy.memory_store import get_store

    if not ask_daemon("new conversation"):
        get_store().new_session()
    print("Fresh conversation. Everything stays stored locally and searchable.")


def cmd_usage(cfg, args) -> None:
    from batsy import usage

    st = usage.stats()
    for label, key in (("today", "today"), ("last 7 days", "week"), ("all time", "all")):
        t = st[key]
        print(f"{label:12} {t['requests']:5} requests | {t['tokens']:>10,} tokens (input {t['in']:,}, cached "
              f"{t['cache_read']:,}, cache write {t['cache_write']:,}, output {t['out']:,}) | ≈ ${t['cost']:.2f}")


def cmd_vocab(cfg, args) -> None:
    from batsy.vocab import get_vocab

    v = get_vocab()
    if args.forget:
        print("Removed." if v.forget(args.forget) else "No such phrase.")
        return
    for r in v.list():
        print(f"{'learned' if r['learned'] else 'built-in':8} {r['intent']:8} used {r['uses']:3}x  {r['template']}")


def cmd_spotify_login(cfg, args) -> None:
    from batsy.spotify import Spotify, login

    login(cfg)
    devs = Spotify(cfg).devices()
    print("Spotify devices online now:", ", ".join(d["name"] for d in devs) or "none (open Spotify on the iPad)")


def cmd_doctor(cfg, args) -> None:
    def check(label, ok, hint=""):
        print(f"  [{'ok' if ok else '!!'}] {label}" + ("" if ok else f"  -> {hint}"))

    print("Batsy doctor")
    if cfg.get("brain.engine") == "claude":
        check("Claude API key", bool(os.environ.get("ANTHROPIC_API_KEY")), f"add ANTHROPIC_API_KEY to {CONFIG_DIR / 'env'}")
        print(f"  model: {cfg.get('claude.model')} (effort {cfg.get('claude.effort')})")
    else:
        import json
        import urllib.request

        try:
            with urllib.request.urlopen(cfg.get("local.url") + "/api/tags", timeout=3) as r:
                names = [m["name"] for m in json.loads(r.read()).get("models", [])]
            check(f"local model {cfg.get('local.model')} (Ollama)", any(n.startswith(cfg.get("local.model")) for n in names),
                  f"ollama pull {cfg.get('local.model')}")
        except Exception:
            check("Ollama running", False, "sudo systemctl enable --now ollama")
        check("Claude Code for 'ask Claude' (optional, uses your Pro plan)", bool(shutil.which("claude")),
              "npm install -g @anthropic-ai/claude-code, then run claude once to log in")
    try:
        import sounddevice as sd

        dev = sd.query_devices(kind="input")
        check(f"microphone: {dev['name']}", True)
    except Exception as e:
        check("microphone", False, f"{e} (dnf install portaudio; pip install sounddevice)")
    vosk_dir = MODELS_DIR / cfg.get("voice.vosk_model")
    check(f"wake word model {vosk_dir.name}", vosk_dir.exists(), "run install.sh")
    try:
        import faster_whisper  # noqa: F401
        check("faster-whisper speech recognition", True)
    except Exception:
        check("faster-whisper", False, "pip install faster-whisper (falls back to Vosk)")
    voice = MODELS_DIR / "piper" / f"{cfg.get('voice.piper_voice')}.onnx"
    check(f"piper voice {voice.name}", voice.exists(), "run install.sh (falls back to espeak-ng)")
    for tool, hint in [("arduino-cli", "install.sh installs it to ~/.local/bin"),
                       ("espeak-ng", "dnf install espeak-ng"), ("pw-play", "dnf install pipewire-utils"),
                       ("notify-send", "dnf install libnotify"), ("gnome-screenshot", "dnf install gnome-screenshot"),
                       ("ffmpeg", "dnf install ffmpeg-free"), ("ydotool", "dnf install ydotool (optional)")]:
        check(tool, bool(shutil.which(tool) or os.path.exists(os.path.expanduser(f"~/.local/bin/{tool}"))), hint)
    from batsy.spotify import Spotify, SpotifyError

    sp = Spotify(cfg)
    if not sp.ready():
        check("Spotify", False, "set [spotify] client_id in config.toml, then run: batsy spotify-login")
    else:
        try:
            devs = sp.devices()
            check(f"Spotify devices: {', '.join(d['name'] for d in devs) or 'none online'}", bool(devs),
                  "open Spotify on the iPad")
        except SpotifyError as e:
            check("Spotify", False, str(e))
    try:
        import cv2  # noqa: F401
        check("opencv (camera window)", True)
    except Exception:
        check("opencv (camera window)", False, "pip install opencv-python")
    from batsy.camera_view import find_camera, list_cameras

    cams = list_cameras()
    check(f"cameras: {'; '.join(cams) or 'none'} -> using /dev/video{find_camera(cfg.get('camera.name'))}", bool(cams),
          "plug in the webcam")
    try:
        import grp

        check("serial access (dialout group)", "dialout" in [grp.getgrgid(g).gr_name for g in os.getgroups()],
              "sudo usermod -aG dialout $USER, then log out and back in")
    except KeyError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="batsy", description="Batsy voice assistant")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to the terminal too")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="voice assistant (default)")
    p = sub.add_parser("chat", help="type to Batsy")
    p.add_argument("--speak", action="store_true", help="also speak replies aloud")
    p = sub.add_parser("ask", help="one-off request")
    p.add_argument("text", nargs="+")
    p.add_argument("--speak", action="store_true")
    sub.add_parser("doctor", help="check the setup")
    sub.add_parser("reset", help="start a fresh conversation")
    sub.add_parser("usage", help="show token usage")
    sub.add_parser("spotify-login", help="connect your Spotify account")
    p = sub.add_parser("vocab", help="list (or --forget) the phrases Batsy knows for showing things")
    p.add_argument("--forget", metavar="PHRASE")
    args = parser.parse_args()

    cfg = Config.load()
    _setup_logging(args.verbose or args.cmd in (None, "run") and sys.stderr.isatty())
    {"run": cmd_run, None: cmd_run, "chat": cmd_chat, "ask": cmd_ask,
     "doctor": cmd_doctor, "reset": cmd_reset, "usage": cmd_usage,
     "spotify-login": cmd_spotify_login, "vocab": cmd_vocab}[args.cmd](cfg, args)
