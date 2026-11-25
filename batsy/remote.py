"""Running the laptop without being at it: everything that used to need you at the keyboard, done once, from a
terminal (`batsy remote setup`), then managed from the website's Remote panel (batsy/web.py).

What setup does, each step skipped if it's already done:
  1. Tailscale SSH + operator: a terminal from any of your devices (`tailscale ssh workstation`), and Batsy may
     collect files sent with Taildrop (no more files stuck in Tailscale's holding area).
  2. Lingering: your user's services keep running with nobody logged in.
  3. Claude Remote Control: a Claude Code session on this laptop that you drive from claude.ai/code or the Claude
     app on your phone, kept running by systemd and restarted if it stops.
  4. (asks first) Automatic login, locked straight away: after a reboot the desktop comes back by itself, so Batsy
     (voice, screen, everything) is running again without anyone at the laptop. The screen stays locked.
"""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from batsy.config import DATA_DIR, HOME

RC_UNIT = "batsy-claude-rc.service"
UNIT_DIR = HOME / ".config" / "systemd" / "user"
LOG = DATA_DIR / "batsy.log"
GDM_CONF = Path("/etc/gdm/custom.conf")
LOCK_AUTOSTART = HOME / ".config" / "autostart" / "batsy-lock-on-login.desktop"


def _run(argv: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{argv[0]} isn't installed"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


# --- what's set up ------------------------------------------------------------------------------------------------
def _ts_prefs() -> dict:
    code, out = _run(["tailscale", "debug", "prefs"])
    try:
        return json.loads(out) if code == 0 else {}
    except ValueError:
        return {}


def linger() -> bool:
    return _run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"])[1].strip() == "Linger=yes"


def autologin() -> bool:
    try:
        return bool(re.search(r"^\s*AutomaticLoginEnable\s*=\s*true", GDM_CONF.read_text(), re.I | re.M))
    except OSError:
        return False


def rc_state() -> dict:
    """The Claude Remote Control service: running?, and the session link it printed (if any)."""
    active = _run(["systemctl", "--user", "is-active", RC_UNIT])[1].strip()
    link = ""
    code, out = _run(["journalctl", "--user", "-u", RC_UNIT, "-n", "200", "--no-pager", "-o", "cat"])
    if code == 0:
        links = re.findall(r"https://claude\.ai/code\S*", out)
        link = links[-1].rstrip(".,)") if links else ""
    return {"installed": (UNIT_DIR / RC_UNIT).exists(), "state": active or "unknown", "link": link}


def status() -> dict:
    p = _ts_prefs()
    return {"tailscale_ssh": bool(p.get("RunSSH")), "operator": p.get("OperatorUser", "") == getpass.getuser(),
            "linger": linger(), "autologin": autologin(), "claude_rc": rc_state(),
            "claude": bool(shutil.which("claude"))}


# --- doing it -------------------------------------------------------------------------------------------------------
def rc_unit_text() -> str:
    claude = shutil.which("claude") or str(HOME / ".local/bin/claude")
    name = os.uname().nodename.split(".")[0]
    return f"""[Unit]
Description=Claude Code Remote Control for Batsy (drive this laptop's Claude from claude.ai/code or the Claude app)
After=network-online.target

[Service]
Type=simple
WorkingDirectory={HOME}
Environment=PATH={Path(claude).parent}:{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={claude} remote-control --name {name}-batsy --permission-mode acceptEdits
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
"""


def install_rc() -> str:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / RC_UNIT).write_text(rc_unit_text())
    _run(["systemctl", "--user", "daemon-reload"])
    code, out = _run(["systemctl", "--user", "enable", "--now", RC_UNIT])
    return "Claude Remote Control is running." if code == 0 else f"Couldn't start Claude Remote Control: {out[-200:]}"


def rc(action: str) -> str:
    if action not in ("start", "stop", "restart"):
        return "Unknown action."
    if not (UNIT_DIR / RC_UNIT).exists():
        return install_rc()
    code, out = _run(["systemctl", "--user", action, RC_UNIT])
    return f"Claude Remote Control: {action} done." if code == 0 else f"{action} failed: {out[-200:]}"


