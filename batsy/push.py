"""Push notifications to the Batsy app on your phone (iPhone home-screen app, iOS 16.4+, or Android Chrome).

Standard Web Push: the app subscribes once ("Turn on notifications"), and Batsy sends to the push service the phone
named (Apple's web.push.apple.com for an iPhone) signed with its own VAPID key, encrypted for that phone. No
account, no third-party app. Used for the 3D printer (done, failed, paused by itself) and reminders.

  vapid.pem          Batsy's signing key (made once)
  push_subs.json     the phones that subscribed; ones the push service says are gone get dropped
"""
import json
import logging
import threading
from pathlib import Path

from batsy.config import DATA_DIR

log = logging.getLogger("batsy.push")
KEY = DATA_DIR / "vapid.pem"
SUBS = DATA_DIR / "push_subs.json"
CLAIM = "https://workstation.taila566eb.ts.net"  # who's sending (VAPID "sub"); a URL, no email address needed
_lock = threading.Lock()


def _vapid():
    from py_vapid import Vapid

    if not KEY.exists():
        v = Vapid()
        v.generate_keys()
        v.save_key(str(KEY))
        KEY.chmod(0o600)
    return Vapid.from_file(str(KEY))


def public_key() -> str:
    """The key the app subscribes with (base64url, uncompressed P-256 point)."""
    import base64

    from cryptography.hazmat.primitives import serialization

    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _subs() -> list[dict]:
    try:
        return json.loads(SUBS.read_text())
    except (OSError, ValueError):
        return []


def subscribe(sub: dict, device: str = "") -> int:
    if not isinstance(sub, dict) or not sub.get("endpoint") or not (sub.get("keys") or {}).get("p256dh"):
        raise ValueError("not a push subscription")
    with _lock:
        subs = [s for s in _subs() if s["endpoint"] != sub["endpoint"]] + [{**sub, "device": device[:80]}]
        SUBS.write_text(json.dumps(subs))
        SUBS.chmod(0o600)
    return len(subs)


def unsubscribe(endpoint: str) -> None:
    with _lock:
        SUBS.write_text(json.dumps([s for s in _subs() if s["endpoint"] != endpoint]))


def notify(title: str, body: str, tag: str = "batsy", url: str = "/app/") -> int:
    """Send to every subscribed phone, in the background. Returns how many it's sending to."""
    subs = _subs()
    if not subs:
        return 0

    def send():
        from pywebpush import WebPushException, webpush

        payload = json.dumps({"title": title, "body": body, "tag": tag, "url": url})
        for s in subs:
            try:
                webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]}, data=payload,
                        vapid_private_key=str(KEY), vapid_claims={"sub": CLAIM}, ttl=3600, timeout=15)
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code in (404, 410):  # that phone unsubscribed / reinstalled the app
                    unsubscribe(s["endpoint"])
                else:
                    log.warning("push to %s failed: %s", s.get("device") or "a phone", e)
            except Exception as e:
                log.warning("push failed: %s", e)

    threading.Thread(target=send, daemon=True, name="push").start()
    return len(subs)
