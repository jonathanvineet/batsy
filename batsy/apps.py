"""Instant "open X" / "close X" without any AI: installed apps, folders and websites."""
import configparser
import difflib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from batsy.config import HOME, WORKSPACE

APP_DIRS = [
    Path("/usr/share/applications"),
    HOME / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    HOME / ".local/share/flatpak/exports/share/applications",
]
FOLDERS = {
    "home": HOME, "my home": HOME, "home folder": HOME, "downloads": HOME / "Downloads",
    "documents": HOME / "Documents", "pictures": HOME / "Pictures", "photos": HOME / "Pictures",
    "music": HOME / "Music", "videos": HOME / "Videos", "desktop": HOME / "Desktop",
    "batsy folder": WORKSPACE, "workspace": WORKSPACE, "arduino folder": WORKSPACE / "arduino",
}
SITES = {
    "youtube": "https://youtube.com", "github": "https://github.com", "gmail": "https://mail.google.com",
    "google": "https://google.com", "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai",
    "whatsapp": "https://web.whatsapp.com", "whatsapp web": "https://web.whatsapp.com",
    "instagram": "https://instagram.com", "netflix": "https://netflix.com", "linkedin": "https://linkedin.com",
    "maps": "https://maps.google.com", "google maps": "https://maps.google.com", "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com", "calendar": "https://calendar.google.com", "x": "https://x.com",
    "twitter": "https://x.com", "reddit": "https://reddit.com", "amazon": "https://amazon.in",
    "stack overflow": "https://stackoverflow.com", "wikipedia": "https://wikipedia.org",
}
ALIASES = {
    "vs code": "code", "vscode": "code", "visual studio code": "code", "files": "nautilus", "file manager": "nautilus",
    "terminal": "terminal", "browser": "browser", "web browser": "browser", "settings": "gnome-control-center",
    "calculator": "calculator", "text editor": "text editor", "arduino": "arduino ide", "arduino ide": "arduino ide",
}


class App:
    def __init__(self, desktop_id: str, name: str, generic: str, keywords: str, exec_: str):
        self.id, self.name, self.generic, self.keywords = desktop_id, name, generic, keywords
        self.binary = Path(re.sub(r"%\w", "", exec_).split()[0]).name if exec_.split() else ""
        self.search = " ".join([name, generic, keywords, desktop_id.replace(".", " "), self.binary]).lower()


class AppIndex:
    def __init__(self, dirs=APP_DIRS):
        self.dirs, self.apps, self.loaded = dirs, [], 0.0

    def refresh(self) -> None:
        apps = {}
        for d in self.dirs:
            for f in sorted(Path(d).glob("*.desktop")) if Path(d).is_dir() else []:
                cp = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    cp.read(f, encoding="utf-8")
                    e = cp["Desktop Entry"]
                except (configparser.Error, KeyError, UnicodeDecodeError):
                    continue
                if e.get("NoDisplay", "false").lower() == "true" or e.get("Type", "Application") != "Application":
                    continue
                apps[f.stem] = App(f.stem, e.get("Name", f.stem), e.get("GenericName", ""),
                                   e.get("Keywords", "").replace(";", " "), e.get("Exec", ""))
        self.apps, self.loaded = list(apps.values()), time.time()

    def find(self, name: str) -> App | None:
        if time.time() - self.loaded > 600:
            self.refresh()
        q = ALIASES.get(name.lower().strip(), name.lower().strip())
        if q == "browser":
            default = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True,
                                     text=True).stdout.strip() if shutil.which("xdg-settings") else ""
            q = default.removesuffix(".desktop") or "firefox"
        if q == "terminal":
            q = next((t for t in ("org.gnome.Ptyxis", "org.gnome.Terminal", "org.gnome.Console", "konsole")
                      if any(a.id == t for a in self.apps)), "terminal")
        best, best_score = None, 0.0
        for a in self.apps:
            names = [a.name.lower(), a.id.lower(), a.id.split(".")[-1].lower(), a.binary.lower()]
            if q in names:
                return a
            score = max(difflib.SequenceMatcher(None, q, n).ratio() for n in names)
            if q in a.search:
                score = max(score, 0.8 if len(q) > 3 else 0.6)
            if score > best_score:
                best, best_score = a, score
        return best if best_score >= 0.75 else None


INDEX = AppIndex()


def _launch(argv: list[str]) -> bool:
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except FileNotFoundError:
        return False


def open_target(target: str) -> str | None:
    """Open a folder, website or app. Returns what to say, or None if it doesn't know the target."""
    t = re.sub(r"^(the|my|up)\s+", "", target.strip().lower()).removesuffix(" app").strip()
    if t in FOLDERS:
        return f"Opening {t}." if _launch(["xdg-open", str(FOLDERS[t])]) else None
    if t in SITES or re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?", t.replace(" dot ", ".")):
        url = SITES.get(t) or "https://" + t.replace(" dot ", ".")
        return f"Opening {t}." if _launch(["xdg-open", url]) else None
    app = INDEX.find(t)
    if app:
        return f"Opening {app.name}." if _launch(["gtk-launch", app.id]) else None
    return None


def close_app(target: str) -> str | None:
    app = INDEX.find(re.sub(r"^(the|my)\s+", "", target.strip().lower()))
    if not app or not app.binary:
        return None
    subprocess.run(["pkill", "-x", app.binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Closed {app.name}."
