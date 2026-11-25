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
