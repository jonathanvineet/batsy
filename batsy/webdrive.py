"""Batsy Drive: a web drive on the tailnet (http://<laptop's Tailscale IP>:8766), served by the inbox's receiver.

No passwords: who you are comes from the Tailscale address you connect from (`tailscale whois`, the same way Batsy
files what people send), so Jonathan, Karan and Rehaan each see only their own two spaces:
  pc     their home on the PC (Jonathan -> /batman, Karan -> nightwing, Rehaan -> superman), over SSH
  sent   what they've sent Batsy (~/Batsy/inbox/<name>)
Anyone else is turned away. Paths can't leave a space (no ../, no hidden folders).

  GET  /                          the app (batsy/ui/drive.*)
  GET  /api/me                    who you are and your spaces
  GET  /api/list?space&path       a folder          GET /api/recent?space     newest files
  GET  /api/search?space&q        by name            GET /api/file?space&path[&dl=1]   open / download
  GET  /api/thumb?space&path&t&size   a small preview (pictures, PDFs)
  POST /api/upload?space&path&name    a file (the body)    POST /api/mkdir {space, path, name}
"""
import io
import json
import logging
import mimetypes
import os
import posixpath
import shutil
import threading
import urllib.parse
from pathlib import Path

log = logging.getLogger("batsy.drive")
UI = Path(__file__).parent / "ui"
STATIC = {"/": ("drive.html", "text/html; charset=utf-8"), "/drive.css": ("drive.css", "text/css; charset=utf-8"),
          "/drive.js": ("drive.js", "text/javascript; charset=utf-8")}
_drives: dict = {}
_lock = threading.Lock()


def _pc(cfg, who: str):
    from batsy.cloud import Drive

    with _lock:
        if who not in _drives:
            _drives[who] = Drive(cfg, who)
        return _drives[who]


# --- "sent": the person's folder in Batsy's inbox, on this laptop ------------------------------------------------------
def _local(root: Path, path: str) -> Path:
    p = Path(posixpath.normpath("/" + (path or "").lstrip("/"))).relative_to("/")
    full = (root / p).resolve()
    if full != root.resolve() and root.resolve() not in full.parents:
        raise ValueError("outside your space")
    if any(part.startswith(".") for part in p.parts):
        raise ValueError("not shared")
    return full


def _entry(root: Path, p: Path) -> dict:
    st = p.stat()
    return {"path": "/" + str(p.relative_to(root.resolve())), "name": p.name, "dir": p.is_dir(), "size": st.st_size,
            "t": int(st.st_mtime)}


def _walk(root: Path):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            if not f.startswith(".") and not f.endswith(".part"):
                p = Path(d) / f
                try:
                    yield p, p.stat()
                except OSError:
                    pass


