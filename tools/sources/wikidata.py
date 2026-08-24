"""Wikidata SPARQL (Query Service) — structured entity graph. Tier 2.

query.wikidata.org/sparql. No key. Stated policy: one query at a time,
keep queries under 60s, be gentle; we self-limit to ~1 req/2s and set a
30s client timeout. Query strings are URL-encoded GET requests with
format=json.

Answers: entity relationships and attributes across the sum of human
catalogued entities (people, orgs, places, works), via SPARQL.
Cannot answer: real-time anything (lag minutes to days behind edits),
citations of scholarly claims (use OpenAlex), market or time-series data
(Wikidata stores point values, not series), contested/vandalized facts —
statements carry ranks but the model must check references.
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="wikidata",
    base_url="https://query.wikidata.org",
    description="Wikidata SPARQL: structured entity graph",
    answers=(
        "entity attributes and relationships via SPARQL",
        "cross-language labels/aliases for entities",
        "graph queries (who held office X when, subclasses of Y)",
        "capital cities of countries; where an entity is located or born "
            "(place of birth); what country an entity belongs to",
    ),
    cannot_answer=(
        "real-time data (edit lag minutes-to-days)",
        "time series or market data (point-in-time statements only)",
        "scholarly citation graphs (use OpenAlex)",
        "facts without references should not be relied on",
    ),
    tier=2,
    min_interval_s=2.0,
    terms_url="https://www.wikidata.org/wiki/Wikidata:Licensing",
)


class WikidataAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def sparql(self, query: str) -> dict:
        """Run a SPARQL SELECT/ASK query, JSON results binding form."""
        url = self.source.build_url(
            "/sparql", {"query": query, "format": "json"})
        return self.source.get_json(url)[0]
