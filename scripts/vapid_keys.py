"""Generate a production VAPID key pair for Web Push.

    python -m scripts.vapid_keys

Prints the two values to put in the hosting provider's secret settings (never in the repo).
Keep them stable: rotating the key invalidates every phone's existing push subscription.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

key = ec.generate_private_key(ec.SECP256R1())
b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
print(f"FINN_VAPID_PRIVATE_KEY={b64(der)}")
print(f"FINN_VAPID_PUBLIC_KEY={b64(pub)}")
