"""How long the user has been away from keyboard and mouse (ms), or None if we can't tell."""
import re
import shutil
import subprocess


def idle_ms() -> int | None:
    # GNOME (Wayland or X11)
    if shutil.which("gdbus"):
        try:
            out = subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                 "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                 "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
                capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"(\d+)", out)
            if m:
                return int(m.group(1))
        except (subprocess.SubprocessError, OSError):
            pass
    # X11 fallback
    if shutil.which("xprintidle"):
        try:
            return int(subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=3).stdout.strip())
        except (ValueError, subprocess.SubprocessError, OSError):
            pass
    return None
