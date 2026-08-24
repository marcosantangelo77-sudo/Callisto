"""Evidence age measurement (build/evidence-age).

A question can take tens of minutes; evidence fetched at minute 2 is stale
by seal time and nothing said so. These tests prove the run RECORDS that
age honestly — oldest, newest, median — and does NOT pretend a wide window
was simultaneous.

MEASUREMENT ONLY: nothing in these tests (or the code under them) may
adjust confidence on age. No decay function, no staleness penalty.
"""

from datetime import datetime, timedelta, timezone

from tools.pipeline.engine import FetchResult, PipelineResult, \
    evidence_age_summary


def _fetch(seconds_ago: float) -> FetchResult:
    ts = (datetime.now(timezone.utc)
          - timedelta(seconds=seconds_ago)).isoformat()
    return FetchResult(source_name="s", url="https://u", content_sha256="ab",
                       body="{}", parsed={}, question_id="q1",
                       fetched_at=ts)


# ── 1. Long-window spread is reported honestly ───────────────────────────

def test_wide_window_reports_oldest_and_newest():
    # Evidence fetched 43 minutes ago and 30 seconds ago must NOT be
    # presented as simultaneous.
    fetches = [_fetch(43 * 60), _fetch(60), _fetch(30)]
    ea = evidence_age_summary(fetches)
    assert ea["n"] == 3
    assert ea["oldest_s"] > 42 * 60          # the minute-2 fetch is old
    assert ea["newest_s"] < 60               # the last fetch is fresh
    assert ea["oldest_s"] - ea["newest_s"] > 40 * 60   # spread preserved


def test_median_is_the_middle_age():
    # Timestamps are captured at slightly different instants, so assert
    # ordering/tolerance rather than bit-exact equality.
    fetches = [_fetch(100), _fetch(200), _fetch(300)]
    ea = evidence_age_summary(fetches)
    assert 190 <= ea["median_s"] <= 215


def test_even_count_median_interpolates():
    fetches = [_fetch(100), _fetch(300)]
    ea = evidence_age_summary(fetches)
    assert 190 <= ea["median_s"] <= 215


# ── 2. Unknown ages are unknown, never zero ──────────────────────────────

def test_legacy_fetch_without_timestamp_reports_unknown():
    legacy = FetchResult(source_name="s", url="https://u",
                         content_sha256="ab", body="{}", parsed={},
                         question_id="q1")     # no fetched_at
    ea = evidence_age_summary([legacy])
    assert ea["oldest_s"] is None
    assert ea["newest_s"] is None
    assert ea["median_s"] is None
    assert ea["n"] == 1


def test_mixed_timestamped_and_legacy_uses_only_known_ages():
    ea = evidence_age_summary([_fetch(120),
                               FetchResult("s", "u", "a", "{}", {}, "q")])
    assert ea["n"] == 2
    assert ea["n_timestamped"] == 1
    assert ea["oldest_s"] >= 120              # only the timestamped one


def test_naive_timestamp_treated_as_utc():
    ts = (datetime.now(timezone.utc)
          - timedelta(seconds=90)).replace(tzinfo=None).isoformat()
    f = FetchResult("s", "u", "ab", "{}", {}, "q1", fetched_at=ts)
    ea = evidence_age_summary([f])
    assert 0 <= ea["oldest_s"] < 600


# ── 3. The sealed result carries the summary ────────────────────────────

def test_pipeline_result_summary_includes_evidence_age():
    r = PipelineResult(root_query="q", sealed=True,
                       fetches=[_fetch(2580)])   # 43 minutes ago
    r.evidence_age = evidence_age_summary(r.fetches)
    s = r.summary_dict()
    assert s["evidence_age"]["oldest_s"] > 2500
    assert s["evidence_age"]["newest_s"] > 2500   # one fetch: it IS the oldest


# ── 4. Checkpoint round-trip preserves the ORIGINAL fetch time ──────────

def test_fetch_payload_roundtrip_preserves_fetched_at():
    from tools.pipeline.engine import dataclasses as _dc  # noqa: F401
    import dataclasses
    from tools.pipeline.engine import _fetch_from_payload

    fr = _fetch(3600)
    rec = dataclasses.asdict(fr)
    back = _fetch_from_payload(rec)
    assert back.fetched_at == fr.fetched_at


def test_resume_does_not_reset_fetch_time_to_resume_time():
    import dataclasses
    from tools.pipeline.engine import _fetch_from_payload

    # A checkpoint written an hour ago restores a fetch stamped an hour
    # ago — not "now". Otherwise every resumed run would report fresh.
    hour_ago = (datetime.now(timezone.utc)
                - timedelta(hours=1)).isoformat()
    rec = {"source_name": "s", "url": "u", "content_sha256": "ab",
           "body": "{}", "parsed": None, "question_id": "q1",
           "fetched_at": hour_ago}
    back = _fetch_from_payload(rec)
    ea = evidence_age_summary([back])
    assert ea["oldest_s"] > 55 * 60
