"""Print a G-code file straight over USB (no copying to the printer's card), as its own process.

    python -m batsy.printstream <gcode> <job name>

It owns the printer's serial port for the whole print and doesn't depend on Batsy: Batsy restarting (it often does)
doesn't touch the print. The laptop is kept awake (systemd-inhibit, from printer3d). Every line goes numbered and
checksummed, one at a time, each acknowledged by the printer before the next ("Resend: N" goes back to N), with a
temperature check every few seconds.

Talks to Batsy through two small files in DATA_DIR:
  print_status.json   written every couple of seconds: state, line / total, temperatures, job, pid
  print_control       Batsy writes "pause", "resume" or "cancel"; this process acts on it and deletes it
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from batsy.config import DATA_DIR

STATUS = DATA_DIR / "print_status.json"
CONTROL = DATA_DIR / "print_control"
TEMPS = re.compile(r"T:\s*([\d.]+)\s*/\s*([\d.]+).*?B:\s*([\d.]+)\s*/\s*([\d.]+)")


def _cs(s: str) -> int:
    c = 0
    for ch in s.encode():
        c ^= ch
    return c


class Stream:
    def __init__(self, gcode: Path, job: str):
        import serial

        self.lines = [ln for ln in (raw.split(";", 1)[0].strip()
                                    for raw in gcode.read_text(errors="replace").splitlines()) if ln]
        self.st = {"state": "starting", "job": job, "gcode": str(gcode), "line": 0, "total": len(self.lines),
                   "started": time.time(), "pid": os.getpid(), "hotend": None, "hotend_target": 0, "bed": None,
                   "bed_target": 0, "error": None}
        port = sys.argv[3] if len(sys.argv) > 3 else "/dev/ttyUSB0"
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = port, 115200, 0.5
        self.ser.open()  # -hupcl is set on the port: opening doesn't restart the printer
        self.saved_at = 0.0

    # --- talking ------------------------------------------------------------------------------------------
    def read(self) -> str:
        raw = self.ser.readline()
        line = raw.decode(errors="replace").strip()
        m = TEMPS.search(line)
        if m:
            self.st.update(hotend=float(m.group(1)), hotend_target=float(m.group(2)), bed=float(m.group(3)),
                           bed_target=float(m.group(4)))
        if line.startswith("Error") and "Line Number" not in line and "checksum" not in line.lower():
            self.st["error"] = line
        return line

    def send(self, n: int, cmd: str, timeout: float = 900) -> bool:
        """Send line n, wait for its "ok" (long waits are heating: the printer says "busy" meanwhile).
        False if it asked for an earlier line again (the caller goes back)."""
        body = f"N{n} {cmd}"
        self.ser.write(f"{body}*{_cs(body)}\n".encode())
        end = time.time() + timeout
        while time.time() < end:
            line = self.read()
            self.save()
            if not line:
                continue
            m = re.match(r"(?:Resend|rs):?\s*(\d+)", line, re.I)
            if m:
                self.resend = int(m.group(1))
                return False
            if line.startswith("ok"):
                return True
        raise OSError(f"the printer stopped answering at line {n}")

    def save(self, force: bool = False) -> None:
        if force or time.time() - self.saved_at > 2:
            self.saved_at = time.time()
            tmp = STATUS.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.st))
            tmp.replace(STATUS)

    # --- printing -----------------------------------------------------------------------------------------
    def run(self) -> None:
        time.sleep(0.5)
        while self.ser.in_waiting:
            self.read()
        self.ser.write(b"M110 N0\n")  # line numbers from 0
        t = time.time() + 3
        while time.time() < t and not self.read().startswith("ok"):
            pass
        self.st["state"] = "printing"
        self.save(True)
        n, i, last_temp = 1, 0, time.time()
        while i < len(self.lines):
            if self._control():
                return
            if time.time() - last_temp > 5:  # temperatures for Batsy, between lines
                last_temp = time.time()
                if not self.send(n, "M105"):
                    n, i = self._back(n, i)
                    continue
                n += 1
            self.resend = None
            if not self.send(n, self.lines[i]):
                n, i = self._back(n, i)
                continue
            n, i = n + 1, i + 1
            self.st["line"] = i
        self.st["state"] = "done"
        self.st["finished"] = time.time()
        self.save(True)

    def _back(self, n: int, i: int) -> tuple[int, int]:
        """The printer asked for line `resend` again: step back by the difference."""
        back = max(0, n - (self.resend or n))
        return n - back, max(0, i - back)

    def _control(self) -> bool:
        """Pause / resume / cancel from Batsy. True when the print is over (cancelled)."""
        if not CONTROL.exists():
            return False
        what = CONTROL.read_text().strip()
        CONTROL.unlink(missing_ok=True)
        if what == "pause":
            self.st["state"] = "paused"
            self.save(True)
            while True:  # wait here; the printer finishes the moves it has and holds its temperatures
                time.sleep(0.5)
                while self.ser.in_waiting:
                    self.read()
                if CONTROL.exists():
                    nxt = CONTROL.read_text().strip()
                    CONTROL.unlink(missing_ok=True)
                    if nxt == "resume":
                        self.st["state"] = "printing"
                        self.save(True)
                        return False
                    if nxt == "cancel":
                        return self._cancel()
                self.save()
        if what == "cancel":
            return self._cancel()
        return False

    def _cancel(self) -> bool:
        for cmd in ("M104 S0", "M140 S0", "M107", "G91", "G1 Z10 F600", "G90", "G28 X Y", "M84"):
            self.ser.write((cmd + "\n").encode())
            t = time.time() + 30
            while time.time() < t and not self.read().startswith("ok"):
                pass
        self.st["state"] = "cancelled"
        self.st["finished"] = time.time()
        self.save(True)
        return True


def main() -> None:
    gcode, job = Path(sys.argv[1]), sys.argv[2]
    CONTROL.unlink(missing_ok=True)
    s = Stream(gcode, job)
    try:
        s.run()
    except Exception as e:
        s.st.update(state="failed", error=str(e), finished=time.time())
        s.save(True)
    finally:
        try:
            s.ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
