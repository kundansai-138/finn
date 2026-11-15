"""Push, literally: Web Push notifications to the investor's phone.

What earns a notification (deliberately narrow, a phone buzz is expensive attention):
  - critical filings on holdings (results, CXO changes, M&A, litigation…) in categories you watch
  - your price / volume triggers firing
Each is sent once per user (notifications_sent), and carries the network angle when there is one:
"Deepak Nitrite: new CFO. Ananya can introduce you."

Keys: VAPID keys come from FINN_VAPID_PRIVATE_KEY / FINN_VAPID_PUBLIC_KEY in production. In local dev
they're generated once and kept in data/vapid/ (gitignored) so subscriptions survive restarts.
Delivery runs through the browser vendors' push services (FCM, Mozilla, Apple); on iOS it needs
the app installed to the home screen (iOS 16.4+).
"""
from __future__ import annotations

import base64
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from . import push, store
from .config import DATA

log = logging.getLogger(__name__)
SUBJECT = os.getenv("FINN_VAPID_SUBJECT", "mailto:alerts@example.com")


@lru_cache(maxsize=1)
def keys() -> tuple[str, str]:
    """(private key as base64url DER, the form pywebpush expects; public key as base64url uncompressed EC
    point, the form browsers expect for applicationServerKey)."""
    priv, pub = os.getenv("FINN_VAPID_PRIVATE_KEY"), os.getenv("FINN_VAPID_PUBLIC_KEY")
    if priv and pub:
        return priv, pub
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid

    d = Path(DATA) / "vapid"
    pem = d / "private_key.pem"
    if not pem.exists():
        d.mkdir(parents=True, exist_ok=True)
        v = Vapid()
        v.generate_keys()
        pem.write_bytes(v.private_pem())
    v = Vapid.from_file(str(pem))
    der = v.private_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return b64(der), b64(raw)


def send(title: str, body: str, url: str = "/", tag: str | None = None, user_id: str | None = None) -> dict:
    from pywebpush import WebPushException, webpush

    priv, _ = keys()
    ok = gone = failed = 0
    for sub in store.push_subs_for(user_id):
        try:
            webpush(subscription_info=sub, data=json.dumps({"title": title, "body": body, "url": url, "tag": tag}),
                    vapid_private_key=priv, vapid_claims={"sub": SUBJECT}, ttl=6 * 3600)
            ok += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):  # the device unsubscribed: forget it
                store.push_sub_remove(sub["endpoint"], user_id)
                gone += 1
            else:
                failed += 1
                log.warning("push failed (%s): %s", status, e)
        except Exception as e:  # unreachable endpoint, bad key...: one broken device must not block the others
            failed += 1
            log.warning("push to a device failed: %s", e)
    return {"sent": ok, "expired": gone, "failed": failed}


def _message(i: dict) -> tuple[str, str]:
    title = f"{i['company']} · {i['category_label']}"
    body = i.get("one_liner") or i["title"]
    if i.get("paths") and i["paths"][0].get("via"):
        body += f" {i['paths'][0]['via']['name'].split()[0]} can introduce you."
    elif i.get("best_call"):
        body += f" Best call: {i['best_call']['person']['name']}."
    return title, body[:220]


def dispatch(user_id: str | None = None) -> dict:
    """Push anything new and important since the last dispatch. Safe to call after every refresh."""
    if not store.push_subs_for(user_id):
        return {"sent": 0, "reason": "no devices subscribed"}
    b = push.brief("24h", "critical")
    candidates = [i for i in b["items"] if i["tier"] == "critical" and i.get("alert")]
    candidates += [{**a, "category_label": "Price/volume", "one_liner": a["title"]} for a in b["alerts"]]
    fresh = [c for c in candidates if c["id"] not in store.notified_filter([c["id"] for c in candidates], user_id)]
    sent = 0
    for c in fresh[:5]:  # never a burst
        title, body = _message(c)
        r = send(title, body, url=f"/#/c/{c['ticker']}", tag=c["id"], user_id=user_id)
        sent += r["sent"]
    store.notified_mark([c["id"] for c in fresh], user_id)
    return {"sent": sent, "items": len(fresh)}
