# How FINN scores people: three flows, three different questions

The network appears in three places, and each asks a different question, so each has its own scoring.
Scoring is deterministic: no model ranks anyone. Models are used for two things only: searching the
web and drafting a message. Every number below is shown to the investor as a sentence.

| Flow | The question | Engine | Where |
|---|---|---|---|
| 1. Pull after an update | Who understands *this event* best? If no one we know does, who out there does? | Cause-conditioned logistic ranker, then a verified web search with its own fit score | `network/recommend.py`, `network/discover.py`, `pull._cause` |
| 2. Push on an alert | What does the web add to this filing, and who should I call *today*? | Verified web context, then an expected-value-of-a-call ranker | `enrich.py`, `recommend.push_value`, `push._people_line` |
| 3. Grow and keep the network | Who am I losing touch with, and who matters most? | Rhythm-relative overdue × portfolio value × reason-to-call | `network/nudges.py` |

---

## Flow 1: pull after an update (cause-based ranking, web fallback)

### 1a. The cause

A dossier now takes the **latest material filing (last 45 days)** as its cause, skipping routine
paperwork and meeting notices. The cause has two halves:
- the **event type**: capex, M&A, management change and so on;
- the **subject** the filing names, extracted with `TOPIC_PATTERNS`. For example, "OSAT" maps to semiconductors and "pumped storage" to power & renewables.

### 1b. In-network ranking (pull engine)

**Stage 1: candidates.** The union of four independent sources:
1. **Career links:** worked at the company, a competitor, a customer or a supplier.
2. **Personalised PageRank** on a people–org–theme graph.
3. **Domain similarity:** time-decayed theme vectors, smoothed by theme co-occurrence.
4. **Text similarity:** TF-IDF.

**Stage 2: ranking.** `P(relevant) = σ(bias + Σ wᵢ·fᵢ)`, with these prior weights (learned per investor from "Helpful / Not relevant" taps):

| Feature | Weight | What it measures |
|---|---|---|
| insider | 2.6 | worked at the company; time-decayed after leaving |
| **topic** (new) | 2.2 | years doing the *specific subject* the filing names, credited per role: 1.0 if the job title names it, 0.7 if it's the employer's main business, 0.25 if a side business |
| adjacent | 1.8 | at a competitor, customer or supplier |
| domain | 1.5 | cosine similarity of theme vectors |
| function_fit | 1.3 | the person's function suits the event (e.g. M&A → strategy/banking/finance); **also 1.0 if they ran the named subject itself** |
| graph | 1.0 | PageRank proximity |
| text | 0.9 | wording match |
| warmth | 0.9 | how warm the tie is |
| recency | 0.6 | how recent their relevant experience is |
| seniority | 0.5 | seniority of the role |
| bias | −4.6 | |

**Cause rule.** When the filing names a subject that is not the company's main business (a new
venture), insider credit is discounted for insiders whose own role doesn't touch it:
`insider × (0.5 + 0.5 · max(own topic match, function fit))`.

**Stage 3: diversity.** MMR, so the top three don't all come from the same company.

**What this fixed.** On the Kaynes–Vishay OSAT joint venture, the sales VP used to rank above both
semiconductor-assembly specialists. Two causes:
- "worked there" outweighed knowing the subject;
- the first version of the topic feature credited anyone at Kaynes, because Kaynes lists semiconductors as a secondary theme.

Role-level credit fixed both. A broad sector theme (e.g. "specialty chemicals M&A") is deliberately
excluded from the function override; otherwise a plant head whose title mentions "phenolics" would
beat the M&A person.

| Recommender eval (28 graded queries) | NDCG@3 | MRR | Weak queries |
|---|---|---|---|
| Rules baseline | 0.977 | 0.958 | – |
| Before today | 0.992 | 0.982 | Kaynes OSAT |
| **Now** | **0.998** | **1.000** | none |

### 1c. When to go to the web

`coverage()` classifies the network's answer:
- **strong:** someone scores ≥ 0.8 with a direct link (insider ≥ 0.5, adjacent ≥ 0.4, or topic ≥ 0.5);
- **partial:** someone ≥ 0.5, but only through domain overlap;
- **none:** nobody.

The web is searched automatically unless coverage is strong. When it is strong, the dossier shows a
"Look beyond your network" button instead.

### 1d. Web people: search, verify, score

1. **Search.** One Sonnet call with the basic web-search tool, at most 3 searches. It is told the date, asked
   for current leadership separately from the subject, and must return one line per person with the URL that names them.
