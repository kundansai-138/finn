# Testing FINN: correctness, quality, performance

Three different questions, three different tools:

| Question | Tool | Command |
|---|---|---|
| Does the code do what it was written to do? | **Tests** (pytest, 96 tests, real Postgres; 4 more live) | `python -m pytest -q` |
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
| Agent | invented tools dropped; an ungrounded draft is repaired; tool plans; price forecasts and open-ended asks are declined with no tool or model call; a confident rule plan skips the planner; dates aren't figures, and a derived figure passes only when its inputs are shown beside it and the arithmetic checks; leverage computed with its inputs | hallucination control |
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
| Agent with a model key (cold: response cache off) | same 12 cases, real planner and answer model | entity 1.0, tool recall 1.0, grounded **1.0**, no invention **1.0**; p50 **9.7 s**, p90 13.8 s; **$0.020 per question**; 0 repair passes | same harness on the code before these fixes, run straight after: 1.0 / 0.875 / 0.917 / 0.833; p50 22.5 s, p90 35.8 s; $0.057 per question; 5 repair passes |

**What the evals found** (that's what they're for):
- Title-only triage misses a third of the critical filings, because companies bury news under "General Updates". Reading the body fixes it.
- **The first recommender eval was too easy:** both systems scored ~1.0, so it measured nothing. Harder queries, where the event type should change the answer, now separate them.
- They also exposed a real weakness: for an OSAT joint venture at Kaynes, the sales insider outranked the two semiconductor-assembly specialists, because "worked there" outweighed topic expertise. Fixed with cause-conditioned ranking (role-level topic credit; insider discount when a filing names a subject the company isn't mainly in); see [SCORING.md](SCORING.md).
- The `modes` suite rejected two push designs before the third: an additive warmth weight lost on both label sets, and a product of probabilities let reachability swamp saturated relevance scores. Multiplying in odds space won where it matters, on the two names an alert shows.
- Hybrid retrieval doesn't find more passages than BM25 here, but it ranks the right one higher (MRR +0.08).

**The agent suite with a model key.** The first run (15 Nov 2026) scored entity 1.0, tool recall 0.875, grounded 0.917, no invention on unanswerables **0.833**, p50 24 s. The eval now runs cold (response cache off, so a rerun can't replay the last run's answers) and records time per graph node, answer passes, the first draft's grounding failures, and spend from the run's own ledger rows. That showed where the problems were:
- **Invention on unanswerables.** "What will the Sensex do tomorrow?" was answered from web market outlooks, and "Tell me something interesting" from the week's filings. The planner reached for *some* tool, and the answer model wrote from whatever came back. Fix: a deterministic unanswerable route in `understand`. Price or index forecasts, and questions naming no company, person, theme, topic or the portfolio, get an empty plan: no tool runs, no model is called, and a fixed decline offers questions FINN can ground. With nothing gathered, there's nothing to invent from. The forecast pattern is kept narrow and pinned by tests both ways: "what will Dixon's revenue be" is a guidance question; "will Dixon's stock recover" is declined.
- **Ungrounded drafts.** 5–7 of 12 first drafts failed the grounding check, each costing a second Opus call. There were three causes:
  - the model derived ratios itself (Laurus debt-to-equity);
  - the verifier read dates ("Nov 10–15", "12 Nov") as figures;
  - it couldn't check sound arithmetic shown in the answer ("PBT ex other income ₹460 cr (₹532 cr − ₹72 cr)").

  The fixes:
  - the balance-sheet tool now returns leverage as *computed* evidence, with its inputs shown;
  - dates aren't figures;
  - a figure that is the sum, difference, ratio or % change of two traced figures *in the same sentence* is checked as arithmetic.

  A derived figure without its inputs, or with wrong arithmetic, still fails. First drafts that need repair: 5 → 0 in the paired run.
- **Latency.** The p50 went from 22.5 s to 9.7 s (27.0 → 9.7 s in an earlier pair). Where the time went:
  - Planning (3–10 s of Opus) is skipped when the rule planner is confident: one resolved entity, an explicit intent, no comparison, at most 18 words. On these cases the rule planner already had tool recall 1.0 against the model's 0.875.
  - Repairs (8–15 s each) mostly disappeared, and a repair that does happen runs at low effort.
  - Learning investor facts (a 1–2 s Haiku call) now runs alongside the turn instead of after it.
  - Declines take 0.1 s.

  What's left is one Opus answer call at medium effort, 6–17 s. That's the next lever, but cutting it trades away answer depth, which no suite here scores yet.
- **Trade-off.** A confident single-entity question no longer gets the model planner's judgement. For example, it won't add a web search for news around a price move (the rule plan uses price + filings, primary sources only). Comparisons, long or multi-part questions, questions without an explicit topic, and replans still go to the model, and an empty rule plan now gets one model replan. This eval has no comparison or multi-part cases, so it doesn't measure what the model planner adds there; those cases should be added next.
- **Cost.** Per question, from the ledger rows behind `GET /api/metrics` → `llm`: $0.057 → $0.020. The calls behind it went from 12 planner, 17 answer and 2 web-search calls to 1 planner and 10 answer calls. Cache reads fell from 57% to 30% of input tokens. That's a share, not a regression: the removed planner and web calls were the cache-heavy ones, and per answer call the cache reads are unchanged (~0.7k tokens).

Results: `evals/results/20261115-165018.json` (after) and `20261115-165514.json` (before, same harness, run straight after). Re-run on the sectioned first-person answer format ("**Bottom line:**", "#### " sections, "What I'd watch"): still 1.0 on all four, no repairs, p50 9.7 s, p90 17.0 s, $0.023 per question. The longer answers cost ~18% more output tokens, mostly at the slow end (`20261115-170547.json`).

**Still to build with a model key:** two more suites.
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
