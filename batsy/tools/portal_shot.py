"""Take a screenshot through the xdg-desktop-portal (works on GNOME Wayland, where gnome-screenshot can't).

Run with the system Python, which has PyGObject:  /usr/bin/python3 portal_shot.py OUT.png
Standalone on purpose: the Batsy venv has no `gi`.
"""
import os
import shutil
import sys
from urllib.parse import unquote, urlparse

from gi.repository import Gio, GLib


def main(out: str) -> int:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    token = f"batsy{os.getpid()}"
    sender = bus.get_unique_name()[1:].replace(".", "_")
    handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    loop = GLib.MainLoop()
    result = {}

    def on_response(_conn, _sender, _path, _iface, _signal, params):
        code, results = params.unpack()
        result["code"], result["uri"] = code, results.get("uri")
        loop.quit()

    # Subscribe before the call so the response can't be missed.
    bus.signal_subscribe("org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request", "Response",
                         handle, None, Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)
    bus.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                  "org.freedesktop.portal.Screenshot", "Screenshot",
                  GLib.Variant("(sa{sv})", ("", {"handle_token": GLib.Variant("s", token),
                                                 "interactive": GLib.Variant("b", False)})),
                  None, Gio.DBusCallFlags.NONE, -1, None)
    GLib.timeout_add_seconds(15, loop.quit)
    loop.run()

    if result.get("code") != 0 or not result.get("uri"):
        print(f"portal screenshot failed (response {result.get('code', 'timeout')})", file=sys.stderr)
        return 1
    src = unquote(urlparse(result["uri"]).path)
    shutil.move(src, out)  # the portal saves into ~/Pictures; don't leave a copy behind
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
