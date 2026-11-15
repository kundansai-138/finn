# FINN — design notes

Working notes behind the prototype. The covering email will be a short version of this.

## 1. What the brief is really asking

Read past the feature list and the brief tests four things:

1. **Filtering.** "The scarce skill is not aggregating information, it's filtering it." Most announcements are paperwork, so the product's job is to decide what to drop. FINN has to make that call and also show why it made it.
2. **The network as a lens.** The network should not live in a separate app. It should change what Push and Pull show: "CFO resigned" becomes "CFO resigned, the incoming one overlapped four years at HUL with Ananya, your ex-colleague."
3. **Analysis, not summaries.** The worked example (promised vs. delivered over four quarters) is the clearest hint of what they value. Restating what management said is easy. Holding management to what it said, using only primary sources, is the valuable part.
4. **Judgment about cost and architecture.** Use a cheap model where it is good enough, a strong model only where reasoning needs it, cache everything, and keep personal data handling deliberate.

The bar: thin but working beats broad but broken, and a demo that doesn't run is the worst outcome.

## 2. Point of view: how an investor should use this

**A brief, not a feed.** An investor uses FINN in short gaps: before the open, between meetings, before a call. A feed that never ends works against them. FINN's home screen is a **finite, ranked brief that ends**: "3 things matter. 41 filings, 38 routine. You're caught up."

Three interaction ideas carry the product:

| Idea | What it means in the UI |
|---|---|
| **Signal dial** | One control: *Critical / Material / Everything*. Filtering is something the user can see and adjust, not a hidden algorithm. Routine filings are collapsed into one counted line, never deleted. |
| **Every signal ends in a person** | Each material card ends with *who to call and why*: the path (You → Ananya → new CFO), the link strength, and the reason it matters for this event. |
| **Ask bar, everywhere** | One input pinned to the bottom. A ticker opens a dossier. "Who knows specialty chemicals?" queries the network. "Who can introduce me to Bajaj Finance's CFO?" returns paths. Pull and direct network queries use the same input. |

Card types have their own shapes, so the reader learns them once and then skims:

