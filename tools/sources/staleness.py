"""staleness — persisted per-source health history, and what it proves.

Task 61 (source health) probes each registered API live, ON DEMAND.
This module makes those observations DURABLE and turns them into
evidence: APIs drift continuously, so a one-shot check rots the day
after it runs. A source that silently stops returning results is worse
than one that errors — eleven live-API defects passed the fixture suite
cleanly by looking exactly like "the literature is silent".

What is persisted per source (a JSON record next to this module's
registry — a file, not a service):

    last_ok            ISO timestamp of the last probe with a non-empty,
                       shape-correct result
    last_shape_match   ISO timestamp of the last shape validation that
                       passed (regardless of row count)
    consecutive_bad    how many recent probes were DEGRADED or BROKEN
    last_verdict       OK | DEGRADED | BROKEN | SKIPPED
    last_evidence      evidence string from the most recent probe

Derived status (the only thing downstream code should consume):

    HEALTHY   last probe OK
    STALE     once-healthy source whose last probe failed — the dangerous
              case: it USED to work, so its silence is a change, not a fact
    NEVER_OK  no successful observation on record — silence is unproven
              either way; the system must not claim staleness it has not
              earned
    UNSEEN    never probed

Network access: NONE. This module reads and writes JSON files and
consumes already-collected ProbeResults. It is safe under the no-socket
test guard. Probing stays opt-in behind CALLISTO_SOURCE_HEALTH_NET=1 in
tools.sources.health; callers record() the results here afterwards.

Classification ONLY. Nothing here raises or lowers any confidence
score, retries, or disables a source. The owner decides what to do;
the system's job is to stop pretending nothing is wrong.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: Derived statuses (distinct from probe verdicts: these carry HISTORY).
HEALTHY = "HEALTHY"
STALE = "STALE"
NEVER_OK = "NEVER_OK"
UNSEEN = "UNSEEN"

DEFAULT_FILENAME = "source_health_history.json"


def default_store_path() -> Path:
    """The history file lives next to the registry that defines the
    sources — one directory, discoverable without configuration."""
    return Path(__file__).resolve().parent / DEFAULT_FILENAME


@dataclass
class SourceHealth:
    """One source's durable health record."""
    source: str
    last_ok: Optional[str] = None          # ISO timestamp
    last_shape_match: Optional[str] = None
    consecutive_bad: int = 0
    last_verdict: str = ""                 # raw probe verdict
    last_evidence: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "last_ok": self.last_ok,
            "last_shape_match": self.last_shape_match,
            "consecutive_bad": self.consecutive_bad,
            "last_verdict": self.last_verdict,
            "last_evidence": self.last_evidence,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, source: str, d: dict) -> "SourceHealth":
        return cls(
            source=source,
            last_ok=d.get("last_ok"),
            last_shape_match=d.get("last_shape_match"),
            consecutive_bad=int(d.get("consecutive_bad") or 0),
            last_verdict=str(d.get("last_verdict") or ""),
            last_evidence=str(d.get("last_evidence") or ""),
            updated_at=str(d.get("updated_at") or ""),
        )

    @property
    def status(self) -> str:
        """Derived status — the history-aware verdict."""
        if self.last_verdict == "OK":
            return HEALTHY
        if self.last_ok is not None:
            # Worked before, failing now: the dangerous silent-drift case.
            return STALE
        if self.last_verdict:       # probed, but never succeeded
            return NEVER_OK
        return UNSEEN


class HealthStore:
    """JSON-backed per-source health history."""

    def __init__(self, path: Optional[os.PathLike | str] = None):
        self.path = Path(path) if path is not None else default_store_path()
        self._records: dict[str, SourceHealth] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                for name, rec in (raw.get("sources") or {}).items():
                    self._records[name] = SourceHealth.from_dict(name, rec)
            except (ValueError, OSError):
                # A corrupt history degrades to empty — it must never take
                # the pipeline down, and it must never be silently rewritten
                # with fabricated healthy state either.
                self._records = {}

    # ── persistence ────────────────────────────────────────────────────

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "written_at": _now_iso(),
            "sources": {n: r.to_dict() for n, r in
                        sorted(self._records.items())},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)

    # ── recording ──────────────────────────────────────────────────────

    def record(self, result: Any) -> SourceHealth:
        """Fold one tools.sources.health ProbeResult into the history.

        Duck-typed: needs .source, .verdict, and optionally .row_count
        and .evidence. SKIPPED probes are recorded verbatim but change
        neither the good nor the bad counters — an untested source is
        not a failing one.
        """
        name = getattr(result, "source", "")
        verdict = str(getattr(result, "verdict", "") or "")
        rec = self._records.get(name) or SourceHealth(source=name)
        now = _now_iso()
        rec.updated_at = now
        rec.last_verdict = verdict
        ev = getattr(result, "evidence", "")
        rec.last_evidence = "; ".join(ev) if isinstance(ev, list) \
            else str(ev or "")

        if verdict == "OK":
            rec.last_ok = now
            rec.last_shape_match = now
            rec.consecutive_bad = 0
        elif verdict in ("DEGRADED", "BROKEN"):
            rec.consecutive_bad += 1
            # shape_match keeps its old timestamp: a degraded source may
            # still be shaped right, just empty today.
        elif verdict == "SKIPPED":
            pass                    # no evidence either way
        self._records[name] = rec
        self._flush()
        return rec

    def record_all(self, results) -> list[SourceHealth]:
        return [self.record(r) for r in results]

    # ── reading ────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[SourceHealth]:
        return self._records.get(name)

    def status_of(self, name: str) -> str:
        rec = self._records.get(name)
        return rec.status if rec is not None else UNSEEN

    def all_records(self) -> dict[str, SourceHealth]:
        return dict(self._records)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


# ── Feeding history into the null classifier ───────────────────────────────
#
# tools.gaps.classify_null_kind is THE single membership rule for honest
# null vs retrieval failure. Its blind spot is exactly the one the live-API
# defects exposed: a source returns 200-with-zero-results, nothing errors,
# nothing is rejected-with-reasons — so the trace reads as HONEST NULL even
# though the source used to return hundreds of rows for the same query.
#
# The fix is evidence, not a new rule: when the sources a leaf actually
# touched include one whose own history says STALE (worked before, empty
# now), the null can no longer be called honest on that source's testimony.

def stale_sources_among(names) -> list[str]:
    """Which of these registry names have a history of working but a
    current record of failure? Reads the default store."""
    store = HealthStore()
    out = []
    for n in names:
        rec = store.get(n)
        if rec is not None and rec.status == STALE:
            out.append(n)
    return out


def amend_null_classification(kind: str, explanation: str,
                              source_names) -> tuple[str, str]:
    """Apply the health-history amendment to one null classification.

    Returns (possibly revised kind, possibly extended explanation).
    Rule: an otherwise-honest null that leaned on at least one source
    which USED to return data and NOW returns none is a RETRIEVAL_FAILURE
    — the owner action (RETRY, then investigate the source) applies, and
    'the literature is silent' would be a false claim. Sources with NO
    history or NEVER_OK records do NOT flip the verdict: absence of
    evidence about a source must not invent a failure.
    """
    stale = stale_sources_among(source_names)
    if not stale or kind != "honest_null":
        return kind, explanation
    note = ("health-history amendment: source(s) " + ", ".join(stale) +
            " previously returned results for known-good queries but the "
            "most recent observation was empty/broken — this null leans on "
            "at least one degraded source, so treat as RETRIEVAL_FAILURE "
            "(owner: retry / investigate source), not as silence")
    return ("retrieval_failure",
            explanation + " | " + note if explanation else note)
