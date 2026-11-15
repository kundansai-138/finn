"""Prepare a fresh Neon database for FINN, from your machine, in one go.

    1. Put the Neon owner connection string in deploy/production.env (git ignores deploy/):
           NEON_OWNER_URL=postgresql://neondb_owner:...@ep-....neon.tech/neondb?sslmode=require
    2. python -m scripts.setup_production

What it does (idempotent; safe to run again):
  - creates the app role `finn_app` (a plain role, so row-level security binds it) with a generated
    password, grants it the schema, and enables pgvector and pg_trgm
  - creates FINN's tables as finn_app, applies the row-level security policies, seeds the demo investor
  - copies the already-fetched public reference data from your local database (filings, prices,
    transcripts, companies), so the deployed app works on its first request
  - creates the shareable test account (demo portfolio and network) with a generated password
  - generates the Web Push (VAPID) key pair, once

Everything to paste into Render ends up in deploy/production.env. Nothing secret is printed.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
ENV = Path(os.environ.get("FINN_PROD_ENV_FILE") or ROOT / "deploy" / "production.env")
ROLE = os.environ.get("FINN_APP_ROLE", "finn_app")


def read() -> dict[str, str]:
    if not ENV.exists():
        raise SystemExit(f"Create {ENV.relative_to(ROOT)} with NEON_OWNER_URL=... first.")
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"^([A-Z_]+)=(.*)$", ENV.read_text(encoding="utf-8"), re.M)}


def write(updates: dict[str, str]) -> None:
    text = ENV.read_text(encoding="utf-8")
    for k, v in updates.items():
        line = f"{k}={v}"
        text = re.sub(rf"^{k}=.*$", line, text, flags=re.M) if re.search(rf"^{k}=", text, re.M) else text.rstrip("\n") + f"\n{line}\n"
    ENV.write_text(text, encoding="utf-8")


def sa_url(url: str) -> str:
    return re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql+psycopg://", url)


def with_user(url: str, user: str, password: str) -> str:
    p = urlsplit(url)
    host = p.netloc.split("@", 1)[1]
    return urlunsplit((p.scheme, f"{user}:{quote(password, safe='')}@{host}", p.path, p.query, p.fragment))


def run(args: list[str], env: dict) -> None:
    r = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, env={**os.environ, **env}, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{' '.join(args[:1])} failed:\n{r.stderr[-1500:]}")
    print("  " + "\n  ".join(line for line in r.stdout.strip().splitlines()[-12:] if "PASSWORD" not in line))


def main() -> None:
    cfg = read()
    owner = cfg.get("NEON_OWNER_URL")
    if not owner:
        raise SystemExit("NEON_OWNER_URL is empty in deploy/production.env")
    from sqlalchemy import create_engine, text

    app_pw = cfg.get("FINN_APP_DB_PASSWORD") or secrets.token_urlsafe(24)
    print("1/5 app role and extensions")
    eng = create_engine(sa_url(owner))
    with eng.begin() as c:
        db = c.execute(text("select current_database()")).scalar()
        exists = c.execute(text("select 1 from pg_roles where rolname = :r"), {"r": ROLE}).first()
        # Postgres can't bind a parameter in CREATE ROLE, so quote the generated (URL-safe) password literally.
        c.execute(text(f"{'ALTER' if exists else 'CREATE'} ROLE {ROLE} LOGIN PASSWORD '{app_pw}'"))
        c.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO {ROLE}'))
        c.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {ROLE}"))
        for ext in ("vector", "pg_trgm"):
            c.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
        bypass = c.execute(text("select rolbypassrls or rolsuper from pg_roles where rolname = :r"), {"r": ROLE}).scalar()
    if bypass:
        raise SystemExit(f"{ROLE} can bypass row-level security on this server; refusing to continue.")
    app_url = sa_url(with_user(owner, ROLE, app_pw))
    write({"FINN_APP_DB_PASSWORD": app_pw, "DATABASE_URL": app_url})
    env = {"DATABASE_URL": app_url, "FINN_SCHEDULER": "off", "FINN_LLM": "off"}

    print("2/5 tables, row-level security, demo investor")
    run(["scripts.init_db"], env)

    print("3/5 public reference data from your local database")
    local = os.environ.get("LOCAL_DATABASE_URL") or _local_url()
    run(["scripts.copy_reference", "--from", local, "--to", app_url], env)

    print("4/5 shareable test account")
    user = cfg.get("FINN_TEST_USER") or "finn-demo"
    pw = cfg.get("FINN_TEST_PASSWORD") or secrets.token_urlsafe(9)
    run(["scripts.create_user", user, "--name", "FINN Demo", "--env-file", str(ENV)] + (["--reset-password"] if cfg.get("FINN_TEST_PASSWORD") else []),
        {**env, "FINN_NEW_PASSWORD": pw})

    print("5/5 Web Push keys")
    if not cfg.get("FINN_VAPID_PRIVATE_KEY"):
        out = subprocess.run([sys.executable, "-m", "scripts.vapid_keys"], cwd=ROOT, capture_output=True, text=True).stdout
        write(dict(re.findall(r"^(FINN_VAPID_\w+)=(.*)$", out, re.M)))
    if not cfg.get("FINN_VAPID_SUBJECT"):
        write({"FINN_VAPID_SUBJECT": "mailto:alerts@example.com"})
    print(f"\nDone. Paste the values from {ENV} into Render (see the comments in that file).")


def _local_url() -> str:
    env = (ROOT / ".env").read_text(encoding="utf-8")
    m = re.search(r"^DATABASE_URL=(.*)$", env, re.M)
    return m.group(1).strip()


if __name__ == "__main__":
    main()
