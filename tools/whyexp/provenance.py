"""Provenance replay: why did THIS item get THIS class?"""
from __future__ import annotations

from typing import Optional

_PRIMARY_RULE = ("exact bytes returned by a real tool call this session "
                 "(content-hash match on a primary observation)")
_OBSERVED_RULE = ("bytes matching something a tool returned this session "
                  "(hash match, non-primary observation)")
_CITED_RULE = ("cites a URL this session genuinely fetched "
               "(citation grounding)")
_INFERRED_RULE = ("no tool bytes or fetched URL back it — model output "
                  "without verification")


class _probe:
    """Minimal Evidence-shaped object for ledger queries."""

    def __init__(self, content: str):
        self.content = content
        self.source_class = None


def assignment_reason(evidence_content: str,
                      ledger) -> tuple[str, str]:
    """(source_class_value, reason) for one evidence item.

    Replays the assignment with the SAME ledger rules the pipeline used and
    names the specific rule that fires. With no ledger available, returns
    ("", "") — callers fall back to the recorded class, honestly marked.
    """
    if ledger is None:
        return "", ""
    assigned = ledger.assign_source_class(_probe(content=evidence_content))
    if ledger.is_primary_bytes(evidence_content):
        reason = _PRIMARY_RULE
    elif ledger.has_observation(evidence_content):
        reason = _OBSERVED_RULE
    elif ledger.cites_verified_url(evidence_content):
        reason = _CITED_RULE
    else:
        reason = _INFERRED_RULE
    return assigned.value, reason