def log_tail(lines: int = 150) -> str:
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            text = f.read().decode(errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except OSError as e:
        return f"(no log: {e})"


LOG_FILES = {"batsy": "batsy.log", "crash": "crash.log", "overlay": "overlay.log", "dock": "dock.log",
             "sidetab": "sidetab.log", "printer": "printstream.log"}
SOURCES = ("failures", *LOG_FILES, "service", "system", "kernel", "boot", "pc")

# Run on the PC (as Batsy's PC user, over its SSH connection): its disks, what's mounted, and disk/kernel errors
PC_DIAG = r"""echo '== disks'; lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,STATE 2>/dev/null
echo; echo '== mounts of /batman and /home'; findmnt -T /batman 2>&1; findmnt -T /home 2>&1
echo; echo '== space'; df -h / /batman /home 2>&1 | sort -u
echo; echo '== can /batman be read?'; ls -la /batman 2>&1 | head -15; timeout 10 stat /batman 2>&1 | head -4
echo; echo '== kernel errors and warnings (disk, USB, filesystem)'; (journalctl -k -p warning -n 120 --no-pager 2>/dev/null   || dmesg 2>/dev/null | tail -120 || tail -120 /var/log/kern.log 2>/dev/null) | grep -iE 'error|fail|i/o|ata|usb|sd[a-z]|nvme|ext4|ntfs|exfat|btrfs|xfs|remount|read-only|reset' | tail -80
echo; echo '== uptime'; uptime"""


def _tail_file(path: Path, lines: int, grep: str = "") -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4_000_000))
            text = f.read().decode(errors="replace").splitlines()
    except OSError as e:
        return f"({path.name}: {e.strerror or e})"
    if grep:
        rx = re.compile(grep, re.I)
        text = [ln for ln in text if rx.search(ln)]
    return "\n".join(text[-lines:]) or f"({path.name} is empty)"


def _journal(args: list[str], lines: int, grep: str = "") -> str:
    argv = ["journalctl", "--no-pager", "-n", str(lines), *args] + (["-g", grep] if grep else [])
    code, out = _run(argv, timeout=30)
    return out or "(nothing)"


def logs(source: str = "failures", lines: int = 200, grep: str = "") -> str:
    """Any of Batsy's logs or the system's, as text. source: failures (everything that went wrong lately, in one
    place), batsy, crash, overlay, dock, sidetab, printer, service (Batsy's systemd user units), system (the
    whole machine's journal, warnings and worse), kernel, boot (this boot's errors). grep: only matching lines."""
    lines = max(10, min(int(lines or 200), 5000))
    if source in LOG_FILES:
        return _tail_file(DATA_DIR / LOG_FILES[source], lines, grep)
    if source == "service":
        return _journal(["--user", "-u", "batsy*", "-u", RC_UNIT, "-o", "short-iso"], lines, grep)
    if source == "system":
        return _journal(["-p", "warning", "-o", "short-iso"], lines, grep)
    if source == "kernel":
        return _journal(["-k", "-p", "warning", "-o", "short-iso"], lines, grep)
    if source == "boot":
        return _journal(["-b", "-p", "err", "-o", "short-iso"], lines, grep)
    if source == "failures":
        return failures(lines)
    if source == "pc":  # the desktop PC's disks and kernel errors, over Batsy's SSH connection to it
        from batsy.config import Config
        from batsy.pc import Remote

        pc = Remote(Config.load())
        if not pc.configured:
            return "The PC isn't set up ([pc] host)."
        code, out = pc.run(PC_DIAG, timeout=60)
        return out if code != 255 else "The PC is off or can't be reached."
    return f"Unknown log {source!r}. Try one of: {', '.join(SOURCES)}"


