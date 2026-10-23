"""Built-in laptop actions that must never be improvised: a terminal running a command, the system update, and
restarting or shutting down. Each one is visible (a terminal you can watch) or confirmed by voice first.

handle(app, n) returns what to say, or None if n isn't one of these.
"""
import re
import shlex
import shutil
import subprocess

from batsy import safety

# What you say -> the command to run ("type in Claude" means start Claude Code).
SPOKEN = {"claude": "claude", "claude code": "claude", "python": "python3", "python three": "python3",
          "top": "top", "h top": "htop", "htop": "htop", "batsy": "batsy chat", "git status": "git status",
          "neofetch": "neofetch", "fastfetch": "fastfetch", "update": "sudo dnf upgrade --refresh"}

TERMINAL = re.compile(r"^(?:please )?(?:open|launch|start|bring up)(?: up)? (?:a |the |my |new )?(?:terminal|console|shell)"
                      r"(?: window)?(?:(?: and| then)(?: then)? (?:type|run|start|launch|execute|enter)(?: in)? (?P<cmd>.+))?$")
RUN_IN = re.compile(r"^(?:please )?(?:run|type|start|launch) (?P<cmd>.+?) in (?:a |the |new )?(?:terminal|console|shell)$")
UPDATE = re.compile(r"^(?:please )?(?:run |do |install |check for |get )?(?:the |all |my )?(?:system |software |fedora |package )"
                    r"?updates?(?: (?:on )?(?:the |my )?(?:system|laptop|computer|fedora|packages))?$|"
                    r"^(?:please )?(?:update|upgrade) (?:the |my )?(?:system|laptop|computer|fedora|packages|os)"
                    r"(?: packages)?$")
REBOOT = re.compile(r"^(?:please )?(?:restart|reboot) (?:the |my )?(?:whole )?(?:laptop|computer|system|pc|machine)(?: now)?$")
POWEROFF = re.compile(r"^(?:please )?(?:shut ?down|power off|turn off|switch off) (?:the |my )?(?:whole )?"
                      r"(?:laptop|computer|system|pc|machine)(?: now)?$")


def _terminal(command: str, title: str = "") -> bool:
    """Open a new terminal window running `command`, then leave you a shell in it. True if one opened."""
    inner = f"{command}; echo; exec bash" if command else "exec bash"
    if shutil.which("ptyxis"):  # -x works from Batsy's background service (it hands off to the running Ptyxis)
        argv = ["ptyxis", "-x", f"bash -lc {shlex.quote(inner)}"] + (["-T", title] if title else [])
    elif shutil.which("gnome-terminal"):
        argv = ["gnome-terminal", "--", "bash", "-lc", inner]
    elif shutil.which("kgx"):
        argv = ["kgx", "--", "bash", "-lc", inner]
    elif shutil.which("konsole"):
        argv = ["konsole", "-e", "bash", "-lc", inner]
    else:
        return False
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def _spoken_command(said: str) -> str:
    said = re.sub(r"[.!?,]+$", "", said.strip().lower())
    said = re.sub(r"^(the command |the word |command )", "", said)
    return SPOKEN.get(said, said)


def _updating() -> bool:
    return subprocess.run(["pgrep", "-x", "dnf|dnf5|rpm|packagekitd"], capture_output=True).returncode == 0 and \
        subprocess.run(["pgrep", "-f", "dnf5? (upgrade|update|install)"], capture_output=True).returncode == 0


def handle(app, n: str) -> str | None:
    m = TERMINAL.match(n) or RUN_IN.match(n)
    if m:
        cmd = _spoken_command(m.group("cmd") or "")
        if cmd:
            first = cmd.split()[0] if cmd.split()[0] != "sudo" else cmd.split()[1]
            if not shutil.which(first):
                return f"I couldn't find a program called {first} on this laptop."
            why = safety.danger_reason(cmd)
            if why and not app._confirm_now(f"That will {why}: {cmd}. Run it?"):
                return "Okay, I won't."
        if not _terminal(cmd):
            return "I couldn't find a terminal app."
        return f"Opened a terminal running {cmd}." if cmd else "Opened a terminal."

    if UPDATE.match(n):
        steps = []
        if shutil.which("dnf"):
            steps.append("echo 'Updating system packages (type your password when asked)...'; sudo dnf upgrade --refresh -y")
        if shutil.which("flatpak"):
            steps.append("echo; echo 'Updating Flatpak apps...'; flatpak update -y")
        if not steps:
            return "I couldn't find dnf or flatpak to update with."
        steps.append("echo; echo 'All done. You can close this window.'")
        if not _terminal("; ".join(steps), "System update"):
            return "I couldn't find a terminal app to run the update in."
        return "I've opened the system update in a terminal. Type your password there, and you can watch it go."

    reboot, poweroff = REBOOT.match(n), POWEROFF.match(n)
    if reboot or poweroff:
        verb = "restart" if reboot else "shut down"
        warn = " A system update is still running, and this would interrupt it." if _updating() else ""
        if not app._confirm_now(f"{verb.capitalize()} the laptop now?{warn}"):
            return "Okay, I won't."
        app._reply(f"{'Restarting' if reboot else 'Shutting down'} in five seconds.")
        app.speaker.wait_idle(10)
        subprocess.Popen(["bash", "-c", f"sleep 4; systemctl {'reboot' if reboot else 'poweroff'}"],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ""
    return None
