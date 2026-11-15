"""Ask FINN: a plan-and-execute research agent with a grounding check.

    understand ─► plan ─► execute ─┬─► answer ─► verify ─┬─► remember ─► END
                   ▲               │                      │
                   └── replan ◄────┘ (nothing found)      └─► repair ─► verify (once)

Why plan-and-execute instead of a free ReAct loop:
  - one planning call, then tools run in parallel, then one answer call: fewer model round-trips,
    and a growing transcript isn't re-sent on every tool call
  - the plan is visible and auditable before any answer is written
  - deterministic tools do the work; the model only decides which to call and writes the prose

Hallucination controls:
  - the answer model sees only evidence (no open-ended "what do you know about X")
  - every claim cites evidence ids; figures are checked against the evidence by code
  - financial analysis must rest on primary sources; web results are labelled secondary
  - with no model available, the agent still runs: a rule planner plus an extractive answer
    that quotes the sources (clearly labelled), never a guessed narrative
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import ask as router
from .. import store
from ..sources import universe
from ..config import LLM_ENABLED, MODEL_AGENT
from ..intel import llm
from ..network import graph as netgraph
from . import memory, tools, verify
from .runtime import END, StateGraph

log = logging.getLogger(__name__)

EVENTS = {
    "M&A": r"acqui|merger|takeover|deal|joint venture|jv|stake",
    "capex": r"capex|capacity|new plant|expansion",
    "management_change": r"cfo|ceo|managing director|appoint|resign|new (?:md|head|chief)",
    "results": r"results|quarter|earnings",
    "litigation": r"litigation|penalt|regulator|lawsuit|tax demand",
}
INTENTS = {
    "numbers": r"revenue|sales|margin|ebitda|profit|\bpat\b|growth|debt|borrow|cash|roce|roe|valuation|p/?e\b|working capital|inventory|receivable|numbers|financial|quarter|results|eps|leverage",
    "guidance": r"guid|promis|target|outlook|deliver|commit|expect|said they|management said",
    "commentary": r"\bwhy\b|reason|explain|management|commentary|concall|earnings call|transcript|strategy|segment|demand|pricing|competition|plan|order book|ramp|capacity|jv|joint venture|customer",
    "people": r"\bwho\b|know|intro|expert|network|contact|talk to|speak to|should i call|reach",
    "filings": r"filing|announce|disclos|board|appoint|acqui|latest|recent|this week|today",
    "price": r"\bprice\b|stock|share price|\bmove|fell|rose|volume|rally|drop|crash",
    "annual_report": r"annual report|related part|contingent|auditor|risk factor|subsidiar",
    "web": r"\bweb\b|internet|\bnews\b|google|search online|online|what's happening|outside",
}


# ---------------------------------------------------------------------------
# Node: understand (entity + intent resolution, follow-up handling)
# ---------------------------------------------------------------------------
REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "standalone": {"type": "string", "description": "The question rewritten to stand alone (resolve 'they', 'it', 'that')"},
        "ticker": {"type": ["string", "null"], "description": "NSE symbol if a company is in question"},
        "wants_web": {"type": "boolean"},
    },
    "required": ["standalone", "ticker", "wants_web"], "additionalProperties": False,
}


def understand(s: dict, emit) -> dict:
    q = s["question"]
    t = s["thread"]
    net = netgraph.get()
    focus = dict(t.get("focus") or {})
    pinned = t.get("pinned") or {}
    # Company: the investor's own universe first, then any NSE-listed name ("Titan's", "Navin Fluorine")
    ticker = router._find_company(q, net) or universe.resolve(q)
    person = router._find_person(q, net)
    if ticker and router.NEW_ROLE.search(q):  # "the new CFO of Deepak Nitrite" -> the appointee
        for r in store.ann_for_ticker(ticker, 5, category="management_change"):
            pid = json.loads(r["extra"] or "{}").get("person")
            if pid and pid in net.people:
                person = pid
                break
    theme = router._find_theme(q, net)
    followup = not (ticker or person or theme)
    standalone = q
    if followup and LLM_ENABLED and t["messages"]:
        last = t["messages"][-2:]
        res = llm.complete(
            "rewrite_followup",
            "Conversation so far:\n" + "\n".join(f"{m['role']}: {memory._strip_cites(m['content'])[:400]}" for m in last)
            + f"\nFocus company: {focus.get('ticker')}\nNew question: {q}\nRewrite the new question so it stands alone.",
            tier="fast", schema=REWRITE_SCHEMA, max_tokens=200)
        if res:
            standalone = res["standalone"] or q
            ticker = ticker or (res.get("ticker") or None)
    ticker = ticker or (focus.get("ticker") if followup else None) or pinned.get("ticker")
    person = person or (focus.get("person") if followup else None)
    intents = {k for k, rx in INTENTS.items() if re.search(rx, standalone, re.I)}
    event = next((ev for ev, rx in EVENTS.items() if re.search(rx, standalone, re.I)), None)
    if not intents:
        intents = {"commentary"} if ticker else {"people"} if (theme or person) else set()
    focus.update({k: v for k, v in {"ticker": ticker, "person": person, "theme": theme}.items() if v})
    bits = [f"company {ticker}" if ticker else None, f"person {net.people[person]['name']}" if person else None,
            f"theme {theme}" if theme else None]
    s["_detail"] = ("Focus: " + ", ".join(b for b in bits if b) if any(bits) else "No specific entity") + \
                   (f" · intents: {', '.join(sorted(intents))}" if intents else "") + (" · follow-up resolved" if followup and ticker else "")
    return {"standalone": standalone, "focus": focus, "intents": intents, "ticker": ticker, "person": person,
            "theme": theme, "event": event}


# ---------------------------------------------------------------------------
# Node: plan
# ---------------------------------------------------------------------------
ARG_KEYS = ["ticker", "statement", "query", "theme", "event", "person", "url", "days"]
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "args": {"type": "object", "properties": {k: {"type": ["string", "null"]} for k in ARG_KEYS},
                         "required": ARG_KEYS, "additionalProperties": False},
                "why": {"type": "string"},
            },
            "required": ["tool", "args", "why"], "additionalProperties": False}},
        "answer_style": {"type": "string", "enum": ["brief", "table", "comparison", "list"]},
    },
    "required": ["steps", "answer_style"], "additionalProperties": False,
}

PLANNER_SYSTEM = """You plan research for FINN, an agent serving a portfolio manager investing in Indian listed companies.
Given a question, choose the smallest set of tool calls (1-5) that gathers the evidence to answer it. Tools run in parallel.
Rules:
- Prefer primary tools (financials, transcripts, filings, annual report). Financial analysis must rest on them.
- Use web_search only for what primary sources can't give (very recent events, a person's background, industry context) or when asked.
- Use who_can_help / route_to_person whenever the question is about people, or when a contact could add colour.
- Tickers are NSE symbols (e.g. DIXON, PIIND). Use the focus company for follow-ups.
- Don't call a tool whose answer is already in the conversation summary.
- Set unused args to null."""


def _rule_plan(s: dict) -> list[dict]:
    t, it, q = s.get("ticker"), s.get("intents") or set(), s["standalone"]
    steps: list[dict] = []

    def add(tool, **args):
        if tool in tools.TOOLS and (not tools.TOOLS[tool].needs_model or LLM_ENABLED):
            steps.append({"tool": tool, "args": args, "why": ""})

    if s.get("person"):
        add("route_to_person", person=netgraph.get().people[s["person"]]["name"])
    if t:
        if "numbers" in it:
            stmt = ("ratios" if re.search(r"working capital|inventory|receivable|debtor|payable|days|roce", q, re.I) else
                    "balance_sheet" if re.search(r"debt|borrow|leverage|balance", q, re.I) else
                    "cash_flow" if re.search(r"cash", q, re.I) else
                    "annual" if re.search(r"year|annual|cagr|trend|decade", q, re.I) else "quarterly")
            add("company_overview", ticker=t)
            add("financials", ticker=t, statement=stmt)
        if "guidance" in it:
            add("promises", ticker=t)
        if it & {"guidance", "commentary"}:
            add("search_transcripts", ticker=t, query=q)
        if "annual_report" in it:
            add("search_annual_report", ticker=t, query=q)
        if "filings" in it:
            add("filings", ticker=t, query=q)
        if "price" in it:
            add("price", ticker=t)
        if "people" in it and not s.get("person"):
            add("who_can_help", ticker=t, query=q, event=s.get("event"))
        if not steps:
            add("company_overview", ticker=t)
            add("search_transcripts", ticker=t, query=q)
    elif s.get("theme") and not s.get("person"):
        add("who_can_help", theme=s["theme"], query=q)
    elif "filings" in it:
        add("filings", query=q, days="7")
    if "web" in it:
        add("web_search", query=q)
    seen, out = set(), []
    for st in steps:
        k = (st["tool"], json.dumps(st["args"], sort_keys=True))
        if k not in seen:
            seen.add(k)
            out.append(st)
    return out[:5]


def plan(s: dict, emit) -> dict:
    t = s["thread"]
    steps, style, source = None, "brief", "rules"
    if LLM_ENABLED:
        hist = "\n".join(f"{m['role']}: {memory._strip_cites(m['content'])[:300]}" for m in t["messages"][-4:])
        prompt = (f"Tools:\n{tools.catalog()}\n\nInvestor's standing preferences:\n{memory.facts_block()}\n\n"
                  f"Conversation summary: {t.get('summary') or '(new conversation)'}\nRecent turns:\n{hist or '(none)'}\n\n"
                  f"Focus: {json.dumps(s.get('focus'))}\nPinned context: {json.dumps(t.get('pinned') or {})}\n"
                  + (f"Note: {s['replan_note']}\n" if s.get("replan_note") else "")
                  + f"\nQuestion: {s['standalone']}")
        res, _ = llm.call("agent_plan", model=MODEL_AGENT, effort="low", max_tokens=1500, schema=PLAN_SCHEMA,
                          system=PLANNER_SYSTEM, messages=[{"role": "user", "content": prompt}],
                          cache_key=hashlib.sha1(prompt.encode()).hexdigest())
        if res:
            steps = [st for st in res["steps"] if st["tool"] in tools.TOOLS and st["tool"] in {x.name for x in tools.available()}][:5]
            style, source = res.get("answer_style", "brief"), "model"
    if not steps:
        steps, source = _rule_plan(s), "rules"
    for i, st in enumerate(steps, 1):
        st["id"] = f"s{i}"
        st["args"] = {k: v for k, v in (st.get("args") or {}).items() if v not in (None, "", "null")}
    emit({"type": "plan", "steps": [{k: st[k] for k in ("id", "tool", "args", "why")} for st in steps], "source": source})
    s["_detail"] = f"{len(steps)} step{'s' if len(steps) != 1 else ''} ({'planned by model' if source == 'model' else 'rule planner'})"
    return {"plan": steps, "style": style, "plan_source": source}


# ---------------------------------------------------------------------------
# Node: execute (parallel, cached)
# ---------------------------------------------------------------------------
def execute(s: dict, emit) -> dict:
    steps = s.get("plan") or []

    def run(st):
        emit({"type": "step", "id": st["id"], "tool": st["tool"], "status": "start"})
        try:
            ev = tools.run(st["tool"], st["args"])
            emit({"type": "step", "id": st["id"], "tool": st["tool"], "status": "done", "n": len(ev)})
            return st, ev, None
        except Exception as e:
            emit({"type": "step", "id": st["id"], "tool": st["tool"], "status": "error", "error": str(e)[:160]})
            return st, [], str(e)

    # Each tool runs in a copy of the caller's context, so it reads this user's data.
    ctxs = [contextvars.copy_context() for _ in steps]
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda pair: pair[0].run(run, pair[1]), zip(ctxs, steps)))
    evidence = list(s.get("evidence") or [])
    for st, ev, err in results:
        for e in ev:
            evidence.append({**e, "id": f"E{len(evidence) + 1}", "step": st["id"], "tool": st["tool"]})
    emit({"type": "evidence", "items": [{k: e.get(k) for k in ("id", "title", "source", "cite", "url", "text", "tool", "graph", "person")} for e in evidence]})
    srcs = sorted({e["source"] for e in evidence})
    s["_detail"] = f"{len(evidence)} evidence items ({', '.join(srcs)})" if evidence else "No evidence found"
    return {"evidence": evidence}


def after_execute(s: dict) -> str:
    if not s.get("evidence") and s.get("replans", 0) < 1 and s.get("plan_source") == "model":
        s["replans"] = s.get("replans", 0) + 1
        s["replan_note"] = "The previous plan returned no evidence. Try different tools or broader queries."
        return "plan"
    return "answer"


# ---------------------------------------------------------------------------
# Node: answer (model) or extractive fallback
# ---------------------------------------------------------------------------
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Markdown. Lead with the answer in 1-2 sentences, then support. Cite [E#] after every factual sentence."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "follow_ups": {"type": "array", "items": {"type": "string"}, "description": "Up to 3 natural next questions"},
    },
    "required": ["answer", "confidence", "follow_ups"], "additionalProperties": False,
}

ANSWER_SYSTEM = """You are FINN, a research agent for a portfolio manager investing in Indian listed companies. You answer from evidence gathered by tools, never from memory.

