"""World Bank Indicators API v2 — cross-country macro development series.
Tier 1.

api.worldbank.org/v2/country/{ISO3}/indicator/{CODE}?format=json&per_page=
&date=. No key at all; documented courtesy ceiling ~a few req/s — we
self-limit to 2 req/s. Response is a two-element array: [page_meta,
[records]] where each record carries country, date (year string), value
(null when missing), unit, indicator id.

Answers: GDP, population, trade shares, debt-to-GDP, energy use,
emissions, education/health indicators for any country since ~1960.
Cannot answer: subnational detail, monthly/quarterly frequency (annual
mostly; some monthly financial indicators exist but sparse), forecasts
(projections live in a separate dataset with explicit caveats),
sub-annual market data.
"""

from __future__ import annotations

import re

from tools.sources.base import RestSource, SourceError, SourceSpec

SPEC = SourceSpec(
    name="worldbank",
    base_url="https://api.worldbank.org/v2",
    description="World Bank Open Data: country development indicators",
    answers=(
        "annual country macro series (GDP, population, trade, debt)",
        "cross-country comparison on any WDI indicator",
        "long-run annual history back to ~1960",
    ),
    cannot_answer=(
        "monthly/quarterly macro detail (WDI is mostly annual)",
        "subnational statistics",
        "forecasts (separate projection dataset)",
        "real-time market prices",
    ),
    tier=1,
    min_interval_s=0.5,
    terms_url="https://www.worldbank.org/en/about/legal/terms-of-use",
)

_ISO_RE = re.compile(r"^[A-Za-z]{3}$")


class WorldBankAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def search_indicators(self, query: str, limit: int = 10) -> dict:
        """Full-text search over the WDI indicator catalogue (source=2).
        Results carry real indicator codes for a follow-up indicator()
        fetch — we never invent a code from a keyword."""
        url = self.source.build_url("/indicator", {
            "format": "json", "source": "2",
            "search": query.strip(),
            "per_page": max(1, min(int(limit), 200)),
        })
        data, rec = self.source.get_json(url)
        meta = data[0] if isinstance(data, list) and len(data) > 1 else {}
        rows = data[1] if isinstance(data, list) and len(data) > 1 else []
        return {"total": meta.get("total", len(rows)),
                "rows": [{"code": r.get("id"),
                          "name": r.get("name"), }
                         for r in rows],
                "_fetch": {"url": rec.url, "sha256": rec.content_sha256,
                           "fetched_at": rec.fetched_at}}

    def indicator(self, iso3: str, code: str, start: str = "",
                  end: str = "", per_page: int = 200) -> dict:
        """Records for one country/indicator. iso3 like 'USA' or 'all';
        code like 'NY.GDP.MKTP.CD'. Returns normalized rows + '_fetch'."""
        c = iso3.strip().lower()
        if c != "all" and not _ISO_RE.fullmatch(c):
            raise ValueError(f"bad ISO3 {iso3!r}")
        kcode = code.strip().upper()
        if not re.fullmatch(r"[A-Z0-9.]+", kcode):
            raise ValueError(f"bad indicator code {code!r}")
        params = {"format": "json",
                  "per_page": max(1, min(int(per_page), 2000))}
        if start or end:
            params["date"] = f"{start}:{end}" if end else start
        url = self.source.build_url(f"/country/{c}/indicator/{kcode}", params)
        data, rec = self.source.get_json(url)
        meta = data[0] if isinstance(data, list) and len(data) > 1 else {}
        rows = data[1] if isinstance(data, list) and len(data) > 1 else []
        out = {
            "total": meta.get("total", len(rows)),
            "rows": [{"country": r.get("country", {}).get("value"),
                      "date": r.get("date"), "value": r.get("value"),
                      "indicator": kcode} for r in rows],
            "_fetch": {"url": rec.url, "sha256": rec.content_sha256,
                       "fetched_at": rec.fetched_at},
        }
        return out
