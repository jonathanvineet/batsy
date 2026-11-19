"""Your 3D printer on USB (Ender-5 Pro, Marlin 1.1.6): status, heating, moving, and printing, by voice.

The USB serial link is opened ONCE and kept open: opening it restarts the printer's board (Creality boards reset on
connect), which would kill a print. A reader thread follows everything the printer says: temperatures (it reports
them every few seconds by itself, M155), "ok"s, SD progress, "Done printing file", errors.

Printing goes through the printer's SD card: Batsy copies the G-code onto it over USB (M28 ... M29, every line
checksummed) and then starts it from the card (M23 / M24). From then on the printer prints by itself, so the
laptop can sleep, Batsy can restart, the cable can even come out, and the print carries on.

Voice: "how's the printer", "preheat the printer", "cool down the printer", "home the printer", "print it" /
"print the iphone stand", "pause the print", "resume the print", "stop the print", "move the nozzle up 10 mm",
"turn off the printer motors". Heating and printing ask first (and printing reminds you to clear the bed).
"""
import glob
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("batsy.printer3d")
STOPPED_FILE = None  # set below: remembers a print stopped on the printer's screen, across Batsy restarts
BAUD = 115200
TEMPS = re.compile(r"T:\s*([\d.]+)\s*/\s*([\d.]+).*?B:\s*([\d.]+)\s*/\s*([\d.]+)")
SD = re.compile(r"(?:SD|TF) printing byte (\d+)/(\d+)")  # Creality's firmware says "TF printing"


def find_port() -> str | None:
    """The printer's USB serial (CH340 on Creality boards), not your phone's."""
    for link in sorted(glob.glob("/dev/serial/by-id/*")):
        if re.search(r"1a86|ch34|usb-serial|creality|marlin", link, re.I) and "android" not in link.lower():
            return str(Path(link).resolve())
    ports = sorted(glob.glob("/dev/ttyUSB*"))
    return ports[0] if ports else None