2. **Verify, in code.** The model is never trusted to check itself:
   - the URL must be one the search actually returned (this catches invented sources);
   - FINN fetches that page itself, with an honest user agent and caching, and the person's surname must appear on it (this catches invented or conflated names);
   - the organisation should appear too; if it doesn't, the evidence weight drops;
   - if the fetch is blocked, it falls back to the search result's headline, and the evidence weight drops again.
3. **Score** with a separate linear fit. There is no relationship to score, only how well the role fits the cause:

| Feature | Weight | |
|---|---|---|
| company | 0.30 | at the company now 1.0; formerly 0.7; at a competitor 0.6; customer or supplier 0.5. Matched by name when the company isn't in the network map. |
| topic | 0.25 | the same role-level subject match as above |
| function_fit | 0.20 | the event's function table; CEO or MD 0.7 for any event; 1.0 if the title names the subject |
| seniority | 0.10 | |
| evidence | 0.10 | name and organisation verified on the page 1.0; partial 0.7; ×0.6 if only the headline was checked |
| current | 0.05 | in the role now, × source freshness: 1.0 under 1.5 years, 0.6 under 4, 0.3 older. A 2013 article about an MD is not a current MD. |

The cut-off is a fit of 0.35. Each lead also gets an **intro route**: contacts who work or worked at
the same organisation, weighted by warmth. Anyone already in the network is reported as such, not
re-suggested.

**Live results (15 Nov 2026):**

| Cause | Kept | Rejected | Top lead |
|---|---|---|---|
| Titan (outside the network) | 1–3 | 1–2 | the current jewellery-division CEO; the old 2013 "MD" lead correctly scored down for staleness |
| Kaynes OSAT | 3 Kaynes Semicon leaders | 1 | every lead has an intro via Kiran |

Cost is about $0.05 per search, cached 7 days per cause and shared across users, since the facts are public.

---

## Flow 2: push (web context first, then who to call today)

### 2a. Web context for a filing

The scope is material and critical portfolio filings only. Simulated ones are never sent. After each refresh,
at most 6 filings are enriched, under a daily cap (`FINN_WEB_BUDGET_USD`, default $0.50). Results are cached
per filing forever, so the brief reads them for free. The cache is versioned, so a format change
refetches instead of showing half-formed data.

Each filing gets one search returning `FACT` and `PERSON` lines, each tied to a URL from the
results. Verification:
- **FACT:** every number in the sentence must appear on the fetched page (₹11,871 crore and 1,800 MW must both be there). With no numbers, at least 60% of its content words must. A fact that fails is dropped, not softened.
- **PERSON:** the same check as discovery. Prior roles are parsed, matched to the network's organisations, and turned into "Ask Ishaan, who worked at Tata Power until 2021".

**Sources: trusted first.**
- *Why:* most of India's established business press blocks Anthropic's crawler, and the search tool refuses those domains outright (a 400 error). That covers Economic Times, Mint, Moneycontrol, BusinessLine, Financial Express, CNBC-TV18 and NDTV Profit, plus Reuters, FT and WSJ. The open web therefore returned mostly blogs.
- *What:* each filing is searched first on exchanges, regulators, rating agencies and the press that allows it (Business Standard, Bloomberg, Business Today). The open web is a second, paid pass only when that finds fewer than 2 verified items.
- *Tier labels:* facts carry a tier (primary / press / other) and are sorted by it.

| Tata Power CEO change | Facts kept | Source tiers | Time |
|---|---|---|---|
| Open web | 4 of 4 | all "other" (blogs) | 29 s |
| **Trusted first** | 3 of 3 | 1 primary, 2 press | **12 s** |

### 2b. Who to call today (push engine)

Pull asks "who understands this best". An alert asks "who do I call **now**". So push ranks by the
expected value of one call, multiplied in **odds space**:

```
log-odds EV = logit(P can help) + ln(P picks up) + ln(freshness)
P can help  = the relevance model, without warmth or recency
P picks up  = 0.35 + 0.65 · warmth        (a dormant contact still answers sometimes)
freshness   = 0.60 + 0.40 · recency of their relevant experience
```

Only people scoring 0.5 or more are shown, at most 2 per alert. The UI shows the breakdown, for
example "can help 91% × likely to pick up 68% × knowledge fresh 100%".

**How this was arrived at.** The `modes` eval decided it:

| Push design | NDCG@3 (expert labels) | NDCG@3 (call-today labels) | Precision of what's shown | Shown | Dormant share |
|---|---|---|---|---|---|
| Pull engine used for alerts | 0.998 | 0.990 | 0.894 | 47 | 0.277 |
| v1: additive, warmth weight 2.6 | 0.976 | 0.973 | – | – | – |
| v2: product of probabilities | 0.966 | 0.960 | – | – | – |
| **v3: product in odds space** | **0.988** | 0.977 | **0.974** | **39** | **0.256** |