Grounding rules (hard):
1. Every sentence that states a fact ends with its evidence ids, e.g. [E2] or [E2, E5].
2. Copy figures from the evidence. You may round them. Don't derive new figures unless you show the inputs, each cited.
3. Analysis of a company's business or financials must rest on primary or computed evidence. Evidence marked SECONDARY (web) may add context, introduced as "per <source>".
4. If the evidence doesn't answer the question, say exactly what's missing. Never fill gaps with outside knowledge.
5. People recommendations come only from network evidence; say why each person fits.

Style: lead with the answer in one or two sentences, then at most five tight bullets. Say "₹ cr" for crore. No preamble or hedging boilerplate. Write for someone who reads fast."""


def _evidence_pack(evidence: list[dict]) -> str:
    out = []
    for e in evidence:
        tag = {"secondary": "SECONDARY", "network": "NETWORK", "market": "MARKET", "computed": "COMPUTED"}.get(e["source"], "PRIMARY")
        out.append(f"[{e['id']}] ({tag}; {e.get('cite')})\n{e['title']}\n{e['text']}")
    return "\n\n".join(out)


def _best_sentences(text: str, query: str, n: int = 2) -> str:
    from .retrieval import _tokens

    qt = set(_tokens(query))
    sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", re.sub(r"^Q \(.*?\):.*?\nA \(.*?\):\s*", "", text, flags=re.S)) if len(x.split()) > 5]
    if not sents:
        return text[:240]
    ranked = sorted(sents, key=lambda x: -len(qt & set(_tokens(x))))[:n]
    keep = [x for x in sents if x in ranked]  # preserve original order
    return " ".join(keep)[:420]


def _extractive(s: dict) -> dict:
    ev = s.get("evidence") or []
    q = s["standalone"]
    if not ev:
        return {"answer": "I couldn't find evidence for that in FINN's sources. Try naming a company (e.g. DIXON), a theme, or a person.",
                "confidence": "low", "follow_ups": []}
    lines = ["*Model offline: these are the most relevant passages from the sources, not a written analysis.*", ""]
    if "people" in (s.get("intents") or set()):  # a people question leads with people
        ev = sorted(ev, key=lambda e: e["source"] != "network")
    for e in ev[:7]:
        body = _best_sentences(e["text"], q) if e["tool"] in ("search_transcripts", "search_annual_report", "web_search", "read_url") else e["text"][:420].replace("\n", "; ")
        lines.append(f"- **{e['title']}**: {body} [{e['id']}]")
    t = s.get("ticker")
    fu = [f"What did {t}'s management guide for this year?", f"Who in my network can explain {t}?", f"How good is {t}'s cash conversion?"] if t else []
    return {"answer": "\n".join(lines), "confidence": "medium", "follow_ups": fu[:3]}


def pseudonymizer(evidence: list[dict]):
    """Replace the investor's private contacts' names with stable aliases before text leaves for the
    model, and map them back afterwards. Public people (executives named in filings) stay named:
    they're already public. Full names first, then first names, on word boundaries."""
    names: dict[str, str] = {}
    for e in evidence:
        p = e.get("person") or {}
        if p.get("is_contact") and p.get("name") and p["name"] not in names:
            names[p["name"]] = f"Contact-{len(names) + 1}"
    pairs = sorted(names.items(), key=lambda kv: -len(kv[0]))
    firsts = {n.split()[0]: a for n, a in names.items() if len(n.split()[0]) > 2}
    back = {a: n for n, a in names.items()}

    def hide(text: str) -> str:
        for n, a in pairs:
            text = text.replace(n, a)
        for f, a in firsts.items():
            text = re.sub(rf"\b{re.escape(f)}\b", a, text)
        return text

    def show(text: str) -> str:
        return re.sub(r"Contact-\d+", lambda m: back.get(m.group(0), m.group(0)), text)

    return hide, show


