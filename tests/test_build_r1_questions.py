"""Question generation, held-out answer separation, and persistence tests."""
import json
from datetime import date

import pytest

from tools.retrodiction.questions import (
    QuestionType,
    RetrodictionQuestion,
    generate_earnings_questions,
    load_questions,
    save_questions,
)

HISTORY = [
    {"ticker": "NVDA", "report_date": date(2024, 5, 22),
     "eps_actual": 6.12, "eps_consensus": 5.59},
    {"ticker": "TSLA", "report_date": date(2024, 4, 23),
     "eps_actual": 0.45, "eps_consensus": 0.51},
]


class TestGeneration:
    def test_generates_one_question_per_report(self):
        qs = generate_earnings_questions(HISTORY)
        assert len(qs) == 2
        assert all(q.validate() == [] for q in qs)

    def test_answer_matches_history(self):
        qs = {q.text: q for q in generate_earnings_questions(HISTORY)}
        nvda = [q for q in qs.values() if "NVDA" in q.text][0]
        tsla = [q for q in qs.values() if "TSLA" in q.text][0]
        assert nvda.answer_binary is True
        assert tsla.answer_binary is False

    def test_claim_date_precedes_report(self):
        q = generate_earnings_questions(HISTORY)[0]
        assert q.claim_date < q.resolution_date
        assert q.horizon_days == 30

    def test_domain_general_types_exist(self):
        # Any dated resolvable fact works; types are not finance-only.
        assert QuestionType.EVENT_OUTCOME.value == "event_outcome"
        assert QuestionType.THRESHOLD_CROSS.value == "threshold_cross"


class TestHeldOutAnswers:
    def test_prompt_never_contains_answer(self):
        q = generate_earnings_questions(HISTORY)[0]
        prompt = q.prompt_for_researcher()
        blob = json.dumps(prompt)
        assert "answer" not in blob
        assert q.resolution_date.isoformat() not in blob
        assert set(prompt) == {"question_id", "text", "domain",
                               "question_type", "claim_date"}

    def test_prompt_is_json_serializable_and_dated(self):
        q = generate_earnings_questions(HISTORY)[0]
        prompt = q.prompt_for_researcher()
        assert date.fromisoformat(prompt["claim_date"]) == q.claim_date


class TestPersistence:
    def test_round_trip_preserves_everything(self):
        qs = generate_earnings_questions(HISTORY)
        path = save_questions(qs, "/tmp/r1_test_questions.json")
        loaded = load_questions(path)
        assert len(loaded) == len(qs)
        for a, b in zip(qs, loaded):
            assert a.question_id == b.question_id
            assert a.answer_binary == b.answer_binary
            assert a.claim_date == b.claim_date
            assert a.question_type == b.question_type

    def test_validate_flags_bad_records(self):
        q = RetrodictionQuestion(text="", claim_date=date(2024, 1, 1),
                                 resolution_date=date(2024, 6, 1),
                                 answer_confidence=0.3)
        errs = q.validate()
        assert any("empty text" in e for e in errs)
        assert any("ambiguous ground truth" in e for e in errs)

    def test_resolution_before_claim_rejected_at_construction(self):
        with pytest.raises(ValueError):
            RetrodictionQuestion(text="x", claim_date=date(2024, 6, 1),
                                 resolution_date=date(2024, 1, 1))
