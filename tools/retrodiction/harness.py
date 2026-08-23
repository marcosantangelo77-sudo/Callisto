"""A/B harness and loop-calibration measurement over the retrodiction set.

The pipeline may not be fully wired end-to-end; everything here programs
against a pluggable Researcher interface, so the harness is ready the moment
the real pipeline lands. The stub researcher exists so tests and demos run
with zero network.

Loop calibration: run identical questions at k loop iterations for k in
(3, 5, 10). If confidence rises while accuracy (Brier) does not improve,
the loop is manufacturing overconfidence — measurable here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from tools.retrodiction.cutoff import CutoffEnforcer, EvidenceRecord
from tools.retrodiction.scoring import Prediction, score_brier


class Researcher:
    """Interface the pipeline must satisfy. Receives ONLY what it is allowed
    to see: question prompts and cutoff-filtered evidence. Returns a
    Prediction per question. Implementations must not receive answers."""
    name = "researcher"

    def answer(self, prompts: list[dict],
               evidence: list[EvidenceRecord],
               loops: int = 1) -> list[Prediction]:
        raise NotImplementedError


class StubResearcher(Researcher):
    """Deterministic offline stand-in: answers from a fixed probability map.
    Lets the whole harness be exercised before the pipeline lands."""
    name = "stub"

    def __init__(self, probabilities: Optional[dict[str, float]] = None):
        self.probabilities = dict(probabilities or {})

    def answer(self, prompts, evidence, loops=1):
        return [
            Prediction(question_id=p["question_id"],
                       probability=self.probabilities.get(p["question_id"], 0.5),
                       config_label=f"{self.name}@{loops}",
                       loops=loops)
            for p in prompts
        ]


@dataclass
class RunConfig:
    """One arm of an A/B comparison. Adding a config axis = adding a field."""
    label: str
    researcher_factory: Callable[[], Researcher] = Researcher  # () -> Researcher
    loops: int = 1
    # Config axes under test (e.g. reference_class_first=True). Opaque to the
    # harness except that they are recorded on results; researchers read them.
    axes: dict = field(default_factory=dict)
    # Fail-closed cutoff policy: with strict=True, any rejected record aborts
    # the arm instead of silently continuing with less evidence.
    strict_cutoff: bool = False


@dataclass
class RunResult:
    config_label: str
    axes: dict
    loops: int
    brier: float
    n_scored: int
    n_evidence_admitted: int = 0
    n_evidence_rejected: int = 0
    predictions: list = field(default_factory=list)
    # Paired permutation result vs the other arm (two-arm run_ab only).
    significance: dict = field(default_factory=dict)

    def summary(self) -> dict:
        s = {
            "label": self.config_label, "axes": self.axes,
            "loops": self.loops, "brier": round(self.brier, 6),
            "n": self.n_scored,
            "evidence": {"admitted": self.n_evidence_admitted,
                         "rejected": self.n_evidence_rejected},
        }
        if self.significance:
            s["significance"] = self.significance
        return s


def _run_arm(config: RunConfig, questions, evidence_records) -> RunResult:
    enforcer = CutoffEnforcer(min(q.claim_date for q in questions))
    admitted, rejected = enforcer.admit(evidence_records)
    if config.strict_cutoff and rejected:
        from tools.retrodiction.cutoff import CutoffViolation
        raise CutoffViolation(
            f"strict mode: {len(rejected)} unverifiable records")
    researcher = config.researcher_factory()
    prompts = [q.prompt_for_researcher() for q in questions]
    predictions = researcher.answer(prompts, admitted, loops=config.loops)
    brier = score_brier(predictions, questions)
    return RunResult(
        config_label=config.label, axes=dict(config.axes), loops=config.loops,
        brier=brier, n_scored=len(predictions),
        n_evidence_admitted=len(admitted), n_evidence_rejected=len(rejected),
        predictions=predictions)


def run_ab(configs, questions, evidence_records) -> dict[str, RunResult]:
    """Run the SAME question set under each configuration and compare.
    Returns {config_label: RunResult}. Identical questions, different arms —
    any Brier difference is attributable to the config axis.

    When exactly two arms are compared, a paired permutation test is attached
    to each result as `.significance`: raw Brier means alone do not say
    whether a gap is real or sampling noise on N small questions."""
    results = {}
    for cfg in configs:
        results[cfg.label] = _run_arm(cfg, questions, evidence_records)
    if len(results) == 2:
        (a, b) = list(results.values())
        from tools.retrodiction.scoring import paired_significance
        sig = paired_significance(a.predictions, b.predictions, questions)
        a.significance = sig
        b.significance = sig
    return results


def loop_calibration(researcher_factory, questions, evidence_records,
                     loop_levels=(3, 5, 10)) -> dict[int, dict]:
    """Identical questions at increasing loop iterations. Flags manufactured
    overconfidence: mean stated confidence rising while Brier does not fall.

    Returns {loops: {"brier", "mean_confidence", "overconfidence_delta"}} plus
    a top-level verdict key "manufactured_overconfidence": bool (True when the
    highest-loop arm is more confident AND not more accurate than the lowest).
    """
    results = {}
    base_brier = None
    for k in loop_levels:
        cfg = RunConfig(label=f"loops={k}",
                        researcher_factory=researcher_factory, loops=k)
        r = _run_arm(cfg, questions, evidence_records)
        confs = [p.effective_confidence for p in r.predictions]
        mean_conf = sum(confs) / len(confs) if confs else 0.0
        results[k] = {
            "brier": r.brier,
            "mean_confidence": mean_conf,
            "n": r.n_scored,
        }
        if base_brier is None:
            base_brier = r.brier

    levels = sorted(results)
    lo, hi = levels[0], levels[-1]
    confidence_up = results[hi]["mean_confidence"] > results[lo]["mean_confidence"]
    accuracy_not_up = results[hi]["brier"] >= results[lo]["brier"]
    results["manufactured_overconfidence"] = bool(confidence_up and accuracy_not_up)
    return results
