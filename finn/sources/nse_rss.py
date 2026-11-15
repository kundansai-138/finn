"""NSE's public 'Online announcements' RSS feed.

A rolling, exchange-wide feed (ttl 5 min). The PDF filename starts with the NSE symbol, which is
how we map an item to a ticker. A real deployment polls this every few minutes and accumulates;
the prototype fetches it on refresh.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from lxml import etree

from ..config import TTL_RSS
from ..http import FetchError, fetch

FEED = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
IST = timezone(timedelta(hours=5, minutes=30))


def fetch_items(symbols: set[str] | None = None) -> list[dict]:
    try:
        xml = fetch(FEED, TTL_RSS, binary=True)
    except FetchError:
        return []
    return parse_items(xml, symbols)


def parse_items(xml: bytes, symbols: set[str] | None = None) -> list[dict]:
    """Pure parse of the RSS body (testable without the network)."""
    root = etree.fromstring(xml, parser=etree.XMLParser(recover=True))
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        m = re.search(r"/corporate/([A-Z0-9&\-]+)_\d{8,}", link)
        sym = m.group(1) if m else None
        if symbols and sym not in symbols:
            continue
        desc = (it.findtext("description") or "").strip()
        subject = ""
        if "|SUBJECT:" in desc:
            desc, subject = [x.strip() for x in desc.split("|SUBJECT:", 1)]
        try:
            ts = datetime.strptime((it.findtext("pubDate") or "").strip(), "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            continue
        out.append({
            "ticker": sym,
            "company": (it.findtext("title") or "").strip(),
            "title": subject or desc[:120],
            "summary": re.sub(r"^.*? has informed the Exchange (?:regarding|about|that)\s*", "", desc).strip(),
            "url": link,
            "ts": ts.isoformat(),
        })
    return out