class Printer:
    def __init__(self, app=None):
        self.app = app
        self.ser = None
        self.lock = threading.Lock()          # one command at a time
        self.ok = threading.Event()
        self.lines: list[str] = []            # what the printer said since the last command
        self.state = {"connected": False, "hotend": None, "hotend_target": 0, "bed": None, "bed_target": 0,
                      "printing": False, "paused": False, "progress": None, "file": None, "started": None,
                      "job": None, "firmware": "", "uploading": None, "error": None}
        from batsy.config import DATA_DIR

        global STOPPED_FILE
        STOPPED_FILE = DATA_DIR / "printer_stopped.json"
        try:
            import json

            saved = json.loads(STOPPED_FILE.read_text())
            self.stopped_byte = saved.get("byte")  # still frozen there: still stopped
            if saved.get("job") and saved.get("byte") is None:  # a print that was running when Batsy restarted
                self.state.update(job=saved["job"], file=saved.get("file"), started=saved.get("started"),
                                  gcode=saved.get("gcode"))
                self.ours = bool(saved.get("ours"))
                self._load_times(saved.get("gcode"))
        except (OSError, ValueError):
            self.stopped_byte = None
        self._stop = False
        threading.Thread(target=self._keep, daemon=True, name="printer3d").start()

    # --- the link ----------------------------------------------------------------------------------------------
    def _keep(self) -> None:
        """Connect, and reconnect when the cable comes back. Never reopen a working link."""
        import serial

        while not self._stop:
            if self._streaming():  # a print is streaming from its own process: it has the port
                self._sync_stream()
                time.sleep(2)
                continue
            if getattr(self, "stream_seen", False) or getattr(self, "hand_over", False):
                self._sync_stream()
                self.stream_ended()
                self.stream_seen = False
            port = find_port()
            if not port:
                self.state["connected"] = False
                time.sleep(5)
                continue
            try:
                s = serial.Serial()
                s.port, s.baudrate, s.timeout = port, BAUD, 0.5
                s.open()  # DTR stays as it is: with -hupcl below, reopening never pulses it, so the board never resets
            except (OSError, serial.SerialException) as e:
                log.info("printer: can't open %s (%s)", port, e)
                time.sleep(10)
                continue
            self.ser = s
            # keep DTR up when the port closes (stty -hupcl): otherwise the next open (a Batsy restart) reboots the
            # printer's board and kills a print. The board restarts once now, on this first open, never again.
            subprocess.run(["stty", "-F", port, "-hupcl"], capture_output=True)
            log.info("printer: connected on %s", port)
            boot_until = time.time() + 6  # the board restarts as the port opens: let it finish
            try:
                self._read_until(boot_until)
                self.state["connected"] = True
                self.send("M155 S3")       # report temperatures every 3 s by itself
                self.send("M105", 3)       # temperatures first: a "printing" report with the heaters off is a stopped one
                fw = self.send("M115", 3)
                m = re.search(r"MACHINE_TYPE:([^\s]+(?: [^\s:]+)*?) EXTRUDER", " ".join(fw))
                self.state["firmware"] = m.group(1) if m else ""
                self._poll()
            except (OSError, serial.SerialException) as e:
                log.info("printer: link lost (%s)", e)
            finally:
                self.state["connected"] = False
                self.ser = None
                try:
                    s.close()
                except Exception:
                    pass
            time.sleep(3)

    def _read_until(self, until: float) -> None:
        while time.time() < until:
            self._read_line()

    def _read_line(self) -> None:
        raw = self.ser.readline()
        if not raw:
            return
        line = raw.decode(errors="replace").strip()
        if not line:
            return
        m = TEMPS.search(line)
        if m:
            self.state.update(hotend=float(m.group(1)), hotend_target=float(m.group(2)), bed=float(m.group(3)),
                              bed_target=float(m.group(4)))
            self.temps_at = time.time()
            if self.state["hotend_target"] > 100:  # what the print is using, to re-heat to after a pause
                self.print_temps = (self.state["hotend_target"], self.state["bed_target"])
        m = SD.search(line)
        if m and int(m.group(1)) == getattr(self, "stopped_byte", None):
            m = None  # the frozen number of a print stopped on the screen: not printing (still passed on below)
        if m and not self.state["printing"] and getattr(self, "temps_at", 0) and self.state["hotend_target"] == 0 \
                and self.state["bed_target"] == 0 and getattr(self, "last_byte", None) is None:
            self._mark_stopped(int(m.group(1)))  # first sight since connecting, heaters off: a stopped print, not a new one
            m = None
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            pct = round(100 * done / total, 1) if total else None
            if not self.state["printing"] and pct is not None and pct < 100:  # started (maybe from the printer's screen)
                self.state.update(started=self.state.get("started") or time.time(), paused=False)
                self.samples = []
                if not self.state.get("job"):
                    self.want_name = True
            self.state["progress"] = pct
            self.state["printing"] = done < total
            if pct is not None:
                self.samples = [s for s in getattr(self, "samples", []) if time.time() - s[0] < 900] + [(time.time(), pct)]
                if done != getattr(self, "last_byte", None):
                    self.last_byte, self.byte_at = done, time.time()
        m = re.search(r"Current file:\s*(\S+)", line)
        if m:
            self.state["job"] = self.state.get("job") or m.group(1)
        if "Not SD printing" in line or "Not TF printing" in line:
            if self.state["printing"] and not self.state["paused"]:
                self.state.update(printing=False, started=None, job=None)
            self.state["printing"] = False if not self.state["paused"] else self.state["printing"]
        if "Done printing file" in line:
            from batsy import vault_capture

            vault_capture.print_ended(self.state.get("job") or "print", "done")
            self.state.update(printing=False, paused=False, progress=100, started=None, finished_at=time.time())
            self._say(f"The print is done: {self.state.get('job') or 'your model'}. Let the bed cool before taking it off.")
        if line.startswith("Error") or "!!" in line:
            self.state["error"] = line
            log.warning("printer: %s", line)
            if "thermal" in line.lower() or "MINTEMP" in line or "MAXTEMP" in line:
                self._say(f"The printer stopped with an error: {line.split(':', 1)[-1].strip()}.")
        m = re.match(r"(?:Resend|rs):?\s*(\d+)", line, re.I)
        if m:
            self.resend_from = int(m.group(1))
        if "error writing" in line.lower():
            self.write_errors = getattr(self, "write_errors", 0) + 1
        if line.startswith("ok"):
            self.ok.set()
            self.ok_count = getattr(self, "ok_count", 0) + 1
        elif not line.startswith(" T:") and not line.startswith("T:"):
            self.lines.append(line)

    def _poll(self) -> None:
        """Read forever; ask for SD progress every 10 s while printing."""
        last = 0.0
        self.temps_at = getattr(self, "temps_at", 0.0)
        while not self._stop and self.ser:
            if getattr(self, "hand_over", False):  # a print is about to stream: let go of the port
                return
            if time.time() - self.temps_at > 6 and not self.state["uploading"]:  # no report by itself: ask
                self.temps_at = time.time()
                self.send("M105", 3)
            if not self.lock.locked():
                with self.lock:
                    self._read_line()
            else:
                time.sleep(0.05)
            if time.time() - getattr(self, "dock_at", 0) > 4:
                self.dock_at = time.time()
                self._to_dock()
            if time.time() - last > 10 and self.state["uploading"] is None:  # progress, whoever started the print
                last = time.time()
                self.send("M27", 2)
                self._unstick()
                if getattr(self, "want_name", False):
                    self.want_name = False
                    self.send("M27 C", 2)  # newer firmware says the file name; 1.1.6 may not

    def send(self, cmd: str, timeout: float = 5, until: str | None = None) -> list[str]:
        """Send one G-code line and wait for its "ok" (or, with `until`, for a line matching that pattern: the
        printer's real answer). Returns what the printer said.
        Anything still waiting from before is read first: a late "ok" (a check that timed out) would otherwise be
        taken as this command's, and every answer after it would be one command late."""
        if not self.ser:
            raise OSError("The printer isn't connected.")
        with self.lock:
            while self.ser.in_waiting:  # the leftovers of earlier commands: not this one's
                self._read_line()
            self.ok.clear()
            self.lines = []
            self.ser.write((cmd.strip() + "\n").encode())
            end = time.time() + timeout
            rx = re.compile(until, re.I) if until else None
            while time.time() < end:
                self._read_line()
                if rx is None and self.ok.is_set():
                    break
                if rx is not None and any(rx.search(x) for x in self.lines):
                    t = time.time() + 1.5  # and its "ok"
                    while time.time() < t and not self.ok.is_set():
                        self._read_line()
                    break
            return list(self.lines)

    def _send_fast(self, lines: list[str], window_bytes: int = 110, window_lines: int = 3) -> bool:
        """Copy with a few lines in flight (the printer's 128-byte receive buffer holds them), so the cable never
        sits idle waiting for each "ok": about twice as fast as line by line. Checksummed and numbered as always;
        "Resend: N" goes back to line N. A card write error can't be pinned to one line with several in flight,
        so it returns False and the caller starts over carefully. True when every line is in."""
        self.write_errors, self.resend_from, self.ok_count = 0, None, 0
        framed = []
        for i, line in enumerate(lines, 1):
            body = f"N{i} {line}"
            framed.append(f"{body}*{_cs(body)}\n".encode())
        nxt, inflight = 0, []  # inflight: sizes of lines sent but not yet acknowledged, oldest first
        with self.lock:
            while self.ser.in_waiting:
                self._read_line()
            last_progress = time.time()
            while nxt < len(framed) or inflight:
                if self.cancel_upload.is_set():
                    raise OSError("stopped")
                while nxt < len(framed) and len(inflight) < window_lines and sum(inflight) + len(framed[nxt]) <= window_bytes:
                    self.ser.write(framed[nxt])
                    inflight.append(len(framed[nxt]))
                    nxt += 1
                before = self.ok_count
                self._read_line()
                if self.write_errors:
                    return False
                if self.resend_from is not None:  # go back: the printer drops everything after the bad line
                    nxt, inflight = self.resend_from - 1, []
                    self.resend_from = None
                    time.sleep(0.05)
                    while self.ser.in_waiting:
                        self._read_line()
                    self.ok_count = 0
                    continue
                for _ in range(self.ok_count - before):
                    if inflight:
                        inflight.pop(0)
                if self.ok_count > before:
                    last_progress = time.time()
                    if nxt % 500 < window_lines:
                        self.state["uploading"] = round(100 * nxt / len(framed))
                        self._to_dock()
                elif time.time() - last_progress > 15:
                    raise OSError(f"the printer stopped answering at line {nxt}")
        return True

    # --- printing straight over USB (batsy/printstream.py, its own process) --------------------------------
    def print_now(self, gcode: Path, job: str) -> str:
        """Print directly: no copying to the card. The file is kept in the print library to print again."""
        import json
        import os
        import shutil
        import subprocess
        import sys

        from batsy import printstream
        from batsy.config import DATA_DIR, SOURCE_DIR

        if self._streaming() or self.state["printing"]:
            return "The printer is busy."
        lib = DATA_DIR / "prints"
        lib.mkdir(exist_ok=True)
        keep = lib / gcode.name
        if gcode.resolve() != keep.resolve():
            shutil.copy2(gcode, keep)
        hist_f = lib / "history.json"
        try:
            hist = json.loads(hist_f.read_text())
        except (OSError, ValueError):
            hist = []
        started = time.time()
        hist = [h for h in hist if h.get("gcode") != str(keep)] + [{"job": job, "gcode": str(keep), "t": started}]
        hist_f.write_text(json.dumps(hist[-100:], indent=1))
        try:  # a note for this print in the brain (with the slicer's preview, grams and time)
            from batsy import vault_capture

            self._load_times(str(keep))
            vault_capture.print_started(job, str(gcode), dict(self.state.get("info") or {}), started)
        except Exception:
            log.exception("brain: print note")
        port = find_port() or "/dev/ttyUSB0"
        self.hand_over = True  # the poll loop lets go of the port
        with self.lock:
            try:
                if self.ser:
                    self.ser.close()  # -hupcl: closing doesn't restart the printer
            except Exception:
                pass
            self.ser = None
        printstream.STATUS.unlink(missing_ok=True)
        cmd = [sys.executable, "-m", "batsy.printstream", str(keep), job, port]
        if shutil.which("systemd-inhibit"):  # the laptop mustn't sleep in the middle of a print
            cmd = ["systemd-inhibit", "--what=sleep:idle", "--who=Batsy", f"--why=Printing {job}", "--mode=block"] + cmd
        subprocess.Popen(cmd, cwd=str(SOURCE_DIR.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=open(DATA_DIR / "printstream.log", "ab"), start_new_session=True,
                         env={**os.environ, "PYTHONPATH": str(SOURCE_DIR.parent)})
        self.state.update(job=job, printing=True, paused=False, progress=0, started=time.time(), gcode=str(keep),
                          uploading=None, connected=True)
        self._load_times(str(keep))
        self.stream_seen = False
        return f"Printing {job} straight from the laptop. It's saved, so you can print it again any time."

    def _stream_status(self) -> dict:
        import json

        from batsy import printstream

        try:
            return json.loads(printstream.STATUS.read_text())
        except (OSError, ValueError):
            return {}

    def _streaming(self) -> bool:
        import os

        st = self._stream_status()
        pid = st.get("pid")
        if not pid or st.get("state") in ("done", "failed", "cancelled"):
            return bool(getattr(self, "hand_over", False) and time.time() - (self.state.get("started") or 0) < 30)
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _sync_stream(self) -> None:
        """The streaming print's status into Batsy's (the app, the corner bar, "how's the printer")."""
        st = self._stream_status()
        if not st:
            return
        self.stream_seen = True
        total = st.get("total") or 1
        s = self.state
        s.update(connected=True, job=st.get("job"), gcode=st.get("gcode"), started=st.get("started"),
                 progress=round(100 * st.get("line", 0) / total, 1), paused=st.get("state") == "paused",
                 printing=st.get("state") in ("printing", "paused", "starting"), uploading=None,
                 hotend=st.get("hotend"), hotend_target=st.get("hotend_target") or 0, bed=st.get("bed"),
                 bed_target=st.get("bed_target") or 0)
        if not getattr(self, "time_table", None) and st.get("gcode"):
            self._load_times(st["gcode"])
        self._to_dock()

    def stream_ended(self) -> None:
        """Called once the streaming process is gone: say how it went, then take the port back."""
        st = self._stream_status()
        state = st.get("state")
        if getattr(self, "announced_end", None) == st.get("started"):
            return
        self.announced_end = st.get("started")
        self.hand_over = False
        self.state.update(printing=False, paused=False)
        if state in ("done", "failed", "cancelled"):
            from batsy import vault_capture

            vault_capture.print_ended(st.get("job") or "print", state, error=st.get("error") or "")
        if state == "done":
            self.state.update(progress=100, finished_at=time.time())
            self._say(f"The print is done: {st.get('job')}. Let the bed cool before taking it off.")
        elif state == "failed":
            self._say(f"The print stopped: {st.get('error') or 'the printer stopped answering'}.")
        elif state == "cancelled":
            self.state.update(progress=None)
        self._to_dock()

    def _control(self, what: str, said: str) -> str:
        from batsy import printstream

        printstream.CONTROL.write_text(what)
        if what == "pause":
            self.state["paused"] = True
        elif what == "resume":
            self.state["paused"] = False
        return said

    def history(self) -> list[dict]:
        import json

        from batsy.config import DATA_DIR

        try:
            return json.loads((DATA_DIR / "prints" / "history.json").read_text())
        except (OSError, ValueError):
            return []

    def _to_dock(self) -> None:
        """The copy / print progress under the bat in the screen's corner (cleared when idle)."""
        ov = getattr(self.app, "overlay", None)
        if ov is None or not hasattr(ov, "_dock_send"):
            return
        s = self.state
        if s["uploading"] is not None:
            pct, label = s["uploading"], f"Copying to printer {s['uploading']}%"
        elif s["printing"] and s["progress"] is not None and not self.stopped():
            left = self.remaining()
            pct = s["progress"]
            label = f"Printing {s['progress']:.0f}%" + (f" · {_short(left)} left" if left else "")
        else:
            pct, label = None, ""
        if (pct, label) != getattr(self, "dock_last", None):
            self.dock_last = (pct, label)
            try:
                ov._dock_send("progress", pct=pct, label=label)
            except Exception:
                pass

    def _say(self, text: str) -> None:
        try:  # and on your phone: the printer's news is the kind you want away from the laptop
            from batsy import push

            push.notify("3D printer", text, tag="printer")
        except Exception:
            pass
        if self.app is not None:
            try:
                self.app.announce(text)
            except Exception:
                pass

    # --- actions -----------------------------------------------------------------------------------------------
    def _mark_stopped(self, byte: int | None) -> None:
        import json

        self.stopped_byte = byte
        self.state.update(printing=False, job=None, started=None)
        self.ours = False
        self._save_job()

    def _save_job(self) -> None:
        """Remembered across Batsy restarts: a stopped print's frozen byte, or the running print's name."""
        import json

        s = self.state
        try:
            STOPPED_FILE.write_text(json.dumps({"byte": self.stopped_byte, "t": time.time(), "job": s.get("job"),
                                                "file": s.get("file"), "started": s.get("started"), "gcode": s.get("gcode"),
                                                "ours": bool(getattr(self, "ours", False))}))
        except OSError:
            pass

    def stopped(self) -> bool:
        """Creality's firmware keeps answering "TF printing byte N" after you stop a print on its screen. Frozen there
        for 2 minutes with the heaters off (and not paused by Batsy) means it's not printing any more."""
        s = self.state
        return bool(s["printing"] and not s["paused"] and s["hotend_target"] == 0
                    and time.time() - getattr(self, "byte_at", time.time()) > 120)

    def status(self) -> str:
        s = self.state
        if self.stopped():
            self._mark_stopped(getattr(self, "last_byte", None))
        if not s["connected"]:
            return "The printer isn't connected. Is it switched on, with the USB cable in?"
        temps = f"the nozzle is at {round(s['hotend'] or 0)} degrees" + \
            (f" heading for {round(s['hotend_target'])}" if s["hotend_target"] else "") + \
            f", the bed at {round(s['bed'] or 0)}" + (f" heading for {round(s['bed_target'])}" if s["bed_target"] else "")
        if s["uploading"]:
            return f"I'm copying {s['job']} to the printer's card, {s['uploading']} percent done. Then it starts."
        if s["printing"] or s["paused"]:
            # short: how far, how long, when it's done (temperatures only while it's still heating up)
            secs = self.remaining()
            pct = s["progress"] or 0
            pct = f"{pct:.0f}" if pct >= 10 else f"{pct:.1f}".rstrip("0").rstrip(".")
            left = f", about {_dur(secs)} left, done at {time.strftime('%-I:%M %p', time.localtime(time.time() + secs)).lower()}" \
                if secs else ""
            heating = s["hotend_target"] and abs((s["hotend"] or 0) - s["hotend_target"]) > 5
            job = (s["job"] or "the print").split(" · ")[0]
            return (f"{job} is {'paused at' if s['paused'] else ''} {pct} percent{' done' if not s['paused'] else ''}{left}."
                    .replace("is  ", "is ") + (f" Still heating: {temps}." if heating else ""))
        return f"The printer is idle. {temps[0].upper()}{temps[1:]}."

    def _unstick(self) -> None:
        """A pause near the start that nobody asked for (a pause written into the file, after the purge line): the
        firmware turns the heaters off and waits. Heat back to the print's temperatures, wait for them, then go on.
        Later pauses are left alone: you might have paused it on purpose."""
        s = self.state
        stuck = (s["printing"] and not s["paused"] and getattr(self, "ours", False) and not getattr(self, "unstuck_once", False)
                 and s["progress"] is not None and s["progress"] < 3
                 and time.time() - getattr(self, "byte_at", time.time()) > 45
                 and s["hotend_target"] == 0 and getattr(self, "print_temps", None))
        if not stuck or time.time() - getattr(self, "unstuck_at", 0) < 300:
            return
        self.unstuck_at = time.time()
        self.unstuck_once = True  # once per print: a second stop is yours
        noz, bed = self.print_temps
        log.info("printer: paused by itself at %s%%: re-heating to %s/%s and resuming", s["progress"], noz, bed)
        self._say(f"The print paused by itself at the start. Heating back up to {round(noz)} degrees and carrying on.")
        self.send(f"M140 S{bed:g}")
        self.send(f"M104 S{noz:g}")
        self.send(f"M190 S{bed:g}", 600)   # wait for the bed...
        self.send(f"M109 S{noz:g}", 600)   # ...and the nozzle, so nothing extrudes cold
        self.send("M24", 5)
        self.byte_at = time.time()

    def _load_times(self, gcode: str | None) -> None:
        """The slicer's own time left at every layer ("M73 P<done> R<minutes>"), by position in the file: the
        printer reports how far through the file it is, so this gives the real time left, not a guess."""
        self.time_table = []
        self.state["info"] = None
        if not gcode or not Path(gcode).exists():
            return
        try:  # what the slicer says about it: weight, length, total time, the profile
            from batsy import slicer

            est = slicer._estimates(Path(gcode))
            m = re.search(r" - (.+?)\.gcode$", Path(gcode).name)
            self.state["info"] = {"grams": est.get("grams"), "metres": est.get("metres"), "total": est.get("time"),
                                  "profile": m.group(1) if m else ""}
        except Exception:
            pass
        n, marks = 0, []
        for raw in Path(gcode).read_text(errors="replace").splitlines():
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            n += 1
            if line.startswith("M73") and " R" in line:
                m = re.search(r"R(\d+)", line)
                if m:
                    marks.append((n, int(m.group(1))))
        self.time_table = [(i / n, r) for i, r in marks] if n else []

    def remaining(self) -> float | None:
        """Seconds left: the slicer's per-layer estimate at the printer's place in the file; without one, from how
        fast the progress has been moving (the last 15 minutes)."""
        s, samples = self.state, getattr(self, "samples", [])
        p = s.get("progress")
        if not p or p >= 100:
            return None
        table = getattr(self, "time_table", [])
        if table:
            f = p / 100
            prev = table[0]
            for frac, mins in table:
                if frac > f:
                    break
                prev = (frac, mins)
            return prev[1] * 60
        if len(samples) >= 3 and samples[-1][1] > samples[0][1]:
            rate = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])  # percent per second
            return (100 - p) / rate
        if s.get("started") and p > 1:
            return (time.time() - s["started"]) * (100 - p) / p
        return None

    def snapshot(self) -> dict:
        """For the phone app."""
        if self.stopped():
            self._mark_stopped(getattr(self, "last_byte", None))
        done = bool(self.state.get("finished_at") and time.time() - self.state["finished_at"] < 900)
        return {**self.state, "remaining_s": round(self.remaining() or 0) or None, "done": done,
                "streaming": self._streaming()}

    def preheat(self, nozzle: int = 200, bed: int = 60) -> str:
        self.send(f"M104 S{nozzle}")
        self.send(f"M140 S{bed}")
        return f"Heating the nozzle to {nozzle} and the bed to {bed}."

    def cool(self) -> str:
        for c in ("M104 S0", "M140 S0", "M107"):
            self.send(c)
        return "Heaters off. Cooling down."

    def home(self) -> str:
        self.send("G28", 60)
        return "Homed."

    def move(self, axis: str, mm: float) -> str:
        speed = 600 if axis == "Z" else 3000
        for c in ("G91", f"G1 {axis}{mm:g} F{speed}", "G90"):
            self.send(c, 30)
        return f"Moved {axis} by {mm:g} millimetres."

    def motors_off(self) -> str:
        self.send("M84")
        return "Motors off. You can move the head by hand now."

    def pause(self) -> str:
        if self._streaming():
            return self._control("pause", "Paused. The printer finishes the move it's on and keeps its temperature.")
        self.send("M25")
        self.state["paused"] = True
        return "Paused."

    def resume(self) -> str:
        if self._streaming():
            return self._control("resume", "Carrying on.")
        self.send("M24")
        self.state["paused"] = False
        return "Carrying on."

    def sd_files(self) -> list[str]:
        """What's on the printer's SD card (G-code only)."""
        self.send("M21", 5)
        said = self.send("M20", 15, until=r"End file list")
        files, inside = [], False
        for l in said:
            if "Begin file list" in l:
                inside = True
            elif "End file list" in l:
                inside = False
            elif inside and l.strip():
                name = l.split()[0]
                if name.lower().endswith((".gco", ".gcode", ".g")):
                    files.append(name)
        return files

    def long_names(self, files: list[str]) -> dict:
        """Short 8.3 name -> the real name, where the firmware can tell (M33)."""
        out = {}
        for f in files:
            for l in self.send(f"M33 {f}", 3):
                if l.startswith("/") or l.lower().endswith((".gcode", ".gco")) and "~" not in l:
                    out[f] = l.strip().rsplit("/", 1)[-1]
        return out

    def print_sd(self, name: str, job: str = "") -> str:
        """Start a file that's already on the card."""
        self.send(f"M23 {name}", 10)
        self.send("M24", 5)
        self.state.update(job=job or name, printing=True, paused=False, progress=0, started=time.time(), file=name)
        self.samples, self.ours, self.unstuck_once, self.byte_at = [], True, False, time.time()
        self._mark_stopped(None)
        self.state.update(job=job or name, printing=True, started=time.time())
        return f"Printing {job or name} from the card."

    def cancel(self) -> str:
        if self._streaming():
            return self._control("cancel", "Stopping the print: heaters off, and the head moves away.")
        ev = getattr(self, "cancel_upload", None)
        if ev is not None:
            ev.set()  # still copying: stop that too
        for c in ("M25", "M104 S0", "M140 S0", "M107", "G91", "G1 Z10 F600", "G90", "G28 X Y", "M84"):
            try:
                self.send(c, 30)
            except OSError:
                break
        self.state.update(printing=False, paused=False, progress=None)
        return "Stopped the print, turned the heaters off and moved the head away."

    def print_file(self, gcode: Path, job: str, start: bool = True) -> None:
        """Copy to the SD card, then start it there. Runs in the background; announces how it goes.
        Every step is checked: if the card won't open the file, nothing is sent (the lines would be EXECUTED, not
        saved, as happened once), and a line the printer doesn't acknowledge stops the copy."""
        name = "BT" + time.strftime("%d%H%M") + ".GCO"  # 8.3, at most 8 letters before the dot: Marlin 1.1 is strict
        lines = []
        for raw in gcode.read_text(errors="replace").splitlines():
            line = raw.split(";", 1)[0].strip()
            if line:
                lines.append(line)
        self.cancel_upload = threading.Event()
        self.write_errors = 0
        self.copy_t0 = time.time()
        self.state.update(job=job, uploading=0, error=None)
        try:
            for c, t in (("M29", 5), ("M21", 8), ("M110 N0", 3)):  # close a stale file; card ready; line numbers from 0
                log.info("printer: %s -> %s", c, self.send(c, t))  # (M110 BEFORE M28: an open file would swallow it)
            said = self.send(f"M28 {name}", 20, until=r"Writing to file|open failed|error")
            log.info("printer: M28 %s -> %s", name, said)
            opened = " ".join(said)
            if "Writing to file" not in opened:
                raise OSError(f"the printer's card wouldn't open a file ({opened.strip()[:80] or 'no answer'})")
            fast_ok = self._send_fast(lines) if getattr(self, "fast_copy", True) else False
            if not fast_ok:  # careful mode: one line at a time, each acknowledged before the next
                if fast_ok is False and getattr(self, "fast_copy", True):
                    log.info("printer: fast copy hit a card write error: starting over line by line")
                    self.send("M29", 5)
                    self.send("M110 N0", 3)
                    said = self.send(f"M28 {name}", 20, until=r"Writing to file|open failed|error")
                    if not any("Writing to file" in x for x in said):
                        raise OSError("the printer's card wouldn't open the file again")
            for i, line in enumerate(lines if not fast_ok else [], 1):
                if self.cancel_upload.is_set():
                    raise OSError("stopped")
                body = f"N{i} {line}"
                for attempt in range(4):
                    self.ok.clear()
                    said = self.send(f"{body}*{_cs(body)}", 10)
                    bad_write = any("error writing" in x.lower() for x in said)  # the card dropped this line: again
                    if bad_write:
                        self.write_errors = getattr(self, "write_errors", 0) + 1
                        log.info("printer: card write error at line %d, sending it again", i)
                    if self.ok.is_set() and not bad_write and not any("Resend" in x or x.lower().startswith("rs") for x in said):
                        break
                else:
                    raise OSError(f"the printer stopped answering at line {i}")
                if i % 500 == 0:
                    self.state["uploading"] = round(100 * i / len(lines))
                    self._to_dock()
            n = len(lines) + 1
            self.send(f"N{n} M29*{_cs(f'N{n} M29')}", 10)  # end of file: the card closes it
            self.send("M29", 5)  # in case the numbered one above wasn't taken (it's harmless twice)
            listing = " ".join(self.send("M20", 15, until=r"End file list"))
            if name.split(".")[0] not in listing.upper():
                raise OSError("the file isn't on the card after copying")
            if not start:  # copy only (a speed test, or to print it later from the printer's screen)
                self.state["uploading"] = None
                self.last_copy = {"file": name, "secs": round(time.time() - self.copy_t0), "lines": len(lines)}
                log.info("printer: copied %s in %s s", name, self.last_copy["secs"])
                self._to_dock()
                return
            self.send(f"M23 {name}", 10)
            self.send("M24", 5)
            self.state.update(uploading=None, printing=True, paused=False, progress=0, started=time.time(), file=name,
                              gcode=str(gcode))
            self._load_times(str(gcode))
            self.ours, self.unstuck_once, self.byte_at, self.samples = True, False, time.time(), []
            self._mark_stopped(None)
            self.state.update(job=job, printing=True)
            self._save_job()
            self._say(f"{job} is printing. It's running from the printer's card, so it carries on by itself.")
        except Exception as e:
            self.state["uploading"] = None
            try:
                self.send("M29", 5)  # close the file if one is open
            except Exception:
                pass
            log.warning("printer: upload failed: %s", e)
            if str(e) != "stopped":
                self._say(f"I couldn't start the print: {e}.")