def answer(s: dict, emit) -> dict:
    ev = s.get("evidence") or []
    out, mode = None, "extractive"
    if LLM_ENABLED and ev:
        t = s["thread"]
        system = ANSWER_SYSTEM + f"\n\nInvestor's standing preferences (tailor emphasis to these):\n{memory.facts_block()}"
        if t.get("summary"):
            system += f"\n\nEarlier in this conversation: {t['summary']}"
        repair = ""
        if s.get("verification") and not s["verification"]["ok"]:
            v = s["verification"]
            repair = (f"\n\nYour previous draft failed the grounding check. Unsupported figures: {v['unsupported']}. "
                      f"Uncited sentences: {v['uncited']}. Unknown citations: {v['unknown_citations']}. "
                      f"Rewrite it using only the evidence; drop anything you can't cite.\nPrevious draft:\n{s['answer']['answer']}")
        user = (f"Evidence:\n{_evidence_pack(ev)}\n\nQuestion: {s['standalone']}\nPreferred format: {s.get('style', 'brief')}{repair}")
        msgs = memory.history_for_model(t) + [{"role": "user", "content": user}]
        # Privacy: the investor's contacts are named only on our side. The model sees "Contact-2".
        hide, show = pseudonymizer(ev)
        system, msgs = hide(system), [{**m, "content": hide(m["content"])} for m in msgs]
        res, _ = llm.call("agent_answer", model=MODEL_AGENT, effort="medium", max_tokens=3000, schema=ANSWER_SCHEMA,
                          system=system, messages=msgs, cache_key=hashlib.sha1(msgs[-1]["content"].encode()).hexdigest())
        if res:
            res = {**res, "answer": show(res["answer"]), "follow_ups": [show(f) for f in res.get("follow_ups", [])]}
            out, mode = res, "model"
    if out is None:
        out = _extractive(s)
    emit({"type": "answer", "text": out["answer"], "confidence": out["confidence"], "follow_ups": out.get("follow_ups", [])[:3], "mode": mode})
    s["_detail"] = "written by model" if mode == "model" else "extractive (model offline)"
    return {"answer": out, "answer_mode": mode}


