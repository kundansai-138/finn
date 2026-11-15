"""A minimal state-graph runtime (the LangGraph idea, without the dependency).

Nodes are functions over a shared state dict; edges are fixed or conditional. The runtime emits
an event as each node starts and finishes, which is what the UI streams as the live
"how FINN answered" flowchart. `describe()` exports the graph itself for rendering.

Why not LangGraph itself: the agent's graph is six nodes with two loops. The library would add
a large dependency tree and a layer between us and the Anthropic SDK features we rely on
(prompt caching breakpoints, structured outputs, server-side web search, refusal fallbacks).
"""
from __future__ import annotations

import time
from typing import Any, Callable

END = "__end__"
Emit = Callable[[dict[str, Any]], None]


class StateGraph:
    def __init__(self, name: str):
        self.name = name
        self.nodes: dict[str, tuple[Callable[[dict, Emit], dict | None], str]] = {}
        self.edges: dict[str, str | tuple[Callable[[dict], str], dict[str, str]]] = {}
        self.entry: str | None = None

    def add_node(self, name: str, fn: Callable[[dict, Emit], dict | None], label: str) -> "StateGraph":
        self.nodes[name] = (fn, label)
        self.entry = self.entry or name
        return self

    def add_edge(self, a: str, b: str) -> "StateGraph":
        self.edges[a] = b
        return self

    def add_branch(self, a: str, router: Callable[[dict], str], options: dict[str, str]) -> "StateGraph":
        """options: router-return-value -> human label (shown on the edge)."""
        self.edges[a] = (router, options)
        return self

    def describe(self) -> dict[str, Any]:
        edges = []
        for a, e in self.edges.items():
            if isinstance(e, str):
                edges.append({"from": a, "to": e, "label": ""})
            else:
                edges += [{"from": a, "to": to, "label": label} for to, label in e[1].items()]
        return {"name": self.name, "nodes": [{"id": n, "label": l} for n, (_, l) in self.nodes.items()], "edges": edges}

    def run(self, state: dict[str, Any], emit: Emit, max_steps: int = 14) -> dict[str, Any]:
        cur = self.entry
        steps = 0
        while cur and cur != END and steps < max_steps:
            fn, label = self.nodes[cur]
            emit({"type": "node", "node": cur, "label": label, "status": "start"})
            t0 = time.time()
            try:
                upd = fn(state, emit) or {}
            except Exception as e:  # a failing node degrades the answer, it doesn't kill the turn
                emit({"type": "node", "node": cur, "label": label, "status": "error", "detail": str(e)[:200]})
                state.setdefault("errors", []).append(f"{cur}: {e}")
                upd = {}
            state.update(upd)
            emit({"type": "node", "node": cur, "label": label, "status": "end", "ms": int((time.time() - t0) * 1000),
                  "detail": state.pop("_detail", None)})
            e = self.edges.get(cur, END)
            cur = e if isinstance(e, str) else e[0](state)
            steps += 1
        return state
