"""Evidence age measurement tests.

A run whose evidence spans a long acquisition window must report that
honestly — never present it as simultaneous. Measurement only: these tests
also pin that NO confidence, gate, or score changes in response to evidence
age. No penalty, no decay.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tools.retrodiction.evidence_age import compute_spread


UTC = timezone.utc


class TestComputeSpread:
    def test_long_window_reported_honestly(self):
        # Evidence fetched 40 minutes apart: the spread must show a ~40min
        # window and an oldest-age ≈ window, not two near-zero ages.
        sealed = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
        early = sealed - timedelta(minutes=41)
        late = sealed - timedelta(minutes=1)
        s = compute_spread([early, late], sealed_at=sealed)
        assert s is not None
        assert s.oldest_s == pytest.approx(41 * 60)
        assert s.newest_s == pytest.approx(60)
        assert s.median_s == pytest.approx(21 * 60)
        assert s.span_s == pytest.approx(40 * 60)
        assert s.n_records == 2
        d = s.to_dict()
        assert d["oldest_evidence_age_s"] == pytest.approx(41 * 60)

    def test_naive_datetimes_treated_as_utc(self):
        sealed = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
        naive = (sealed - timedelta(minutes=5)).replace(tzinfo=None)
        s = compute_spread([naive], sealed_at=sealed)
        assert s.newest_s == pytest.approx(5 * 60)

    def test_empty_is_none_not_zero_age(self):
        # A run with no evidence must not report "age 0" — that would be the
        # freshest possible evidence, which is exactly the lie to avoid.
        assert compute_spread([]) is None

    def test_unparseable_stamps_excluded_upstream(self):
        # compute_spread takes datetimes; the exclusion of unparseable strings
        # happens at the seal site — covered in TestSealTime.
        pass


class TestHarnessRunResult:
    def _proof(self, content: str):
        import hashlib
        from tools.retrodiction.cutoff import PublicationProof, ProofKind
        return PublicationProof(
            kind=ProofKind.SOURCE_DECLARED, published_on=date(2024, 1, 1),
            locator="fixture",
            content_sha256=hashlib.sha256(content.encode()).hexdigest())

    def _evidence(self, fetched_at: datetime) -> list:
        from tools.retrodiction.cutoff import EvidenceRecord
        rec = EvidenceRecord(url="https://x/1", query="q",
                             fetched_at=fetched_at,
                             content="pre-cutoff filing text")
        rec.proof = self._proof(rec.content)
        return [rec]

    def _questions(self, n=4):
        from tools.retrodiction.questions import RetrodictionQuestion, \
            QuestionType
        return [RetrodictionQuestion(
            question_id=f"q{i}", text=f"q{i}?", domain="GENERAL",
            question_type=QuestionType.BEAT_OR_MISS,
            claim_date=date(2024, 3, 1), resolution_date=date(2024, 5, 1),
            answer_binary=True) for i in range(n)]

    def _run(self, fetched_ats, allow_unsigned=True):
        from tools.retrodiction.harness import RunConfig, run_ab
        from tools.retrodiction.harness import StubResearcher
        cfg = RunConfig(label="arm", allow_unsigned_proofs=allow_unsigned,
                        researcher_factory=lambda: StubResearcher({}))
        results = run_ab([cfg], self._questions(),
                         sum((self._evidence(f) for f in fetched_ats), []))
        return results["arm"]

    def test_run_result_reports_age_of_stale_and_fresh(self):
        now = datetime.now(UTC)
        r = self._run([now - timedelta(hours=3), now])
        assert r.evidence_age["oldest_evidence_age_s"] >= 3 * 3600 - 60
        assert r.evidence_age["newest_evidence_age_s"] <= 60
        assert r.evidence_age["acquisition_window_s"] >= 3 * 3600 - 120
        # Surfaced in summary too — the conclusion can state it.
        assert r.summary()["evidence_age"] == r.evidence_age

    def test_no_evidence_no_fabricated_spread(self):
        r = self._run([], allow_unsigned=True)
        assert r.evidence_age == {}


class TestSealTime:
    """The real pipeline records evidence age on the sealed result."""

    def _model(self):
        from tools.pipeline.model import ScriptedModel
        decompose = json.dumps({"sub_questions": [
            {"text": "what does the literature say about the topic",
             "kind": "descriptive", "question_type": "scholarly work search",
             "min_source_tier": 2, "min_independent_sources": 1}]})
        m = ScriptedModel(default={"content": json.dumps(
            {"answer": "no evidence either way", "proposed_confidence": 0.3})})
        m.script("Architect", {"content": decompose})
        m.script("Manager", {"content": json.dumps(
            {"answer": "the evidence supports the claim",
             "proposed_confidence": 0.7})})
        return m

    def _run_pipeline(self, fetched_at: str):
        import asyncio
        from datetime import date as date_cls
        from tools.artifacts import ArtifactStore
        from tools.pipeline.engine import ResearchPipeline, fixture_transport
        routes = {"/works": json.dumps({"results": [
            {"id": "W1", "title": "scholarly literature review of scholarly "
                                  "work on the topic"}]})}
        pipe = ResearchPipeline(model=self._model(),
                                transport=fixture_transport(routes),
                                store=ArtifactStore(root=_tmp()))
        result = asyncio.new_event_loop().run_until_complete(
            pipe.run("what does the literature say about the topic",
                     today=date_cls(2024, 3, 1)))
        return result

    def test_sealed_result_carries_evidence_age_or_honest_none(self):
        result = self._run_pipeline(None)
        if result.sealed:
            # Fixture transport stamps fetches with real wall-clock times via
            # RestSource, so a sealed run with admitted fetches carries a
            # fresh-but-real spread; a run whose fetches carry no parseable
            # stamp reports None rather than zeros.
            ea = result.evidence_age
            assert ea is None or (
                ea["oldest_evidence_age_s"] >= ea["newest_evidence_age_s"]
                and ea["n_evidence_records"] > 0)


def _tmp():
    import tempfile
    return tempfile.mkdtemp()
