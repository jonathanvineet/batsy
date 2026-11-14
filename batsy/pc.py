"""Your desktop PC (Ubuntu, on the laptop's Ethernet port), controlled from the laptop over SSH.

"Open YouTube on the PC", "PC volume to 30", "is the PC on?", "shut down the PC", "wake up the PC", "lock the PC",
"run df -h on the PC", "update the PC", "open a terminal on the PC": handled here, instantly, no AI. Anything else
about the PC ("on the PC, find my biggest files") goes to the AI, which has the run_on_pc tool (tools/pc.py).

Config: [pc] host = "bob@10.10.10.3", mac = "94:de:80:fb:d7:a0" (for Wake-on-LAN), broadcast = "10.10.10.255".
Shutting down, restarting and updating need root on the PC: "set up the PC" opens a terminal where you type the
PC's password once; it allows exactly those commands without a password (/etc/sudoers.d/batsy) and turns on
Wake-on-LAN. One SSH connection is kept open (ControlMaster), so each command takes ~0.1 s.
"""
import re
import shlex
import socket
import subprocess
import time
from pathlib import Path

from batsy import safety

# Everything that names the PC. "Computer" stays the laptop (system.py), as it always was.
PC = r"(?:the |my )?(?:pc|p c|desktop|desktop pc|other computer|big computer|bob|bob'?s computer)"
ON_PC = re.compile(rf"\b(?:on|in|at|from|of) {PC}\b|\b{PC}'?s?\b")
GUI = ("export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus "
       "WAYLAND_DISPLAY=wayland-0 DISPLAY=:0; ")
SITES = {"youtube": "https://www.youtube.com", "google": "https://www.google.com", "gmail": "https://mail.google.com",
         "github": "https://github.com", "netflix": "https://www.netflix.com", "spotify": "https://open.spotify.com",
         "whatsapp": "https://web.whatsapp.com", "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai",
         "instagram": "https://www.instagram.com", "reddit": "https://www.reddit.com", "x": "https://x.com",
         "twitter": "https://x.com", "amazon": "https://www.amazon.in", "maps": "https://maps.google.com",
         "prime video": "https://www.primevideo.com", "hotstar": "https://www.hotstar.com"}
# what speech recognition writes -> the app
APP_WORDS = {"files": "Files", "file manager": "Files", "browser": "Brave", "brave": "Brave", "firefox": "Firefox",
             "terminal app": "Ptyxis", "settings": "Settings", "calculator": "Calculator", "text editor": "Text Editor",
             "editor": "Text Editor", "clock": "Clocks", "clocks": "Clocks", "app store": "App Center",
             "software": "App Center", "system monitor": "Resources", "task manager": "Resources"}

SUDO_RULES = ("bob ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot, "
              "/usr/bin/systemctl suspend, /usr/bin/apt-get update, /usr/bin/apt-get -y full-upgrade, "
              "/usr/sbin/ethtool")


