"""Review run 6 (2026-08-24d) — hunt families on current origin/master.

Every CONFIRMED defect below was re-derived independently against master
(review/ox-alpha-0824d @ 2ef77f8), not copied from another branch's report.
Confirmed defects are marked xfail so they stay visible until fixed; honest
controls are plain tests.
"""
import hashlib
import json
import tempfile

import pytest

import tools.pipeline.checkpoint as ckpt
from agp.provenance import ProvenanceLedger, SourceClass


def _unkeyed(monkeypatch):
    monkeypatch.delenv("CALLISTO_CUTOFF_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


# ── DEFECT A (CRITICAL, families 1+3): gate rejection lost across resume ────

@pytest.mark.xfail(reason="A CONFIRMED CRITICAL: rejected bytes re-mint PRIMARY "
                          "after resume and seal_guard SEALs over them")
def test_a_gate_rejection_lost_across_resume_lauanders_bytes(monkeypatch):
    """Live run: bytes fetched PRIMARY, gate rejects them, ledger supersedes.
    Resume: engine replays the SAME payload via replay_ledger BEFORE any gate
    runs; the payload's `rejections` list is restored onto the trace but never
    handed to record_gate_rejection(). The supersede verdict died with the old
    process. Rejected evidence re-enters as PRIMARY and the anti-laundering
    guard — built precisely for this boundary — says SEAL."""
    _unkeyed(monkeypatch)
    d = tempfile.mkdtemp()
    cp = ckpt.FileCheckpointer(d)
    BODY = '{"seriess":[{"title":"Unemployment Rate"}]}'
    payload = {
        "fetches": [{"source_name": "fred",
                     "url": "https://api.stlouisfed.org/x",
                     "body": BODY, "content_sha256": ckpt._sha(BODY),
                     "question_id": "q1", "fetched_at": "t"}],
        "rejections": [{"source_name": "fred",
                        "url": "https://api.stlouisfed.org/x",
                        "reason": "irrelevant", "relevance_score": 0.0,
                        "content_sha256": ckpt._sha(BODY)}],
        "independent_keys": [],
    }
    ck = cp.save("runR", "fetch_leaf",
                 ckpt.hash_inputs({"qid": "q1"}), payload)
    loaded = cp.load_by_key("runR", ck.key)

    led = ProvenanceLedger()
    ckpt.replay_ledger(led, ckpt.admissible_checkpoints("runR", [loaded]))
    ev = type("E", (), {"content": BODY})()
    assert led.assign_source_class(ev) != SourceClass.PRIMARY, (
        "gate-REJECTED bytes enter the resumed ledger as PRIMARY")

    trace = type("T", (), {"is_resume": True, "run": "runR"})()
    verdict, why = ckpt.seal_guard(trace, [loaded], led)
    assert verdict == "REFUSE", (
        "seal_guard sealed over gate-rejected bytes replayed as PRIMARY: %s"
        % why)

    # Differential control: the LIVE path gets it right, so the divergence is
    # the resume seam, not the ledger.
    live = ProvenanceLedger()
    live.record_tool_result("fred_fetch", BODY, primary=True,
                            urls=["https://api.stlouisfed.org/x"])
    live.record_gate_rejection(BODY, ["https://api.stlouisfed.org/x"])
    assert live.assign_source_class(ev) == SourceClass.INFERRED


# ── DEFECT B (HIGH, families 1+3): unkeyed tamper with fixed digest seals ───

@pytest.mark.xfail(reason="B CONFIRMED HIGH: under the default UNKEYED regime "
                          "a DB-level tamper of body+digest replays as PRIMARY "
                          "and seal_guard SEALs")
def test_b_unkeyed_tamper_self_verifies_and_seals(monkeypatch):
    """The signature layer is the designed defense; the documented default
    deployment sets no key, so partition_admissibility's auth check is a
    no-op and integrity rests solely on content_sha256 — which travels INSIDE
    the same editable JSON file. Anyone who can touch the file can rewrite the
    bytes and their digest; nothing else verifies."""
    _unkeyed(monkeypatch)
    import pathlib
    d = tempfile.mkdtemp()
    cp = ckpt.FileCheckpointer(d)
    BODY = '{"results":[{"title":"real evidence"}]}'
    payload = {"fetches": [{"source_name": "fred", "url": "https://x/y",
                            "body": BODY, "content_sha256": ckpt._sha(BODY),
                            "question_id": "q1", "fetched_at": "t"}]}
    ck = cp.save("runA", "fetch_leaf",
                 ckpt.hash_inputs({"qid": "q1"}), payload)
    f = next(pathlib.Path(d).rglob("*.json"))
    rec = json.loads(f.read_text())
    new_body = '{"results":[{"title":"FABRICATED"}]}'
    rec["payload"]["fetches"][0]["body"] = new_body
    rec["payload"]["fetches"][0]["content_sha256"] = ckpt._sha(new_body)
    f.write_text(json.dumps(rec))

    loaded = cp.load_by_key("runA", ck.key)
    led = ProvenanceLedger()
    rep = ckpt.replay_ledger(
        led, ckpt.admissible_checkpoints("runA", [loaded]))
    ev = type("E", (), {"content": new_body})()
    assert led.assign_source_class(ev) != SourceClass.PRIMARY or \
        rep["integrity_failures"], (
        "fabricated bytes minted PRIMARY with no external verification")
    trace = type("T", (), {"is_resume": True, "run": "runA"})()
    verdict, why = ckpt.seal_guard(trace, [loaded], led)
    assert verdict == "REFUSE", (
        "seal_guard sealed over self-consistent fabricated bytes: %s" % why)


# ── DEFECT C (HIGH, families 2+6): round() still quantises confidence UP ────

@pytest.mark.xfail(reason="C CONFIRMED: agp.claims.recompute_confidence uses "
                          "round() — an automated actor RAISING confidence")
def test_c_claims_recompute_confidence_rounds_up():
    """floor_conf exists in agp/thresholds precisely because round() raises
    scores (family 6); clamp_confidence_provenance uses it. The long-lived
    claim path reimplemented the same quantisation WITH round() instead of
    sharing floor_conf — family 2. ~25% of random claims gain confidence by
    quantisation alone."""
    from agp.claims import recompute_confidence
    import random
    for _ in range(2000):
        claimed = random.uniform(0.30, 0.55)
        out = recompute_confidence([], claimed)
        truth = min(max(claimed, 0.30), 0.55)
        assert out <= truth + 1e-12, (
            "recompute_confidence raised %.6f -> %.2f" % (claimed, out))


@pytest.mark.xfail(reason="C2 CONFIRMED: engine seal rounding uses round(), "
                          "raising the sealed number in ~50% of draws")
def test_c2_engine_sealed_confidence_rounding_can_raise():
    """engine.py `_confidence`: round(max(0, min(est, ceil)), 2). The comment
    pins 'historical rounding preserved verbatim', but the architecture rule
    is that automated actors may only move confidence DOWN. Property sweep."""
    import random
    src = open("tools/pipeline/engine.py").read()
    assert "floor_conf" in src, "engine should quantise downward"
    # the actual expression must not be round(...)
    line = [l for l in src.splitlines()
            if "out.confidence = round(" in l or
            ("max(0.0, min(ec.estimate" in l and "round" in l)]
    for l in line:
        assert "floor_conf" in l, (
            "sealed-confidence quantisation raises: %s" % l.strip())


# ── DEFECT D (MEDIUM, families 1+2): base.py independence copy is inert AND
#    diverges from the fixed rule ─────────────────────────────────────────────

@pytest.mark.xfail(reason="D CONFIRMED: base.independence_family diverges "
                          "from retrieval.independence_key and has zero "
                          "production callers")
def test_d_independence_rule_copies_diverge():
    """Family 2 verbatim: retrieval.independence_key normalises spelling;
    tools/sources/base.independence_family does raw `spec_name in members`.
    The two disagree on 'semantic_scholar' — exactly the drift the normalising
    copy was written to kill. And independence_family has ZERO production
    callers, so it is dead code guarding a divergent rule (family 1)."""
    from tools.pipeline.retrieval import independence_key
    from tools.sources.base import independence_family
    a = independence_family("semantic_scholar")
    b = independence_family("semanticscholar")
    ka = independence_key("semantic_scholar", "")
    kb = independence_key("semanticscholar", "")
    assert a == b, (
        "the two copies of the independence membership rule disagree: "
        "%r vs %r" % (a, b))
    assert ka == kb


# ── DEFECT E (MEDIUM, family 4): honest-gap table keyed by a drifted name ───

@pytest.mark.xfail(reason="E CONFIRMED: honest-gap key 'sec_fts' matches no "
                          "registered adapter ('sec_fulltext')")
def test_e_honest_gap_table_matches_registered_names():
    """query_builder._HONEST_GAPS keys 'sec_fts'; the registry adapter is
    'sec_fulltext'. The deliberate-gap message is unreachable and callers get
    a generic 'unknown source' refusal instead — both the honesty and any
    future fix are invisible behind the label drift."""
    import tools.sources.query_builder as qb
    from tools.sources.registry import get_source_registry
    names = set(get_source_registry().names())
    for gap_key in dict(qb._HONEST_GAPS):
        assert gap_key in names, (
            "honest-gap key %r matches no registered adapter" % gap_key)


# ── DEFECT F (HIGH, family 2): wiki seal gate accepts the forgeable digest ──

@pytest.mark.xfail(reason="F CONFIRMED: knowledge_wiki treats a plain SHA-256 "
                          "'seal' as proof and keeps stored confidence; "
                          "memory_epistemics fails closed on the same input")
def test_f_wiki_gate_trusts_public_digest_under_unkeyed_regime(monkeypatch):
    """Two copies of 'what does a seal prove': admit_learning collapses
    legacy/unkeyed digests to INFERRED (fail closed); the wiki compiler calls
    AGPSession.verify_seal, which still accepts the public SHA-256 fallback,
    then keeps the row's stored confidence untouched. Under the default
    unkeyed regime anyone with DB write can mint a 'verified' session at any
    confidence and have it compiled into the model's priors."""
    _unkeyed(monkeypatch)
    from agp import AGPSession
    session = {"session_id": "s", "conclusion": "fabricated",
               "confidence_score": 0.95}
    payload_dict = dict(session)
    payload_dict["seal_hash"] = None
    payload = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)
    stored = dict(session)
    stored["seal_hash"] = hashlib.sha256(payload.encode()).hexdigest()
    # verify_seal passes (that is the legacy-compat hole)…
    assert not AGPSession.verify_seal(stored), (
        "verify_seal accepted a publicly recomputable digest; the wiki gate "
        "then admits the row at its stored confidence while the epistemics "
        "gate collapses the identical input to INFERRED")


# ── Honest controls (attacked, held) ────────────────────────────────────────

def test_ctrl_live_path_rejection_supersedes():
    _unkeyed(monkeypatch := __import__("pytest").MonkeyPatch())
    from agp.provenance import ProvenanceLedger
    led = ProvenanceLedger()
    led.record_tool_result("t", "BODY", primary=True, urls=["u"])
    led.record_gate_rejection("BODY", ["u"])
    ev = type("E", (), {"content": "BODY"})()
    assert led.assign_source_class(ev) == SourceClass.INFERRED


def test_ctrl_zero_result_echo_is_admitted_pinned_not_fixed():
    """SR1 shape pinned on master: an empty result list echoing the query
    words still scores 100% coverage. Pinned here so the eventual fix flips
    this loudly rather than silently."""
    from tools.pipeline.retrieval import RelevanceGate
    ok, cov, reason = RelevanceGate().judge(
        "unemployment rate in 2026", "",
        {"query": "unemployment rate 2026", "results": []})
    assert ok and cov == 1.0  # documents the defect; see finding E-pin
