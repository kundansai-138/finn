"""Web Push end to end against a local mock push service: encryption, VAPID auth, dedupe, expiry."""
import base64
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from finn import notify, store


@pytest.fixture
def push_service():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            self.send_response(410 if "gone" in self.path else 201)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", received
    srv.shutdown()


def _subscription(endpoint):
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return {"endpoint": endpoint, "keys": {"p256dh": b64(pub), "auth": b64(os.urandom(16))}}


def test_dispatch_sends_encrypted_once_then_dedupes(push_service, as_user):
    base, received = push_service
    with as_user("demo"):
        for s in store.push_subs_for():          # isolation from earlier runs
            store.push_sub_remove(s["endpoint"])
        store.push_sub_add(_subscription(f"{base}/device-1"))
        try:
            first = notify.dispatch()
            assert first["sent"] >= 1
            msg = received[0]
            assert msg["headers"]["content-encoding"] == "aes128gcm"           # payload is end-to-end encrypted
            assert msg["headers"]["authorization"].startswith("vapid t=")      # signed with our VAPID key
            assert b"Siddharth" not in msg["body"]                              # the push service can't read it
            n = len(received)
            assert notify.dispatch()["sent"] == 0 and len(received) == n        # never the same alert twice
        finally:
            store.push_sub_remove(f"{base}/device-1")


def test_expired_devices_are_forgotten(push_service, as_user):
    base, _ = push_service
    with as_user("demo"):
        store.push_sub_add(_subscription(f"{base}/gone"))
        r = notify.send("t", "b")
        assert r["expired"] == 1 and store.push_subs_for() == []


def test_one_dead_device_does_not_block_the_others(push_service, as_user):
    base, received = push_service
    with as_user("demo"):
        store.push_sub_add(_subscription("http://127.0.0.1:9/unreachable"))
        store.push_sub_add(_subscription(f"{base}/device-2"))
        try:
            r = notify.send("t", "b")
            assert r["sent"] == 1 and r["failed"] == 1
        finally:
            for s in store.push_subs_for():
                store.push_sub_remove(s["endpoint"])
