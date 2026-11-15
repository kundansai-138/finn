"""Create FINN's tables, row-level security policies and the demo investor in DATABASE_URL (idempotent)."""
from finn import store

store.init()
print("schema, policies and demo investor ready")