- **Results**: a 4×2 grid (Revenue / EBITDA / PBT / PAT × QoQ / YoY) plus one line on what drove it.
- **Management change**: a mini-bio of the incoming person (from the filing's mandatory "brief profile") plus how your network connects to them.
- **Material event** (M&A, capex, litigation): the best-placed person in your network and why.
- **Price / volume alert**: a sparkline with the move marked and volume shown as a multiple of its 20-day average.
- **Everything else**: one line, or just the filing link, at its priority.

**Pull is a dossier with a ledger as the hero.** The first thing you see for a company is the **Promises ledger**: each piece of guidance from the last four concalls, what happened next, and a verdict (*Delivered / Missed / Pending / Quietly dropped*). Each entry cites the transcript. The supporting numbers (snapshot, trajectory, balance sheet, cash quality, bull vs. bear) follow and are computed deterministically from the financials.

## 3. Data: what's real and what's mocked

| Source | Used for | How |
|---|---|---|
| Yahoo Finance (`yfinance`) | EOD price and volume | Batch download for the portfolio; 20-day average volume and daily % move computed locally |
| NSE public RSS (`Online_announcements.xml`) | Live announcements feed | Polled; the symbol is parsed from the PDF filename; accumulated in SQLite |
| screener.in company page | Per-company recent announcements, standardized financials, annual report and concall transcript links | Fetched politely (honest UA, rate limited, cached for 24h) |
| BSE / NSE PDF archives | Concall transcripts, results filings | Downloaded on demand, text extracted with `pypdf`, cached by hash |
| **Synthetic** | The investor's network (people, careers, ties) | Fictional people with realistic careers at real companies; demo scenario events are marked **Simulated** in the UI |

NSE's and BSE's JSON APIs sit behind bot protection, so FINN doesn't use them. It uses the public RSS feed and the document links screener publishes. In production: exchange data-vendor feeds, plus XBRL results filings, which are structured and need no LLM to parse.

## 4. Architecture

```
            ┌──────────── EOD + 5-min jobs (cron) ────────────┐
 yfinance ─►│ prices.py   ─┐                                   │
 NSE RSS  ─►│ nse_rss.py  ─┼─► SQLite (snapshot) ─► push.py ───┼─► /api/brief
 screener ─►│ screener.py ─┘        ▲               (classify,  │
            └───────────────────────┼────────────── score,      │
                                    │               enrich)     │
 PDFs ─► documents.py ─► text cache ┤                           │
                                    ├─► dossier.py ─────────────┼─► /api/company/{t}
 network.json ─► network/graph.py ──┘   (numbers: deterministic │
                 (overlaps, paths,       guidance: LLM)         │
                  tie strength)  ─────────────────────────────── ─► /api/ask, /api/people
                                                                     │
                                         web/ (Preact, no build) ◄───┘
```

**Snapshot first.** Jobs write a snapshot and the API serves it, so the demo works even when an upstream source is down or blocks cloud IPs. Pull on an arbitrary ticker fetches live and caches the result.

### Where LLMs are used, and where they aren't

| Step | Model | Why |
|---|---|---|
| Classifying announcements | **Rules first** (regex over exchange subjects); Haiku only for the unclassified remainder | Exchange subjects are semi-structured; most of the volume needs no model |
| One-line summary of a material filing | Haiku 5.5, `effort: low` | Bulk, short, cheap; cached by document hash |
| Bio extraction from appointment filings | Haiku 5.5, structured output | Extraction, not reasoning |
| Guidance extraction per transcript | Haiku 5.5, structured output, per-chunk | Bulk extraction; each transcript processed once, forever |
| Promised-vs-delivered verdicts, bull/bear | Opus 5.5 | Real reasoning over numbers plus quotes; one call per company per quarter |
| Ask-bar intent parsing, follow-up rewriting, memory extraction and summaries | Haiku 5.5 | Short, structured, high volume |
| Agent planning | Opus 5.5, `effort: low` | Choosing tools is light reasoning; one call per question, cached |
| Agent answer | Opus 5.5, `effort: medium`, 1M context | Synthesis over evidence; history prefix prompt-cached across turns |
| Web search step | Sonnet 5.5 + server-side `web_search` | Fresh context, labelled secondary |
| People recommendations, routes, evidence graphs | **No LLM** | Recommender: graph walk + similarity + learned ranking |
| Grounding check | **No LLM** | Figures checked against evidence by code |
| Results numbers, ratios, trajectory, cash quality | **No LLM** | Arithmetic; a model would only add error |
| Network paths and tie strength | **No LLM** | Graph search plus explainable scoring |

All LLM calls go through `finn/intel/llm.py`: model routing, a disk cache keyed by `sha256(model + prompt)`, prompt caching for long transcripts, and a cost ledger. Without an API key every LLM step has a deterministic fallback, so the app still runs.

### The Network as a recommender system

"Who should I call?" is a recommendation problem: rank people for a query (a company, a theme, an event). FINN uses the two-stage shape of production feed and recommendation systems, scaled down to one investor ([finn/network/recommend.py](../finn/network/recommend.py)):

1. **Candidate generation.** Four independent sources are unioned, so no single weak signal hides a good person:
   - *Structural:* insiders, competitors, customers, suppliers.
   - *Graph walk:* personalised PageRank on a people–company–theme graph seeded at the query (Pinterest's Pixie idea). It finds multi-hop relevance: Meera → UPL → agrochemicals → PI.
   - *Domain similarity:* time-decayed, seniority-weighted career vectors over themes, smoothed by theme co-occurrence. This is item-item similarity: agrochemicals and specialty chemicals share companies, so experience in one partly counts for the other.
   - *Text similarity:* TF-IDF between the filing or question and each profile.
2. **Ranking.** A logistic model over nine explainable features: worked there, competitor side, domain, graph proximity, topic match, function fit for the event type, recency, seniority, warmth.
   - The weights start from priors.
   - They learn online from the investor's *Helpful / Not relevant* taps (SGD with an L2 pull back towards the prior, so a few taps nudge the model rather than overturn it).
   - It learns which *kinds* of match you value, not just which people.
3. **Re-ranking.** MMR diversity, so the top three aren't three people from one company.

Every recommendation carries its feature contributions and an **evidence graph** (You → person → bridge company or theme → target), drawn in the UI. Routes to a *person* (e.g. an incoming CFO) get the same graph. No model calls anywhere in this layer: it's fast, private and fully explainable.

### Ask FINN: the conversational agent

Push and Pull answer once, and investors then want to ask "why?". Ask FINN is a plan-and-execute agent ([finn/agent/](../finn/agent/)) on a small state-graph runtime:

```
understand ─► plan ─► execute (parallel tools) ─┬─► answer ─► verify ─┬─► remember
               ▲                                │                     │
               └──── replan (nothing found) ◄───┘          repair ◄───┘ (unsupported figures, once)
```

- **Why plan-and-execute and not a free ReAct loop:**
  - one planning call, tools in parallel, one answer call;
  - the transcript isn't re-sent on every tool hop;
  - the plan is visible before any answer is written, and the UI streams the graph live.
- **Tools** return *evidence* tagged by source type: primary, computed, market, network or secondary.
  - Tools: company overview, financial statements, the promises ledger, transcript search, annual-report search, filings, prices, who-can-help (the recommender), routes to a person, web search, read a URL.
  - Results are cached per (tool, args) with TTLs that match the source.
- **Retrieval** ([retrieval.py](../finn/agent/retrieval.py)):
  - transcripts are chunked by meaning: an analyst's question and management's answer form one chunk, with call, page and speaker kept;
  - hybrid search: word-level BM25 with finance-aware query expansion, plus character n-gram TF-IDF, which is robust to PDF extraction damage like "g rowth";
  - the two are fused with Reciprocal Rank Fusion, then boosted for recency and Q&A, then de-duplicated.
- **Memory**, in three layers:
  - the thread: recent turns verbatim, older ones compacted into a rolling summary;
  - focus: the company, person and theme in play, so "and their debt?" resolves;
  - long-term investor facts ("I care most about cash conversion"), which the investor can see and delete.
- **Hallucination controls:**
  - the answer model sees only evidence;
  - every claim cites evidence ids;
  - a deterministic **verifier** checks every figure against the evidence (allowing for rounding) and flags uncited figures and dangling citations;
  - a failed check triggers one repair pass;
  - the result is shown as a badge, e.g. "✓ 16/16 figures traced to sources".
- **Primary vs secondary:** the brief forbids news and secondary sources in Pull analysis. Web search is still available, for very recent events, people's backgrounds or when asked. It runs as Anthropic's server-side tool, its results are tagged *Secondary · web*, and the answer prompt bars resting financial analysis on them.
- **No key, still works:** a rule planner plus an extractive answer that quotes the most relevant sentences, clearly labelled. It never produces a guessed narrative.
- **Why not LangGraph or LangChain:** the graph is six nodes and two loops, about 60 lines of runtime. The libraries would add a large dependency tree and a layer between FINN and the SDK features it relies on: prompt-cache breakpoints, structured outputs, server-side web search and refusal fallbacks.

### Privacy and the network

- **The network belongs to the investor, enforced by the database.**
  - Every personal table carries `user_id`, and Postgres row-level security admits only the requesting user's rows.
  - The app connects as a non-superuser, so the policies bind it too.
  - A test bypasses the application entirely and proves raw SQL as another user sees nothing and can't write.
- **Pseudonymised model prompts:** before the agent's answer prompt leaves the server, the investor's contacts become `Contact-1`, `Contact-2`; names are restored in the reply. Public people (executives named in filings) stay named. This is enforced by a test.
- Profile data comes only from sources the investor can legitimately use: their own contacts and LinkedIn data export (consent: their own relationships), public filings (appointees' "brief profile" is a mandatory disclosure), or a licensed people-data provider. No scraping of LinkedIn.
- Retention: enrichment refreshed every 90 days, and a contact's derived data is deleted when the contact is removed. LLM calls are logged with a hash of the prompt, not its contents.

### Accounts, notifications and editable scoring

- **Who is asking** ([finn/auth.py](../finn/auth.py)):
  - a session cookie (HttpOnly, SameSite=Lax, 30 days; only its SHA-256 is stored, so a leaked table can't be replayed);
  - else, in development, the `X-FINN-User` header;
  - else the guest demo.
  - `FINN_AUTH_STRICT=on` drops the header.
- **Passwords:** stdlib scrypt with a per-user salt, in a table under the same row-level security as everything else. Five failed sign-ins per username per 10 minutes, then a 429.
- **Alerts follow the account.**
  - A device's push subscription is stored under whoever is signed in.
  - Signing in or out on a device moves the registration and clears cached API responses.
  - The scheduler pushes to every user with a registered device, each run in that user's own database scope, so one user's failure never blocks another.
- **The radar is the investor's own.**
  - Add any NSE-listed company (as a holding with a weight, or watch-only), or remove one.
  - The shared refresh fetches filings for every ticker anyone holds. It keeps only the set of tickers, nothing linking them to who holds them.
- **Scoring starts from FINN's defaults and is editable per investor** ([finn/scoring.py](../finn/scoring.py)). There are four sections, matching the four engines in [SCORING.md](SCORING.md).
  - Only differences from the defaults are stored, so a better default still reaches everyone who hasn't changed that value.
  - Values are clamped to their ranges.
  - Weights that are shares of a whole are normalised.
  - Saving a ranking section replaces what FINN had learned from that investor's feedback taps: the edit becomes the new starting point, and later taps learn from there.

## 5. Database: why PostgreSQL

FINN's data has three shapes: relational personal data that must be isolated per user, reference market data (time series, documents, chunks), and caches.

| Option | Verdict | Why |
|---|---|---|
| **PostgreSQL 16** (+ pgvector, pg_trgm) | **Chosen** | One engine covers everything the product needs. See the list below. |
| SQLite | Dev fallback only | One writer at a time (refresh job, agent and API write concurrently). A file on a PaaS disk vanishes on redeploy. Can't be shared by two instances. No RLS. |
| MongoDB | No | The data is relational (people–roles–companies, user–contacts). No RLS-style isolation. Vectors would mean a proprietary Atlas tie-in. |
| Neo4j (graph) | No | The network is small per user (hundreds of nodes) and is ranked in memory anyway. A second database means a second security boundary for the most sensitive data. |
| Dedicated vector DB (Pinecone, Qdrant) | Not yet | pgvector keeps embeddings under the same row-level security as the documents. A separate store would need its own access control. |
| Redis | Later, if needed | Response caches are fine in Postgres at this scale. |

What PostgreSQL 16 gives FINN in one engine:
- **Row-level security** for the network (the brief's privacy requirement);
- **concurrent writers** under MVCC;
- **JSONB** for parsed snapshots;
- **full-text search** (a GIN index on transcript chunks);
- **trigram** fuzzy name matching;
- **pgvector** for embeddings in the same transaction and security boundary;
- managed offerings with free tiers, backups and branching.

**Where it runs:**
- **Local:** `docker compose up -d db`, running the `pgvector/pgvector:pg16` image. An init script creates the plain `finn_app` role and a separate `finn_test` database.
- **Production:** Neon, managed serverless Postgres 16 with pgvector and a free tier. Its *branching* gives every CI run or eval an isolated copy of the database. Supabase is the alternative if we later want its built-in auth.

**Schema** ([finn/store.py](../finn/store.py)):
- **reference tables:** companies, eod_prices, announcements, documents, doc_pages, doc_chunks, orgs, people, roles, education;
- **personal tables:** users, holdings, contacts, alert rules, chat threads and messages, memory facts, recommender weights and feedback, push subscriptions, notifications sent, dismissed items;
- **caches:** key-value.

Private contact profiles are namespaced by owner, so two investors who know the same person never overwrite each other.

## 6. Mobile-first

FINN is a PWA, so it installs to the home screen and runs full-screen. The brief explicitly allows responsive web. A native wrapper (Expo/Capacitor) is a packaging step on top of the same app, not a rewrite.

| Phone behaviour | How |
|---|---|
| Installable, opens instantly, works offline | manifest, icons, service worker. The app shell is served from cache. The brief falls back to the last good copy, labelled "Offline · from 08:42". The cache version is a hash of the assets, so each release reaches phones on the next open. |
| **Push = real notifications** | Web Push (VAPID, payload encrypted end to end). Only critical filings in watched categories and your price/volume triggers buzz the phone, once each, carrying the network angle: "Ananya can introduce you." On iOS this needs the app on the Home Screen (16.4+). |
| Thumb-first interactions | swipe a card when handled (saved server-side, so every device agrees; undo for 5s); pull to refresh; drag sheets down to close; the ask bar and tabs within thumb reach |
| Physical-device hygiene | 44px minimum tap targets; 16px inputs (no iOS zoom); notch and home-indicator safe areas; reduced-motion respected; charts drawn at true pixel size so 11px stays 11px on a 320px phone |

The mobile audit ([evals/mobile_audit.js](../evals/mobile_audit.js)) checks overflow, tap targets, text size and zoom-triggering inputs on every screen at 320/375/390/412px. All clean.

### Screens: point at the next step, not at everything

The first version showed every fact on every card (one CFO-change card carried a timeline, four routes and three footer links). The redesign keeps the same data, but every screen leads with one thing to look at and one action:

| Tab | Leads with | Depth on tap |
|---|---|---|
| **Radar** (home) | Every holding as a tile sized by weight, ringed when it filed something that matters (dashed: moved with no filing). Below it, one row per flagged holding: what happened and the one person to call. | The holding |
| **Holding** | Three panes, in the order an investor thinks: *Updates* (the latest material filing and "your best move"), *Condition* (three things to know, four numbers, a question worth asking), *Network* (who can explain it, each with a Draft button) | Promises, growth, cash and bull/bear fold away |
| **Research** | One box for a ticker or a question, then the research list and recent names | Dossier or Ask FINN |
| **People** | A to-do list, verb first ("Ask Ananya to introduce you to Siddharth Kapadia"): news-driven calls, then keep-in-touch nudges, one row per person. Then the network as an orbit (closer = warmer), then a searchable directory | Profile sheet |

**Message drafter** ([finn/network/drafter.py](../finn/network/drafter.py)): pick what you want (intro, their view, catch up, introduce yourself) and the channel (WhatsApp, email, LinkedIn's 300-character note). The draft uses only facts FINN can show, and lists them under the text ("How you know Ananya…", "Last spoke 3 months ago"). With a model key, the fast model writes it from those facts and never sees a real name; without one, templates. FINN never sends: it copies the text or opens the investor's own app, then offers to log the contact.

**Colour has one job each.** Greys for everything; amber only for "this needs you" (rings, the best move, the one primary button per screen); green and red only on numbers; simulated data is a dashed outline, never a colour. Dark ("night desk") is the home palette, and light mode keeps the same roles.

## 7. Deployment

- **Image:** a Docker image ([Dockerfile](../Dockerfile)). It runs as a non-root user, with two uvicorn workers by default. **Exactly one process runs the scheduler**, elected with a Postgres advisory lock; the rest only serve requests, and if the leader dies another takes over. This was verified with two containers sharing one database.
- **App host:** Render ([render.yaml](../render.yaml)) in the Singapore region, near India, with a health check and secrets set in the dashboard.
- **Database:** Neon. [scripts/copy_reference.py](../scripts/copy_reference.py) seeds the already-fetched public data, so the first request works even if a cloud IP is blocked by a source.
- **GitHub Actions:**
  - **CI** runs every test against a real `pgvector:pg16` service, plus the network-free evals.
  - **refresh** wakes the free-tier app on market hours and after the close.
- **Production setup checklist:** Neon `DATABASE_URL`, `ANTHROPIC_API_KEY`, VAPID keys (`python -m scripts.vapid_keys`), `FINN_ADMIN_TOKEN`; then GitHub secrets `FINN_URL` and `FINN_ADMIN_TOKEN` for the refresh workflow.

## 8. Deliberately out of scope (for now)

- Real-time intraday data.
- Single sign-on (Google, Microsoft) and password reset by email: accounts are username and password, created in the app or with `scripts/create_user.py`.
- A separate worker process and queue for ingestion.
- Segment-level revenue mix.
- Embeddings in pgvector: the table and extension are ready, waiting on an embedding provider.

Testing and evaluation: see [TESTING.md](TESTING.md).
