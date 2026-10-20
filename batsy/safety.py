"""Spot shell commands that deserve a spoken 'are you sure?' before running."""
import re

_DANGEROUS = [
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+", "delete files recursively or forcibly"),
    (r"\bmkfs(\.\w+)?\b", "format a filesystem"),
    (r"\bdd\b.*\bof=", "write raw data to a device or file"),
    (r"\b(shutdown|poweroff|reboot|halt)\b", "power off or restart the laptop"),
    (r"\bsystemctl\s+(poweroff|reboot|suspend|hibernate|halt)\b", "change the power state"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "stop or disable a system service"),
    (r"\b(dnf|yum|rpm)\s+.*\b(remove|erase|autoremove)\b", "uninstall packages"),
    (r"\bflatpak\s+(uninstall|remove)\b", "uninstall apps"),
    (r">\s*/dev/(sd|nvme|mmcblk)", "overwrite a disk"),
    (r"\b(wipefs|fdisk|parted|sgdisk|cryptsetup)\b", "change disk partitions"),
    (r"\bchmod\s+-R\b|\bchown\s+-R\b", "change permissions recursively"),
    (r":\(\)\s*\{", "run a fork bomb"),
    (r"\bgit\s+(push\s+.*--force|reset\s+--hard|clean\s+-[a-z]*f)", "discard or overwrite git history"),
    (r"\bkill(all)?\s+-9\b|\bpkill\b", "force-kill processes"),
    (r"\buserdel\b|\bpasswd\b", "change user accounts"),
]


def danger_reason(command: str) -> str | None:
    for pattern, reason in _DANGEROUS:
        if re.search(pattern, command):
            return reason
    return None


def is_yes(answer: str) -> bool:
    words = re.findall(r"[a-z']+", answer.lower())
    if any(w in ("no", "nope", "don't", "dont", "cancel", "stop", "negative", "abort") for w in words):
        return False
    return any(w in ("yes", "yeah", "yep", "sure", "confirm", "confirmed", "do", "go", "proceed", "affirmative", "ok", "okay") for w in words)
