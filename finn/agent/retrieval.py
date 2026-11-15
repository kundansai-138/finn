"""Retrieval over primary documents (concall transcripts, annual reports).

Chunking: by meaning, not by character count.
  - Transcripts are split into speaker turns. An analyst's question and management's answer
    become ONE chunk ("Q: … A: …"), because the question is what makes the answer findable
    ("what's the margin outlook?" retrieves the answer that never repeats the word "margin").
  - Long answers are windowed (~220 words, 40-word overlap); each window keeps its question.
  - Annual reports are windowed per page (they have no speakers).
  - Every chunk keeps call, page and speaker, so every retrieved fact is citable.

Retrieval: hybrid, fused with Reciprocal Rank Fusion.
  - BM25 over word tokens, with finance-aware query expansion (margin → EBITDA, OPM…).
  - TF-IDF over character n-grams: robust to PDF extraction damage ("g rowth", "quart er")
    that silently breaks word matching.
  - Boosts: newer calls, management answers over boilerplate.
  - MMR de-duplication so overlapping windows of the same answer don't crowd the results.
Dense embeddings would slot in as a third RRF list (e.g. Voyage) with no other changes.

Everything is cached: chunks per document forever (filings are immutable), indexes in memory.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from typing import Any

import numpy as np

from .. import store
from ..intel.guidance import _ANALYST, _TURN, _fix, transcripts
from ..sources import documents

STOP = set("""a an the and or of to in on for with at by from as is are was were be been being it its this that these those we our
you your they their he she his her i me my us so but if then than there here what which who whom how when where why do does did
done have has had having will would shall should can could may might must not no yes also just very really like about into over
under up down out off more most less some any all each per sir madam thank thanks okay ok please question yeah well actually
call conference transcript page limited ltd""".split())
# Letterhead lines look like "Name:" turns to a naive parser ("Tel.: 0294…", "Encl.: As above").
NOT_SPEAKER = re.compile(r"^(tel|fax|encl|email|e-mail|website|web|sub|subject|ref|date|note|cin|regd|phone|mobile|isin|scrip code|symbol|dear|to|from|place|kind attn|attn|disclaimer|safe harbour|safe harbor|note to readers)\.?$", re.I)

SYNONYMS = {
    "margin": ["ebitda", "opm", "operating", "profitability"],
    "margins": ["ebitda", "opm", "operating", "profitability"],
    "revenue": ["sales", "topline", "turnover"],
    "sales": ["revenue", "topline"],
    "growth": ["grow", "growing", "yoy", "increase"],
    "guidance": ["guide", "guided", "outlook", "expect", "target"],
    "outlook": ["guidance", "expect", "visibility"],
    "capex": ["capital", "expenditure", "capacity", "investment", "plant"],
    "debt": ["borrowing", "borrowings", "leverage", "gearing"],
    "demand": ["volume", "volumes", "offtake", "orders", "order"],
    "profit": ["pat", "earnings", "bottomline"],
    "pat": ["profit", "earnings"],
    "cash": ["cashflow", "fcf", "receivables", "inventory"],
    "working": ["inventory", "receivables", "debtor", "payables"],
    "competition": ["competitor", "competitors", "pricing", "share"],
    "pricing": ["price", "prices", "realisation", "realization"],
    "costs": ["cost", "raw", "material", "input"],
    "export": ["exports", "international", "overseas", "global"],
}


def _tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())
    out = []
    for t in toks:
        if t in STOP or len(t) < 2:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def chunk_transcript(pages: list[str], call: str, url: str, ticker: str) -> list[dict[str, Any]]:
    full = "\n".join(f"\n[[p{i + 1}]]\n{p}" for i, p in enumerate(pages))
    analysts = {m.group(1).strip().lower() for m in _ANALYST.finditer(_fix(full))}
    marks = list(_TURN.finditer(full))
    turns = []
    for k, m in enumerate(marks):
        speaker = m.group(1).strip()
        body_raw = full[m.end(): marks[k + 1].start() if k + 1 < len(marks) else len(full)]
        pm = [int(x.group(1)) for x in re.finditer(r"\[\[p(\d+)\]\]", full[: m.end()])]
        page = pm[-1] if pm else 1
        body = _fix(re.sub(r"\[\[p\d+\]\]", " ", body_raw)).strip()
        if NOT_SPEAKER.match(speaker) or (len(speaker.split()) == 1 and speaker.endswith(".")):
            continue
        sl = speaker.lower()
        role = "moderator" if sl == "moderator" else "analyst" if any(a in sl or sl in a for a in analysts if len(a) > 3) else "management"
        if len(body.split()) < 8:
            continue
        turns.append({"speaker": speaker, "role": role, "page": page, "text": body})

    chunks: list[dict[str, Any]] = []
    last_q = None
    for t in turns:
        if t["role"] == "moderator":
            continue
        if t["role"] == "analyst":
            last_q = t
            continue
        if t["page"] == 1 and not last_q:  # page 1 is the exchange cover letter
            continue
        words = t["text"].split()
        q_prefix = ""
        if last_q:
            qw = last_q["text"].split()
            q_prefix = f"Q ({last_q['speaker']}): {' '.join(qw[:70])}{'…' if len(qw) > 70 else ''}\n"
        step, size = 180, 220
        for s in range(0, max(1, len(words)), step):
            window = " ".join(words[s: s + size])
            if len(window.split()) < 15 and s > 0:
                break
            chunks.append({
                "ticker": ticker, "doc": "transcript", "call": call, "url": url, "page": t["page"],
                "speaker": t["speaker"], "qa": bool(last_q),
                "text": f"{q_prefix}A ({t['speaker']}): {window}" if q_prefix else f"{t['speaker']}: {window}",
            })
            if s + size >= len(words):
                break
        last_q = None
    for i, c in enumerate(chunks):
        c["id"] = f"{ticker}:{call}:{i}"
    return chunks


def chunk_pages(pages: list[str], doc: str, label: str, url: str, ticker: str) -> list[dict[str, Any]]:
    chunks = []
    for pno, p in enumerate(pages, 1):
        words = _fix(p).split()
        if len(words) < 40:
            continue
        for s in range(0, len(words), 200):
            w = words[s: s + 240]
            if len(w) < 30:
                break
            chunks.append({"ticker": ticker, "doc": doc, "call": label, "url": url, "page": pno, "speaker": None,
                           "qa": False, "text": " ".join(w), "id": f"{ticker}:{doc}:{pno}:{s}"})
    return chunks


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
class HybridIndex:
    def __init__(self, chunks: list[dict[str, Any]], recency: dict[str, float] | None = None):
        self.chunks = chunks
        self.recency = recency or {}
        self.docs = [_tokens(c["text"]) for c in chunks]
        self.df = Counter(t for d in self.docs for t in set(d))
        self.N = len(self.docs)
        self.avgdl = (sum(len(d) for d in self.docs) / self.N) if self.N else 1
        self.tf = [Counter(d) for d in self.docs]
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=1) if chunks else None
        self.CX = self.char.fit_transform([c["text"].lower() for c in chunks]) if chunks else None

    def bm25(self, q: list[str], k1: float = 1.4, b: float = 0.75) -> np.ndarray:
        s = np.zeros(self.N)
        for t in q:
            n = self.df.get(t)
            if not n:
                continue
            idf = math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for i, tf in enumerate(self.tf):
                f = tf.get(t)
                if f:
                    s[i] += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(self.docs[i]) / self.avgdl))
        return s

    def search(self, query: str, k: int = 6) -> list[dict[str, Any]]:
        if not self.N:
            return []
        base = _tokens(query)
        expanded = base + [s for t in base for s in SYNONYMS.get(t, [])]
        lex = self.bm25(base) + 0.35 * self.bm25([t for t in expanded if t not in base])
        cha = (self.CX @ self.char.transform([query.lower()]).T).toarray().ravel()
        # Reciprocal Rank Fusion of the two retrievers
        rrf = np.zeros(self.N)
        for scores in (lex, cha):
            order = np.argsort(-scores)
            for rank, i in enumerate(order[:60]):
                if scores[i] > 0:
                    rrf[i] += 1 / (60 + rank)
        boost = np.array([self.recency.get(c["call"], 1.0) * (1.08 if c["qa"] else 1.0) for c in self.chunks])
        final = rrf * boost
        # MMR-style de-dup: skip windows from the same speaker turn on the same page
        out, seen = [], set()
        for i in np.argsort(-final):
            if final[i] <= 0 or len(out) >= k:
                break
            c = self.chunks[i]
            key = (c["call"], c["page"], c.get("speaker"))
            if key in seen:
                continue
            seen.add(key)
            out.append({**c, "score": round(float(final[i]) * 1000, 2)})
        return out


@lru_cache(maxsize=32)
def transcript_index(ticker: str) -> HybridIndex:
    from .. import companies

    c = companies.get(ticker)
    calls = transcripts(c, 4)
    chunks: list[dict[str, Any]] = []
    recency = {}
    for rank, call in enumerate(calls):
        key = f"{ticker}:{call['transcript']}"
        cached = store.chunks_get(key)
        if cached is None:
            pages = documents.pdf_pages(call["transcript"], ticker=ticker, kind="transcript", label=call.get("period")) or []
            cached = chunk_transcript(pages, call.get("period") or "", call["transcript"], ticker)
            store.chunks_put(key, cached)
        chunks += cached
        recency[call.get("period") or ""] = 1.15 - 0.05 * rank
    return HybridIndex(chunks, recency)


@lru_cache(maxsize=8)
def annual_report_index(ticker: str) -> HybridIndex:
    from .. import companies

    c = companies.get(ticker)
    ars = c.get("documents", {}).get("annual_reports", [])
    if not ars:
        return HybridIndex([])
    ar = ars[0]
    key = f"{ticker}:{ar['url']}"
    cached = store.chunks_get(key)
    if cached is None:
        pages = documents.pdf_pages(ar["url"], max_pages=220, ticker=ticker, kind="annual_report", label=str(ar.get("year"))) or []
        cached = chunk_pages(pages, "annual_report", f"Annual Report {ar.get('year')}", ar["url"], ticker)
        store.chunks_put(key, cached)
    return HybridIndex(cached)
