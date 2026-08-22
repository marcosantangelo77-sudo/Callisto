"""SEC EDGAR full-text search (EFTS) — narrative text across filings. Tier 1.

The EDGAR company-facts surface (tools/domains/finance/edgar.py) covers
structured XBRL only; EFTS (efts.sec.gov) is what answers "which filings
MENTION X". Endpoint: GET
  https://efts.sec.gov/LATEST/search-index?q=<query>&dateRange=custom
      &startdt=YYYY-MM-DD&enddt=YYYY-MM-DD&forms=10-K
Returns {hits:{total:{value}, hits:[{_source:{_id, ciks, file_type,
file_date, display_names}}]}}. _id parses to "accession:filename".
No key; SEC fair-access applies (declared User-Agent, ≤10 req/s declared —
we self-limit to ~4 req/s like the XBRL client). Full-text index starts
2001.

Answers: which filings mention a phrase/company/topic since 2001,
filtered by form type and date range.
Cannot answer: filings before 2001 (not in the full-text index),
exhibit-only text in some legacy formats, non-EDGAR documents.
"""

from __future__ import annotations

import re

from tools.sources.base import RestSource, SourceError, SourceSpec

SPEC = SourceSpec(
    name="sec_fulltext",
    base_url="https://efts.sec.gov/LATEST",
    description="EDGAR full-text search: which filings mention what, since 2001",
    answers=(
        "filings mentioning a phrase, person, or topic",
        "form-type and date-filtered filing discovery",
        "cross-company co-mention queries (risk-factor sweeps)",
    ),
    cannot_answer=(
        "full-text before 2001",
        "XBRL structured facts (use the edgar companyfacts source)",
        "documents never filed on EDGAR",
    ),
    tier=1,
    min_interval_s=0.25,
    terms_url="https://www.sec.gov/privacy",
)

_ACCESSION_RE = re.compile(r"^(?P<acc>[0-9\-]+):(?P<file>.+)$")


class SecFullTextAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def search(self, query: str, start: str = "", end: str = "",
               forms: str = "", limit: int = 20) -> dict:
        """Full-text search. dates as YYYY-MM-DD; forms comma-separated
        (e.g. '10-K,10-Q'). Returns normalized hits + '_fetch' provenance."""
        if not query.strip():
            raise ValueError("query must be non-empty")
        params: dict[str, str] = {"q": query}
        if start or end:
            params["dateRange"] = "custom"
            if start:
                params["startdt"] = start
            if end:
                params["enddt"] = end
        if forms:
            params["forms"] = forms
        url = self.source.build_url("/search-index", params)
        data, rec = self.source.get_json(url)
        out = self._normalize(data, limit)
        out["_fetch"] = {"url": rec.url, "sha256": rec.content_sha256,
                         "fetched_at": rec.fetched_at}
        return out

    @staticmethod
    def _normalize(data: dict, limit: int) -> dict:
        hits = []
        raw = data.get("hits", {})
        for h in raw.get("hits", [])[:limit]:
            src = h.get("_source", {})
            m = _ACCESSION_RE.match(src.get("_id", ""))
            hits.append({
                "accession": m.group("acc") if m else "",
                "filename": m.group("file") if m else "",
                "cik": src.get("ciks", [None])[0],
                "company": src.get("display_names", [""])[0],
                "form": src.get("file_type", ""),
                "filed": src.get("file_date", ""),
            })
        return {"total": raw.get("total", {}).get("value", len(hits)),
                "hits": hits}
