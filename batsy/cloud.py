"""Your Batman drive (/batman on the PC, 458 GB) as cloud storage for the phone app.

Batsy signs in to the PC as `batman` (its own SSH key, added once by "set up the batman drive") and lists, previews,
streams and uploads files there. Nothing is copied to the laptop except small thumbnails (cloud_thumbs/), which are
made once per file version. The phone keeps listings, thumbnails and opened files in its own cache.
"""
import hashlib
import io
import json
import logging
import shlex
import subprocess
import threading
from pathlib import Path

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.cloud")
ROOT = "/batman"
THUMBS = DATA_DIR / "cloud_thumbs"
IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff"}
SKIP = {"lost+found", "snap", ".ssh", ".cache", ".local", ".config", ".bash_history", ".bashrc", ".profile", ".bash_logout",
        ".sudo_as_admin_successful"}

# Runs on the PC: lists a folder, the newest files anywhere, or a name search, as JSON
LISTER = r'''
import json, os, sys
root, mode, arg = sys.argv[1], sys.argv[2], sys.argv[3]
skip = set(sys.argv[4].split("/"))
def entry(p, st):
    return {"path": p, "name": os.path.basename(p), "dir": os.path.isdir(p), "size": st.st_size, "t": int(st.st_mtime)}
def walk():
    n = 0
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in skip and not x.startswith(".")]
        for f in files:
            if f.startswith(".") or f.endswith(".part"):
                continue
            n += 1
            if n > 50000:
                return
            p = os.path.join(d, f)
            try:
                yield p, os.stat(p)
            except OSError:
                pass
out = {}
if mode == "list":
    items = []
    for name in os.listdir(arg):
        if name.startswith(".") or name in skip or name.endswith(".part"):
            continue
        p = os.path.join(arg, name)
        try:
            items.append(entry(p, os.stat(p)))
        except OSError:
            pass
    out["items"] = items
elif mode == "recent":
    out["items"] = [entry(p, st) for p, st in sorted(walk(), key=lambda x: -x[1].st_mtime)[:int(arg)]]
elif mode == "search":
    q = arg.lower()
    out["items"] = [entry(p, st) for p, st in walk() if q in os.path.basename(p).lower()][:200]
s = os.statvfs(root)
out["free"], out["total"] = s.f_bavail * s.f_frsize, s.f_blocks * s.f_frsize
print(json.dumps(out))
'''


HOMES = {"batman": "/batman"}  # PC accounts whose home isn't /home/<name>


