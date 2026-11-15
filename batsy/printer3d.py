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
        from batsy.config import DATA_DIR

        global STOPPED_FILE
        STOPPED_FILE = DATA_DIR / "printer_stopped.json"
        try:
            import json

            self.stopped_byte = json.loads(STOPPED_FILE.read_text()).get("byte")  # still frozen there: still stopped
        except (OSError, ValueError):
            self.stopped_byte = None
        self.ser = None
        self.lock = threading.Lock()          # one command at a time
        self.ok = threading.Event()
        self.lines: list[str] = []            # what the printer said since the last command
        self.state = {"connected": False, "hotend": None, "hotend_target": 0, "bed": None, "bed_target": 0,
                      "printing": False, "paused": False, "progress": None, "file": None, "started": None,
                      "job": None, "firmware": "", "uploading": None, "error": None}
        self._stop = False
        threading.Thread(target=self._keep, daemon=True, name="printer3d").start()

    # --- the link ----------------------------------------------------------------------------------------------
    def _keep(self) -> None:
        """Connect, and reconnect when the cable comes back. Never reopen a working link."""
        import serial

        while not self._stop:
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
            self.state.update(printing=False, paused=False, progress=100, started=None)
            self._say(f"The print is done: {self.state.get('job') or 'your model'}. Let the bed cool before taking it off.")
        if line.startswith("Error") or "!!" in line:
            self.state["error"] = line
            log.warning("printer: %s", line)
            if "thermal" in line.lower() or "MINTEMP" in line or "MAXTEMP" in line:
                self._say(f"The printer stopped with an error: {line.split(':', 1)[-1].strip()}.")
        if line.startswith("ok"):
            self.ok.set()
        elif not line.startswith(" T:") and not line.startswith("T:"):
            self.lines.append(line)

    def _poll(self) -> None:
        """Read forever; ask for SD progress every 10 s while printing."""
        last = 0.0
        self.temps_at = getattr(self, "temps_at", 0.0)
        while not self._stop and self.ser:
            if time.time() - self.temps_at > 6 and not self.state["uploading"]:  # no report by itself: ask
                self.temps_at = time.time()
                self.send("M105", 3)
            if not self.lock.locked():
                with self.lock:
                    self._read_line()
            else:
                time.sleep(0.05)
            if time.time() - last > 10 and self.state["uploading"] is None:  # progress, whoever started the print
                last = time.time()
                self.send("M27", 2)
                self._unstick()
                if getattr(self, "want_name", False):
                    self.want_name = False
                    self.send("M27 C", 2)  # newer firmware says the file name; 1.1.6 may not

    def send(self, cmd: str, timeout: float = 5) -> list[str]:
        """Send one G-code line and wait for its "ok". Returns what the printer said."""
        if not self.ser:
            raise OSError("The printer isn't connected.")
        with self.lock:
            self.ok.clear()
            self.lines = []
            self.ser.write((cmd.strip() + "\n").encode())
            end = time.time() + timeout
            while time.time() < end and not self.ok.is_set():
                self._read_line()
            return list(self.lines)

    def _say(self, text: str) -> None:
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
        try:
            STOPPED_FILE.write_text(json.dumps({"byte": byte, "t": time.time()}))
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
            left = ""
            secs = self.remaining()
            if secs:
                left = f", about {_dur(secs)} left"
            return (f"{'Paused' if s['paused'] else 'Printing'} {s['job'] or 'a file'}: {s['progress'] or 0} percent{left}. "
                    f"{temps[0].upper()}{temps[1:]}.")
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

    def remaining(self) -> float | None:
        """Seconds left, from how fast the progress has been moving (the last 15 minutes), else since the start."""
        s, samples = self.state, getattr(self, "samples", [])
        p = s.get("progress")
        if not p or p >= 100:
            return None
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
        return {**self.state, "remaining_s": round(self.remaining() or 0) or None}

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
        self.send("M25")
        self.state["paused"] = True
        return "Paused."

    def resume(self) -> str:
        self.send("M24")
        self.state["paused"] = False
        return "Carrying on."

    def sd_files(self) -> list[str]:
        """What's on the printer's SD card (G-code only)."""
        self.send("M21", 5)
        said = self.send("M20", 10)
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
        for c in ("M25", "M104 S0", "M140 S0", "M107", "G91", "G1 Z10 F600", "G90", "G28 X Y", "M84"):
            try:
                self.send(c, 30)
            except OSError:
                break
        self.state.update(printing=False, paused=False, progress=None)
        return "Stopped the print, turned the heaters off and moved the head away."

    def print_file(self, gcode: Path, job: str) -> None:
        """Copy to the SD card, then start it there. Runs in the background; announces how it goes."""
        name = "BATSY" + time.strftime("%H%M") + ".GCO"  # 8.3: what Marlin 1.1 can write
        lines = []
        for raw in gcode.read_text(errors="replace").splitlines():
            line = raw.split(";", 1)[0].strip()
            if line:
                lines.append(line)
        self.state.update(job=job, uploading=0, error=None)
        try:
            self.send("M21", 5)                 # card ready
            self.send("M110 N0", 3)
            self.send(f"M28 {name}", 10)
            n = 0
            for i, line in enumerate(lines, 1):
                n = i
                body = f"N{i} {line}"
                cs = 0
                for ch in body.encode():
                    cs ^= ch
                for attempt in range(3):
                    said = self.send(f"{body}*{cs}", 10)
                    if not any("Resend" in x or "rs " in x.lower() for x in said):
                        break
                if i % 500 == 0:
                    self.state["uploading"] = round(100 * i / len(lines))
            self.send(f"N{n + 1} M29*" + str(_cs(f"N{n + 1} M29")), 10)  # end of file: Marlin closes it
            self.send(f"M23 {name}", 10)
            self.send("M24", 5)
            self.state.update(uploading=None, printing=True, paused=False, progress=0, started=time.time(), file=name)
            self.ours, self.unstuck_once, self.byte_at = True, False, time.time()
            self._say(f"{job} is printing. It's running from the printer's card, so it carries on by itself.")
        except Exception as e:
            self.state["uploading"] = None
            log.exception("printer: upload failed")
            self._say(f"I couldn't start the print: {e}")


def _cs(s: str) -> int:
    c = 0
    for ch in s.encode():
        c ^= ch
    return c


def _dur(secs: float) -> str:
    h, m = int(secs // 3600), int(secs % 3600 // 60)
    return (f"{h} hour{'s' if h != 1 else ''} " if h else "") + f"{m} minute{'s' if m != 1 else ''}"


# --- voice ---------------------------------------------------------------------------------------------------------
P = r"(?:the |my )?(?:3d )?printer"
STATUS = re.compile(rf"^(?:how'?s|how is|check(?: on)?|what'?s|what is) (?:the |my )?(?:3d )?(?:printer|print)(?: doing| status)?$|"
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
        m = PRINT.match(n)
        if m and m["what"] not in ("screen", "the screen", "a document", "this page") and not re.search(r"\bpdf\b|\bpage", n):
            return _print(app, pr, m["what"], m["proc"])
    except OSError as e:
        return str(e)
    return None


def _print(app, pr: Printer, what: str, proc: str | None) -> str | None:
    from batsy import slicer

    model = slicer._named_model(what) if what not in ("it", "this", "that", "the model") else None
    model = model or slicer._target(app, None if what in ("it", "this", "that", "the model") else what)
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
    threading.Thread(target=pr.print_file, args=(Path(r["gcode"]), model.stem.replace("_", " ")), daemon=True,
                     name="print-upload").start()
    return "Copying it to the printer's card. It starts by itself when that's done, in a few minutes."
