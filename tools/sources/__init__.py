"""Source registry — declarative adapters for tier-1/2 primary data sources.

The pattern (NEXT.md SOURCE REGISTRY): adding a source is a SourceSpec
(declarative metadata: tiers, rate limits, terms, UA) plus thin query
functions that call RestSource.get_json(). The generic client owns every
cross-source concern exactly once:

  - rate limiting (per-source minimum interval, thread-safe, process-wide
    shared per source so bursts of tool calls cannot approach the ceiling)
  - User-Agent / required headers (declared per source, enforced here)
  - provenance: every successful fetch is recorded into
    agp.provenance.ProvenanceLedger with the exact URL, so any number in a
    downstream model traces to fetched bytes by content hash
  - retry with backoff and Retry-After handling

Adapters NEVER open sockets in tests: RestSource accepts an injectable
``transport`` callable; tests pass canned fixtures. tests/test_build_r4_
sources.py additionally fails the suite if any test opens a real socket.
"""

from tools.sources.base import (
    FetchRecord,
    RestSource,
    SourceError,
    SourceSpec,
    PROVENANCE_TIERS,
)

__all__ = [
    "FetchRecord",
    "RestSource",
    "SourceError",
    "SourceSpec",
    "PROVENANCE_TIERS",
]
