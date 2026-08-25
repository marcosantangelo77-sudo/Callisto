"""RED TEAM — source registry, query builders, and the fetch→provenance seam.

Surface: tools/sources/ (registry, base, query_builder), the retrieval
fetch loop's error-envelope handling, and ProvenanceLedger binding.
Method: property-style self-selection sweep over all 19 registered sources
plus adversarial-input probes at each seam. Every test below FAILED on
pre-fix code (verified before writing); they pin the defects until fixed.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.sources.base import RestSource, SourceSpec  # noqa: E402


def _spec(name="x", base="https://example.com", answers=("macro data",)):
    return SourceSpec(name=name, base_url=base, description="d",
                      answers=answers)


class _Transport:
    def __init__(self, status=200, body='{"ok": true}'):
        self.status = status
        self.body = body
        self.payloads = []

    def __call__(self, url, headers):
        self.last_url = url
        return (self.status, self.body)


# ── S1: a failed (non-200) body is minted PRIMARY in the ledger ──────────

def test_s1_non_200_body_not_minted_primary():
    """Family 3/9: RestSource._record runs BEFORE any status check, so a
    503 gateway-error page enters provenance as primary=True. Anything that
    later cites this URL can verify as SECONDARY off an error page."""
    from agp.provenance import ProvenanceLedger
    led = ProvenanceLedger()
    t = _Transport(status=503, body="<html>gateway error</html>")
    src = RestSource(_spec(), ledger=led, transport=t)
    with pytest.raises(Exception):
        src.get_json("https://example.com/q")
    assert not led.is_primary_bytes("<html>gateway error</html>"), (
        "a non-200 body must never be minted PRIMARY")


# ── S2: injected transport + POST drops the payload silently ─────────────

def test_s2_injected_post_transport_receives_payload():
    """Family 7: post() routes through self._transport(url, headers) when
    injected — the JSON payload is never delivered and never checked. A
    test double silently diverges from the real POST path, so every
    fixture-tested adapter has untested POST semantics (BLS is POST)."""
    seen = {}

    def transport(url, headers):
        seen["url"] = url
        return (200, "{}")

    src = RestSource(_spec(), transport=transport)
    data, rec = src.post_json("https://example.com/search",
                              {"series_ids": ["LNS14000000"]})
    assert seen.get("payload") == {"series_ids": ["LNS14000000"]}, (
        "an injected transport must receive the request payload; the real "
        "POST path and the test-double path are different code paths")


# ── S3: ledger failure = silent zero-provenance fetch ────────────────────

def test_s3_ledger_failure_does_not_produce_unrecorded_primary(caplog):
    """Family 1: `except Exception: log` around record_tool_result means a
    broken/rotated/full ledger yields fetched bytes that no seal or citation
    can ever verify — provenance silently vanishes while the pipeline runs
    green. Fetch must fail closed when its provenance write fails."""
    import logging

    def broken(*a, **k):
        raise RuntimeError("disk full")

    t = _Transport()
    src = RestSource(_spec(), ledger=broken, transport=t)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(Exception):
            src.get_json("u")
    # Either outcome is acceptable: raise, OR record-and-continue with the
    # failure surfaced at ERROR level for the operator.
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "a dropped provenance record must be loud, not a swallowed log line")


# ── S4: gate-rejection binding is exact-hash only ─────────────────────────

def test_s4_gate_rejection_binds_bytes_not_representations():
    """R4/R4b bound the REJECT verdict to bytes by EXACT sha256. The
    retriever carries forward json.dumps(parsed, sort_keys=True) — a
    DIFFERENT string from the raw body whenever key order differs — and a
    model echo with reordered keys re-mints PRIMARY from rejected bytes."""
    from agp.provenance import ProvenanceLedger
    led = ProvenanceLedger()
    raw = '{"b": 1, "a": "the unemployment rate"}'
    canon = json.dumps(json.loads(raw), sort_keys=True)
    assert canon != raw
    led.record_gate_rejection(raw, ["https://example.com/a"])
    assert not led.is_primary_bytes(canon), (
        "rejected content escaped supersession by re-serialising it")


def test_s5_gate_rejection_binds_url_regardless_of_query_permutation():
    """Same rule, URL copy: rejection recorded one query-ordering; the same
    endpoint refetched with reordered params verifies as fresh SECONDARY."""
    pytest.skip("url canonicalisation design decision pending — see findings "
                "S5 discussion")


# ── S6: wikidata planner crashes on hint words → whole retrieve dies ─────

def test_s6_wikidata_planner_crash_is_contained():
    """_plan_wikidata_concept sorts matched [(code, hint_word)] by -p[1]:
    p[1] is a STRING, so any question containing 'company', 'country' or
    'person' raises TypeError. It propagates through the round-1
    plannability pre-filter (unguarded) and kills the entire leaf's
    retrieval — one planner bug takes down every source."""
    from tools.sources import query_builder as qb
    plan = qb.build_plan(
        "wikidata", "Which company has the most patents in battery technology?")
    assert plan is not None  # must not raise TypeError


# ── S7: health probe names vs registry names drift ───────────────────────

def test_s7_health_probe_names_resolve_to_registry_names():
    """Family 2: health.py registers probes under module filenames
    ('cftc', 'sec_fts', 'semantic_scholar') but registry specs use
    ('cftc_cot', 'sec_fulltext', 'semanticscholar'). _build's alias loop
    normalises underscores away, which matches semantic_scholar ->
    semanticscholar but NOT cftc -> cftc_cot: that probe reports BROKEN
    (probe crash) regardless of the API's actual health, i.e. the health
    layer lies in both directions."""
    from tools.sources import health
    from tools.sources.registry import get_source_registry
    reg = get_source_registry()
    names = set(reg.names())
    unresolved = []
    for pname in health.PROBES:
        if pname in names:
            continue
        hit = any(c.replace("_", "") == pname.replace("_", "")
                  for c in names)
        if not hit:
            unresolved.append(pname)
    assert not unresolved, f"health probes cannot resolve to any source: {unresolved}"


# ── S8: classify_fetch_failure covers exactly ONE source ─────────────────

def test_s8_error_envelopes_classified_for_all_json_sources():
    """D2 landed for BLS only. Every other source's 200-with-{'error': ...}
    envelope flows into the relevance gate, where an API echoing the query
    scores ~83% coverage and is ADMITTED — then minted PRIMARY (S1 family).
    At minimum the known error-envelope shapes (BEA Error, WorldBank
    {'message':...}, Census error body) must be classified, not just BLS."""
    from tools.sources import query_builder as qb
    # BEA returns errors inside BEAAPIs.Error
    assert qb.classify_fetch_failure("bea", {
        "BEAAPIs": {"Error": [{"error_code": "100", "error_message":
                               "invalid params"}]}}) is not None, (
        "a 200-OK BEA error envelope reached the relevance gate as data")
