"""
Provenance-assigned source classes and verified citations.

Design (findings/instance4.md, mechanism 1 + 2 of the earned-confidence
plan): source_class is a function of WHICH CODE PATH produced the evidence —
a real tool call returning real bytes is PRIMARY/SECONDARY by construction,
text a model produced without a tool call is INFERRED. The label is never a
field in the model's JSON output.

The ledger is append-only: ``record_tool_result`` is called once per real
tool return, keyed by content hash. Nothing else can add to it, so an
evidence item can only claim tool provenance if bytes with that exact hash
actually came back from a tool during this session.

Nothing here requires database access; the orchestrator adoption diff is in
findings/instance4.md as a PROPOSAL (orchestrator.py is read-only for this
instance).
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlparse

from agp import Evidence, SourceClass


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


# ── URL extraction ────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)


def extract_urls(text: str) -> set[str]:
    """Return every http(s) URL appearing in *text* (scheme lowercased)."""
    if not text:
        return set()
    out = set()
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;")
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            out.add(url)
    return out


# ── Provenance ledger ─────────────────────────────────────────────────────

@dataclass
class ToolObservation:
    """One real tool return, recorded by the code path that executed it."""
    tool_name: str                      # e.g. "web_search", "web_fetch"
    content_hash: str                   # sha256 of the returned bytes/text
    urls: frozenset[str] = frozenset()  # URLs the tool actually retrieved
    primary: bool = False               # True = fetched document body (PRIMARY)


class ProvenanceLedger:
    """Append-only record of what tools actually returned this session.

    Source-class assignment rules (mechanism 1):
      - evidence whose exact content hash matches an observation marked
        ``primary=True``                       → PRIMARY
      - evidence whose hash matches any other observation, OR which cites a
        URL the session actually fetched       → SECONDARY
      - anything else                          → INFERRED (regardless of what
        the model declared)

    Citation rule (mechanism 2): a citation counts ONLY when it names a URL
    present in the ledger. Printing a fabricated URL buys nothing.
    """

    def __init__(self) -> None:
        self._by_hash: dict[str, list[ToolObservation]] = {}
        self._urls: dict[str, ToolObservation] = {}

    def record_tool_result(
        self, tool_name: str, content: str, *, primary: bool = False,
        urls: Optional[Iterable[str]] = None,
    ) -> ToolObservation:
        """Record one real tool return. Call from the executor only."""
        obs = ToolObservation(
            tool_name=tool_name,
            content_hash=_content_hash(content or ""),
            urls=frozenset(urls or ()),
            primary=primary,
        )
        self._by_hash.setdefault(obs.content_hash, []).append(obs)
        for u in obs.urls:
            # First fetch wins; later observations don't erase provenance.
            self._urls.setdefault(u, obs)
        return obs

    # ── queries ──

    def observed_urls(self) -> set[str]:
        return set(self._urls.keys())

    def has_observation(self, content: str) -> bool:
        return _content_hash(content or "") in self._by_hash

    def is_primary_bytes(self, content: str) -> bool:
        for obs in self._by_hash.get(_content_hash(content or ""), ()):
            if obs.primary:
                return True
        return False

    def cites_verified_url(self, text: str) -> bool:
        """True iff *text* contains at least one URL the session fetched.

        This is the replacement for orchestrator._response_cites_urls's bare
        substring check: fabricated URLs fail, and 'http://' as a literal
        string fails because it does not parse as a URL present in the
        ledger.
        """
        return any(u in self._urls for u in extract_urls(text))

    # ── assignment ──

    def assign_source_class(self, evidence: Evidence) -> SourceClass:
        """Provenance-derived class for one evidence item. Never reads
        evidence.source_class — that field is model-declared and untrusted."""
        content = evidence.content or ""
        if self.is_primary_bytes(content):
            return SourceClass.PRIMARY
        if self.has_observation(content):
            return SourceClass.SECONDARY
        # Citing a genuinely-fetched URL grounds the claim as SECONDARY.
        if self.cites_verified_url(content):
            return SourceClass.SECONDARY
        return SourceClass.INFERRED


def clamp_confidence_provenance(score: float, source_class: SourceClass,
                                max_by_source: dict[str, float]) -> float:
    """Clamp to the ceiling of the PROVENANCE-assigned class, not the declared one."""
    score = max(0.0, min(1.0, float(score)))
    return round(min(score, max_by_source.get(source_class.value, 0.55)), 2)


def relabel_evidence(evidence_list: "Iterable[Evidence]",
                     ledger: ProvenanceLedger,
                     max_by_source: dict[str, float],
                     floor: float = 0.30) -> int:
    """Rewrite source_class/confidence on each item from provenance.

    Returns number of items DEMOTED (declared class > provenance class).
    Promotions are possible too (real tool bytes declared INFERRED become
    PRIMARY). Confidence is clamped to the assigned class's ceiling and
    floored at *floor* (the DB CHECK floor).
    """
    demoted = 0
    rank = {c.value: i for i, c in enumerate(
        (SourceClass.INFERRED, SourceClass.SIGNAL, SourceClass.SECONDARY, SourceClass.PRIMARY))}
    for ev in evidence_list:
        assigned = ledger.assign_source_class(ev)
        if rank.get(assigned.value, 0) < rank.get(ev.source_class.value, 0):
            demoted += 1
        ev.source_class = assigned
        conf = max(floor, min(float(ev.confidence_score),
                              max_by_source.get(assigned.value, 0.55)))
        ev.confidence_score = round(conf, 2)
    return demoted