# ---------------------------------------------------------------------------
# Node: verify (+ one repair pass)
# ---------------------------------------------------------------------------
def verify_node(s: dict, emit) -> dict:
    v = verify.check(s["answer"]["answer"], s.get("evidence") or [])
    emit({"type": "verify", **v})
    s["_detail"] = (f"{v['supported']}/{v['figures']} figures traced to sources" if v["figures"] else "no figures to check") + \
                   ("" if v["ok"] else f" · {len(v['unsupported']) + len(v['uncited'])} issue(s)")
    return {"verification": v}


def after_verify(s: dict) -> str:
    v = s["verification"]
    if not v["ok"] and LLM_ENABLED and s.get("answer_mode") == "model" and s.get("repairs", 0) < 1:
        s["repairs"] = s.get("repairs", 0) + 1
        return "answer"
    return "remember"


# ---------------------------------------------------------------------------
# Node: remember (thread, focus, long-term facts, compaction)
# ---------------------------------------------------------------------------
def remember(s: dict, emit) -> dict:
    t = s["thread"]
    t["focus"] = s.get("focus") or t.get("focus") or {}
    t["messages"].append({"role": "user", "content": s["question"]})
    t["messages"].append({"role": "assistant", "content": s["answer"]["answer"], "meta": {
        "evidence": [{k: e.get(k) for k in ("id", "title", "source", "cite", "url", "text", "tool", "graph", "person")} for e in s.get("evidence") or []],
        "verification": s.get("verification"), "plan": [{k: st[k] for k in ("id", "tool", "args")} for st in s.get("plan") or []],
        "mode": s.get("answer_mode"), "confidence": s["answer"].get("confidence"), "follow_ups": s["answer"].get("follow_ups", []),
        "trace": s.get("trace", [])}})
    t["title"] = t.get("title") or s["question"][:60]
    learned = memory.learn_from(s["question"])
    memory.compact(t)
    memory.save(t)
    if learned:
        emit({"type": "memory", "learned": learned})
    s["_detail"] = f"thread saved ({len(t['messages']) // 2} turns)" + (f" · learned: {learned[0][:40]}" if learned else "")
    return {}


