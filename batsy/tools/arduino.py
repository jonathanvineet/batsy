"""Arduino workflow on top of arduino-cli: find boards, write sketches, compile, upload, talk serial."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from batsy.config import WORKSPACE
from batsy.tools import truncate

SKETCH_ROOT = WORKSPACE / "arduino"


def _cli() -> str:
    path = shutil.which("arduino-cli") or os.path.expanduser("~/.local/bin/arduino-cli")
    if not os.path.exists(path):
        raise RuntimeError("arduino-cli is not installed. Run install.sh, or: "
                           "curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=~/.local/bin sh")
    return path


def _run(args: list[str], timeout: int = 600) -> tuple[int, str]:
    p = subprocess.run([_cli(), *args], capture_output=True, text=True, timeout=timeout, errors="replace")
    return p.returncode, (p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip()


def _sketch_dir(sketch: str) -> Path:
    p = Path(os.path.expanduser(sketch))
    if p.suffix == ".ino":
        p = p.parent
    return p if p.is_absolute() else SKETCH_ROOT / p


def detect_boards() -> list[dict]:
    code, out = _run(["board", "list", "--format", "json"], timeout=30)
    if code != 0:
        raise RuntimeError(out)
    data = json.loads(out or "[]")
    ports = data.get("detected_ports", []) if isinstance(data, dict) else data  # cli 1.x vs 0.x
    boards = []
    for entry in ports:
        port = entry.get("port", {})
        matches = entry.get("matching_boards") or port.get("matching_boards") or []
        if not matches and port.get("protocol") != "serial":
            continue
        boards.append({
            "port": port.get("address"),
            "kind": port.get("protocol_label") or port.get("protocol"),
            "boards": [{"name": m.get("name"), "fqbn": m.get("fqbn")} for m in matches],
        })
    return boards


def _auto_target(fqbn: str | None, port: str | None) -> tuple[str | None, str | None, str]:
    if fqbn and port:
        return fqbn, port, ""
    found = [b for b in detect_boards() if b["port"]]
    known = [b for b in found if b["boards"]]
    pick = (known or found or [None])[0]
    if pick is None:
        return fqbn, port, "No board detected. Is it plugged in, and is the user in the dialout group?"
    note = f"Using {pick['port']}" + (f" ({pick['boards'][0]['name']})" if pick["boards"] else "")
    return fqbn or (pick["boards"][0]["fqbn"] if pick["boards"] else None), port or pick["port"], note


def register(reg):
    @reg.tool(
        "arduino",
        "Arduino work via arduino-cli. Actions: "
        "boards (list connected boards, ports, FQBNs); "
        "write_sketch (save `code` as sketch `sketch`; plain names live in ~/Batsy/arduino/<name>/<name>.ino); "
        "compile; upload (compiles then flashes; board/port auto-detected if omitted); "
        "install_core (e.g. arduino:avr, esp32:esp32 with additional_url); install_lib / search_lib; "
        "serial (open port, optionally send `data`, read for `seconds`).",
        {
            "action": {"type": "string", "enum": ["boards", "write_sketch", "compile", "upload", "install_core",
                                                   "install_lib", "search_lib", "serial"]},
            "sketch": {"type": "string", "description": "sketch name or path"},
            "code": {"type": "string", "description": "full .ino source for write_sketch"},
            "fqbn": {"type": "string", "description": "e.g. arduino:avr:uno, arduino:avr:nano, esp32:esp32:esp32"},
            "port": {"type": "string", "description": "e.g. /dev/ttyACM0, /dev/ttyUSB0"},
            "name": {"type": "string", "description": "core or library name, or search query"},
            "additional_url": {"type": "string", "description": "board manager URL for third-party cores"},
            "baud": {"type": "integer", "description": "serial baud rate (default 9600)"},
            "seconds": {"type": "number", "description": "how long to read serial (default 5)"},
            "data": {"type": "string", "description": "text to send over serial (newline appended)"},
        },
        ["action"],
    )
    def arduino(ctx, action, sketch=None, code=None, fqbn=None, port=None, name=None,
                additional_url=None, baud=9600, seconds=5, data=None):
        if action == "boards":
            boards = detect_boards()
            return json.dumps(boards, indent=1) if boards else "No boards or serial ports detected."

        if action == "write_sketch":
            if not sketch or code is None:
                return "write_sketch needs sketch and code."
            d = _sketch_dir(sketch)
            d.mkdir(parents=True, exist_ok=True)
            ino = d / f"{d.name}.ino"
            ino.write_text(code)
            return f"Saved {ino}"

        if action in ("compile", "upload"):
            if not sketch:
                return f"{action} needs sketch."
            d = _sketch_dir(sketch)
            if not d.exists():
                return f"No sketch at {d}."
            note = ""
            if action == "upload" or not fqbn:
                fqbn, port, note = _auto_target(fqbn, port)
            if not fqbn:
                return f"{note} Could not work out the board type; pass fqbn (e.g. arduino:avr:uno)."
            args = ["compile", "-b", fqbn, str(d)]
            if action == "upload":
                if not port:
                    return note or "No port found to upload to."
                args[1:1] = ["--upload", "-p", port]
            rc, out = _run(args)
            status = "SUCCESS" if rc == 0 else f"FAILED (exit {rc})"
            return truncate(f"{note}\n{action} {status} for {fqbn}\n{out}".strip(), 10000)

        if action == "install_core":
            if not name:
                return "install_core needs name, e.g. arduino:avr."
            extra = ["--additional-urls", additional_url] if additional_url else []
            _run(["core", "update-index", *extra])
            rc, out = _run(["core", "install", name, *extra], timeout=1800)
            return truncate(out)

        if action == "install_lib":
            rc, out = _run(["lib", "install", name or ""], timeout=600)
            return truncate(out)

        if action == "search_lib":
            rc, out = _run(["lib", "search", name or "", "--format", "json"], timeout=120)
            try:
                libs = json.loads(out).get("libraries", [])[:10]
                return "\n".join(f"{l['name']}: {l.get('latest', {}).get('sentence', '')}" for l in libs) or "Nothing found."
            except (ValueError, AttributeError):
                return truncate(out, 4000)

        if action == "serial":
            import serial  # pyserial

            if not port:
                _, port, note = _auto_target("x", None)
                if not port:
                    return note
            with serial.Serial(port, baud, timeout=0.2) as s:
                time.sleep(2.0 if data else 0.1)  # most boards reset when the port opens
                if data is not None:
                    s.write((data + "\n").encode())
                end, buf = time.time() + seconds, b""
                while time.time() < end and len(buf) < 20000:
                    buf += s.read(4096)
            text = buf.decode(errors="replace")
            return f"Read {len(buf)} bytes from {port} @ {baud}:\n{truncate(text, 8000)}" if text else f"No data from {port} in {seconds}s."

        return f"Unknown action {action}"
