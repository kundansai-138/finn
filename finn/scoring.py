"""Editable scoring: every investor starts from FINN's defaults and can change any weight.

Four sections, one per scoring engine (docs/SCORING.md explains each):

  pull     research ranking: who understands this best (logistic model weights)
  push     alert ranking: who to call today (relevance weights, plus the reachability and freshness floors)
  nudges   keep in touch: how much being overdue, mattering to the portfolio and having a reason to call count
  web      people from the public web: what makes a stranger worth adding

Only differences from the defaults are stored (per user, under row-level security), so improving a
default later reaches everyone who hasn't changed that value. Every value is clamped to its range.
Weights that are shares of a whole (nudges, web) are normalised when read, so a priority or fit stays
on 0-1 whatever the sliders say. Saving the pull or push section replaces what FINN had learned from
that investor's "Helpful / Not relevant" taps for that mode: the edit becomes the new starting point.
"""
from __future__ import annotations

from typing import Any

from . import store
from .context import current_user

FEATURE_HELP = {
    "insider": "Worked at the company itself; fades after they leave.",
    "adjacent": "Works or worked at a competitor, customer or supplier.",
    "domain": "Overlap between their career's themes and the company's.",
    "topic": "Years doing the specific subject the filing names, e.g. OSAT.",
    "graph": "Close to the company in the career graph (random walks).",
    "text": "Their profile's wording matches the event's.",
    "function_fit": "Their function suits the event, e.g. strategy for M&A.",
    "recency": "How recent their relevant experience is.",
    "seniority": "How senior the relevant role was.",
    "warmth": "How warm your relationship is.",
}


def _schema() -> dict[str, dict]:
    from .network import discover, nudges
    from .network import recommend as R

    def weights(mode: str, skip: tuple = ()) -> list[dict]:
        return [{"key": k, "label": R.FEATURE_LABEL[k], "help": FEATURE_HELP[k], "default": R.PRIORS[mode][k],
                 "min": 0.0, "max": 5.0, "step": 0.1} for k in R.FEATURES if k not in skip]

    return {
        "pull": {"label": "Research: who understands this best", "kind": "weights",
                 "about": "Used in a company's dossier and when you ask who knows something. Higher = counts more.",
                 "fields": weights("pull")},
        "push": {"label": "Alerts: who to call today", "kind": "weights",
                 "about": "Used on alerts. Ranks by the value of one call: can they help × will they pick up × is what they know fresh. "
                          "Warmth and recency enter through the two floors below, not as weights.",
                 "fields": weights("push", skip=("warmth", "recency")) + [
                     {"key": "reach_floor", "label": "Chance a cold contact picks up", "default": R.REACH_FLOOR, "min": 0.05, "max": 1.0,
                      "step": 0.05, "help": "1.0 = warmth doesn't matter on alerts; lower = prefer warm ties more strongly."},
                     {"key": "fresh_floor", "label": "Value of old experience", "default": R.FRESH_FLOOR, "min": 0.1, "max": 1.0,
                      "step": 0.05, "help": "1.0 = years-old experience counts fully; lower = prefer people in the thick of it now."}]},
        "nudges": {"label": "Keep in touch", "kind": "shares",
                   "about": "Ranks who you're losing touch with. The three weights are shares of the priority.",
                   "fields": [
                       {"key": "w_urgency", "label": "Past your usual rhythm", "default": nudges.W_URGENCY, "min": 0, "max": 1, "step": 0.05,
                        "help": "How much being overdue counts, measured against how often you normally talk."},
                       {"key": "w_value", "label": "Matters to your portfolio", "default": nudges.W_VALUE, "min": 0, "max": 1, "step": 0.05,
                        "help": "Works at, used to work at, or competes with a company you hold."},
                       {"key": "w_hook", "label": "A reason to call now", "default": nudges.W_HOOK, "min": 0, "max": 1, "step": 0.05,
                        "help": "Their employer just filed something material."},
                       {"key": "min_priority", "label": "Nudge me above", "default": nudges.MIN_PRIORITY, "min": 0.1, "max": 0.9, "step": 0.05,
                        "help": "Higher = fewer, more important nudges."},
                       {"key": "push_priority", "label": "Notify my phone above", "default": nudges.PUSH_PRIORITY, "min": 0.2, "max": 1.0,
                        "step": 0.05, "help": "At most two a week either way."}]},
        "web": {"label": "People from the public web", "kind": "shares",
                "about": "Scores strangers found online for a cause. The weights are shares of the fit.",
                "fields": [{"key": k, "label": discover.WEB_LABEL[k], "default": v, "min": 0, "max": 1, "step": 0.05,
                            "help": h} for (k, v), h in zip(discover.WEB_WEIGHTS.items(), [
                                "At the company, or a competitor, customer or supplier.",
                                "Works on the exact subject the filing names.",
                                "Their function suits the event.",
                                "How senior they are.",
                                "Name and organisation found on the source page.",
                                "In the role now, per a recent source."])]
                          + [{"key": "min_fit", "label": "Suggest above", "default": discover.MIN_FIT, "min": 0.1, "max": 0.9, "step": 0.05,
                              "help": "Higher = fewer, better-fitting suggestions."}]},
    }


