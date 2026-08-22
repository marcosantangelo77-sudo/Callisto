"""Provenance-verified temporal cutoff.

THE hard constraint of the retrodiction harness: evidence acquisition must be
genuinely limited to sources PUBLISHED before the cutoff date. Leakage makes
every downstream number meaningless, and it is easy to leak:

  - a source's publication date differs from its fetch date (fetched today,
    published 2019 — fine; fetched today, page edited 2025 — NOT fine);
  - a page may have been silently edited after the cutoff;
  - an API may return current values under a historical-looking query.

Design rule: **provenance-verified or excluded.** A fetch record carries a
PublicationProof; anything unverifiable is EXCLUDED, never assumed safe.
The enforcer can only ever shrink the evidence set.

No network anywhere in this module. Callers supply fetch records; tests
supply fixtures.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class ProofKind(str, Enum):
    """How publication timing is established. Ordered by strength."""

    # Source itself declares a publication/revision timestamp AND the fetched
    # bytes hash-match that version (e.g. EDGAR filing with filedAt and a
    # content-addressed archive).
    SOURCE_DECLARED = "source_declared"
    # An immutable snapshot taken on/before the cutoff proves the content
    # existed then (Wayback-style snapshot id + capture date).
    IMMUTABLE_SNAPSHOT = "immutable_snapshot"
    # Dated inside an authoritative container: the document's own embedded
    # metadata (XBRL period, arXiv v1 announcement date) verified against the
    # bytes.
    EMBEDDED_METADATA = "embedded_metadata"

    @property
    def rank(self) -> int:
        return list(self.__class__).index(self)


class CutoffViolation(Exception):
    """A fetch record cannot prove pre-cutoff publication. Never a warning —
    always an exclusion."""


@dataclass(frozen=True)
class PublicationProof:
    """Evidence that specific bytes were public at a specific time."""
    kind: ProofKind
    # The date the source claims to have become public.
    published_on: date
    # What establishes the claim: snapshot id, filing accession, archive URL…
    locator: str
    # sha256 of the exact bytes this proof covers. Content-addressed so a
    # later edit of the live page does not retroactively satisfy an old proof.
    content_sha256: str
    # HMAC-SHA256 over (kind, published_on, locator, content_sha256), keyed by
    # the harness secret. Without a valid signature anyone could fabricate a
    # proof claiming any date over any bytes; signatures make the proof
    # issuer the single trust boundary.
    signature: Optional[str] = None

    def __post_init__(self):
        if len(self.content_sha256) != 64:
            raise ValueError("content_sha256 must be a 64-char hex digest")
        if not self.locator:
            raise ValueError("proof requires a non-empty locator")

    @property
    def signing_payload(self) -> str:
        return "|".join([self.kind.value, self.published_on.isoformat(),
                         self.locator, self.content_sha256])

    def sign(self, key: str) -> "PublicationProof":
        return PublicationProof(
            kind=self.kind, published_on=self.published_on,
            locator=self.locator, content_sha256=self.content_sha256,
            signature=hmac.new(key.encode(), self.signing_payload.encode(),
                               hashlib.sha256).hexdigest())

    def has_valid_signature(self, key: str) -> bool:
        if not self.signature:
            return False
        expected = hmac.new(key.encode(), self.signing_payload.encode(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.signature, expected)


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


@dataclass
class EvidenceRecord:
    """One fetch, as the pipeline produced it. Carries its own provenance;
    nothing about safety is inferred from the URL, query, or fetch time."""
    url: str
    query: str                      # what was asked for
    fetched_at: datetime            # when WE got it (irrelevant to cutoff)
    content: str                    # the actual bytes/text returned
    # PublicationProof if one exists. None ⇒ unverifiable ⇒ EXCLUDED.
    proof: Optional[PublicationProof] = None

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(
            self.content.encode("utf-8", errors="replace")).hexdigest()

    def verify_proof(self) -> list[str]:
        """Reasons this record's proof is invalid for ITS OWN bytes.
        Empty = the proof genuinely covers the content we hold."""
        errs: list[str] = []
        if self.proof is None:
            return ["no publication proof attached"]
        if self.proof.content_sha256 != self.content_sha256:
            errs.append(
                "proof covers different bytes than the fetched content "
                "(post-cutoff edit?)")
        return errs


class CutoffEnforcer:
    """Filters evidence to records provably published before the cutoff.

    Policy (all defaults are fail-closed):
      - no proof                       → EXCLUDED
      - proof whose bytes don't match  → EXCLUDED
      - published_on == cutoff date    → EXCLUDED (strictly-before only)
      - unparseable dates              → EXCLUDED
    """

    def __init__(self, cutoff: date, signing_key: Optional[str] = None):
        self.cutoff = _as_date(cutoff)
        if self.cutoff is None:
            raise TypeError("cutoff must be a date")
        self._signing_key = signing_key

    @property
    def violations(self) -> list[str]:
        return [v for v in getattr(self, "_violations", [])]

    def admit(self, records) -> tuple[list[EvidenceRecord], list[CutoffViolation]]:
        """Split records into admitted / violated. Returns both lists; the
        caller decides what to do with violations (log, count, alert) but can
        never resurrect them into the evidence set through this class."""
        admitted: list[EvidenceRecord] = []
        rejected: list[CutoffViolation] = []
        for rec in records:
            reason = self._reject_reason(rec)
            if reason is None:
                admitted.append(rec)
            else:
                rejected.append(CutoffViolation(
                    f"{getattr(rec, 'url', '?')}: {reason}"))
        return admitted, rejected

    def require_admitted(self, records) -> list[EvidenceRecord]:
        """Admit or raise. For call sites where running with zero admissible
        evidence must be loud rather than silently empty."""
        admitted, rejected = self.admit(records)
        if rejected:
            raise CutoffViolation("; ".join(v.args[0] for v in rejected))
        return admitted

    def _reject_reason(self, rec: EvidenceRecord) -> Optional[str]:
        errs = rec.verify_proof()
        if errs:
            return errs[0]
        # Signature check (when a key is configured): an unsigned or forged
        # proof is exactly as untrustworthy as no proof at all. This closes
        # the "declare any date over any bytes" forgery path.
        if self._signing_key and not rec.proof.has_valid_signature(
                self._signing_key):
            return "proof signature missing or invalid"
        pub = _as_date(rec.proof.published_on)
        if pub is None:
            return f"unparseable publication date {rec.proof.published_on!r}"
        if pub >= self.cutoff:
            return (f"published {pub.isoformat()} is not strictly before "
                    f"cutoff {self.cutoff.isoformat()}")
        return None
