"""C1 — a missing or empty content_sha256 bypasses the integrity check.

replay_ledger guards with `if digest and _sha(body) != digest`. A FALSY digest
skips verification entirely and the bytes are still replayed via
record_tool_result(..., primary=True). One absent JSON field in a checkpoint
file therefore mints PRIMARY provenance for arbitrary fabricated bytes, and
seal_guard then returns SEAL over them.

Second defect in the same branch: with an empty digest the dedup key is "",
so a second, DIFFERENT record is silently dropped as a "duplicate".

This is the same shape as W5: an integrity check that quietly does nothing
when its input is absent, on the one path that is supposed to be load-bearing.
"""
import hashlib
import pytest

from tools.pipeline.checkpoint import Checkpoint, replay_ledger


class _Ledger:
    """Minimal ledger recording what was admitted and at what trust."""
    def __init__(self):
        self.records = []

    def record_tool_result(self, tool, body, primary=True, urls=None):
        self.records.append({"tool": tool, "body": body,
                             "primary": primary, "urls": urls})


def _ck(fetches, key="k1"):
    return Checkpoint(key=key, run="r1", stage="fetch_leaf",
                      input_hash="ih", payload={"fetches": fetches})


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def test_absent_digest_is_an_integrity_failure():
    """No content_sha256 at all -> must NOT be admitted as PRIMARY."""
    led = _Ledger()
    ck = _ck([{"body": "FABRICATED", "url": "https://evil/x"}])  # no digest
    rep = replay_ledger(led, [ck])
    assert rep["integrity_failures"], "absent digest passed the integrity check"
    assert not led.records, "unverified bytes were recorded into the ledger"


def test_empty_digest_is_an_integrity_failure():
    led = _Ledger()
    ck = _ck([{"body": "FABRICATED", "url": "https://evil/x",
               "content_sha256": ""}])
    rep = replay_ledger(led, [ck])
    assert rep["integrity_failures"], "empty digest passed the integrity check"
    assert not led.records


def test_empty_digest_does_not_poison_the_dedup_key():
    """Two DIFFERENT unverified records must not collapse into one "duplicate"."""
    led = _Ledger()
    ck = _ck([{"body": "A", "url": "https://a", "content_sha256": ""},
              {"body": "B", "url": "https://b", "content_sha256": ""}])
    rep = replay_ledger(led, [ck])
    assert rep["skipped_duplicates"] == 0, \
        "distinct records deduped against each other on an empty digest"


def test_valid_digest_still_replays():
    """The fix must not break the legitimate path."""
    led = _Ledger()
    body = "REAL BYTES"
    ck = _ck([{"body": body, "url": "https://ok",
               "content_sha256": _sha(body)}])
    rep = replay_ledger(led, [ck])
    assert not rep["integrity_failures"]
    assert rep["replayed"] == 1 and len(led.records) == 1
