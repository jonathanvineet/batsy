"""`batsy selftest`: check every part of Batsy on this laptop and say how to fix what's broken.

  batsy selftest            quick checks (seconds, no Claude usage)
  batsy selftest --gui      also open the overlay, camera window and 3D viewer briefly
  batsy selftest --voice    also record from the mic and speak a line
  batsy selftest --claude   also send one tiny request to Claude Code (uses a few Pro tokens)
  batsy selftest --arduino  also compile a blink sketch (about 30 s)
  batsy selftest --all      everything
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from batsy.config import CONFIG_DIR, DATA_DIR, MODELS_DIR, SKILLS_DIR, Config

RESULTS: list[tuple[str, str, str]] = []


def check(section: str):
    def deco(fn):
        def run(*a):
            t = time.time()
            try:
                note = fn(*a) or ""
                RESULTS.append(("PASS", section, f"{note}  ({time.time() - t:.1f}s)"))
                print(f"  \033[32mPASS\033[0m  {section:34} {note}")
            except Skip as s:
                RESULTS.append(("SKIP", section, str(s)))
                print(f"  \033[33mSKIP\033[0m  {section:34} {s}")
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                RESULTS.append(("FAIL", section, msg))
                print(f"  \033[31mFAIL\033[0m  {section:34} {msg[:160]}")
                if os.environ.get("BATSY_DEBUG"):
                    traceback.print_exc()
        return run
    return deco


class Skip(Exception):
    pass


def need(cond, fix: str):
    if not cond:
        raise RuntimeError(fix)


# --- basics ------------------------------------------------------------------------------------------------
@check("Python & environment")
def t_python(cfg):
    need(sys.version_info >= (3, 11), "Python 3.11+ needed")
    return f"Python {sys.version.split()[0]} at {sys.executable}"


@check("Config + data folders")
def t_config(cfg):
    for d in (CONFIG_DIR, DATA_DIR, SKILLS_DIR):
        need(os.access(d, os.W_OK), f"{d} is not writable")
    return f"brain = {cfg.get('brain.engine')}, model = {cfg.get('local.model')}"


@check("No Claude API in local mode")
def t_no_api(cfg):
    from batsy import app  # noqa: F401
    if cfg.get("brain.engine") == "claude":
        raise Skip("engine = claude (API) chosen in config")
    need("anthropic" not in sys.modules, "the Claude API library was loaded; check batsy/app.py imports")
    return "all Claude work goes through Claude Code (Pro plan)"


@check("Desktop tools")
def t_tools(cfg):
    want = {"xdg-open": "xdg-utils", "gtk-launch": "gtk3", "wpctl": "wireplumber", "pw-play": "pipewire-utils",
            "notify-send": "libnotify", "gdbus": "glib2", "pkill": "procps-ng", "playerctl": "playerctl",
            "gnome-screenshot": "gnome-screenshot", "ffmpeg": "ffmpeg-free", "espeak-ng": "espeak-ng",
            "tesseract": "tesseract"}
    missing = [f"{b} (dnf install {p})" for b, p in want.items() if not shutil.which(b)]
    need(not missing, "missing: " + ", ".join(missing))
    return f"{len(want)} tools found"


@check("Autostart + serial permission")
def t_autostart(cfg):
    import grp

    auto = Path.home() / ".config/autostart/batsy.desktop"
    need(auto.exists(), "run install.sh again (autostart entry missing)")
    groups = [grp.getgrgid(g).gr_name for g in os.getgroups()]
    need("dialout" in groups, "log out and back in (dialout group not active yet)")
    return "starts at login; serial ports allowed"


# --- brains ------------------------------------------------------------------------------------------------
@check("Local AI (Ollama)")
def t_ollama(cfg):
    import urllib.request

    base = cfg.get("local.url")
    tags = json.loads(urllib.request.urlopen(base + "/api/tags", timeout=5).read())
    names = [m["name"] for m in tags.get("models", [])]
    need(any(n.startswith(cfg.get("local.model")) for n in names), f"ollama pull {cfg.get('local.model')}")
    t = time.time()
    body = json.dumps({"model": cfg.get("local.model"), "stream": False, "keep_alive": cfg.get("local.keep_alive"),
                       "messages": [{"role": "user", "content": "Reply with one word: ready"}],
                       "options": {"num_predict": 5}}).encode()
    out = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/chat", data=body,
                     headers={"Content-Type": "application/json"}), timeout=120).read())
    return f"replied {out['message']['content'].strip()[:20]!r} in {time.time() - t:.1f}s"


@check("Claude Code installed")
def t_claude_installed(cfg):
    exe = shutil.which("claude") or (str(Path.home() / ".local/bin/claude") if (Path.home() / ".local/bin/claude").exists() else None)
    need(exe, "install: curl -fsSL https://claude.ai/install.sh | bash   then run: claude  (log in with Pro)")
    v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    return v


@check("Claude Code answers (Pro)")
def t_claude_live(cfg, enabled):
    if not enabled:
        raise Skip("run with --claude (uses a few Pro tokens)")
    from batsy import claude_code

    claude_code.CFG = cfg
    before = len(claude_code._rows())
    out = claude_code.run("Reply with exactly: BATSY OK", "selftest", timeout=120, force=True)
    need("BATSY OK" in out.upper(), f"unexpected answer: {out[:120]} (logged in? run: claude)")
    need(len(claude_code._rows()) > before, "usage wasn't recorded (Claude Code JSON output changed?)")
    r = claude_code._rows()[-1]
    return f"OK, {r['in'] + r['out'] + r['cache_read'] + r['cache_write']} tokens counted"


@check("Claude budget")
def t_budget(cfg):
    from batsy import claude_code

    b = claude_code.budget(cfg)
    return f"session {b['session']['used']:,}/{b['session']['limit']:,} · week {b['week']['used']:,}/{b['week']['limit']:,}"


# --- voice ---------------------------------------------------------------------------------------------------
@check("Microphone")
def t_mic(cfg, live):
    import numpy as np
    import sounddevice as sd

    dev = sd.query_devices(kind="input")
    if not live:
        return f"{dev['name']} (record test: --voice)"
    print("        speak now for 2 seconds...")
    audio = sd.rec(32000, samplerate=16000, channels=1, dtype="int16")
    sd.wait()
    level = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    need(level > 150, f"very quiet input (level {level:.0f}); check Settings > Sound > Input")
    return f"{dev['name']}, level {level:.0f}"


@check("Wake word model (Vosk)")
def t_vosk(cfg):
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    path = MODELS_DIR / cfg.get("voice.vosk_model")
    need(path.exists(), "run install.sh (Vosk model missing)")
    Model(str(path))
    return path.name


@check("Speech recognition (Whisper)")
def t_whisper(cfg):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise Skip("faster-whisper not installed; Vosk is used instead")
    t = time.time()
    WhisperModel(cfg.get("voice.whisper_model"), device="cpu", compute_type="int8")
    return f"{cfg.get('voice.whisper_model')} loaded in {time.time() - t:.1f}s"


@check("Voice output")
def t_tts(cfg, live):
    from batsy.voice.tts import Speaker

    sp = Speaker(cfg, enabled=True)
    need(sp.engine != "none", "no TTS engine (pip install piper-tts or dnf install espeak-ng)")
    if live:
        sp.say("Batsy self test. You should hear this.")
        sp.wait_idle(20)
    return f"engine {sp.engine}" + ("" if live else " (hear it: --voice)")


# --- screen ---------------------------------------------------------------------------------------------------
@check("Idle detection (screensaver)")
def t_idle(cfg):
    from batsy import idle

    ms = idle.idle_ms()
    need(ms is not None, "GNOME idle monitor not reachable (gdbus)")
    return f"idle {ms / 1000:.0f}s"


@check("Screenshot")
def t_screenshot(cfg):
    from batsy.tools.screen import capture

    out = Path(tempfile.mkdtemp()) / "s.jpg"
    err = capture("screen", out, cfg)
    need(not err, err or "")
    return "captured"


@check("Overlay window")
def t_overlay(cfg, gui):
    import PySide6  # noqa: F401
    from batsy.overlay import _has_webengine

    kind = "GPU (line + particles)" if _has_webengine() else "classic (Qt WebEngine missing: pip install PySide6-Addons)"
    if not gui:
        return f"PySide6 present, {kind} overlay (open it: --gui)"
    from batsy.overlay import Overlay

    ov = Overlay(cfg, enabled=True)
    need(ov.enabled, "overlay process didn't start")
    ov.listen(fresh=True)
    ov.say("Self test: this is the Batsy overlay.")
    ov.card("Self test", "If you can read this, the overlay works.")
    time.sleep(4)
    ov.hide()
    from batsy import saver

    ov.saver_spec({"theme": {"style": "aurora", "accent": "#d646ff"},
                   "widgets": [{"type": "clock", "span": 3}, {"type": "note", "title": "Self test", "span": 3}],
                   "data": {"1": {"text": "Screensaver drawing works. Move the mouse to close."}}})
    time.sleep(4)
    ov.hide()
    time.sleep(0.5)
    need(not ov.deaths, f"the {ov.renderer} overlay crashed; see overlay.log")
    ov.close()
    return f"shown ({ov.renderer}{', software rendering' if ov.safe_gpu else ''}): listening view, card, screensaver"


@check("Camera")
def t_camera(cfg, gui):
    from batsy.camera_view import find_camera, list_cameras

    cams = list_cameras()
    need(cams, "no camera found (plug in the webcam)")
    import cv2

    idx = cfg.get("camera.device") if cfg.get("camera.device") is not None else find_camera(cfg.get("camera.name"))
    cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
    ok, frame = cap.read()
    cap.release()
    need(ok, f"/dev/video{idx} didn't give a frame (in use by another app?)")
    return f"/dev/video{idx}: {frame.shape[1]}x{frame.shape[0]}  ({'; '.join(cams)[:60]})"


# --- features -------------------------------------------------------------------------------------------------
@check("Memory database")
def t_memory(cfg):
    from batsy.memory_store import get_store

    s = get_store()
    ctx = s.build_context("selftest", cfg)
    return f"FTS search {'on' if s.fts else 'off (LIKE fallback)'}; context {len(ctx)} chars"


@check("App index (instant open)")
def t_apps(cfg):
    from batsy import apps

    apps.INDEX.refresh()
    need(apps.INDEX.apps, "no .desktop apps found")
    files = apps.INDEX.find("files")
    return f"{len(apps.INDEX.apps)} apps; 'files' -> {files.name if files else 'not found'}"


@check("Learned skills runner")
def t_skills(cfg):
    from batsy.learning import SkillBook

    book = SkillBook(lambda p: "ANSWER: x")
    path = SKILLS_DIR / "zz_selftest.py"
    path.write_text('"""Self test."""\nTRIGGERS = ["selftest {x}"]\ndef run(x):\n    return sh(f"echo hi {x}")\n')
    try:
        book.reload()
        hit = book.match("selftest batsy")
        need(hit, "trigger matching failed")
        out, ok = book.run(hit[0], hit[1], lambda q: False)
        need(ok and out == "hi batsy", f"runner failed: {out}")
    finally:
        book.forget("zz_selftest")
    return f"{len(book.listing())} learned skills"


@check("Documents (PDF mark-up)")
def t_docs(cfg):
    import pymupdf

    from batsy import docs

    src = Path(tempfile.mkdtemp()) / "selftest.pdf"
    d = pymupdf.open()
    d.new_page().insert_text((72, 120), "Batsy self test date 12 November 2026", fontsize=14)
    d.save(src)
    res = docs.annotate(src, {"actions": [{"type": "circle", "text": "12 November 2026"}]})
    need(res["applied"] == 1 and Path(res["output"]).exists(), str(res))
    Path(res["output"]).unlink()
    return "find + circle + save OK"


@check("OCR (images, scans)")
def t_ocr(cfg):
    from PIL import Image, ImageDraw

    from batsy import docs

    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((40, 60), "BATSY OCR TEST", fill="black", font=docs._font(64))
    path = Path(tempfile.mkdtemp()) / "ocr.png"
    img.save(path)
    text = " ".join(t for _, t in docs.extract_text(path))
    need("OCR" in text.upper(), f"tesseract read {text!r} (dnf install tesseract tesseract-langpack-eng)")
    return f"read {text.strip()!r}"


@check("Picture search (web)")
def t_images(cfg):
    from batsy import images

    p, cap = images.best_image("Taj Mahal")
    return f"{cap} ({p.stat().st_size // 1024} KB)"


@check("3D engine")
def t_3d(cfg, gui):
    import trimesh

    box = trimesh.creation.box(extents=(40, 20, 10))
    need(abs(box.volume - 8000) < 1, "trimesh maths wrong")
    from batsy.viewer3d import pack

    need(pack(box)[:4] == b"BMSH", "mesh packing broken")
    if not gui:
        return "trimesh present (open the viewer: --gui)"
    from batsy.model3d import Viewer3D, handle, parse  # noqa: F401
    from batsy.viewer3d import OUT

    OUT.mkdir(parents=True, exist_ok=True)
    stl = OUT / "selftest_box.stl"
    box.export(stl)
    v = Viewer3D()
    info = v.open(stl)
    need(info.get("size_mm") == [40.0, 20.0, 10.0], f"load: {info}")
    for c in ({"cmd": "rotate", "axis": "z", "deg": 90}, {"cmd": "section", "axis": "z", "at": 0.5},
              {"cmd": "explode"}, {"cmd": "view", "view": "top"}, {"cmd": "screenshot"}):
        r = v.send(c)
        need("error" not in r, f"{c['cmd']}: {r}")
    size = v.send({"cmd": "info"})["models"][0]["size_mm"]
    need(abs(size[0] - 20) < 0.01, f"rotation didn't apply: {size}")
    time.sleep(2)
    v.send({"cmd": "close"})
    if v.proc:
        v.proc.terminate()
    stl.unlink()
    return "viewer: load, rotate, section, explode, screenshot OK"


@check("Arduino toolchain")
def t_arduino(cfg, compile_):
    from batsy.tools.arduino import _cli, _run

    exe = _cli()
    rc, out = _run(["core", "list"], timeout=60)
    need("arduino:avr" in out, "arduino-cli core install arduino:avr")
    if not compile_:
        return f"{Path(exe).name}; AVR core installed (compile test: --arduino)"
    d = Path(tempfile.mkdtemp()) / "blink"
    d.mkdir()
    (d / "blink.ino").write_text("void setup(){pinMode(13,OUTPUT);}\nvoid loop(){digitalWrite(13,!digitalRead(13));delay(500);}\n")
    rc, out = _run(["compile", "-b", "arduino:avr:uno", str(d)], timeout=300)
    need(rc == 0, out[-300:])
    return "blink compiles for Uno"


@check("Spotify")
def t_spotify(cfg):
    from batsy.spotify import Spotify

    sp = Spotify(cfg)
    if not sp.ready():
        raise Skip("not connected (optional): set [spotify] client_id, then batsy spotify-login")
    devs = sp.devices()
    return f"devices: {', '.join(d['name'] for d in devs) or 'none online (open Spotify on the iPad)'}"


@check("Phrase understanding")
def t_parsers(cfg):
    from batsy import docs, model3d, saver, tasks
    from batsy.app import _norm
    from batsy.vocab import get_vocab

    need(get_vocab().match(_norm("pull up connections for Nikola Tesla")) == ("pullup", "nikola tesla"), "vocab")
    need(model3d.parse(_norm("rotate it 90 degrees around x"))["axis"] == "x", "3D parser")
    need(tasks.parse_schedule("gym monday and friday at 6")[0]["days"] == [0, 4], "schedule parser")
    need(docs.parse_request("circle the dates in my physics notes pdf") == ("physics notes", "pdf"), "file parser")
    need(saver._date("12 November") is not None, "date parser")
    return "vocab, 3D, schedules, files, dates"


def main(argv: list[str]) -> None:
    flags = set(argv)
    allf = "--all" in flags
    gui, voice, claude, arduino = (allf or f in flags for f in ("--gui", "--voice", "--claude", "--arduino"))
    cfg = Config.load()
    print("\nBatsy self-test\n")
    for fn, args in [
        (t_python, ()), (t_config, ()), (t_no_api, ()), (t_tools, ()), (t_autostart, ()),
        (t_ollama, ()), (t_claude_installed, ()), (t_claude_live, (claude,)), (t_budget, ()),
        (t_mic, (voice,)), (t_vosk, ()), (t_whisper, ()), (t_tts, (voice,)),
        (t_idle, ()), (t_screenshot, ()), (t_overlay, (gui,)), (t_camera, (gui,)),
        (t_memory, ()), (t_apps, ()), (t_skills, ()), (t_docs, ()), (t_ocr, ()), (t_images, ()),
        (t_3d, (gui,)), (t_arduino, (arduino,)), (t_spotify, ()), (t_parsers, ()),
    ]:
        fn(cfg, *args)
    p = sum(r[0] == "PASS" for r in RESULTS)
    f = sum(r[0] == "FAIL" for r in RESULTS)
    s = sum(r[0] == "SKIP" for r in RESULTS)
    print(f"\n{p} passed, {f} failed, {s} skipped.")
    if f:
        print("Fix the FAIL lines above (each says how), then run this again.")
    (DATA_DIR / "selftest.json").write_text(json.dumps(RESULTS, indent=1))
    sys.exit(1 if f else 0)
