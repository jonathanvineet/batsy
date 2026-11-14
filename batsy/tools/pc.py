"""The desktop PC (Ubuntu, over SSH) for requests batsy/pc.py doesn't handle by itself."""
from batsy import safety
from batsy.pc import Remote
from batsy.tools import truncate


def register(reg):
    @reg.tool(
        "run_on_pc",
        "Run a bash command on the user's desktop PC (Ubuntu 26.04, user bob, i3-4130, 3.4 GB RAM, GNOME on "
        "Wayland), reached over SSH from the laptop. When the user says 'the PC' or 'the desktop' they mean this "
        "machine, never the laptop. Set gui=true for anything touching the PC's screen or sound (opening apps with "
        "gtk-launch or xdg-open, wpctl, notify-send, gsettings); GUI apps must be started in the background with "
        "`(setsid APP >/dev/null 2>&1 &)`. Root: only `sudo -n` works, and only for systemctl poweroff/reboot/suspend "
        "and apt-get update / -y full-upgrade; for anything else tell the user to say 'open a terminal on the PC'. "
        "Exit code 255 means the PC is off or unreachable.",
        {
            "command": {"type": "string", "description": "bash command line to run on the PC"},
            "gui": {"type": "boolean", "description": "run with the PC's desktop session environment"},
            "timeout": {"type": "integer", "description": "seconds before giving up (default 60)"},
        },
        ["command"],
    )
    def run_on_pc(ctx, command, gui=False, timeout=60):
        pc = Remote(ctx.cfg)
        if not pc.configured:
            return "No PC is configured ([pc] host in the config)."
        reason = safety.danger_reason(command)
        if reason and ctx.cfg.get("safety.confirm_dangerous"):
            if not ctx.confirm(f"On the PC, this will {reason}: {command[:120]}. Should I go ahead?"):
                return "The user declined. Command NOT run."
        code, out = pc.run(command, timeout=timeout, gui=gui)
        if code == 255:
            return "The PC is unreachable (off, asleep, or the cable is out)."
        return truncate(f"exit code {code}\n{out}")
