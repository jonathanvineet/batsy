"""Put things on the screen overlay: a picture (looked up locally, so you never pay to see it) or a text card."""
import os
import shutil
import subprocess

from batsy import images


def register(reg):
    @reg.tool(
        "display",
        "Show a specific local image file (image_path, e.g. from ~/Pictures) or exact image URL full-screen. "
        "For pictures of things in general, and text cards, use the inline [[show: ...]] / [[card: ...]] "
        "markers instead (no extra round trip).",
        {
            "image_query": {"type": "string", "description": "what to find a picture of, e.g. 'Eiffel Tower at night'"},
            "image_path": {"type": "string"},
            "image_url": {"type": "string"},
            "title": {"type": "string"},
            "text": {"type": "string"},
        },
    )
    def display(ctx, image_query=None, image_path=None, image_url=None, title=None, text=None):
        shown = []
        if image_query or image_url or image_path:
            if image_path:
                path, caption = os.path.expanduser(image_path), os.path.basename(image_path)
                if not os.path.exists(path):
                    return f"No file at {path}."
            elif image_url:
                path, caption = images.download(image_url), ""
            else:
                try:
                    path, caption = images.find_image(image_query)
                except LookupError as e:
                    return str(e)
            if ctx.display:
                ctx.display("image", path=str(path), caption=caption)
            elif shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shown.append(f"picture '{caption}'")
        if title or text:
            if ctx.display:
                ctx.display("card", title=title or "", text=text or "")
            shown.append("text card")
        return f"Showing {' and '.join(shown)}." if shown else "Nothing to show: pass image_query, image_path, image_url or text."
