"""Agent memory, in three layers:

  1. Thread (short-term): the conversation. The last few turns go to the model verbatim; older
     turns are compacted into a rolling summary, so context stays small however long the chat.
  2. Focus (working memory): the company, person and theme currently under discussion, so
     "and their debt?" resolves to the right ticker without asking.
  3. Investor facts (long-term): durable preferences ("I care about cash conversion",
     "I'm underweight chemicals"), extracted from what the investor says and reused in every
     future thread. Viewable and deletable: the investor owns their memory.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any

from .. import store
from ..intel import llm

KEEP_TURNS = 6  # messages sent verbatim; older ones are summarised


def new_thread(pinned: dict | None = None) -> dict[str, Any]:
    t = {"id": uuid.uuid4().hex[:12], "created": time.time(), "title": None, "summary": "",
         "focus": {}, "pinned": pinned or {}, "messages": []}
    store.thread_create(t)
    return t


def get(tid: str) -> dict[str, Any] | None:
    """Only the owner's threads resolve; another user's thread id returns None."""
    return store.thread_get(tid)


def save(t: dict[str, Any]) -> None:
    store.thread_save(t)


def _strip_cites(s: str) -> str:
    return re.sub(r"\s*\[E\d+(?:,\s*E?\d+)*\]", "", s)


def history_for_model(t: dict[str, Any]) -> list[dict[str, str]]:
    """Recent turns verbatim (citations stripped: evidence ids are per-turn)."""
    msgs = [{"role": m["role"], "content": _strip_cites(m["content"])} for m in t["messages"][-KEEP_TURNS:]]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def compact(t: dict[str, Any]) -> None:
    """Fold turns that fall out of the verbatim window into the rolling summary."""
    old = t["messages"][:-KEEP_TURNS]
    done = t.get("summarised_upto", 0)
    fresh = old[done:]
    if not fresh:
        return
    text = "\n".join(f"{m['role']}: {_strip_cites(m['content'])[:600]}" for m in fresh)
    summary = llm.complete(
        "memory_summary",
        f"Existing summary:\n{t.get('summary') or '(none)'}\n\nNew turns:\n{text}\n\n"
        "Update the summary in at most 120 words: companies, people, numbers and conclusions discussed, open questions.",
        tier="fast", max_tokens=300)
    if not summary:  # no model: keep the first line of each turn
        summary = (t.get("summary", "") + " " + " | ".join(f"{m['role']}: {_strip_cites(m['content']).split('. ')[0][:140]}" for m in fresh)).strip()[-1200:]
    t["summary"] = summary
    t["summarised_upto"] = len(old)


# ---------------------------------------------------------------------------
# Long-term investor facts
# ---------------------------------------------------------------------------
_PREF = re.compile(r"\b(?:remember(?: that)?|note that|fyi)\s+(.+)|\bi (?:prefer|care (?:most )?about|focus on|hold|own|am (?:over|under)weight(?:ed)? (?:on|in)?|avoid|don't like|like)\s+(.+)", re.I)

FACT_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {"type": "string"},
                             "description": "Durable facts about the investor's preferences, positions or process. Empty if none."}},
    "required": ["facts"], "additionalProperties": False,
}


def facts() -> list[dict[str, Any]]:
    return store.facts_get()


def add_facts(new: list[str], source: str) -> list[str]:
    known = {f["text"].lower() for f in facts()}
    added = []
    for n in new:
        n = n.strip().rstrip(".")
        if 6 < len(n) < 200 and n.lower() not in known:
            store.fact_add(n, source)
            known.add(n.lower())
            added.append(n)
    return added


def delete_fact(fact_id: int) -> None:
    store.fact_delete(fact_id)


def learn_from(user_msg: str) -> list[str]:
    found = []
    m = _PREF.search(user_msg)
    if m:
        clause = re.split(r"(?<=[.!?])\s|\?", m.group(0))[0].strip()
        clause = re.sub(r"^(?:remember(?: that)?|note that|fyi)\s+", "", clause, flags=re.I).rstrip(".")
        found.append(clause[:1].upper() + clause[1:])
    if llm.enabled() and len(user_msg.split()) > 4:
        res = llm.complete(
            "memory_facts",
            f"Investor's message: {user_msg!r}\nExtract only durable facts about the investor (positions, preferences, "
            "how they like analysis done). Ignore questions about companies. Usually there are none.",
            tier="fast", schema=FACT_SCHEMA, max_tokens=200)
        if res:
            found = res.get("facts", []) or found
    return add_facts(found, "conversation") if found else []


def facts_block() -> str:
    f = facts()
    return "\n".join(f"- {x['text']}" for x in f[-12:]) if f else "(none yet)"
