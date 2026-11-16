# AI build history

FINN was built with Claude Code (desktop app, Claude Opus) between 13 and 16 November 2026. These are the full transcripts of every session that worked on it, in the order they started.

| # | Session | When (UTC) | Tool calls | What happened |
|---|---|---|---|---|
| 1 | [Main session, part 1: initial build](1-main-session-part1-initial-build.zip) | 13–15 Nov | 716 | Read the brief and planned the product. Built Push, Pull and the Network layer, then reworked people-matching around domain and career similarity. Added the Postgres database, backend and installable frontend, created the GitHub repo, and wrote test scenarios for the network flows. |
| 2 | [Network contact scoring](2-network-contact-scoring.zip) | 15 Nov | 26 | Walked through how Network picks the best person to call when a company is researched. Concluded that each of the three flows needs its own scoring ([docs/SCORING.md](../docs/SCORING.md)). |
| 3 | [Frontend UX redesign](3-frontend-ux-redesign.zip) | 15 Nov | 160 | Turned an info dump into a short, ranked view of what needs a look. Portfolio radar on open, a People section, and research answers structured around the filing they come from. |
| 4 | [Fix Ask FINN invention and latency](4-fix-ask-finn-invention-and-latency.zip) | 15 Nov | 137 | A parallel session started from the main one. Stopped Ask FINN answering questions it can't know, and cut its response time, measured with a real model key. |
| 5 | [Agent and database architecture](5-agent-database-architecture.zip) | 15 Nov | 22 | How the agent talks to the database, and whether MCP, LangGraph or LangChain would add anything. |
| 6 | [API key authentication error](6-api-key-auth-error.zip) | 15 Nov | 24 | Diagnosed the 401 from the model API: the key was the wrong type. |
| 7 | [Main session, part 2: deploy, accounts, bring-your-own-key](7-main-session-part2-deploy-accounts-byok.zip) | 15–16 Nov | 599 | Deployed to Render and Neon with a test user. Added accounts, notifications, user-added companies and people, and editable scoring. Moved every model call to the visitor's own key for any provider, and fixed Gemini model selection. |

The main session ran out of context once and continued in a new transcript, which is why it comes in two parts.

## What's in each zip

- `transcript.jsonl`: the whole conversation, one JSON event per line, including every prompt, every reply, and every tool call with its output.
- Subagent transcripts, where the session used subagents.
- `tool-results/`: screenshots taken while checking the app in a browser.
- `metadata.json`, where present: session title and timing from the desktop app.

## Redactions and what's left out

- In part 2, the production database password was printed once while setting up Neon. It is replaced with `REDACTED`. API keys were already masked when they were printed, and no other secrets appear.
- Not included: a side-chat fork of part 1 (almost entirely a copy of it), and the short session that packaged these files.
