"""Primary documents (concall transcripts, filings): download once, extract text, cache forever.

Documents are immutable once filed, so the cache key is the URL and the TTL is effectively infinite.
Text is stored page-by-page so every downstream claim can cite a page number.
"""
from __future__ import annotations

import hashlib
import io
import logging

from .. import store
from ..http import FetchError, fetch

log = logging.getLogger(__name__)
FOREVER = 10 * 365 * 86400


def pdf_pages(url: str, max_pages: int = 80, ticker: str | None = None, kind: str | None = None,
              label: str | None = None) -> list[str] | None:
    key = hashlib.sha256(url.encode()).hexdigest()
    hit = store.doc_pages_get(key)
    if hit is not None:
        return hit
    try:
        blob = fetch(url, FOREVER, binary=True, timeout=60)
    except FetchError as e:
        log.warning("pdf fetch failed %s", e)
        return None
    if not blob.startswith(b"%PDF"):
        return None
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(blob))
        pages = []
        for p in reader.pages[:max_pages]:
            try:
                pages.append((p.extract_text() or "").strip())
            except Exception:
                pages.append("")
    except Exception as e:
        log.warning("pdf parse failed %s: %s", url, e)
        return None
    store.doc_pages_put(key, url, pages, ticker=ticker, kind=kind, label=label)
    return pages