class Remote:
    def __init__(self, cfg):
        c = cfg.get("pc") or {}
        self.host = c.get("host", "")
        self.mac = c.get("mac", "")
        self.broadcast = c.get("broadcast", "")
        self._apps: tuple[float, dict] = (0, {})

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def ssh_args(self, tty: bool = False) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes" if not tty else "BatchMode=no", "-o", "ConnectTimeout=3",
                "-o", "ControlMaster=auto", "-o", f"ControlPath={Path.home()}/.ssh/batsy-%r@%h",
                "-o", "ControlPersist=10m", "-o", "ServerAliveInterval=15"] + (["-t"] if tty else []) + [self.host]

    def run(self, command: str, timeout: float = 20, gui: bool = False) -> tuple[int, str]:
        """(exit code, output). 255 = the PC can't be reached."""
        try:
            p = subprocess.run(self.ssh_args() + [(GUI if gui else "") + command], capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL, errors="replace")
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        return p.returncode, (p.stdout + p.stderr).strip()

    def up(self) -> bool:
        return self.run("true", timeout=6)[0] == 0

    def status(self) -> str:
        code, out = self.run("echo $(uptime -p); awk '{print $1}' /proc/loadavg; nproc; "
                             "free -m | awk '/Mem/{print $3\"/\"$2}'; df -h / | awk 'NR==2{print $4}'; "
                             "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1")
        if code != 0:
            return "The PC isn't answering. It's probably off or asleep. Say wake up the PC."
        lines = out.splitlines() + [""] * 6
        up, load, cores, mem, free, temp = lines[:6]
        busy = min(100, round(float(load or 0) / max(int(cores or 1), 1) * 100))
        used, total = (int(x) for x in mem.split("/")) if "/" in mem else (0, 1)
        say = f"The PC is on, {up.replace('up ', 'up for ')}. CPU {busy} percent busy, memory {round(used / total * 100)} " \
              f"percent used, {free.replace('G', ' gigabytes')} free on the disk"
        return say + (f", {int(temp) // 1000} degrees." if temp.isdigit() else ".")

    def wake(self) -> bool:
        """Wake-on-LAN magic packet on the Ethernet link."""
        if not self.mac:
            return False
        mac = bytes.fromhex(self.mac.replace(":", ""))
        packet = b"\xff" * 6 + mac * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for addr in {self.broadcast or "255.255.255.255", "255.255.255.255"}:
                for port in (9, 7):
                    s.sendto(packet, (addr, port))
        return True

    def apps(self) -> dict[str, str]:
        """App name -> desktop file id, read from the PC (cached 10 min)."""
        if time.time() - self._apps[0] < 600 and self._apps[1]:
            return self._apps[1]
        code, out = self.run("for f in /usr/share/applications/*.desktop ~/.local/share/applications/*.desktop "
                             "/var/lib/snapd/desktop/applications/*.desktop; do [ -f \"$f\" ] || continue; "
                             "grep -q '^NoDisplay=true' \"$f\" && continue; "
                             "n=$(grep -m1 '^Name=' \"$f\" | cut -d= -f2-); echo \"$n|$(basename \"$f\" .desktop)\"; done")
        apps = {}
        for line in out.splitlines() if code == 0 else []:
            name, _, ident = line.partition("|")
            if name and ident:
                apps.setdefault(name, ident)
        self._apps = (time.time(), apps)
        return apps

    def find_app(self, said: str) -> tuple[str, str] | None:
        from rapidfuzz import fuzz, process

        apps = self.apps()
        if not apps:
            return None
        said = APP_WORDS.get(said, said)
        best = process.extractOne(said, list(apps), scorer=fuzz.WRatio)
        return (best[0], apps[best[0]]) if best and best[1] >= 85 else None

    def open(self, what: str) -> str:
        what = what.strip(" .")
        url = SITES.get(what)
        if not url and re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", what):
            url = what if what.startswith("http") else "https://" + what
        app = None if url else self.find_app(what)
        if app is None and not url:
            url = "https://www.google.com/search?q=" + "+".join(what.split())  # not an app: search for it
        if app:
            code, out = self.run(f"(setsid gtk-launch {shlex.quote(app[1])} >/dev/null 2>&1 &)", gui=True)
            return f"Opened {app[0]} on the PC." if code == 0 else self._why(code, out)
        code, out = self.run(f"(setsid xdg-open {shlex.quote(url)} >/dev/null 2>&1 &)", gui=True)
        return f"Opened {what} on the PC." if code == 0 else self._why(code, out)

    def close(self, what: str) -> str:
        app = self.find_app(what.strip(" ."))
        name = app[1].split(".")[-1].split("_")[0].lower() if app else what.split()[0].lower()
        name = {"browser": "brave"}.get(name, name)
        code, out = self.run(f"pkill -i -f -- {shlex.quote(name)}")
        if code == 255:
            return self._why(code, out)
        return f"Closed {app[0] if app else what} on the PC." if code == 0 else f"{what.capitalize()} isn't open on the PC."

    def _why(self, code: int, out: str) -> str:
        if code in (255, 124):
            return "The PC isn't answering. It's probably off or asleep."
        if "password is required" in out or "a terminal is required" in out:
            return "The PC needs its password for that. Say set up the PC once, and I won't need it again."
        return f"That didn't work on the PC: {out.splitlines()[-1][:120] if out else 'no reason given'}."

    def power(self, action: str) -> str:
        code, out = self.run(f"sudo -n /usr/bin/systemctl {action}", timeout=8)
        if code in (0, 255) and action in ("poweroff", "reboot", "suspend"):  # the link drops as it goes down
            return ""
        return self._why(code, out)


