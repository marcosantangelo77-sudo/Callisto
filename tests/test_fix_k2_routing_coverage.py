"""K2 fixes — routing store question identity + coverage gate.

Red-team finding K2 (findings/redteam_calibration.md), defects 1 and 2.
Defect 3 (task_class never read) was already fixed by the W8 routing pass.
Each test is the acceptance repro: cherry-picked-vs-honest, 500 decisions.
"""
import random

from tools.routing.scores import ModelScoreStore
from tools.routing.policy import ThompsonRoutingPolicy, CandidateModel


def _cherry_vs_honest_store(path):
    """The red team's exact setup: 'cherry' answers its 10 easy questions
    only; 'honest' answers all 30 (harder, brier .24)."""
    s = ModelScoreStore(path=path)
    for i in range(10):
        s.record(role="r", model="cherry", task_class="tc",
                 question_id=f"c{i}", brier=0.01)
    for i in range(30):
        s.record(role="r", model="honest", task_class="tc",
                 question_id=f"h{i}", brier=0.24)
    return s


class TestQuestionIdentity:
    def test_duplicate_question_records_count_once(self, tmp_path):
        """K2.1 FIXED: one question_id recorded 100x is ONE observation.
        Volume can no longer substitute for breadth."""
        s = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(100):
            s.record(role="r", model="spammer", task_class="tc",
                     question_id="same_q", brier=0.0)
        agg = ModelScoreStore.aggregate(s.records_for("r", "spammer"))
        assert agg["n"] == 1
        assert agg["distinct_questions"] == 1
        assert agg["duplicate_rows_ignored"] == 99
        assert ModelScoreStore.basis_label(agg["n"]) == "sparse"

    def test_latest_record_per_question_wins(self, tmp_path):
        """A correction appended for an existing question_id supersedes the
        old value instead of averaging with it."""
        s = ModelScoreStore(path=tmp_path / "s.jsonl")
        s.record(role="r", model="m", task_class="tc",
                 question_id="q1", brier=0.4)
        s.record(role="r", model="m", task_class="tc",
                 question_id="q1", brier=0.1)
        agg = ModelScoreStore.aggregate(s.records_for("r", "m"))
        assert agg["n"] == 1
        assert agg["mean_brier_raw"] == 0.1


class TestCoverageGate:
    def test_cherry_picked_model_loses_all_500_decisions(self, tmp_path):
        """K2.2 ACCEPTANCE REPRO. Property enforced: a comparator must not
        reward selective participation. Before the fix cherry won 500/500;
        after, it cannot win a single decision — its subset mean is not
        evidence about the 20 questions it skipped."""
        s = _cherry_vs_honest_store(tmp_path / "s.jsonl")
        pol = ThompsonRoutingPolicy(store=s, rng=random.Random(1))
        cands = [CandidateModel(name="cherry", tier="t1", config_rank=1),
                 CandidateModel(name="honest", tier="t2", config_rank=0)]
        wins = sum(pol.decide("r", cands).model == "cherry"
                   for _ in range(500))
        assert wins == 0

    def test_gated_candidate_reported_not_hidden(self, tmp_path):
        """The gate is visible in scores_used: coverage counts and the
        coverage_gated flag are returned to every caller."""
        s = _cherry_vs_honest_store(tmp_path / "s.jsonl")
        pol = ThompsonRoutingPolicy(store=s, rng=random.Random(1))
        cands = [CandidateModel(name="cherry", tier="t1"),
                 CandidateModel(name="honest", tier="t2")]
        d = pol.decide("r", cands)
        assert d.model == "honest"
        gated = d.scores_used["cherry"]
        assert gated["coverage_gated"] is True
        assert gated["coverage"] == 10
        assert gated["max_coverage"] == 30

    def test_equal_coverage_competes_normally(self, tmp_path):
        """The gate compares distinct-question coverage, not row counts.
        Two models measured on the same number of DISTINCT questions compete
        on quality even if one has duplicate rows."""
        s = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(5):
            s.record(role="r", model="a", task_class="tc",
                     question_id=f"q{i}", brier=0.20)
        # b duplicates a's questions twice over: same breadth, worse quality.
        for i in range(5):
            s.record(role="r", model="b", task_class="tc",
                     question_id=f"q{i}", brier=0.30)
            s.record(role="r", model="b", task_class="tc",
                     question_id=f"q{i}", brier=0.31)
        pol = ThompsonRoutingPolicy(store=s, rng=random.Random(7))
        cands = [CandidateModel(name="a", tier="ta"),
                 CandidateModel(name="b", tier="tb")]
        wins_a = sum(pol.decide("r", cands).model == "a" for _ in range(200))
        assert wins_a > 180          # better model wins, no coverage penalty
