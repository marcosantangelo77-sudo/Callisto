"""Event set construction with PROVABLE publication timestamps.

An Event is a dated occurrence you can point at. Its date is admissible ONLY
when a Wayback IMMUTABLE_SNAPSHOT proof (tools/sources/wayback.py) pins a
capture of the event's reference URL strictly at-or-before the event date.
Anything else is a hindsight-datable claim and is excluded — fail-closed,
matching the CutoffEnforcer convention: no proof ⇒ event dropped, never
assumed safe.

GDELT artlist rows carry a `seendate` (machine-recorded crawl timestamp) —
that is a provable *coverage* observation, used to DISCOVER candidate dates;
the wayback proof on the canonical source URL is what makes the event date
itself admissible.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from tools.sources.base import RestSource
from tools.sources.gdelt import GdeltAdapter, SPEC as SPEC_GDELT
from tools.sources.wayback import WaybackAdapter, SPEC as SPEC_WAYBACK


@dataclass
class Event:
    """One dated narrative event with an admissibility proof."""
    label: str                      # human-readable identifier
    event_date: dt.date             # t=0
    query: str                      # GDELT discovery query
    source_url: str                 # canonical page whose snapshot proves the date
    seendate: str = ""              # GDELT first-seen stamp (discovery only)
    proof_locator: str = ""         # wayback snapshot URL when proven
    proof_published_on: Optional[dt.date] = None
    proof_sha256: str = ""

    @property
    def proven(self) -> bool:
        return bool(self.proof_locator) and self.proof_published_on is not None


@dataclass
class EventSet:
    events: list = field(default_factory=list)
    excluded: list = field(default_factory=list)   # (label, reason)

    def summary(self) -> dict:
        return {"n_proven": len(self.events),
                "n_excluded": len(self.excluded),
                "excluded_reasons": [r for _, r in self.excluded]}


def _gdelt_candidate_dates(gd: GdeltAdapter, query: str,
                           timespan: str, limit: int) -> list:
    """First-seen coverage timestamps from GDELT artlist, oldest first."""
    data = gd.doc_query(query, mode="artlist", timespan=timespan, limit=limit)
    out = []
    for a in data.get("articles", []):
        sd = str(a.get("seendate", ""))
        if len(sd) >= 8 and sd[:8].isdigit():
            d = dt.datetime.strptime(sd[:8], "%Y%m%d").date()
            out.append((d, sd, a))
    out.sort(key=lambda t: t[0])
    return out


def build_event_set(
    query: str,
    source_url: str,
    label_prefix: str,
    timespan: str = "3y",
    limit: int = 250,
    min_gap_days: int = 21,
    gdelt: Optional[GdeltAdapter] = None,
    wayback: Optional[WaybackAdapter] = None,
) -> EventSet:
    """Discover candidate event dates via GDELT coverage spikes/first-hits,
    then admit each ONLY with a wayback snapshot proof of the canonical
    source_url dated at-or-before the candidate date. Events closer than
    min_gap_days are collapsed to the first (avoid double-counting one
    narrative episode)."""
    gd = gdelt or GdeltAdapter(RestSource(SPEC_GDELT))
    wb = wayback or WaybackAdapter(RestSource(SPEC_WAYBACK))

    es = EventSet()
    last_date: Optional[dt.date] = None
    for d, sd, _art in _gdelt_candidate_dates(gd, query, timespan, limit):
        if last_date is not None and (d - last_date).days < min_gap_days:
            continue
        esummary = EventSet.summary(es)
        label = f"{label_prefix}-{d.isoformat()}"
        proof, reason = wb.snapshot_proof(source_url, before=d + dt.timedelta(days=1))
        if proof is None:
            es.excluded.append((label, reason))
            continue
        if proof.published_on > d:
            # snapshot exists but postdates the event: the page did NOT yet
            # say it — cannot prove the event was public by then. Fail-closed.
            es.excluded.append(
                (label, f"nearest capture {proof.published_on} postdates event"))
            continue
        es.events.append(Event(
            label=label, event_date=d, query=query, source_url=source_url,
            seendate=sd, proof_locator=proof.locator,
            proof_published_on=proof.published_on,
            proof_sha256=proof.content_sha256))
        last_date = d
    return es
