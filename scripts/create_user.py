"""Create (or reset) an investor account.

    python -m scripts.create_user tester --name "Test investor"            # starts from the demo portfolio and network
    python -m scripts.create_user analyst2 --empty                           # empty radar and network
    python -m scripts.create_user tester --reset-password                    # new password for an existing account

The password is generated (unless FINN_NEW_PASSWORD is set) and written to .env, which git ignores,
as FINN_TEST_USER / FINN_TEST_PASSWORD. It is never printed: open .env to read it.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _write_env(user: str, password: str) -> None:
    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    for k, v in (("FINN_TEST_USER", user), ("FINN_TEST_PASSWORD", password)):
        line = f"{k}={v}"
        text = re.sub(rf"^{k}=.*$", line, text, flags=re.M) if re.search(rf"^{k}=", text, re.M) else text.rstrip("\n") + f"\n{line}\n"
    env.write_text(text, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("--name", default=None)
    ap.add_argument("--empty", action="store_true", help="start with an empty radar and network")
    ap.add_argument("--reset-password", action="store_true")
    a = ap.parse_args()

    from finn import auth, store

    store.init()
    password = os.getenv("FINN_NEW_PASSWORD") or secrets.token_urlsafe(12)
    if store.user_exists(a.username):
        if not a.reset_password:
            raise SystemExit(f"{a.username} exists. Use --reset-password to set a new password.")
        auth.set_password(a.username, password)
        print(f"Password reset for {a.username}.")
    else:
        auth.register(a.username, password, a.name, start_from_demo=not a.empty)
        print(f"Created {a.username} ({'empty' if a.empty else 'starting from the demo portfolio and network'}).")
    _write_env(a.username, password)
    print("Credentials written to .env as FINN_TEST_USER / FINN_TEST_PASSWORD (git ignores .env).")


if __name__ == "__main__":
    main()