def _cs(s: str) -> int:
    c = 0
    for ch in s.encode():
        c ^= ch
    return c


def _short(secs: float) -> str:
    h, m = int(secs // 3600), int(secs % 3600 // 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _dur(secs: float) -> str:
    h, m = int(secs // 3600), int(secs % 3600 // 60)
    return (f"{h} hour{'s' if h != 1 else ''} " if h else "") + f"{m} minute{'s' if m != 1 else ''}"


# --- voice ---------------------------------------------------------------------------------------------------------
P = r"(?:the |my )?(?:3d )?printer"
STATUS = re.compile(rf"^(?:how'?s|how is|check(?: on)?|what'?s|what is) (?:the |my )?(?:3d )?(?:printer|print|printing)"
                    rf"(?: doing| status| going| coming along| getting on| looking| progress)?$|"
                    rf"^(?:how far (?:along )?is|how much (?:is )?(?:done|left) (?:on|in)) (?:the |my )?print$|"
                    rf"^{P} status$|^is the print (?:done|finished|going|ok)$|^how (?:long|much) (?:is )?left on the print$")
PREHEAT = re.compile(rf"^(?:please )?(?:pre ?heat|warm up|heat up|heat)(?: up)? {P}(?: for (?P<mat>pla|petg|abs|tpu))?$")
COOL = re.compile(rf"^(?:please )?(?:cool down|turn off the heaters? on|cool) {P}$|^cool down$")
HOME = re.compile(rf"^(?:please )?home {P}$|^home (?:all )?(?:the )?axes$")
PRINT = re.compile(r"^(?:please )?(?:print|start printing) (?P<what>it|this|that|the model|.+?)(?: with (?P<proc>[a-z0-9 -]+))?$")
PAUSE = re.compile(r"^(?:please )?pause (?:the |my )?print(?:ing|er)?$")
RESUME = re.compile(r"^(?:please )?(?:resume|continue|unpause) (?:the |my )?print(?:ing|er)?$")
CANCEL = re.compile(r"^(?:please )?(?:stop|cancel|abort|kill) (?:the |my )?print(?:ing)?$")
MOVE = re.compile(r"^(?:please )?(?:move|raise|lift|lower) (?:the )?(?:nozzle|head|bed|z)(?: (?P<dir>up|down))?(?: by)? "
                  r"(?P<mm>\d+(?:\.\d+)?) ?(?:mm|millimet(?:re|er)s?)$")
MOTORS = re.compile(rf"^(?:please )?(?:turn off|disable|release) (?:the )?(?:{P} )?(?:motors|steppers)(?: on {P})?$")
TEMPS_FOR = {"pla": (200, 60), "petg": (235, 75), "abs": (240, 95), "tpu": (220, 50)}


def _tested_pla() -> None:
    """PLA's nozzle temperature: the one your temperature tower picked, when there is one."""
    from batsy import slicer

    t = slicer.settings().get("nozzle_temp")
    if t:
        TEMPS_FOR["pla"] = (int(t), TEMPS_FOR["pla"][1])


_tested_pla()


def handle(app, n: str) -> str | None:
    pr: Printer | None = getattr(app, "printer3d", None)
    if pr is None:
        return None
    try:
        if STATUS.match(n):
            return pr.status()
        m = PREHEAT.match(n)
        if m:
            noz, bed = TEMPS_FOR.get(m["mat"] or "pla")
            if not app._confirm_now(f"Heat the printer to {noz} and {bed} degrees?"):
                return "Okay, I won't."
            return pr.preheat(noz, bed)
        if COOL.match(n):
            return pr.cool()
        if HOME.match(n):
            return pr.home()
        if PAUSE.match(n):
            return pr.pause()
        if RESUME.match(n):
            return pr.resume()
        if CANCEL.match(n):
            if not app._confirm_now("Stop the print? It can't be resumed after this."):
                return "Okay, it keeps printing."
            return pr.cancel()
        m = MOVE.match(n)
        if m:
            mm = float(m["mm"])
            down = m["dir"] == "down" or n.startswith("lower")
            if "bed" in n:  # an Ender-5 moves its bed: the bed going down is Z going up
                down = not down
            return pr.move("Z", -mm if down else mm)
        if MOTORS.match(n):
            return pr.motors_off()
        m = AGAIN.match(n)
        if m:
            return _again(app, pr, m["what"] or m["what2"])
        m = PRINT.match(n)
        if m and m["what"] not in ("screen", "the screen", "a document", "this page") and not re.search(r"\bpdf\b|\bpage", n):
            return _print(app, pr, m["what"], m["proc"])
    except OSError as e:
        return str(e)
    return None


AGAIN = re.compile(r"^(?:please )?(?:print|reprint) (?P<what>.+?) again$|^reprint (?P<what2>.+)$")


def _again(app, pr: Printer, what: str) -> str:
    """"print the temperature tower again": straight from the print library, no slicing."""
    from rapidfuzz import fuzz

    hist = pr.history()
    if not hist:
        return "You haven't printed anything with me yet."
    if what in ("it", "that", "this", "the last one", "the last print"):
        pick = hist[-1]
    else:
        pick = max(reversed(hist), key=lambda h: fuzz.partial_ratio(what.lower(), h["job"].lower()))
        if fuzz.partial_ratio(what.lower(), pick["job"].lower()) < 70:
            return f"I haven't printed {what} before. Recent ones: {', '.join(h['job'] for h in hist[-3:])}."
    if not Path(pick["gcode"]).exists():
        return f"I can't find the file for {pick['job']} any more."
    if not app._confirm_now(f"Print {pick['job']} again? Is the bed clear?"):
        return "Okay, not printing."
    return pr.print_now(Path(pick["gcode"]), pick["job"])


def _print(app, pr: Printer, what: str, proc: str | None) -> str | None:
    from batsy import slicer

    it = what in ("it", "this", "that", "the model", "them", "these", "those")
    last = getattr(app, "slice_last", None) or {}
    if it and last.get("result", {}).get("gcode") and Path(last["result"]["gcode"]).exists() and not proc:
        model = Path(last["model"])  # "print it": what was sliced last (e.g. the five-dice plate), as sliced
    else:
        model = slicer._named_model(what) if not it else None
        model = model or slicer._target(app, None if it else what)
    if model is None:
        return None  # not a model Batsy has: maybe "print this document" (paper printer), not ours
    if not pr.state["connected"]:
        return "The printer isn't connected. Switch it on and plug in the USB cable."
    if pr.state["printing"] or pr.state["uploading"] is not None:
        return "The printer is busy with another print."
    last = getattr(app, "slice_last", None) or {}
    use_last = last.get("model") == str(model) and not proc and last.get("result")
    try:
        r = last["result"] if use_last else slicer.slice_model(model, proc)
    except Exception as e:
        return f"I couldn't slice it: {e}"
    when = f" It takes about {slicer._say_time(r['time'])}" if r.get("time") else ""
    grams = f" and {round(r['grams'])} grams of filament" if r.get("grams") else ""
    if not app._confirm_now(f"Print {model.stem.replace('_', ' ')} with {r['process']}?{when}{grams}. Is the bed clear?"):
        return "Okay, not printing."
    return pr.print_now(Path(r["gcode"]), model.stem.replace("_", " "))
