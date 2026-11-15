"""Evals: how good are FINN's judgement calls, measured against labelled truth.

    python -m evals.run                    # all suites
    python -m evals.run filings reco       # some suites

Tests check that code does what it was written to do. Evals check that the *decisions* are right,
and every suite is scored against a simpler baseline, so an improvement has to be earned:

  filings    filing triage vs 89 real filings hand-labelled with category + materiality
             baseline: classify by title only (no body escalation)
  reco       "who should I call" vs graded relevance judgements for 28 queries
             baseline: the original rule-based ranking (graph.Network.people_for_org)
  modes      pull (research) vs push (call today) ranking on the same queries: each should win its own job
  retrieval  transcript search: does a passage that answers the question land in the top 5?
             baseline: BM25 alone (no char n-grams, no Q&A boost, no fusion)
  agent      routing and planning: right entity, right tools, grounded answer, no invention
             (with a model key it also runs the model planner and a faithfulness judge)

Results go to evals/results/<timestamp>.json so runs can be compared over time.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data"


def _jsonl(name):
    return [json.loads(l) for l in (DATA / name).read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------------------
def eval_filings() -> dict:
    from finn import portfolio
    from finn.intel import classify as C

    gold = _jsonl("filings_gold.jsonl")
    rules = portfolio.DEFAULT_RULES
    weights = {h["ticker"]: h.get("weight") or 0 for h in portfolio.holdings()}

    def system(title, summary, body=True):
        cat, sub, base = C.classify(title, summary) if body else (C._first(C.clean_title(title)) or ("unclassified", "unknown", 10))
        v = C.score(cat, sub, base, weight=0, watched=set(rules["watch"]), muted=set(rules["mute"]))
        return cat, v.tier

    out = {}
    for name, body in (("system", True), ("baseline_title_only", False)):
        rows = [(g, *system(g["title"], g["summary"], body)) for g in gold]
        crit = [r for r in rows if r[0]["gold_tier"] == "critical"]
        mat = [r for r in rows if r[0]["gold_tier"] != "routine"]
        rout = [r for r in rows if r[0]["gold_tier"] == "routine"]
        flagged = [r for r in rows if r[2] != "routine"]
        out[name] = {
            "n": len(rows),
            "category_accuracy": round(sum(r[1] == r[0]["gold_category"] for r in rows) / len(rows), 3),
            "tier_accuracy": round(sum(r[2] == r[0]["gold_tier"] for r in rows) / len(rows), 3),
            "critical_recall": round(sum(r[2] == "critical" for r in crit) / len(crit), 3),         # missed news is the costliest error
            "material_recall": round(sum(r[2] != "routine" for r in mat) / len(mat), 3),             # anything that matters reaches the brief
            "noise_suppression": round(sum(r[2] == "routine" for r in rout) / len(rout), 3),          # routine stays folded
            "brief_precision": round(sum(r[0]["gold_tier"] != "routine" for r in flagged) / max(1, len(flagged)), 3),
        }
        if name == "system":
            out[name]["misses"] = [f"{r[0]['ticker']}: {r[0]['title'][:50]} (gold {r[0]['gold_tier']}, got {r[2]})"
                                   for r in rows if (r[0]["gold_tier"] != "routine") != (r[2] != "routine")][:8]
            out[name]["tier_confusion"] = dict(Counter(f"{r[0]['gold_tier']}->{r[2]}" for r in rows))
    return out


# ---------------------------------------------------------------------------
def _ndcg(ranked: list[str], grades: dict[str, int], k: int = 3) -> float:
    dcg = sum((2 ** grades.get(p, 0) - 1) / math.log2(i + 2) for i, p in enumerate(ranked[:k]))
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def eval_reco() -> dict:
    from finn.context import set_user
    from finn.network import graph
    from finn.network import recommend as R

    set_user("demo")
    net = graph.get()
    gold = json.loads((DATA / "reco_gold.json").read_text(encoding="utf-8"))
    res = {}
    weak: list[str] = []
    for name in ("system", "baseline_rules"):
        nd, p3, mrr = [], [], []
        for q in gold:
            if name == "system":
                ranked = [r["person"]["id"] for r in R.recommend(q.get("org"), q.get("theme"), q.get("event"), q.get("text", ""), k=5)]
            elif q.get("org"):
                ranked = [r["person"]["id"] for r in net.people_for_org(q["org"], limit=5, event=q.get("event"))]
            else:
                ranked = [r["person"]["id"] for r in net.expertise(q["theme"], limit=5)]
            g = q["grades"]
            if name == "system" and _ndcg(ranked, g) < 0.9:
                weak.append(f"{q.get('org') or q.get('theme')}/{q.get('event') or '-'}: got {ranked[:3]}, best {[p for p, v in g.items() if v == 3]}")
            nd.append(_ndcg(ranked, g))
            p3.append(sum(g.get(p, 0) >= 2 for p in ranked[:3]) / min(3, sum(v >= 2 for v in g.values()) or 1))
            best = [p for p, v in g.items() if v == 3]
            mrr.append(next((1 / (i + 1) for i, p in enumerate(ranked) if p in best), 0.0))
        res[name] = {"queries": len(gold), "ndcg@3": round(sum(nd) / len(nd), 3), "recall@3_of_good": round(sum(p3) / len(p3), 3),
                     "mrr_best": round(sum(mrr) / len(mrr), 3)}
    res["system"]["weak_queries"] = weak
    return res


# ---------------------------------------------------------------------------
def eval_retrieval() -> dict:
    import numpy as np

    from finn.agent import retrieval as RT

    gold = _jsonl("retrieval_gold.jsonl")

    def relevant(text, groups):
        t = text.lower()
        return any(all(k in t for k in grp) for grp in groups)

    res = {}
    for name in ("system_hybrid", "baseline_bm25"):
        hits, rr = [], []
        for q in gold:
            ix = RT.transcript_index(q["ticker"])
            if name == "system_hybrid":
                ranked = ix.search(q["q"], k=5)
            else:
                s = ix.bm25(RT._tokens(q["q"]))
                ranked = [ix.chunks[i] for i in np.argsort(-s)[:5] if s[i] > 0]
            flags = [relevant(c["text"], q["relevant_if"]) for c in ranked]
            hits.append(any(flags))
            rr.append(next((1 / (i + 1) for i, f in enumerate(flags) if f), 0.0))
        res[name] = {"questions": len(gold), "recall@5": round(sum(hits) / len(hits), 3), "mrr": round(sum(rr) / len(rr), 3)}
    res["note"] = "Relevance is keyword-defined (silver labels): a passage counts if it contains every term of any listed group."
    return res


# ---------------------------------------------------------------------------
def eval_agent() -> dict:
    from finn.agent import core, memory
    from finn.context import set_user
    from finn.intel import llm

    set_user("eval")
    from finn import store

    store.seed_user("eval")
    cases = _jsonl("agent_cases.jsonl")
    rows, threads = [], {}
    for c in cases:
        events: list = []
        tid = threads.get(c.get("thread"))
        t0 = time.time()
        st = core.run(c["q"], tid, None, events.append)
        ms = (time.time() - t0) * 1000
        if c.get("thread"):
            threads[c["thread"]] = st["thread"]["id"]
        tools = [s["tool"] for s in (st.get("plan") or [])]
        exp = set(c.get("tools", []))
        ok_entity = (c.get("ticker") is None or st.get("ticker") == c["ticker"]) and (c.get("person") is None or st.get("person") == c["person"])
        answer = st["answer"]["answer"]
        unanswerable_ok = (not c.get("unanswerable")) or ("couldn't find" in answer.lower() and not st.get("evidence"))
        row = {"q": c["q"], "entity_ok": ok_entity, "tool_recall": (len(exp & set(tools)) / len(exp)) if exp else 1.0,
               "grounded": st["verification"]["ok"], "no_invention": unanswerable_ok, "ms": round(ms), "tools": tools}
        if c.get("statement"):
            row["tool_recall"] = min(row["tool_recall"], float(any(s["args"].get("statement") == c["statement"] for s in st.get("plan") or [])))
        rows.append(row)
    n = len(rows)
    out = {"mode": "model" if llm.enabled() else "rules (no model key)", "cases": n,
           "entity_accuracy": round(sum(r["entity_ok"] for r in rows) / n, 3),
           "tool_recall": round(sum(r["tool_recall"] for r in rows) / n, 3),
           "grounded_rate": round(sum(r["grounded"] for r in rows) / n, 3),
           "no_invention_on_unanswerable": round(sum(r["no_invention"] for r in rows) / n, 3),
           "p50_ms": sorted(r["ms"] for r in rows)[n // 2],
           "failures": [r for r in rows if not (r["entity_ok"] and r["tool_recall"] == 1 and r["grounded"] and r["no_invention"])]}
    return out


def eval_modes() -> dict:
    """Pull vs push ranking: what each mode is for, measured.

    pull_gold   the expert relevance labels (who understands this best)
    call_today  derived labels for 'who to call today': the same grades, minus one for a contact you
                haven't spoken to in over 10 months (dormant). Derived, not independent: a check that
                each mode does the job it was designed for, not proof that the trade-off is right.
    Expect: pull wins on pull_gold, push wins on call_today, and push gives up little relevance.
    """
    from finn.context import set_user
    from finn.network import graph
    from finn.network import recommend as R

    set_user("demo")
    net = graph.get()
    gold = json.loads((DATA / "reco_gold.json").read_text(encoding="utf-8"))
    dormant = {pid for pid in net.contacts if net.warmth(pid)["label"] == "dormant"}
    res: dict = {}
    for mode in ("pull", "push"):
        a, b, top_dormant, shown, good, cold = [], [], 0, 0, 0, 0
        for q in gold:
            ranked = [r["person"]["id"] for r in R.recommend(q.get("org"), q.get("theme"), q.get("event"), q.get("text", ""), k=5, mode=mode)]
            g = q["grades"]
            call = {p: max(0, v - (1 if p in dormant else 0)) for p, v in g.items()}
            a.append(_ndcg(ranked, g))
            b.append(_ndcg(ranked, call))
            top_dormant += bool(ranked) and ranked[0] in dormant
            # the push surface itself: at most 2 people, only above the 0.5 gate (as push._people_line)
            surf = [r["person"]["id"] for r in R.recommend(q.get("org"), q.get("theme"), q.get("event"), q.get("text", ""), k=2, min_score=0.5, mode=mode)]
            shown += len(surf)
            good += sum(g.get(p, 0) >= 2 for p in surf)
            cold += sum(p in dormant for p in surf)
        res[mode] = {"ndcg3_pull_gold": round(sum(a) / len(a), 3), "ndcg3_call_today": round(sum(b) / len(b), 3),
                     "top_pick_dormant": f"{top_dormant}/{len(gold)}",
                     "surface_precision": round(good / max(1, shown), 3), "surface_shown": shown,
                     "surface_dormant": round(cold / max(1, shown), 3)}
    top = lambda q, m: [r["person"]["id"] for r in R.recommend(q.get("org"), q.get("theme"), q.get("event"), q.get("text", ""), k=1, mode=m)]  # noqa: E731
    res["reordered_top1"] = f"{sum(top(q, 'pull') != top(q, 'push') for q in gold)}/{len(gold)}"
    return res


SUITES = {"filings": eval_filings, "reco": eval_reco, "modes": eval_modes, "retrieval": eval_retrieval, "agent": eval_agent}


def main(argv: list[str]) -> None:
    os.environ.setdefault("FINN_SCHEDULER", "off")
    from finn import store

    store.init()
    picked = argv or list(SUITES)
    results = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "suites": {}}
    try:
        results["git"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() or None
    except Exception:
        results["git"] = None
    for s in picked:
        t0 = time.time()
        results["suites"][s] = SUITES[s]()
        results["suites"][s]["_seconds"] = round(time.time() - t0, 1)
        print(f"\n== {s}")
        for k, v in results["suites"][s].items():
            if isinstance(v, dict):
                print(f"  {k:22s} " + "  ".join(f"{kk}={vv}" for kk, vv in v.items() if not isinstance(vv, (list, dict))))
                for kk, vv in v.items():
                    if isinstance(vv, (list, dict)) and vv:
                        print(f"  {'':22s} {kk}: {vv}")
            elif isinstance(v, list):
                for item in v:
                    print(f"  ! {item}")
            else:
                print(f"  {k:22s} {v}")
    out = HERE / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved {out.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main(sys.argv[1:])
