"""Harness tests: A/B arms, cutoff enforcement in the loop, loop calibration.

All offline — StubResearcher and fixtures only, no network.
"""
from datetime import date, datetime
import hashlib

import pytest

from tools.retrodiction.cutoff import CutoffViolation, EvidenceRecord, PublicationProof, ProofKind
from tools.retrodiction.harness import (
    RunConfig,
    StubResearcher,
    loop_calibration,
    run_ab,
)
from tools.retrodiction.questions import RetrodictionQuestion, QuestionType


def _proof(content: str) -> PublicationProof:
    return PublicationProof(kind=ProofKind.SOURCE_DECLARED,
                            published_on=date(2024, 2, 1),
                            locator="fixture",
                            content_sha256=hashlib.sha256(
                                content.encode()).hexdigest())


def _questions(n=10):
    return [RetrodictionQuestion(
        question_id=f"q{i}", text=f"q{i}?",
        domain="FINANCIAL" if i % 2 else "GENERAL",
        question_type=QuestionType.BEAT_OR_MISS,
        claim_date=date(2024, 3, 1),
        resolution_date=date(2024, 5, 1),
        answer_binary=(i % 3 == 0)) for i in range(n)]


def _evidence():
    rec = EvidenceRecord(url="https://x/1", query="q",
                         fetched_at=datetime(2026, 8, 22),
                         content="pre-cutoff filing text",
                         proof=None)
    rec.proof = _proof(rec.content)
    return [rec]


class TestRunAB:
    def test_same_questions_both_arms(self):
        qs, ev = _questions(), _evidence()
        results = run_ab(
            [RunConfig(label="direct",
                       researcher_factory=lambda: StubResearcher({f"q{i}": 0.9 for i in range(10)})),
             RunConfig(label="refclass",
                       researcher_factory=lambda: StubResearcher({f"q{i}": 0.55 for i in range(10)}),
                       axes={"reference_class_first": True})],
            qs, ev)
        assert set(results) == {"direct", "refclass"}
        assert all(r.n_scored == 10 for r in results.values())
        assert results["refclass"].axes == {"reference_class_first": True}
        # The better-calibrated arm scores a lower Brier on identical questions.
        assert results["refclass"].brier < results["direct"].brier

    def test_researcher_never_sees_answers(self):
        seen = {}

        class Spy(StubResearcher):
            def answer(self, prompts, evidence, loops=1):
                seen["prompts"] = prompts
                seen["blob"] = str(prompts)
                return super().answer(prompts, evidence, loops)

        run_ab([RunConfig(label="spy", researcher_factory=Spy)], _questions(),
               _evidence())
        assert "answer_binary" not in seen["blob"]
        assert "resolution_date" not in seen["blob"]

    def test_unverifiable_evidence_excluded_from_arm(self):
        leaky = EvidenceRecord(url="https://x/leak", query="q",
                               fetched_at=datetime(2026, 8, 22),
                               content="post-cutoff leak", proof=None)
        results = run_ab([RunConfig(label="a",
                                    researcher_factory=StubResearcher)],
                         _questions(2), [leaky])
        assert results["a"].n_evidence_admitted == 0
        assert results["a"].n_evidence_rejected == 1

    def test_strict_mode_aborts_on_leak(self):
        leaky = EvidenceRecord(url="https://x/leak", query="q",
                               fetched_at=datetime(2026, 8, 22),
                               content="x", proof=None)
        with pytest.raises(CutoffViolation):
            run_ab([RunConfig(label="strict", researcher_factory=StubResearcher,
                              strict_cutoff=True)],
                   _questions(2), [leaky])


class TestLoopCalibration:
    @staticmethod
    def _researcher_factory(confident_but_no_better=False):
        def factory():
            if confident_but_no_better:
                # Confidence rises with loops; accuracy does not.
                class Overconfident(StubResearcher):
                    def answer(self, prompts, evidence, loops=1):
                        preds = super().answer(prompts, evidence, loops)
                        for p in preds:
                            p.probability = min(0.99, 0.6 + 0.03 * loops)
                            p.confidence = min(0.99, 0.6 + 0.03 * loops)
                        return preds
                return Overconfident()
            return StubResearcher()
        return factory

    def test_manufactured_overconfidence_detected(self):
        qs = [_ for _ in _questions(20)]
        report = loop_calibration(self._researcher_factory(True), qs,
                                  _evidence())
        assert report["manufactured_overconfidence"] is True
        assert report[10]["mean_confidence"] > report[3]["mean_confidence"]
        assert report[10]["brier"] >= report[3]["brier"]

    def test_honest_loop_not_flagged(self):
        class Honest(StubResearcher):
            def answer(self, prompts, evidence, loops=1):
                preds = super().answer(prompts, evidence, loops)
                for p, pr in zip(preds, prompts):
                    q = next(q for q in self._qs
                             if q.question_id == p.question_id)
                    # More loops → closer to truth AND more confident.
                    target = 0.95 if q.answer_binary else 0.05
                    step = (target - p.probability) * min(1.0, loops / 10)
                    p.probability += step
                    p.confidence = min(0.95, 0.5 + 0.04 * loops)
                return preds

        factory = self._researcher_factory(False)
        qs = _questions(20)

        class HonestFactoryBound:
            pass

        def bound_factory():
            r = Honest()
            r._qs = qs
            return r

        report = loop_calibration(bound_factory, qs, _evidence())
        assert report["manufactured_overconfidence"] is False
        assert report[10]["mean_confidence"] > report[3]["mean_confidence"]

    def test_default_levels_are_3_5_10(self):
        report = loop_calibration(lambda: StubResearcher(), _questions(4),
                                  _evidence())
        assert {k for k in report if k != "manufactured_overconfidence"} \
            == {3, 5, 10}