class Drive:
    """One person's drive: their home on the PC (Jonathan -> batman -> /batman, Karan -> nightwing, Rehaan ->
    superman), signed in to as that account with Batsy's SSH key."""

    def __init__(self, cfg, person: str = "Jonathan"):
        pc = cfg.get("pc") or {}
        host = pc.get("host", "").split("@")[-1]
        self.person = person
        self.user = (cfg.get("files.pc_users") or {}).get(person, "batman" if person == "Jonathan" else "")
        files = cfg.get("files") or {}  # (cfg.get("files.pc_homes") raises when it isn't set)
        self.root = (files.get("pc_homes") or {}).get(self.user) or HOMES.get(self.user) or f"/home/{self.user}"
        self.host = f"{self.user}@{host}" if host and self.user else ""
        self.lock = threading.Lock()
        THUMBS.mkdir(parents=True, exist_ok=True)

    def ssh(self) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={Path.home()}/.ssh/batsy-%r@%h", "-o", "ControlPersist=10m", self.host]

    def safe(self, path: str) -> str:
        """Only paths inside the drive (no ../ tricks, no hidden folders like .ssh)."""
        import posixpath

        root = self.root
        p = posixpath.normpath(posixpath.join(root, (path or "").removeprefix(root).lstrip("/")))
        if p != root and not p.startswith(root + "/"):
            raise ValueError("outside the drive")
        if any(part in SKIP or part.startswith(".") for part in p[len(root):].split("/") if part):
            raise ValueError("not shared")
        return p

    def _run(self, cmd: str, timeout: float = 20, stdin=None) -> subprocess.CompletedProcess:
        return subprocess.run(self.ssh() + [cmd], capture_output=True, timeout=timeout,
                              stdin=stdin if stdin is not None else subprocess.DEVNULL)

    def query(self, mode: str, arg: str) -> dict:
        if not self.host:
            return {"error": "no PC set up"}
        if mode == "list":
            arg = self.safe(arg)
        cmd = f"python3 -c {shlex.quote(LISTER)} {shlex.quote(self.root)} {mode} {shlex.quote(arg)} {shlex.quote('/'.join(SKIP))}"
        try:
            r = self._run(cmd, timeout=30)
        except subprocess.TimeoutExpired:
            return {"error": "The PC took too long."}
        if r.returncode == 255:
            err = r.stderr.decode(errors="replace")
            return {"error": f"Batsy can't sign in to {self.person}'s drive on the PC yet (its key isn't set up)."
                    if "denied" in err else "The PC is off or asleep.", "offline": "denied" not in err,
                    "setup": "denied" in err}
        if r.returncode:
            return {"error": r.stderr.decode(errors="replace")[-200:] or "couldn't read it"}
        out = json.loads(r.stdout)
        if mode == "list":
            out["path"] = arg
        return out

    def stat(self, path: str) -> tuple[int, int]:
        """(size, modified). Says why it failed: ConnectionError (the PC is off, or Batsy can't sign in to the
        drive), PermissionError, or FileNotFoundError (really not there)."""
        path = self.safe(path)
        if not self.host:
            raise ConnectionError("The PC isn't set up.")
        try:
            r = self._run(f"stat -c '%s %Y' -- {shlex.quote(path)}", timeout=10)
        except subprocess.TimeoutExpired:
            raise ConnectionError("The PC took too long to answer.")
        if r.returncode == 255:
            err = r.stderr.decode(errors="replace")
            raise ConnectionError(f"Batsy can't sign in to {self.person}'s drive on the PC (its key isn't set up: "
                                  f"say set up the batman drive)." if "denied" in err.lower() else
                                  "The PC is off or asleep. Say wake up the PC.")
        if r.returncode:
            err = r.stderr.decode(errors="replace")
            if "ermission denied" in err:
                raise PermissionError(f"Batsy isn't allowed to read {Path(path).name} on the PC.")
            raise FileNotFoundError(path)
        size, t = r.stdout.decode().split()
        return int(size), int(t)

    def stream(self, path: str, out) -> None:
        """Copy the file to `out` (a socket file), as it comes off the PC."""
        p = subprocess.Popen(self.ssh() + [f"cat -- {shlex.quote(self.safe(path))}"], stdout=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while chunk := p.stdout.read(256 * 1024):
                out.write(chunk)
        finally:
            p.kill()
            p.wait()

    def upload(self, folder: str, name: str, body, length: int) -> str:
        """Save an upload into `folder`; a name already there gets " (2)". Returns where it went."""
        folder = self.safe(folder)
        name = Path(name).name.strip() or "upload"
        if name.startswith("."):
            name = "_" + name
        script = ('d="$1"; n="$2"; b="${n%.*}"; e="${n##*.}"; [ "$b" = "$n" ] && e="" || e=".$e"; f="$d/$n"; i=2; '
                  'while [ -e "$f" ]; do f="$d/$b ($i)$e"; i=$((i+1)); done; cat > "$f.part" && mv "$f.part" "$f" && echo "$f"')
        p = subprocess.Popen(self.ssh() + [f"bash -c {shlex.quote(script)} _ {shlex.quote(folder)} {shlex.quote(name)}"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        left = length
        while left > 0:
            chunk = body.read(min(256 * 1024, left))
            if not chunk:
                break
            p.stdin.write(chunk)
            left -= len(chunk)
        p.stdin.close()
        out, err = p.stdout.read().decode().strip(), p.stderr.read().decode()
        if p.wait() or left:
            raise OSError(err[-200:] or "the upload stopped")
        log.info("uploaded %s to %s's drive", out, self.person)
        return out

    def mkdir(self, folder: str, name: str) -> str:
        p = self.safe(f"{self.safe(folder)}/{Path(name).name.strip()}")
        if self._run(f"mkdir -p -- {shlex.quote(p)}").returncode:
            raise OSError("couldn't make the folder")
        return p

    def thumb(self, path: str, t: int, size: int, px: int = 360) -> bytes | None:
        """A JPEG preview of a picture or a PDF's first page, at most px across (360: a tile; 1600: to view on a
        phone without fetching a 3 MB original). Made once per version, kept on the laptop."""
        path = self.safe(path)
        ext = Path(path).suffix.lower()
        key = THUMBS / (hashlib.sha1(f"{path}|{t}|{size}{'|' + str(px) if px != 360 else ''}".encode()).hexdigest() + ".jpg")
        if key.exists():
            return key.read_bytes()
        try:
            if ext == ".pdf":
                r = self._run(f"pdftoppm -f 1 -l 1 -scale-to {px} -jpeg -- {shlex.quote(path)}", timeout=30)
                raw = r.stdout if not r.returncode else b""
            elif ext in IMAGES and size < 40e6:
                raw = self._run(f"cat -- {shlex.quote(path)}", timeout=60).stdout
            else:
                return None
            from PIL import Image, ImageOps

            if ext == ".heic":
                try:
                    import pillow_heif

                    pillow_heif.register_heif_opener()
                except ImportError:
                    return None
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
            im.thumbnail((px, px))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=78 if px <= 360 else 82, progressive=px > 360)
            key.write_bytes(buf.getvalue())
            return buf.getvalue()
        except Exception as e:
            log.info("no preview for %s: %s", path, e)
            return None


SETUP = """
home=$(getent passwd {user} | cut -d: -f6)
install -d -m 700 -o {user} -g {user} "$home/.ssh"
touch "$home/.ssh/authorized_keys"
grep -qxF {key} "$home/.ssh/authorized_keys" || echo {key} >> "$home/.ssh/authorized_keys"
chown {user}:{user} "$home/.ssh/authorized_keys"; chmod 600 "$home/.ssh/authorized_keys"
echo "  {user}: ready"
"""


def setup_command(cfg, people: tuple = ("Jonathan",)) -> str:
    """The one-time terminal command: lets the laptop's SSH key sign in as each person's PC account (asks once for
    the PC's password)."""
    pub = (Path.home() / ".ssh/id_ed25519.pub").read_text().strip()
    host = (cfg.get("pc") or {}).get("host", "")
    script = "".join(SETUP.format(user=shlex.quote(Drive(cfg, p).user), key=shlex.quote(pub)) for p in people
                     if Drive(cfg, p).user) + 'echo; echo "Done: Batsy can open their drives now. You can close this window."'
    return f"ssh -t {shlex.quote(host)} {shlex.quote('sudo bash -c ' + shlex.quote(script))}; read -p 'Press Enter to close'"