def _local_query(root: Path, mode: str, arg: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    if mode == "list":
        folder = _local(root, arg)
        items = [_entry(root, p) for p in folder.iterdir() if not p.name.startswith(".") and not p.name.endswith(".part")]
        out = {"items": items, "path": "/" + str(folder.relative_to(root.resolve())).lstrip(".")}
    elif mode == "recent":
        out = {"items": [_entry(root, p) for p, _ in sorted(_walk(root), key=lambda x: -x[1].st_mtime)[:int(arg)]]}
    else:
        q = arg.lower()
        out = {"items": [_entry(root, p) for p, _ in _walk(root) if q in p.name.lower()][:200]}
    du = shutil.disk_usage(root)
    out["free"], out["total"] = du.free, du.total
    return out


# --- requests ------------------------------------------------------------------------------------------------------------
def handle(h, who: str, inbox, method: str) -> bool:
    """Serve a drive request on handler `h` for `who`. False if it isn't one of ours (the old upload PUT, say)."""
    url = urllib.parse.urlsplit(h.path)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
    route = url.path
    try:
        if method == "GET" and route in STATIC:
            name, kind = STATIC[route]
            return _send(h, 200, (UI / name).read_bytes(), kind, cache=False)
        if method == "GET" and route.startswith("/fonts/") and route.endswith(".woff2"):
            f = UI / "fonts" / Path(route).name
            return _send(h, 200, f.read_bytes(), "font/woff2") if f.is_file() else _send(h, 404, b"", "text/plain")
        if not route.startswith("/api/"):
            return False
        space = q.get("space", "pc")
        if space not in ("pc", "sent"):
            raise ValueError("no such space")
        root = inbox.dir / who
        pc = _pc(inbox.cfg, who) if space == "pc" else None
        if route == "/api/me":
            d = _pc(inbox.cfg, who)
            return _json(h, {"name": who, "spaces": [
                {"id": "pc", "label": "PC drive", "where": f"{d.user} on the PC" if d.user else "", "root": "/"},
                {"id": "sent", "label": "Sent to Batsy", "where": "on Batsy's laptop", "root": "/"}]})
        if method == "GET" and route in ("/api/list", "/api/recent", "/api/search"):
            mode = route.rsplit("/", 1)[1]
            arg = q.get("path", "/") if mode == "list" else q.get("n", "40") if mode == "recent" else q.get("q", "")
            if pc is not None:
                out = pc.query(mode, pc.root + "/" + arg.lstrip("/") if mode == "list" else arg)
                for it in out.get("items", []):  # paths relative to the drive (never the PC's real layout)
                    it["path"] = "/" + it["path"].removeprefix(pc.root).lstrip("/")
                if "path" in out:
                    out["path"] = "/" + out["path"].removeprefix(pc.root).lstrip("/")
            else:
                out = _local_query(root, mode, arg)
            _warm(who, space, pc, root, out.get("items", []))
            return _json(h, out, gzip_ok="gzip" in (h.headers.get("Accept-Encoding") or ""))
        if method == "GET" and route == "/api/file":
            path = q.get("path", "")
            name = posixpath.basename(path)
            kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
            disp = f"{'attachment' if q.get('dl') else 'inline'}; filename*=UTF-8''{urllib.parse.quote(name)}"
            if pc is not None:
                size, _ = pc.stat(pc.root + path)
                h.send_response(200)
                h.send_header("Content-Type", kind)
                h.send_header("Content-Length", str(size))
                h.send_header("Content-Disposition", disp)
                h.end_headers()
                pc.stream(pc.root + path, h.wfile)
                return True
            p = _local(root, path)
            h.send_response(200)
            h.send_header("Content-Type", kind)
            h.send_header("Content-Length", str(p.stat().st_size))
            h.send_header("Content-Disposition", disp)
            h.end_headers()
            with open(p, "rb") as f:
                shutil.copyfileobj(f, h.wfile, 256 * 1024)
            return True
        if method == "GET" and route in ("/api/thumb", "/api/preview"):  # a tile, or a picture sized for a screen
            path, size = q.get("path", ""), int(q.get("size", "0") or 0)
            px = 360 if route == "/api/thumb" else 1600
            data = pc.thumb(pc.root + path, int(q.get("t", "0") or 0), size, px) if pc is not None \
                else _local_thumb(_local(root, path), px)
            return _send(h, 200, data, "image/jpeg") if data else _send(h, 404, b"", "text/plain")
        if method == "POST" and route == "/api/upload":
            name = Path(urllib.parse.unquote(q.get("name", "upload"))).name
            length = int(h.headers.get("Content-Length", "0"))
            limit = int(inbox.cfg.get("files.receive_max_mb")) << 20
            if length > limit:
                raise ValueError(f"over the {limit >> 20} MB limit")
            if pc is not None:
                where = pc.upload(pc.root + "/" + q.get("path", "/").lstrip("/"), name, h.rfile, length)
                return _json(h, {"ok": True, "path": "/" + where.removeprefix(pc.root).lstrip("/")})
            folder = _local(root, q.get("path", "/"))
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f".{name}.{threading.get_ident()}.part"  # hidden until whole: Batsy's watcher skips it
            with open(tmp, "wb") as f:
                left = length
                while left:
                    chunk = h.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ConnectionError("upload cut off")
                    f.write(chunk)
                    left -= len(chunk)
            from batsy.inbox import _place

            dest = _place(tmp, folder, name)
            log.info("drive: %s uploaded %s", who, dest.name)
            return _json(h, {"ok": True, "path": "/" + str(dest.relative_to(root.resolve()))})
        if method == "POST" and route == "/api/mkdir":
            body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", "0") or 0)) or b"{}")
            name = Path(str(body.get("name", ""))).name.strip()
            if not name or name.startswith("."):
                raise ValueError("give the folder a name")
            if pc is not None:
                return _json(h, {"ok": True, "path": "/" + pc.mkdir(pc.root + "/" + str(body.get("path", "/")).lstrip("/"),
                                                                 name).removeprefix(pc.root).lstrip("/")})
            f = _local(root, str(body.get("path", "/"))) / name
            f.mkdir(parents=True, exist_ok=True)
            return _json(h, {"ok": True, "path": "/" + str(f.relative_to(root.resolve()))})
        return _json(h, {"error": "no such thing"}, 404)
    except (ValueError, FileNotFoundError, PermissionError) as e:
        return _json(h, {"error": str(e) or "not found"}, 400 if isinstance(e, ValueError) else 404)
    except (BrokenPipeError, ConnectionError):
        return True
    except Exception as e:
        log.exception("drive %s %s", method, route)
        return _json(h, {"error": f"something went wrong: {e}"}, 500)


PICS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff"}
_warming: set = set()


def _warm(who: str, space: str, pc, root: Path, items: list) -> None:
    """Make a folder's tiles and screen-sized previews now, in the background, so opening them is instant: the PC is
    a cable away, the person may be on slow Wi-Fi. One worker per person and space; newest files first."""
    key = (who, space)
    if key in _warming:
        return
    todo = [it for it in sorted(items, key=lambda x: -x.get("t", 0)) if not it.get("dir")
            and Path(it["name"]).suffix.lower() in PICS | {".pdf"} and it.get("size", 0) < 60e6][:80]
    if not todo:
        return

    def run():
        _warming.add(key)
        try:
            for px in (360, 1600):  # every tile first, then the previews
                for it in todo[:80 if px == 360 else 30]:
                    if px == 1600 and Path(it["name"]).suffix.lower() == ".pdf":
                        continue  # PDFs open in the viewer itself
                    try:
                        if pc is not None:
                            pc.thumb(pc.root + it["path"], it.get("t", 0), it.get("size", 0), px)
                        else:
                            _local_thumb(_local(root, it["path"]), px)
                    except Exception:
                        pass
        finally:
            _warming.discard(key)

    threading.Thread(target=run, daemon=True, name="drive-warm").start()


def _local_thumb(p: Path, px: int = 360) -> bytes | None:
    import hashlib

    from PIL import Image, ImageOps

    from batsy.cloud import THUMBS

    st = p.stat()
    key = THUMBS / (hashlib.sha1(f"local|{p}|{int(st.st_mtime)}|{st.st_size}|{px}".encode()).hexdigest() + ".jpg")
    if key.exists():
        return key.read_bytes()
    try:
        if p.suffix.lower() == ".pdf":
            import fitz

            with fitz.open(p) as d:
                pix = d[0].get_pixmap(dpi=48 if px <= 360 else 150)
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        else:
            im = ImageOps.exif_transpose(Image.open(p))
        im.thumbnail((px, px))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=78 if px <= 360 else 82, progressive=px > 360)
        key.write_bytes(buf.getvalue())
        return buf.getvalue()
    except Exception:
        return None


def _json(h, obj, code: int = 200, gzip_ok: bool = False) -> bool:
    body = json.dumps(obj).encode()
    if gzip_ok and len(body) > 1400:  # big listings: a fifth of the size over slow Wi-Fi
        import gzip

        return _send(h, code, gzip.compress(body, 5), "application/json", encoding="gzip")
    return _send(h, code, body, "application/json")


def _send(h, code: int, body: bytes, kind: str, cache: bool = True, encoding: str = "") -> bool:
    h.send_response(code)
    h.send_header("Content-Type", kind)
    h.send_header("Content-Length", str(len(body)))
    if encoding:
        h.send_header("Content-Encoding", encoding)
    # tiles and previews are keyed by the file's time and size in their URL: a changed file is a new URL
    h.send_header("Cache-Control", "private, max-age=2592000, immutable" if cache and code == 200
                  and kind.startswith("image") else "no-store")
    h.end_headers()
    h.wfile.write(body)
    return True