# ---------------------------------------------------------------------------
GRAPH = (StateGraph("ask-finn")
         .add_node("understand", understand, "Understand")
         .add_node("plan", plan, "Plan")
         .add_node("execute", execute, "Gather evidence")
         .add_node("answer", answer, "Answer")
         .add_node("verify", verify_node, "Verify")
         .add_node("remember", remember, "Remember"))
GRAPH.add_edge("understand", "plan").add_edge("plan", "execute")
GRAPH.add_branch("execute", after_execute, {"answer": "evidence found", "plan": "nothing found: replan"})
GRAPH.add_edge("answer", "verify")
GRAPH.add_branch("verify", after_verify, {"remember": "grounded", "answer": "unsupported figures: repair"})
GRAPH.add_edge("remember", END)


def run(question: str, thread_id: str | None, context: dict | None, emit) -> dict:
    t = (memory.get(thread_id) if thread_id else None) or memory.new_thread(pinned=context)
    if context and not t.get("pinned"):
        t["pinned"] = context
    emit({"type": "thread", "id": t["id"]})
    trace: list[dict] = []

    def tap(ev: dict) -> None:
        if ev["type"] == "node" and ev["status"] != "start":
            trace.append({k: ev.get(k) for k in ("node", "label", "status", "ms", "detail")})
        emit(ev)

    state = {"question": question, "thread": t, "trace": trace}
    GRAPH.run(state, tap)
    emit({"type": "done", "thread_id": t["id"]})
    return state
