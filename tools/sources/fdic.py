"""FDIC BankFind Suite API — bank financials and structure. Tier 1.

api.fdic.gov/banks (formerly banks.data.fdic.gov/api, which now 301s) —
financials (/financials), institutions,
summary, failures. No key; fair use, self-limit to 2 req/s. Responses:
{data:[{data:{...}, tier1... }], totals, parameters}. Filters use the
FDIC filter DSL as `filters=STALP:TX AND ASSET>10000` passed via the
`filters` param; fields selected with `field_names` (comma-separated).

Answers: per-bank quarterly call-report aggregates (assets, deposits,
equity capital, net income, NPLs), institution lookup by name/CERT,
failed-bank history.
Cannot answer: bank-holding-company consolidated data (call reports are
bank-level), intraday liquidity, non-US banks, real-time anything
(quarterly reporting cycle).
"""

from __future__ import annotations

import re

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="fdic",
    base_url="https://api.fdic.gov/banks",
    description="FDIC BankFind: bank-level financials, structure, failures",
    answers=(
        "quarterly bank financials (assets, deposits, capital, income)",
        "institution search by name, CERT, location",
        "failed-bank history",
    ),
    cannot_answer=(
        "holding-company consolidated statements (bank level only)",
        "non-US banks",
        "current-quarter data before call reports are filed",
    ),
    tier=1,
    min_interval_s=0.5,
    terms_url="https://banks.data.fdic.gov/docs/",
)

# characters allowed inside a filter value after an operator
_VALUE_RE = re.compile(r"^[A-Za-z0-9 .,:><=\-+()\"']*$")


class FdicAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def _check_filter(self, filters: str) -> str:
        if not _VALUE_RE.fullmatch(filters or ""):
            raise ValueError(f"suspicious FDIC filter expression: {filters!r}")
        return filters

    def institutions(self, filters: str = "", fields: tuple[str, ...] = (),
                     limit: int = 50) -> dict:
        params: dict[str, str] = {"limit": max(1, min(int(limit), 10000))}
        if filters:
            params["filters"] = self._check_filter(filters)
        if fields:
            params["field_names"] = ",".join(fields)
        url = self.source.build_url("/institutions", params)
        return self.source.get_json(url)[0]

    def search_institutions(self, search: str,
                            fields: tuple[str, ...] = (),
                            limit: int = 20) -> dict:
        """Full-text institution search (Elasticsearch query string).
        search=NAME:"chase" matches partials — unlike filters=NAME:chase,
        which is an exact match and silently returns zero for partial
        names (found in I2 live smoke)."""
        params: dict[str, str] = {
            "search": self._check_filter(search),
            "limit": max(1, min(int(limit), 10000)),
        }
        if fields:
            params["field_names"] = ",".join(fields)
        url = self.source.build_url("/institutions", params)
        return self.source.get_json(url)[0]

    def financials(self, cert: int, fields: tuple[str, ...],
                   end_period: str = "") -> dict:
        """Quarterly financial rows for one institution CERT."""
        params: dict[str, str] = {
            "filters": self._check_filter(f"CERT:{int(cert)}"),
            "field_names": ",".join(fields),
            "limit": 40,
            "sort_by": "REPDTE",
            "sort_order": "DESC",
        }
        if end_period:
            params["end_date"] = end_period
        url = self.source.build_url("/financials", params)
        data, rec = self.source.get_json(url)
        out = {"rows": [row.get("data", row) for row in data.get("data", [])]}
        out["_fetch"] = {"url": rec.url, "sha256": rec.content_sha256,
                         "fetched_at": rec.fetched_at}
        return out

    def failures(self, limit: int = 50) -> dict:
        url = self.source.build_url(
            "/failures", {"limit": max(1, min(int(limit), 10000))})
        return self.source.get_json(url)[0]