def failures(lines: int = 200) -> str:
    """Everything that went wrong lately, in one place: Batsy's errors and tracebacks, its crash log, failed
    services, the system's errors, crashed programs, and what's using the machine right now."""
    parts = []

    def section(title: str, body: str) -> None:
        parts.append(f"===== {title} =====\n{body.strip() or '(none)'}")

    section("Batsy errors and warnings (batsy.log)",
            _tail_file(LOG, lines, r" (ERROR|WARNING|CRITICAL) |Traceback|Error:|Exception|^\s+File \""))
    section("Batsy crash log (crash.log)", _tail_file(DATA_DIR / "crash.log", 60))
    section("Overlay problems (overlay.log)", _tail_file(DATA_DIR / "overlay.log", 30, r"error|warn|fail|crash|Traceback"))
    section("Failed services", "\n".join(x for x in (
        _run(["systemctl", "--failed", "--no-legend", "--plain"])[1],
        _run(["systemctl", "--user", "--failed", "--no-legend", "--plain"])[1]) if x) or "(none)")
    section("Batsy's services (user journal, errors)", _journal(["--user", "-p", "warning", "-S", "-24h",
                                                                   "-o", "short-iso"], 60))
    section("System errors, last 24 h", _journal(["-p", "err", "-S", "-24h", "-o", "short-iso"], 80))
    code, out = _run(["coredumpctl", "list", "--since", "-7d", "--no-pager", "--no-legend"])
    section("Programs that crashed, last 7 days (coredumpctl)", out if code == 0 else "(none)")
    try:
        import psutil

        vm, du = psutil.virtual_memory(), psutil.disk_usage("/")
        top = sorted(psutil.process_iter(["name", "memory_percent", "cpu_percent"]),
                     key=lambda p: p.info.get("memory_percent") or 0, reverse=True)[:6]
        section("Right now", f"memory {vm.percent}% used, disk {du.percent}% used ({du.free // 2**30} GB free), "
                             f"load {', '.join(f'{x:.1f}' for x in psutil.getloadavg())}\n"
                + "\n".join(f"  {p.info['name']}: {p.info.get('memory_percent') or 0:.1f}% memory" for p in top))
    except Exception as e:
        section("Right now", str(e))
    return "\n\n".join(parts)


def doctor() -> str:
    code, out = _run([sys.executable, "-m", "batsy", "doctor"], timeout=120)
    return out[-6000:]


# --- the one-time setup (a terminal: it needs your password for sudo) -----------------------------------------------
def _ask(q: str) -> bool:
    try:
        return input(f"{q} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _sudo(argv: list[str]) -> bool:
    print("  $ sudo " + " ".join(argv))
    return subprocess.run(["sudo", *argv]).returncode == 0


def setup() -> None:
    user = getpass.getuser()
    st = status()
    print("Making this laptop fully remote.\n")

    print("1. Tailscale SSH and file collection")
    if st["tailscale_ssh"] and st["operator"]:
        print("   already on")
    elif shutil.which("tailscale"):
        _sudo(["tailscale", "set", "--ssh", f"--operator={user}"])
        print("   Terminal from anywhere: tailscale ssh " + os.uname().nodename.split(".")[0])
    else:
        print("   Tailscale isn't installed: sudo dnf install tailscale && sudo systemctl enable --now tailscaled")

    print("\n2. Keep your services running with nobody logged in")
    if st["linger"]:
        print("   already on")
    else:
        _sudo(["loginctl", "enable-linger", user])

    print("\n3. Claude Remote Control (drive this laptop's Claude from claude.ai/code or the Claude app)")
    if not st["claude"]:
        print("   Claude Code isn't installed: npm install -g @anthropic-ai/claude-code, then run claude to log in")
    else:
        print("   " + install_rc())
        print("   Open claude.ai/code (or the Claude app > Code) and pick the session named "
              f"{os.uname().nodename.split('.')[0]}-batsy.")

    print("\n4. Come back by itself after a reboot (automatic login, screen locked straight away)")
    if st["autologin"]:
        print("   already on")
    elif GDM_CONF.exists() and _ask("   Turn on automatic login? The screen locks a few seconds after it comes up, "
                                    "but someone at the laptop while it boots could see the desktop briefly."):
        text = GDM_CONF.read_text()
        text = re.sub(r"^\s*AutomaticLogin(Enable)?\s*=.*\n", "", text, flags=re.M)
        text = re.sub(r"^\[daemon\]\s*$", f"[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin={user}", text,
                      count=1, flags=re.M) if re.search(r"^\[daemon\]", text, re.M) else \
            text + f"\n[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin={user}\n"
        tmp = DATA_DIR / "gdm-custom.conf"
        tmp.write_text(text)
        if _sudo(["install", "-m", "644", str(tmp), str(GDM_CONF)]):
            LOCK_AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
            LOCK_AUTOSTART.write_text("[Desktop Entry]\nType=Application\nName=Lock after automatic login\n"
                                      "Exec=sh -c 'sleep 3; loginctl lock-session'\nX-GNOME-Autostart-enabled=true\n")
            print("   on: after a reboot you're logged in and the screen locks at once")
        tmp.unlink(missing_ok=True)
    else:
        print("   skipped")

    print("\nDone. Everything else is on the website: http://" + os.uname().nodename.split(".")[0] + ":8765/web")
