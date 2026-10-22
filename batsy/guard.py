"""Claude Code PreToolUse hook: Claude may run any command anywhere, but risky ones need your spoken "yes".

Claude Code runs this before every Bash call (see claude_code.SETTINGS) and passes the call as JSON on stdin.
Exit 0 lets it run; exit 2 blocks it and the message on stderr goes back to Claude, which then adapts.
The question is asked by the running Batsy (voice, or the `batsy chat` terminal) over its control socket.
"""
import json
import re
import socket
import sys

from batsy import safety
from batsy.config import SOCKET_PATH


def ask_user(question: str, timeout: float = 90) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(SOCKET_PATH))
            with s.makefile("rw") as f:
                f.write(json.dumps({"confirm_only": question}) + "\n")
                f.flush()
                return bool(json.loads(f.readline() or "{}").get("yes"))
    except (OSError, ValueError):
        return False  # nobody to ask: don't do it


def check(call: dict) -> str | None:
    """None = allowed, otherwise the reason it's blocked (told to Claude)."""
    if call.get("tool_name") != "Bash":
        return None
    command = str((call.get("tool_input") or {}).get("command", ""))
    if re.search(r"(^|[;&|]\s*)sudo\b", command):
        return ("sudo can't prompt for a password here. Use pkexec instead (it shows the user a GNOME password "
                "dialog), e.g. pkexec dnf install -y PACKAGE.")
    reason = safety.danger_reason(command)
    if reason is None:
        return None
    short = command if len(command) < 120 else command[:117] + "..."
    if ask_user(f"Claude wants to {reason}: {short}. Should I let it?"):
        return None
    return f"The user said no to this command ({reason}). Don't run it; find another way or explain why it's needed."


def main() -> None:
    try:
        call = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)
    why = check(call)
    if why:
        print(why, file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