def _terminal(command: str, title: str) -> bool:
    from batsy.system import _terminal as term

    return term(command, title)


def handle(app, n: str) -> str | None:
    """Instant PC commands. None if n isn't about the PC (or should go to the AI)."""
    if not ON_PC.search(n):
        return None
    pc = getattr(app, "pc", None) or Remote(app.cfg)
    app.pc = pc
    if not pc.configured:
        return "I don't know where the PC is. Add it under [pc] in the config."
    pre = r"^(?:please |can you |could you )?"

    if re.match(rf"{pre}(?:set ?up|connect|pair)(?: with)? {PC}(?: for (?:you|batsy|voice))?$", n):
        rules = shlex.quote(SUDO_RULES)
        script = ("echo 'Type the PC password when asked (only this once).'; "
                  f"echo {rules} | sudo tee /etc/sudoers.d/batsy >/dev/null && sudo chmod 440 /etc/sudoers.d/batsy && "
                  "sudo visudo -cf /etc/sudoers.d/batsy && "
                  "(con=$(nmcli -t -f NAME,DEVICE con show --active | grep -m1 ':enp' | cut -d: -f1); "
                  "sudo nmcli con modify \"$con\" 802-3-ethernet.wake-on-lan magic 2>/dev/null; "
                  "sudo /usr/sbin/ethtool -s enp2s0 wol g 2>/dev/null); "
                  "echo; echo 'Done. Batsy can now shut down, restart, update and wake the PC. You can close this.'")
        ok = _terminal(" ".join(shlex.quote(a) for a in pc.ssh_args(tty=True)) + " " + shlex.quote(script), "Set up the PC")
        return "I've opened a terminal on the laptop. Type the PC's password there, just this once." if ok else \
            "I couldn't open a terminal."

    if re.match(rf"{pre}(?:is {PC} (?:on|up|awake|running|alive)|(?:how'?s|how is|check(?: on)?) {PC}(?: doing)?|"
                rf"{PC} status|status of {PC})$", n):
        return pc.status()

    if re.match(rf"{pre}(?:turn on|switch on|power on|wake(?: up)?|boot(?: up)?|start(?: up)?) {PC}(?: up)?$|"
                rf"{pre}wake {PC} up$", n):
        if pc.up():
            return "The PC is already on."
        if not pc.wake():
            return "I don't have the PC's network address to wake it."
        for _ in range(12):
            time.sleep(5)
            if pc.up():
                return "The PC is awake."
        return ("I sent the wake-up signal, but the PC hasn't answered. Wake-on-LAN may be off in its BIOS, or it "
                "needs set up the PC first.")

    power = re.match(rf"{pre}(?:(?P<off>shut ?down|turn off|switch off|power off)|(?P<re>restart|reboot)|"
                     rf"(?P<sleep>sleep|suspend|put)) {PC}(?: to sleep)?(?: now)?$", n)
    if power:
        action = "poweroff" if power["off"] else "reboot" if power["re"] else "suspend"
        verb = {"poweroff": "Shut down", "reboot": "Restart", "suspend": "Put to sleep"}[action]
        if action != "suspend" and not app._confirm_now(f"{verb} the PC now?"):
            return "Okay, I won't."
        said = pc.power(action)
        return said or {"poweroff": "Shutting down the PC.", "reboot": "Restarting the PC.",
                        "suspend": "The PC is going to sleep."}[action]

    if re.match(rf"{pre}lock {PC}(?: screen)?$", n):
        code, out = pc.run("loginctl lock-sessions")
        return "Locked the PC." if code == 0 else pc._why(code, out)

    vol = re.match(rf"{pre}(?:(?:set |turn |put )?(?:the )?{PC} volume|(?:set |turn |put )?(?:the )?volume (?:on|of) {PC})"
                   r"(?: to)? (?P<v>\d+|up|down|max|full|half)(?: percent)?$|"
                   rf"{pre}(?:turn (?P<ud>up|down)|raise|lower) (?:the )?volume (?:on|of) {PC}$|"
                   rf"{pre}(?P<m>mute|unmute) {PC}$", n)
    if vol:
        v = vol["v"] or vol["ud"] or ("down" if n.startswith(("lower", "please lower")) else
                                      "up" if "raise" in n else None)
        if vol["m"]:
            arg = "set-mute @DEFAULT_AUDIO_SINK@ " + ("1" if vol["m"] == "mute" else "0")
        else:
            level = {"up": "10%+", "down": "10%-", "max": "1.0", "full": "1.0", "half": "0.5"}.get(v)
            arg = f"set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ {level or str(min(int(v), 100)) + '%'}"
        code, out = pc.run("wpctl " + arg + "; wpctl get-volume @DEFAULT_AUDIO_SINK@", gui=True)
        if code != 0:
            return pc._why(code, out)
        if vol["m"]:
            return "Muted the PC." if vol["m"] == "mute" else "Unmuted the PC."
        m = re.search(r"([\d.]+)", out)
        return f"PC volume {round(float(m.group(1)) * 100)} percent." if m else "Done."

    media = re.match(rf"{pre}(?P<a>pause|play|resume|stop|next|skip|previous)(?: the)?(?: music| video| song| track)?"
                     rf"(?: on {PC})$", n)
    if media:
        method = {"pause": "Pause", "stop": "Pause", "play": "Play", "resume": "Play", "next": "Next", "skip": "Next",
                  "previous": "Previous"}[media["a"]]
        code, out = pc.run("for p in $(busctl --user list --no-legend | awk '{print $1}' | grep '^org.mpris.MediaPlayer2'); "
                           f"do busctl --user call $p /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player {method}; "
                           "echo $p; done", gui=True)
        if code != 0:
            return pc._why(code, out)
        return "Done." if out else "Nothing's playing on the PC."

    if re.match(rf"{pre}(?:open|launch|start|bring up)(?: up)? (?:a |the )?(?:terminal|console|shell) (?:on|for|to|into) {PC}$|"
                rf"{pre}(?:ssh|log ?in|connect) (?:in)?to {PC}$", n):
        ok = _terminal(" ".join(shlex.quote(a) for a in pc.ssh_args(tty=True)), "PC")
        return "Opened a terminal on the PC." if ok else "I couldn't open a terminal."

    if re.match(rf"{pre}(?:update|upgrade) {PC}$|{pre}(?:install |run |do )?(?:the )?updates? (?:on|for) {PC}$", n):
        script = ("echo 'Updating the PC...'; sudo apt-get update && sudo apt-get -y full-upgrade; "
                  "echo; echo 'All done. You can close this window.'")
        ok = _terminal(" ".join(shlex.quote(a) for a in pc.ssh_args(tty=True)) + " " + shlex.quote(script), "PC update")
        return "I've opened the PC's update in a terminal here, so you can watch it." if ok else "I couldn't open a terminal."

    run = re.match(rf"{pre}(?:run|type|execute) (?P<cmd>.+?) (?:on|in) {PC}$", n)
    if run:
        from batsy.system import _spoken_command

        cmd = _spoken_command(run["cmd"])
        why = safety.danger_reason(cmd)
        if why and not app._confirm_now(f"On the PC, that will {why}: {cmd}. Run it?"):
            return "Okay, I won't."
        code, out = pc.run(cmd, timeout=30, gui=True)
        if code in (255, 124):
            return pc._why(code, out)
        last = out.splitlines()[-1][:160] if out else ""
        return ("Done." if code == 0 else f"It failed with exit code {code}.") + (f" {last}" if last and len(out) < 200 else "")

    m = re.match(rf"{pre}(?:close|quit|kill|exit) (?P<what>.+?) (?:on|in) {PC}$", n)
    if m:
        return pc.close(m["what"])
    m = re.match(rf"{pre}(?:open|launch|start|show|put on|go to|pull up) (?P<what>.+?) (?:on|in) {PC}$", n)
    if m:
        return pc.open(m["what"])
    return None  # something else about the PC: the AI handles it, with the run_on_pc tool
