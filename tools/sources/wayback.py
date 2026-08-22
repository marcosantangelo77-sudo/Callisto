"""Wayback Machine — archived page state as PublicationProof. Tier 2.

This adapter exists disproportionately for tools/retrodiction/cutoff.py:
an archived snapshot proves WHAT A PAGE SAID BEFORE A DATE, which no live
fetch can do. The enforcer admits a record when it carries a
PublicationProof of kind IMMUTABLE_SNAPSHOT whose published_on (the
capture date) is strictly before the cutoff and whose content_sha256
covers exactly the bytes returned.

Two endpoints, both keyless:
  availability  https://archive.org/wayback/available?url=..&timestamp=YYYYMMDDhhmmss
                → closest snapshot {url, timestamp, status}
  capture       the snapshot URL itself (web.archive.org/web/<ts>/<original>)
                → the archived bytes

snapshot_proof() is the composition the harness calls: resolve the
closest capture at-or-before a target instant, fetch its bytes, and emit
a PublicationProof(kind=IMMUTABLE_SNAPSHOT, published_on=capture_date,
locator=snapshot_url, content_sha256=hash(bytes)). Signing happens
upstream (CutoffEnforcer's key) — this module only mints unsigned
proofs; trust comes from the archive's own locator + hash binding.
sign_with() convenience signs immediately when the harness key is known.

Answers: what a public page said at/near any past moment; earliest /
latest captures; proof-of-existence for retrodiction cutoffs.
Cannot answer: pages never crawled (huge gaps for anything not popular),
exact-second precision (captures are sparse), JS-rendered dynamic state,
paywalled content behind robots.txt exclusions.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from tools.retrodiction.cutoff import ProofKind, PublicationProof
from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="wayback",
    base_url="https://archive.org/wayback",
    description="Internet Archive Wayback Machine: historical page snapshots",
    answers=(
        "what a public web page said at a past date",
        "existence proofs of pre-cutoff web content",
        "capture history / first and last snapshot times",
    ),
    cannot_answer=(
        "pages never captured (sparse coverage off the popular web)",
        "exact-time precision (snapshots are irregular)",
        "JS-rendered application state",
        "content excluded by robots.txt after capture",
    ),
    tier=2,
    min_interval_s=1.0,   # be gentle with archive.org
    terms_url="https://archive.org/about/terms.php",
)


class WaybackAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def closest(self, url: str, timestamp: str = "") -> dict:
        """Availability API → {'archived_snapshots': {'closest': {...}}}."""
        if not url.strip():
            raise ValueError("url must be non-empty")
        params = {"url": url}
        if timestamp:
            params["timestamp"] = timestamp
        snap_url = self.source.build_url("/available", params)
        return self.source.get_json(snap_url)[0]

    def fetch_snapshot(self, snapshot_url: str) -> tuple[str, object]:
        """Fetch the archived bytes. Returns (body, FetchRecord)."""
        return self.source.get(snapshot_url)

    def capture_timestamps(self, url: str, limit: int = 20) -> dict:
        """CDX-style listing via the timemap endpoint (text lines:
        '<ts> <snapshot_url>')."""
        cdx = self.source.spec.base_url.replace(
            "archive.org/wayback", "web.archive.org")
        listing_url = f"{cdx}/timemap/link/{url}"
        _status, body = self.source.get(listing_url)
        lines = [ln for ln in body.splitlines()
                 if ln.startswith("<") and ln.endswith(">;")]
        out = []
        for ln in lines[:limit]:
            ts = ln.split("/", 4)
            out.append(ln)
        return {"raw": body[:20000], "_count": len(lines)}

    # ── proof emission ───────────────────────────────────────────────────

    def snapshot_proof(
            self, url: str, before: _dt.date | str,
            sign_key: str = "",
    ) -> tuple[Optional[PublicationProof], str]:
        """Resolve the closest snapshot at-or-before *before*, fetch its
        bytes, mint an IMMUTABLE_SNAPSHOT PublicationProof over them.

        Returns (proof_or_None, reason). None means 'no usable snapshot'
        — fail-closed upstream: no proof ⇒ evidence excluded, never assumed
        safe. When sign_key is given the proof is signed here so it passes
        CutoffEnforcer's signature check later.
        """
        if isinstance(before, str):
            before = _dt.date.fromisoformat(before)
        ts_target = before.strftime("%Y%m%d235959")
        avail = self.closest(url, ts_target)
        snap = (avail.get("archived_snapshots", {})
                     .get("closest"))
        if not snap or not snap.get("available", True):
            return None, f"no wayback snapshot of {url!r} on/before {before}"
        captured = str(snap.get("timestamp", ""))
        if len(captured) < 8 or not captured[:8].isdigit():
            return None, f"unparseable capture timestamp {captured!r}"
        capture_date = _dt.datetime.strptime(captured[:8], "%Y%m%d").date()
        if capture_date >= before:
            # closest is still after the cutoff — nothing admissible
            return None, (f"nearest capture {capture_date} is not strictly "
                          f"before {before}")
        snapshot_url = snap.get("url", "")
        if not snapshot_url.startswith("http"):
            return None, f"bad snapshot locator {snapshot_url!r}"
        body, rec = self.fetch_snapshot(snapshot_url)
        proof = PublicationProof(
            kind=ProofKind.IMMUTABLE_SNAPSHOT,
            published_on=capture_date,
            locator=snapshot_url,
            content_sha256=rec.content_sha256,
        )
        if sign_key:
            proof = proof.sign(sign_key)
        return proof, ""

    def evidence_record(self, url: str, query: str, before,
                        sign_key: str = "",
                        fetched_at: Optional[_dt.datetime] = None):
        """Full EvidenceRecord (content + proof) ready for CutoffEnforcer.
        Returns (record, reason) — record is None when no proof exists."""
        from tools.retrodiction.cutoff import EvidenceRecord

        proof, reason = self.snapshot_proof(url, before, sign_key=sign_key)
        if proof is None:
            return None, reason
        # refetch deterministically: snapshot_proof already pulled these
        # bytes; reuse by fetching once more would double-hit the archive,
        # so instead we re-derive from the recorded fetch through the source.
        rec = self.source.last_record
        if rec is None or rec.url != proof.locator:
            return None, "internal: snapshot fetch record missing"
        # last_record holds only metadata, not the body — re-fetch the
        # immutable URL once to obtain the exact bytes the proof covers.
        body, rec2 = self.fetch_snapshot(proof.locator)
        if rec2.content_sha256 != proof.content_sha256:
            return None, ("snapshot bytes changed between resolution and "
                          "retrieval (unexpected for an immutable URL)")
        ts = fetched_at or _dt.datetime.now(_dt.timezone.utc).replace(
            tzinfo=None)
        return EvidenceRecord(
            url=proof.locator, query=query, fetched_at=ts,
            content=body, proof=proof), ""