SHARE_KEYS = {"nudges": ("w_urgency", "w_value", "w_hook"), "web": ("company", "topic", "function_fit", "seniority", "evidence", "current")}


def _overrides(user_id: str | None = None) -> dict:
    return (store.prefs_get(user_id) or {}).get("scoring", {})


def values(group: str, user_id: str | None = None) -> dict[str, float]:
    """Effective values for one section: defaults, then this user's overrides (clamped), shares normalised."""
    sch = _schema()[group]
    over = _overrides(user_id).get(group, {})
    out = {}
    for f in sch["fields"]:
        v = over.get(f["key"], f["default"])
        out[f["key"]] = min(f["max"], max(f["min"], float(v)))
    keys = SHARE_KEYS.get(group)
    if keys:
        tot = sum(out[k] for k in keys)
        if tot <= 0:
            for k in keys:
                out[k] = next(f["default"] for f in sch["fields"] if f["key"] == k)
            tot = sum(out[k] for k in keys)
        for k in keys:
            out[k] = out[k] / tot
    return out


def schema(user_id: str | None = None) -> list[dict[str, Any]]:
    over = _overrides(user_id)
    out = []
    for gid, g in _schema().items():
        mine = over.get(gid, {})
        out.append({"id": gid, "label": g["label"], "about": g["about"], "kind": g["kind"], "customised": bool(mine),
                    "fields": [{**f, "value": mine.get(f["key"], f["default"]), "changed": f["key"] in mine} for f in g["fields"]]})
    return out


def _changed() -> None:
    """Rankings depend on these values: drop this user's memoised rankings and cached dossiers."""
    from .network import recommend as R

    uid = current_user()
    for k in [k for k in R._memo if k[0] == uid]:
        R._memo.pop(k, None)
    store.kv_delete_prefix("dossier", f"{uid}:")


def save(group: str, new: dict[str, Any]) -> dict[str, Any]:
    sch = _schema()
    if group not in sch:
        raise KeyError(group)
    fields = {f["key"]: f for f in sch[group]["fields"]}
    diff = {}
    for k, v in (new or {}).items():
        if k not in fields:
            continue
        f = fields[k]
        v = round(min(f["max"], max(f["min"], float(v))), 4)
        if abs(v - f["default"]) > 1e-9:
            diff[k] = v
    prefs = store.prefs_get() or {}
    sc = prefs.setdefault("scoring", {})
    if diff:
        sc[group] = diff
    else:
        sc.pop(group, None)
    store.prefs_put(prefs)
    if group in ("pull", "push"):
        _reset_learned(group)
    _changed()
    return next(g for g in schema() if g["id"] == group)


def reset(group: str) -> dict[str, Any]:
    return save(group, {})


def _reset_learned(mode: str) -> None:
    w = store.reco_weights_get() or {}
    if "pull" not in w and "push" not in w and w:
        w = {"pull": w}
    if mode in w:
        w.pop(mode)
        store.reco_weights_put(w)


def prior(mode: str, user_id: str | None = None) -> dict[str, float]:
    """The ranking model's starting weights for this user: FINN's defaults with their edits."""
    from .network import recommend as R

    p = dict(R.PRIORS[mode])
    v = values(mode, user_id)
    p.update({k: v[k] for k in R.FEATURES if k in v})
    return p
