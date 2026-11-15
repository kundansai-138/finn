-- Runs once, when the database volume is first created.
--
-- The app connects as `finn_app`, a non-superuser. That matters: Postgres superusers bypass
-- row-level security, so an app running as one would make the per-user isolation policies decorative.
-- Local-only password; production credentials come from the hosting provider's secret store.
CREATE ROLE finn_app LOGIN PASSWORD 'finn_app_local';
GRANT CONNECT ON DATABASE finn TO finn_app;
GRANT USAGE, CREATE ON SCHEMA public TO finn_app;

CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector: embeddings for retrieval (next step)
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- trigram matching: fuzzy company/person name search

-- Isolated database for the test suite, so tests never touch dev data.
CREATE DATABASE finn_test OWNER finn_app;
\connect finn_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
GRANT USAGE, CREATE ON SCHEMA public TO finn_app;
