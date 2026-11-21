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
                return _json(h, out)
            return _json(h, _local_query(root, mode, arg))
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
        if method == "GET" and route == "/api/thumb":
            path, size = q.get("path", ""), int(q.get("size", "0") or 0)
            data = pc.thumb(pc.root + path, int(q.get("t", "0") or 0), size) if pc is not None else _local_thumb(_local(root, path))
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


def _local_thumb(p: Path) -> bytes | None:
    from PIL import Image, ImageOps

    try:
        if p.suffix.lower() == ".pdf":
            import fitz

            with fitz.open(p) as d:
                pix = d[0].get_pixmap(dpi=48)
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        else:
            im = ImageOps.exif_transpose(Image.open(p))
        im.thumbnail((360, 360))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=78)
        return buf.getvalue()
    except Exception:
        return None


def _json(h, obj, code: int = 200) -> bool:
    return _send(h, code, json.dumps(obj).encode(), "application/json")


def _send(h, code: int, body: bytes, kind: str, cache: bool = True) -> bool:
    h.send_response(code)
    h.send_header("Content-Type", kind)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "private, max-age=3600" if cache and code == 200 and kind.startswith("image") else "no-store")
    h.end_headers()
    h.wfile.write(body)
    return True
