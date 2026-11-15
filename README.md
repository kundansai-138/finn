# FINN

A mobile-first investor agent, installable on your phone. **Push** tells you what changed and why it matters. **Pull** researches any NSE company from primary sources. **The Network** sits under both and turns "what happened" into "who do I know who can explain this".

> Design, architecture and trade-offs (including why PostgreSQL): [docs/DESIGN.md](docs/DESIGN.md) · How it's tested: [docs/TESTING.md](docs/TESTING.md)

## Run it

```bash
docker compose up -d db                    # PostgreSQL 16 + pgvector, with the app role and a test database
cp .env.example .env                       # DATABASE_URL for the local db; add ANTHROPIC_API_KEY if you have one
pip install -r requirements.txt
python -m scripts.refresh                  # prices + filings for the portfolio (~30s)
uvicorn finn.api:app --port 8000           # open http://localhost:8000, ideally on a phone
```

Or the whole stack in containers: `docker compose --profile app up --build` (http://localhost:8080).

On a phone, use "Install FINN" (Android) or Share → Add to Home Screen (iPhone), then turn on **Alerts on this phone** in the rules sheet.

Without a model key everything still runs, on deterministic fallbacks. With one, FINN extracts and verifies guidance from transcripts, writes one-line summaries, argues bull and bear, and Ask FINN writes grounded answers.

## Test it

```bash
python -m pytest -q                        # 44 tests on the real Postgres test database (no model cost)
python -m evals.run                        # quality vs labelled ground truth, each against a baseline
python -m scripts.loadtest --users 20      # throughput and p50/p95/p99 per endpoint
```

## What you'll see

- **Brief**: a finite, ranked brief for a time window (24h / 3d / 7d / 30d), filtered by a *signal dial* (Critical / Material / Everything). Routine filings are folded into one counted line. Each material card ends in a person: who's coming in and your way to them, or the best person to call and why.
- **Moved**: end-of-day price and volume alerts against your own thresholds, flagged when no filing explains the move.
- **Dossier** (type any ticker): promises vs delivery from the last four concalls, the latest quarter (QoQ / YoY), trajectory, cash and balance sheet, bull vs bear, the people who can help, and recent filings.
- **Network**: expertise inferred from careers, contacts going cold, and natural reasons to reconnect.
  - "Who should I call" is a recommender: candidates from graph walks, domain similarity and career links, ranked by a model that learns from your *Helpful / Not relevant* taps.
  - Each pick has a "Why this person?" evidence graph.
- **Ask FINN** (any question in the ask bar, or "Ask FINN" on a card or dossier) is a conversational agent with a plan → execute → answer → verify graph you can watch run. It has tools over filings, transcripts (hybrid retrieval), financials, your network and the web. It keeps thread memory and your standing preferences, and every figure in an answer is checked against its cited source.

- **Mobile**:
  - installable and full-screen, and the brief still opens offline;
  - real push notifications for critical filings and your price/volume triggers;
  - swipe a card when it's handled, pull to refresh, drag sheets down to close.

## What's real and what isn't

| | |
|---|---|
| Prices, volumes | Real (Yahoo Finance EOD) |
| Announcements | Real (screener.in per-company feed of BSE filings + NSE public RSS) |
| Financials, transcripts, annual reports | Real (screener.in tables; transcript PDFs from BSE) |
| The investor's network | **Synthetic**: fictional people with careers at real companies |
| Three events (Deepak Nitrite CFO, Laurus capex, PI acquisition) | **Simulated**, badged in the UI, so every card type shows on any day |

## Layout

```
finn/
  sources/    prices.py · screener.py · nse_rss.py · documents.py   (plumbing)
  intel/      classify.py (rules-first filing triage) · financials.py (deterministic maths)
              guidance.py (promises ledger) · llm.py (routing, caching, cost ledger)
  network/    graph.py (warmth, overlap links, paths, reconnect, entity resolution)
              recommend.py (two-stage recommender, learned ranking, evidence graphs)
  agent/      core.py (plan-execute-verify graph) · runtime.py (state-graph runtime) · tools.py
              retrieval.py (Q&A chunking, hybrid BM25 + char-TF-IDF, RRF) · memory.py · verify.py
  store.py    the database: schema, row-level security, per-user data access (Postgres; SQLite fallback)
  push.py     refresh job + the brief          notify.py   Web Push alerts
  pull.py     the dossier                      scheduler.py  ingestion jobs (leader-elected)
  ask.py      ask-bar router                   api.py      FastAPI: API, PWA files, metrics
web/          Preact + htm, no build step: app.js · mobile.js (gestures, install, push) · sw.js · manifest
data/         portfolio.json · network.json (synthetic) · scenarios.json (simulated): seed data
tests/        44 pytest tests            evals/      labelled eval sets, runner, mobile audit
docker/       Postgres init (app role, extensions, test db)
.github/      CI (tests on Postgres) + scheduled refresh          render.yaml   deploy blueprint
```
