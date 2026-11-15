# Testing FINN: correctness, quality, performance

Three different questions, three different tools:

| Question | Tool | Command |
|---|---|---|
| Does the code do what it was written to do? | **Tests** (pytest, 68 tests, real Postgres; 4 more live) | `python -m pytest -q` |
| Are the AI judgement calls *good*? | **Evals**: labelled ground truth, each scored against a simpler baseline | `python -m evals.run` |
| Is it fast enough, under load, on a phone? | **Performance**: profiling, latency budgets, load test, mobile audit | `python -m scripts.loadtest` |

## 1. Tests (correctness)

Setup: `docker compose up -d db`. Tests use the separate `finn_test` database, rebuilt from scratch every run. The model is always off (no cost) and the scheduler never starts.

| Area | What's pinned | Why it matters |
|---|---|---|
| Privacy | users can't see each other's network, threads, rankings or holdings; **raw SQL as another user sees nothing and can't write** (row-level security); contact names never reach the model | the brief's privacy requirement, enforced by the database, not just the code |
| HTTP contracts | every screen's endpoint; the agent's event stream; swipe-to-handle round trip | the frontend depends on these shapes |
| Financial maths | QoQ/YoY, margin bps, CAGR edge cases, lender labels | a wrong number is worse than no number |
| Fiscal calendar | call month → quarter discussed; FY27 = Apr 2026–Mar 2027; guidance verdicts, including "behind" for an incomplete year | the promises ledger depends on it |
| Parsers | screener and NSE RSS against synthetic fixtures (no third-party pages in the repo) | catches parser breakage offline |
| Filing triage | body escalation ("General Updates" → acquisition), paperwork never escalates | missed news is the costliest error |
| Agent | invented tools dropped; an ungrounded draft is repaired; tool plans | hallucination control |
| Push | payload encrypted (aes128gcm), VAPID-signed, sent once, expired devices forgotten, a dead device doesn't block the rest | uses a local mock push service |
| Latency budgets | p95 limits on brief, recommender, portfolio, health | performance regressions fail CI |
| Network flows (24 scenarios, `tests/test_flows.py`) | cause ranking (named subject beats generic insider; sector themes don't override the event); web leads verified against the page they cite (invented URL and conflated name dropped); fact-checks need every number on the page; trusted sources first; budget and cache; push engine gates weak matches; nudges rank overdue × value × reason, logging a call clears them, weekly push cap, the contact's name never reaches the model | the web is where hallucination enters; these pin the checks in code |

`live`-marked tests (`pytest -m live`) hit the real sources to detect upstream HTML changes; they're kept out of CI. `FINN_TEST_LLM=on python -m pytest -m live tests/test_flows_live.py -s` runs the three network flows against the real web and model (~$0.15). They assert properties that must hold whatever the web says today: everything shown is verified, above the bar, labelled, and connected to the network where a route exists.

## 2. Evals (quality vs ground truth)

Results are saved to `evals/results/<timestamp>.json`, so runs are comparable over time.

| Suite | Ground truth | FINN | Baseline |
|---|---|---|---|
| Filing triage | 89 real filings hand-labelled (category + materiality) | critical recall **1.00**, material recall **0.955**, noise suppressed 0.97, category accuracy 0.91 | title-only: 0.64 / 0.73 / 0.97 / 0.80 |
| "Who should I call" | 28 queries with graded relevance | NDCG@3 **0.998**, MRR **1.0** (was 0.992 / 0.982 before cause-based ranking) | original rules: 0.977, 0.958 |
| Push vs pull engine (`modes`) | same 28 queries; plus derived "call today" labels (grade −1 for a dormant contact) | push: 97% of names shown on an alert are good calls (39 shown); NDCG −0.01 | pull engine on alerts: 89% (47 shown) |
| Transcript retrieval | 12 questions, keyword-defined relevance (silver labels) | recall@5 0.917, MRR **0.764** | BM25 only: 0.917, 0.681 |
| Agent routing (no key) | 12 cases incl. follow-ups and unanswerables | entity 1.0, tool recall 1.0, grounded 1.0, no invention 1.0 | — |

**What the evals found** (that's what they're for):
- Title-only triage misses a third of the critical filings, because companies bury news under "General Updates". Reading the body fixes it.
- **The first recommender eval was too easy:** both systems scored ~1.0, so it measured nothing. Harder queries, where the event type should change the answer, now separate them.
- They also exposed a real weakness: for an OSAT joint venture at Kaynes, the sales insider outranked the two semiconductor-assembly specialists, because "worked there" outweighed topic expertise. Fixed with cause-conditioned ranking (role-level topic credit; insider discount when a filing names a subject the company isn't mainly in); see [SCORING.md](SCORING.md).
- The `modes` suite rejected two push designs before the third: an additive warmth weight lost on both label sets, and a product of probabilities let reachability swamp saturated relevance scores. Multiplying in odds space won where it matters, on the two names an alert shows.
- Hybrid retrieval doesn't find more passages than BM25 here, but it ranks the right one higher (MRR +0.08).

**First run of the agent suite with a model key (15 Nov 2026):** entity 1.0, tool recall 0.875, grounded 0.917, no invention on unanswerables **0.833**, p50 24 s. Two cases answered what should have been declined ("What will the Sensex do tomorrow?", "Tell me something interesting"), and one Laurus debt answer wasn't fully grounded. Logged as open work, not tuned away here.

**With a model key, two things change.** The agent suite runs the model planner, and two more suites become possible:
- a promises-ledger suite (extraction precision and recall, plus verdict accuracy against hand-labelled guidance);
- a faithfulness judge (a model checks each answer's claims against its evidence), calibrated against a small human-labelled set before it's trusted.

## 3. Performance

**Method:** measure → profile → fix the hotspot → re-measure under load.

| Step | Finding | Fix | Result |
|---|---|---|---|
| Profile the brief (cProfile) | 43 recommender calls for 33 filings; one-time index build on the first request | memoise per user (cleared on feedback); warm the index at startup | 440 → 200 ms |
| Profile again | 44 DB round trips for prices | one batched query | **200 → 40 ms** |
| Load test, 20 users | p95: dossier 48 s, health 6 s; 5 req/s | single-flight dossier builds (stampede); DB pool sized to worker threads (starvation); pre-build dossiers after refresh | **64.7 req/s; dossier p95 167 ms; health p95 180 ms; 0 errors** |
| Under load | brief p50 rises to ~200 ms (CPU-bound in one process) | 2+ worker processes in production; one elected scheduler | — |

Tools:
- `GET /api/metrics`: live p50/p95/p99 per route from inside the app;
- a `Server-Timing` header on every response;
- `python -m scripts.loadtest --users 20 --duration 30 [--chat]`.

Cost (once a key is set): every model call is logged with its tokens, cache-read tokens and dollars (`GET /api/metrics` → `llm`), so cost per question and the cache hit rate are measured, not guessed.

## 4. Mobile

Paste [evals/mobile_audit.js](../evals/mobile_audit.js) into the console (or run it through Playwright) at 320, 375, 390 and 412px. Each screen is checked for:
- horizontal overflow;
- tap targets under 44px;
- *rendered* text under 11px (SVG text measured at its on-screen scale);
- inputs that make iOS zoom.

What the audit caught and fixed:
- time-window chips at 36px;
- dossier tabs at 28px;
- 10.5px labels;
- graph labels rendering at 6px on a 320px phone;
- **a service worker serving stale CSS after a deploy**: now versioned by asset hash.

## 5. The deployable artifact

`docker compose --profile app up --build` runs the production image against Postgres. Building and running it caught a bug no test could: `requirements.txt` was missing three packages that happened to be installed on the dev machine.
