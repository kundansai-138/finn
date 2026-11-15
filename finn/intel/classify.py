"""Announcement classification + materiality scoring.

Rules first: exchange subjects are semi-structured ("Announcement under Regulation 30 (LODR)-Credit
Rating"), so ordered regexes classify the bulk of filings for free and explainably. Only what falls
through to 'unclassified' with non-routine-looking text is worth a model call (see llm.py).

Score = category base (+ subtype adjustments) + modifiers; tier thresholds turn it into the
three levels of the signal dial.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CRITICAL, MATERIAL = 75, 45

LABELS = {
    "results": "Results", "M&A": "M&A", "fund_raise": "Fund raise",
    "management_change": "Management change", "litigation": "Litigation / regulatory",
    "insider_trading": "Insider trade", "earnings_call": "Earnings call",
    "investor_meetings": "Investor meeting", "credit_rating": "Credit rating",
    "dividend": "Dividend", "bonus_split": "Bonus / split", "capex": "Capex",
    "press_release": "Press release", "postal_ballot": "Postal ballot", "agm_egm": "AGM / EGM",
    "pledging": "Pledging", "unclassified": "Routine",
}

# (category, subtype, base score, pattern). First match wins, so order = precedence.
RULES: list[tuple[str, str, int, str]] = [
    # Routine paperwork that would otherwise trip broader patterns below
    ("unclassified", "esop", 5, r"\besop\b|\besps\b|employee stock option|allotment of .*under .*scheme"),
    ("unclassified", "compliance", 4, r"reg(ulation)?\.?\s*74\s*\(5\)|compliance certificate|certificate under reg|secretarial compliance|trading window|loss of share certificate|duplicate share|investor (grievance|complaints)|statement of deviation|book closure|monitoring agency|dematerialis|registrar and (share )?transfer agent|\brta\b|change in (corporate|registered) office address"),
    ("unclassified", "newspaper", 6, r"newspaper (publication|advertisement)|copy of newspaper|publication of (financial )?results"),
    ("unclassified", "esg", 8, r"business responsibility|sustainability report|\besg\b"),
    ("unclassified", "auditor", 12, r"(appointment|re-?appointment) of (statutory |secretarial |internal |cost )?auditor"),

    ("results", "intimation", 22, r"board meeting intimation|intimation of board meeting|prior intimation"),
    ("results", "letter", 62, r"shareholders?.?\s*letter|letter to shareholders"),
    ("results", "filing", 92, r"financial results?|un-?audited.*results|audited.*results|results? for the (quarter|period|half)|integrated filing.*financ"),

    ("earnings_call", "transcript", 32, r"transcript"),
    ("earnings_call", "recording", 12, r"audio recording|video recording|recording of (the )?(earnings|conference|analyst)"),
    ("earnings_call", "presentation", 28, r"investor presentation|earnings presentation|results presentation"),
    ("earnings_call", "notice", 14, r"earnings call|conference call|concall"),
    ("investor_meetings", "meet", 10, r"analysts?\s*(\(s\))?\s*/?\s*investors?\s*(\(s\))?\s*meet|investor meet|institutional investor|non-deal roadshow|conference participation"),

    ("credit_rating", "downgrade", 82, r"credit rating.*downgrad|downgrad.*rating"),
    ("credit_rating", "upgrade", 55, r"credit rating.*upgrad|upgrad.*rating"),
    ("credit_rating", "rating", 30, r"credit rating|\brating\b"),

    ("management_change", "reappointment", 28, r"re-?appoint(ed|ment)"),
    ("management_change", "division_head", 55, r"(ceo|chief executive)\s+(of|for)\s+(the\s+)?[\w\s]{0,30}(division|complex|business|unit|subsidiary|vertical)"),
    ("management_change", "cxo", 86, r"(appoint|resign|cessation|retire|step(s|ping)? down|change).{0,80}(chief executive|chief financial|\bceo\b|\bcfo\b|managing director|\bmd\b|whole[- ]?time director|chairman|chairperson)|(chief executive|chief financial|\bceo\b|\bcfo\b|managing director).{0,60}(appoint|resign|cessation|retire)"),
    ("management_change", "kmp", 58, r"key managerial|\bkmp\b|senior management personnel"),
    ("management_change", "director", 48, r"change in directorate|(appoint|resign|cessation).{0,40}director|director.{0,40}(appoint|resign|cessation)"),
    ("management_change", "secretarial", 25, r"company secretary|compliance officer"),
    ("management_change", "other", 45, r"change in management|superannuat"),

    ("M&A", "deal", 84, r"acqui(re|sition)|amalgamation|\bmerger\b|demerger|scheme of arrangement|joint venture|\bjv\b|term sheet|slump sale|divest|disinvest|stake sale|\d+%\s*stake|sale of (stake|business|undertaking)"),
    ("M&A", "restructuring", 55, r"restructuring|share swap"),
    ("M&A", "subsidiary", 50, r"incorporation of (a )?(wholly owned )?subsidiary|investment in (wholly owned )?subsidiar|strike off"),

    ("litigation", "regulatory", 78, r"litigation|dispute|regulatory action|penalt|show cause|tax demand|demand order|income tax|\bgst\b|search and seizure|\bsearch\b.*\b(tax|ed|cbi)\b|insolvency|\bnclt\b|\bnclat\b|\bsebi order|adjudicat|\bfine\b"),

    ("fund_raise", "equity", 70, r"\bqip\b|qualified institution|rights issue|preferential (issue|allotment)|warrants|fund raising|raising of funds|capital raise|buy-?back"),
    ("fund_raise", "debt", 28, r"commercial paper|non-convertible debenture|\bncds?\b|bonds?\b|allotment of debentures"),

    ("pledging", "pledge", 58, r"pledge|encumbrance|reg(ulation)?\.?\s*31"),
    ("insider_trading", "disclosure", 30, r"reg(ulation)?\.?\s*7\s*\(2\)|reg(ulation)?\.?\s*29|\bsast\b|insider trading|acquisition of shares by promoter"),

    ("capex", "capex", 68, r"\bcapex\b|capital expenditure|capacity (expansion|addition)|new (plant|facility|unit)|greenfield|brownfield|commercial production|commission(ed|ing) of|investment of .{0,20}crore|\bMW\b.{0,40}project"),
    ("bonus_split", "bonus", 52, r"\bbonus\b|stock split|sub-?division of"),
    ("dividend", "dividend", 30, r"dividend"),
    ("dividend", "record_date", 18, r"record date"),

    ("press_release", "order", 52, r"order (win|received|inflow)|receipt of (an )?order|bagged|award(ed|s)? of (order|contract)|letter of (award|intent)"),
    ("press_release", "clarification", 48, r"clarification|rumou?r|news item|media report"),
    ("press_release", "release", 36, r"press release|media release|launch|partnership|collaborat|\bmou\b|update"),

    ("postal_ballot", "ballot", 14, r"postal ballot|e-?voting|scrutini[sz]er"),
    ("agm_egm", "meeting", 14, r"\bagm\b|\begm\b|annual general meeting|extraordinary general meeting|shareholders? meeting"),
    ("results", "board_outcome", 40, r"outcome of (the )?board meeting|board meeting outcome"),
]
_COMPILED = [(c, s, b, re.compile(p, re.I)) for c, s, b, p in RULES]

_PREFIX = re.compile(r"^announcement under regulation 30 \(lodr\)\s*-\s*", re.I)


@dataclass
class Verdict:
    category: str
    subtype: str
    base: int
    score: float
    tier: str
    reasons: list[str]


def clean_title(title: str) -> str:
    t = _PREFIX.sub("", title or "").strip()
    return t[:1].upper() + t[1:] if t else t


_COMPILED_BY_SUB = {s: rx for _, s, _, rx in _COMPILED}
# Paperwork whose body may mention big words (a QIP, results) without being news itself.
ROUTINE_LOCK = {"esop", "compliance", "newspaper", "esg", "auditor"}
GENERIC = {"other", "unknown", "release", "board_outcome", "intimation", "meet", "ballot", "rating", "notice"}


def _first(text: str) -> tuple[str, str, int] | None:
    for cat, sub, base, rx in _COMPILED:
        if rx.search(text):
            return cat, sub, base
    return None


def _most_material(text: str) -> tuple[str, str, int] | None:
    """Body text: the most material thing it mentions. A body can never *create* a results filing
    (presentations and transcripts all mention results); only the title or a board outcome can."""
    best = None
    division = _COMPILED_BY_SUB["division_head"].search(text)
    for cat, sub, base, rx in _COMPILED:
        if cat == "results" or not rx.search(text) or (sub == "cxo" and division):
            continue
        if cat == "unclassified" and best is None:
            best = (cat, sub, base)
        elif cat != "unclassified" and (best is None or base > best[2]):
            best = (cat, sub, base)
    return best


def classify(title: str, summary: str = "") -> tuple[str, str, int]:
    """Title is the exchange's own subject, so it leads. But filings often hide the news behind a
    boring title ("General Updates", "Change in Management", "Investor Meet - Outcome"), so the
    body may escalate: when it reveals something materially bigger, the body wins."""
    t = clean_title(title)
    r1 = _first(t)
    r2 = _most_material(f"{t} {summary or ''}") if summary else None
    if r1 and r1[1] == "board_outcome" and re.search(r"financial results|results for", summary or "", re.I):
        return "results", "filing", 92
    if r1 is None:
        return r2 or ("unclassified", "unknown", 10)
    if r1[1] in ROUTINE_LOCK:
        return r1
    if r2 and r2[2] > r1[2] and (r1[1] in GENERIC or r1[0] == r2[0] or (r1[2] < 30 and r2[2] >= 60)):
        return r2
    return r1


def tier_of(score: float) -> str:
    return "critical" if score >= CRITICAL else "material" if score >= MATERIAL else "routine"


def score(
    category: str, subtype: str, base: int, *, weight: float = 0.0, watched: set[str] | None = None,
    muted: set[str] | None = None, price_move_pct: float | None = None, network_hits: int = 0,
) -> Verdict:
    s, reasons = float(base), [f"{LABELS.get(category, category)} ({subtype}) base {base}"]
    if watched and category in watched and base >= 25:
        s += 8
        reasons.append("you watch this category +8")
    if muted and category in muted:
        s -= 25
        reasons.append("you muted this category -25")
    if weight:
        adj = round((weight - 5.0) * 1.2, 1)
        if abs(adj) >= 0.5:
            s += adj
            reasons.append(f"position weight {weight:.1f}% {adj:+.1f}")
    if price_move_pct is not None and abs(price_move_pct) >= 3 and base >= 25:
        s += 6
        reasons.append(f"stock moved {price_move_pct:+.1f}% the same day +6")
    if network_hits and base >= 40:
        s += 4
        reasons.append("someone you know is close to this +4")
    s = max(0.0, min(100.0, s))
    return Verdict(category, subtype, base, round(s, 1), tier_of(s), reasons)