What each version showed:
- **v1** let a strong tie promote someone less relevant, and lost on both label sets.
- **v2** failed because relevance probabilities saturate (every qualified person sits at 0.95–0.99), so reachability swamped them.
- **v3** works in odds space, which caps the reachability effect at about one log-odds unit. Warmth can then only separate people of similar relevance.

On the surface that matters (2 names on a lock screen), push is **more precise**: 97% of the
people it shows are good calls, versus 89% from the pull engine, and it interrupts with fewer names.
The cost is 0.01 NDCG on full rankings, from near-ties flipping. The call-today labels are derived
(expert grade minus 1 for a dormant contact), so they show each mode does its own job; they are not
independent proof.

Feedback in push mode retrains only the relevance part. Reachability and freshness stay fixed and explainable.

---

## Flow 3: growing the network and keeping it warm

**Adding people.**
- *By hand:* name, role, company, how you know them, last spoke.
- *From a web lead:* saved as a `to-meet` tie with no last contact. It shows as "not yet contacted", and its warmth is half the tie's base.
- *Dedupe and privacy:* adding someone already in the network returns them instead of creating a duplicate. Contacts are private to the investor (row-level security), and a new company is created as shared reference data (it's a public name).
- *Logging a call:* "I spoke to them" moves the last-contact date, counts the interaction, and turns a lead into a contact.

**Nudges.**

```
cadence   = clamp(365 / (interactions in last 12 months + 1), 21 days, tie cap)
            tie caps: friend/mentor 60 · ex-colleague/founder 90 · classmate/expert 120 · met 180
            new lead: a 14-day first-contact window
overdue   = days since last contact / cadence
urgency   = 0 below 1×; 0.4 at 1×, rising to 1.0 at 3×
value     = strongest link to a holding: works there 1.0 · worked there 0.85 · competitor/customer 0.75 · same domain 0.5
            × (0.7 + 0.3 · weight / largest weight); research names count at 70%
hook      = a material filing in the last 14 days by their current (×1.0) or former (×0.8) employer,
            strength 0.4 at the materiality threshold, rising to 1.0 at the top of the scale
priority  = 0.50 · urgency + 0.30 · value + 0.20 · hook
due       = (overdue ≥ 1 and priority ≥ 0.40)  or  (hook ≥ 0.6 and overdue ≥ 0.6)  or  (new lead ≥ 7 days)
```

- **Why rhythm-relative.** A monthly contact quiet for 10 weeks is a signal. A once-a-year contact quiet for 10 weeks is not.
- **Why weight the hook.** "CFO appointed" is a better reason to call than "rumour verification". The first version counted every hook as 1.0, and almost every contact scored 0.8–0.96. Weighting by materiality spread priorities from 0 to 0.89.
- **Where it shows up.** The brief shows the top 3 nudges, and the network tab the top 6. Each has "I spoke to them" and "Draft a note" (Haiku; the contact's name never reaches the model, which sees `[NAME]`).
- **Phone pushes.** At most 2 nudges a week, enforced in the dispatcher itself, and never the same person twice within two weeks.

**What testing found and fixed:**
- **Lead window.** A new lead's 14-day window was stretched by the 21-day minimum, so leads never came due.
- **Weekly cap.** Lived only in the scheduler: a second dispatch sent two more people.
- **Silent model failures.** Drafts sometimes came back empty because the model's adaptive thinking used up a 200-token cap. Every fast-model call now gets at least 1,024 tokens. Only generated tokens are billed, so this costs nothing.

**Demo data.** 19 of the 24 demo contacts are due, because the seed data's last-contact dates are mostly 9+ months old. So the ranking and the caps (6 shown, 2 pushed a week) carry the weight.

---

## Testing the flows

| What | Command | Result |
|---|---|---|
| Scenario tests, deterministic (fake search response in the SDK's shape, fixture pages) | `python -m pytest -q tests/test_flows.py` | 24 pass |
| Whole suite | `python -m pytest -q` | 68 pass |
| Live flows (real search, real pages, real model; about $0.15) | `FINN_TEST_LLM=on python -m pytest -m live tests/test_flows_live.py -s` | 4 pass |
| Ranking quality, by mode | `python -m evals.run reco modes` | tables above |

**Cost per operation (measured):**

| Operation | Cost |
|---|---|
| People discovery | ~$0.05 per cause (cached 7 days) |
| Filing enrichment | ~$0.04–0.05 per filing (cached forever) |
| Draft note | ~$0.0001 |
| Nudges and both rankers | free (no model) |
