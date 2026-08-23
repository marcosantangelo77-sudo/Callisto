"""RED TEAM — checkpointing & resume boundary (method B: differential).

Surface: checkpointing and resume.
Method: differential — live vs resumed must agree; and any state a resumed
run trusts must be as trustworthy as the state a live run computes itself.

The module's own contract (tools/pipeline/checkpoint.py docstring §4):
    "Resumption must never become a way to launder evidence whose
     provenance was lost."
And engine.py's comment on restoring the retrieval trace:
    "a resumed run scores exactly what the equivalent live run scored."

Every test below is a DIFFERENTIAL between an honest live run and a resumed
run under one realistic perturbation:

  R1  tampered answer_leaf checkpoint (leaf answer/confidence/evidence class)
      -> resumed run seals a FABRICATED conclusion at inflated confidence
  R2  self-consistent fetch forgery (body + matching content_sha256)
      -> integrity check compares a file against ITSELF; forged bytes seal
  R3  deleted fetch_leaf checkpoint -> resumed run seals over ZERO fetches,
      surviving on evidence records inside the answer_leaf checkpoint
  R4  400-day-backdated produced_at -> stale evidence seals with no note;
      nothing downstream consults trace.oldest_produced_at()
  R5  cross-run laundering: seal_guard calls cp.list_all() (the WHOLE store,
      every run ever checkpointed) and replays it into THIS run's ledger ->
      another run's fetched bytes read as PRIMARY here; another run's URLs
      make a fabricated claim SECONDARY here

Invariant under attack: for any perturbation P, resumed_run(P) may not be
MORE confident / less evidenced than live_run(). These tests fail today.

Run: python3 -m pytest tests/test_redteam_resume_boundary.py -q
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

try:
    from tests.helpers.no_socket import NoSocket
    _ns = NoSocket(); _ns.install()
except Exception:  # pragma: no cover
    pass

from agp import SourceClass                                    # noqa: E402
from agp.provenance import Evidence, ProvenanceLedger          # noqa: E402
from tools.artifacts import ArtifactStore                      # noqa: E402
from tools.pipeline.checkpoint import (                        # noqa: E402
    FileCheckpointer, RunTrace, replay_ledger, seal_guard)
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel                 # noqa: E402

TODAY = date(2026, 8, 23)

OPENALEX_BODY = json.dumps({"results": [{
    "id": "W1",
    "title": "supply chain resilience study of foundry concentration",
    "publication_year": 2024,
}]})


def _decompose() -> str:
    return json.dumps({"sub_questions": [{
        "text": "what does research say about foundry supply chain resilience",
        "kind": "descriptive",
        "question_type": "scholarly work search",
        "min_source_tier": 2,
        "min_independent_sources": 1,
    }]})


def _answer(conf: float) -> str:
    return json.dumps({"answer": "resilience is concentrated",
                       "proposed_confidence": conf})


class _QuietAdversary:
    async def complete(self, *a, **k):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _make(tmp: Path, cp=None, transport=None, ledger=None):
    return ResearchPipeline(
        model=ScriptedModel({
            "Architect": [{"content": _decompose()}],
            "Manager": [{"content": _answer(0.7)}],
        }),
        adversary_router=_QuietAdversary(),
        transport=transport or fixture_transport({"/works": OPENALEX_BODY}),
        store=ArtifactStore(root=tmp / f"art{abs(hash(tmp)) % 1000}"),
        ledger=ledger or ProvenanceLedger(),
        checkpointer=cp)


def _run(p, q):
    return asyncio.get_event_loop().run_until_complete(
        p.run(q, today=TODAY))


def _ckpt_files(cp) -> list[Path]:
    return [Path(x) for x in glob.glob(str(cp.root / "*" / "*.json"))]


def _rewrite(cp, fn):
    for p in _ckpt_files(cp):
        d = json.loads(p.read_text())
        fn(d)
        p.write_text(json.dumps(d))


@pytest.fixture()
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


# ── baseline differential (must PASS: honest resume == live) ───────────────

def test_baseline_honest_resume_matches_live(tmp_path, loop):
    tmp = Path(tmp_path)
    live = _run(_make(tmp), "Baseline Q")
    cp = FileCheckpointer(root=tmp / "ck")
    first = _run(_make(tmp, cp=cp), "Baseline Q")
    again = _run(_make(tmp, cp=cp), "Baseline Q")
    assert live.sealed and first.sealed and again.sealed
    assert again.trace.is_resume
    assert again.confidence_score == live.confidence_score


# ── R1: tampered answer_leaf payload flows straight into the seal ──────────

def test_R1_tampered_answer_checkpoint_cannot_inflate(tmp_path, loop):
    """A checkpoint is plain JSON with no authentication. Rewrite the leaf
    answer, its confidence (0.55 -> 0.99), and its evidence class
    (SECONDARY -> PRIMARY); the resumed run must refuse or score honestly."""
    tmp = Path(tmp_path)
    cp = FileCheckpointer(root=tmp / "ck")
    q = "R1 question"
    r1 = _run(_make(tmp, cp=cp), q)

    def tamper(d):
        if d["stage"] == "answer_leaf":
            d["payload"]["leaf"]["answer"] = "FABRICATED: buy now"
            d["payload"]["leaf"]["confidence"] = 0.99
            d["payload"]["leaf"]["tier"] = "VERIFIED"
            for e in d["payload"].get("evidence") or []:
                e["source_class"] = "PRIMARY"
                e["confidence_score"] = 1.0
    _rewrite(cp, tamper)

    r2 = _run(_make(tmp, cp=cp), q)
    assert not r2.sealed or r2.confidence_score <= r1.confidence_score, (
        "resumed run sealed a tampered checkpoint at "
        f"{r2.confidence_score} > honest {r1.confidence_score}")
    assert all("FABRICATED" not in (l.answer or "") for l in r2.leaves), (
        "tampered leaf answer reached the sealed conclusion verbatim")


# ── R2: self-consistent forgery defeats the integrity check ────────────────

def test_R2_self_consistent_fetch_forgery_must_not_seal(tmp_path, loop):
    """replay_ledger verifies body == sha256 recorded IN THE SAME FILE.
    Forge both consistently; provenance_is_intact then passes and the
    fabricated document enters the resumed run as primary evidence."""
    tmp = Path(tmp_path)
    cp = FileCheckpointer(root=tmp / "ck")
    q = "R2 question"
    _run(_make(tmp, cp=cp), q)

    fabricated = json.dumps({"results": [{
        "id": "FAKE", "title": "supply chain resilience peer-reviewed study "
        "confirming foundry concentration", "publication_year": 2025}]})

    def forge(d):
        if d["stage"] != "fetch_leaf":
            return
        for rec in d["payload"]["fetches"]:
            rec["body"] = fabricated
            rec["parsed"] = fabricated
            rec["content_sha256"] = hashlib.sha256(
                fabricated.encode()).hexdigest()
    _rewrite(cp, forge)

    r2 = _run(_make(tmp, cp=cp, transport=lambda u, h: (500, "down")), q)
    leaked = [f for f in r2.fetches if '"FAKE"' in (f.body or "")]
    assert not leaked or not r2.sealed, (
        "resumed run sealed over forged fetch bytes that no source ever "
        "returned; the 'integrity' check hashed a file against itself")


# ── R3: missing fetch checkpoints still seal ───────────────────────────────

def test_R3_deleted_fetch_checkpoint_refuses_to_seal(tmp_path, loop):
    """Delete the fetch_leaf checkpoints (partial GC/corruption). The guard
    iterates cp.list_all() — absent files are invisible, so nothing fails —
    while the answer_leaf checkpoint still supplies session.evidence."""
    tmp = Path(tmp_path)
    cp = FileCheckpointer(root=tmp / "ck")
    q = "R3 question"
    _run(_make(tmp, cp=cp), q)
    for p in _ckpt_files(cp):
        if p.name.startswith("fetch_leaf."):
            p.unlink()

    r2 = _run(_make(tmp, cp=cp, transport=lambda u, h: (500, "down")), q)
    assert not r2.sealed, (
        "resumed run sealed with zero fetch checkpoints on disk; evidence "
        "was carried by the unauthenticated answer_leaf payload alone")


# ── R4: staleness is recorded but never enforced ───────────────────────────

def test_R4_year_old_evidence_cannot_seal_silently(tmp_path, loop):
    """Backdate produced_at 400 days. The trace honestly reports the age and
    then NOTHING consults it: the resumed run seals month-old evidence with
    no note, no refusal, and full confidence."""
    tmp = Path(tmp_path)
    cp = FileCheckpointer(root=tmp / "ck")
    q = "R4 question"
    _run(_make(tmp, cp=cp), q)
    _rewrite(cp, lambda d: d.update(
        produced_at="2025-07-19T00:00:00+00:00"))

    r2 = _run(_make(tmp, cp=cp, transport=lambda u, h: (500, "down")), q)
    assert r2.trace.oldest_produced_at().startswith("2025-07-19")
    assert not r2.sealed, (
        "400-day-stale evidence sealed at full confidence with no note; "
        "oldest_produced_at is recorded but no consumer enforces it")


# ── R5: seal_guard replays EVERY RUN'S checkpoints into this run's ledger ──

def test_R5_cross_run_ledger_laundering(tmp_path, loop):
    """engine.run passes cp.list_all() to seal_guard — every checkpoint ever
    written by ANY run. Replay loads those fetches into THIS run's ledger:
    another run's exact bytes become PRIMARY here, and citing another run's
    URL makes a fabricated claim SECONDARY here. Provenance stops being
    'which code path fetched the bytes THIS session'."""
    tmp = Path(tmp_path)
    cp = FileCheckpointer(root=tmp / "ck")
    _run(_make(tmp, cp=cp), "Run Alpha")

    fresh = ProvenanceLedger()
    verdict, reason = seal_guard(RunTrace(run="a-completely-unrelated-run"),
                                 cp.list_all(), fresh)
    assert verdict == "SEAL"
    assert fresh.observed_urls(), (
        "an unrelated run's guard verdict polluted its ledger with other "
        "runs' fetch observations")

    alpha_body = None
    for ck in cp.list_all():
        for rec in ck.payload.get("fetches") or []:
            alpha_body = rec["body"]
    ev = Evidence(content=alpha_body, source_class=SourceClass.INFERRED,
                  confidence_score=0.5, domain=None, origin_agent="x")
    assert fresh.assign_source_class(ev) != SourceClass.PRIMARY, (
        "another run's fetched bytes assigned PRIMARY in a fresh run's "
        "ledger — provenance laundered across the run boundary")
    url = next(iter(fresh.observed_urls()))
    fab = Evidence(content=f"Fabricated claim, see {url}",
                   source_class=SourceClass.INFERRED, confidence_score=0.9,
                   domain=None, origin_agent="x")
    assert fresh.assign_source_class(fab) == SourceClass.INFERRED, (
        "citing a URL some OTHER run fetched upgraded a fabricated claim")
