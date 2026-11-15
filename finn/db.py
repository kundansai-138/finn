"""Cache helpers. Everything else lives in finn.store (the relational schema).

Kept as a small module so cache call sites read as what they are: `db.kv_get("dossier", …)`.
"""
from .store import kv_age, kv_clear, kv_get, kv_put  # noqa: F401
