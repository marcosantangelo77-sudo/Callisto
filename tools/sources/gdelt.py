"""GDELT 2.0 DOC API — machine-coded global news events. Tier 4 in practice
(news = secondary analysis) though the event database itself is tier-2
machine extraction; we declare tier 4 to keep confidence ceilings honest.

api.gdeltproject.org/api/v2/doc/doc. No key. Stated: no hard published
limit, but aggressive scraping is blocked; we self-limit to ~1 req/5s.
NOTE: responses can be JSON or CSV depending on `format` — we pin JSON.

Answers: which news outlets covered an event/person/topic and when,
volume of coverage over time, cross-country framing comparisons via the
machine-translated corpus (65 languages).
Cannot answer: ground truth about what happened (this is news coverage,
not events), sentiment beyond GDELT's tone score, anything behind a
paywall's full text, coverage before Jan 2017 in DOC 2.0 (use the
1.0 event database for older).
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="gdelt",
    base_url="https://api.gdeltproject.org/api/v2/doc",
    description="GDELT DOC: global news coverage, machine-coded",
    answers=(
        "news coverage volume for a person/org/topic over time",
        "which outlets covered an event and when",
        "cross-language/cross-country coverage comparison",
    ),
    cannot_answer=(
        "ground truth about events (coverage ≠ occurrence)",
        "full text behind paywalls",
        "reliable pre-2017 coverage (DOC 2.0 window)",
        "sentiment beyond GDELT's coarse tone score",
    ),
    tier=4,
    # Live-verified 2026-08: GDELT 2.0 DOC 429s aggressively — a single
    # request right after another client's can fail, then succeed 45s
    # later with the identical URL (reproduced). 5s spacing is not
    # enough; the documented guidance is ~1 call per 5s per IP but the
    # shared pool is unforgiving, so space calls at 30s.
    min_interval_s=30.0,
    terms_url="https://www.gdeltproject.org/about.html#terms",
)


class GdeltAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def doc_query(self, query: str, mode: str = "artlist",
                  timespan: str = "", limit: int = 75) -> dict:
        """mode: artlist | timelinevol | tonechart | wordcloudimagetags ...
        query supports operators: sourcetype:, sourcecountry:, domain:, theme:."""
        params = {"query": query, "mode": mode, "format": "json",
                  "maxrecords": min(int(limit), 250)}
        if timespan:
            params["timespan"] = timespan
        url = self.source.build_url("/doc", params)
        return self.source.get_json(url)[0]

    def coverage_timeline(self, query: str, timespan: str = "1w") -> dict:
        """Volume-of-coverage time series for a query."""
        return self.doc_query(query, mode="timelinevol", timespan=timespan)
