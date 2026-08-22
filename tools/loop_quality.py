"""Loop-quality machinery for the autonomous research loops (R2 build wave).

Everything here serves one question: **does the loop actually know when it is
done, and can its confidence be scored against outcomes later?**

Four components, all pure and unit-testable (no network, no DB, no model):

1. ``InformationGainTerminator`` — stopping rule based on marginal
   information. Stops when additional iterations stop materially moving the
   confidence estimate. Fixed step counts are wrong in both directions; this
   replaces them. Every stop decision is returned WITH its reason so a
   premature stop is diagnosable rather than invisible.

2. ``LoopCalibrationTrace`` — per-iteration record of confidence and
   evidence, shaped for R1's retrodiction harness. Answers: did the system
   get more ACCURATE with more iterations, or merely more CONFIDENT?
   Confidence rising while evidence does not is overconfidence manufacturing,
   and this makes it visible.

3. ``compact_state`` — explicit, biased compaction between iterations.
   Contradicting evidence is NEVER dropped and never summarised away;
   supporting evidence is capped first. What survives an iteration boundary
   determines everything downstream, so the survival policy is explicit and
   logged.

4. ``LOOP_PHASE_TASK_CLASSES`` / ``task_class_for_phase`` — per-phase model
   allocation. Framing (first) and adversarial review (last) benefit most
   from capability; the middle is grind. Emits canonical task_class strings
   the ProviderRouter already routes on.

GATE RULE: nothing here lowers a gate. A terminator only stops SPENDING more
budget on a question; it never weakens any threshold a conclusion must clear.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# 1. Termination by information gain
# ────────────────────────────────────────────────────────────────────────


@dataclass
class StopDecision:
    """One termination evaluation, fully diagnosed."""

    stop: bool
    reason: str
    iteration: int
    confidence: float
    marginal_confidence_delta: float
    stagnant_iterations: int
    # None when still running; otherwise a stable short code for logs/grep:
    # "info_gain_stalled" | "max_iterations" | "not_enough_iterations"
    code: Optional[str] = None


class InformationGainTerminator:
    """Stop when additional evidence stops moving the confidence estimate.

    This is what a good researcher does intuitively: keep digging while the
    picture is still changing, stop when another source would not change the
    answer, and be able to say WHY you stopped.

    Configurable knobs (all keyword, all with safe defaults):
      min_iterations            — never stop before this many recorded
                                  iterations (default 3). Guards against
                                  stopping on a single coincidental plateau.
      max_iterations            — hard ceiling; always stops here regardless
                                  of movement (default 12). Budget guardrail.
      confidence_delta_threshold— marginal |Δconfidence| below which an
                                  iteration counts as stagnant (default 0.02,
                                  i.e. two points of probability).
      stagnant_iterations_needed— consecutive stagnant iterations required to
                                  stop on information gain (default 2).

    The decision is never silent: call sites must pass ``reason`` onward or
    use :meth:`evaluate_and_log`, which logs the full StopDecision.
    """

    def __init__(
        self,
        min_iterations: int = 3,
        max_iterations: int = 12,
        confidence_delta_threshold: float = 0.02,
        stagnant_iterations_needed: int = 2,
        subject: str = "",
    ):
        if min_iterations < 1:
            raise ValueError("min_iterations must be >= 1")
        if max_iterations < min_iterations:
            raise ValueError("max_iterations must be >= min_iterations")
        if confidence_delta_threshold <= 0:
            raise ValueError("confidence_delta_threshold must be > 0")
        if stagnant_iterations_needed < 1:
            raise ValueError("stagnant_iterations_needed must be >= 1")
        self.min_iterations = min_iterations
        self.max_iterations = max_iterations
        self.confidence_delta_threshold = float(confidence_delta_threshold)
        self.stagnant_iterations_needed = stagnant_iterations_needed
        self.subject = subject
        self._confidences: list[float] = []
        self._decisions: list[StopDecision] = []

    @property
    def iteration(self) -> int:
        return len(self._confidences)

    def record(self, confidence: float) -> StopDecision:
        """Record one iteration's confidence and evaluate termination."""
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {confidence!r}")
        self._confidences.append(float(confidence))

        delta = (
            abs(self._confidences[-1] - self._confidences[-2])
            if len(self._confidences) >= 2
            else math.inf  # first iteration always carries maximal information
        )
        stagnant = self._count_trailing_stagnant()

        if self.iteration < self.min_iterations:
            dec = StopDecision(
                stop=False, reason="below min_iterations — forced continuation",
                iteration=self.iteration, confidence=confidence,
                marginal_confidence_delta=delta,
                stagnant_iterations=stagnant, code=None,
            )
        elif self.iteration >= self.max_iterations:
            dec = StopDecision(
                stop=True,
                reason=(
                    f"max_iterations ({self.max_iterations}) reached — "
                    f"hard budget ceiling, stopping regardless of movement"
                ),
                iteration=self.iteration, confidence=confidence,
                marginal_confidence_delta=delta,
                stagnant_iterations=stagnant, code="max_iterations",
            )
        elif stagnant >= self.stagnant_iterations_needed:
            dec = StopDecision(
                stop=True,
                reason=(
                    f"information gain stalled: {stagnant} consecutive "
                    f"iterations moved confidence < "
                    f"{self.confidence_delta_threshold:.3f} "
                    f"(last delta {delta:.4f}) — additional evidence is no "
                    f"longer materially moving the estimate"
                ),
                iteration=self.iteration, confidence=confidence,
                marginal_confidence_delta=delta,
                stagnant_iterations=stagnant, code="info_gain_stalled",
            )
        else:
            dec = StopDecision(
                stop=False,
                reason=(
                    f"information gain alive: last delta {delta:.4f} >= "
                    f"{self.confidence_delta_threshold:.3f} "
                    f"({stagnant}/{self.stagnant_iterations_needed} stagnant)"
                ),
                iteration=self.iteration, confidence=confidence,
                marginal_confidence_delta=delta,
                stagnant_iterations=stagnant, code=None,
            )
        self._decisions.append(dec)
        return dec

    def evaluate_and_log(self, confidence: float) -> StopDecision:
        """record() + a log line carrying the full reason."""
        dec = self.record(confidence)
        log = logger.info if not dec.stop else logger.warning
        subj = f"[{self.subject}] " if self.subject else ""
        log(
            "%stermination check @iter %d: conf=%.3f Δ=%.4f → %s (%s)",
            subj, dec.iteration, dec.confidence,
            dec.marginal_confidence_delta,
            "STOP" if dec.stop else "continue", dec.reason,
        )
        return dec

    def _count_trailing_stagnant(self) -> int:
        n = 0
        confs = self._confidences
        for i in range(len(confs) - 1, 0, -1):
            if abs(confs[i] - confs[i - 1]) < self.confidence_delta_threshold:
                n += 1
            else:
                break
        return n

    def decisions(self) -> list[StopDecision]:
        return list(self._decisions)


# ────────────────────────────────────────────────────────────────────────
# 2. Loop-level calibration trace
# ────────────────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class IterationRecord:
    """One iteration of an evidence-accumulating loop, scoreable later.

    Shape contract with R1's retrodiction harness (stable keys):
      iteration, timestamp, confidence, evidence_total, confirming,
      disconfirming, neutral, task_class, notes.
    """

    iteration: int
    timestamp: str
    confidence: float
    evidence_total: int
    confirming: int
    disconfirming: int
    neutral: int
    task_class: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "timestamp": self.timestamp,
            "confidence": self.confidence,
            "evidence_total": self.evidence_total,
            "confirming": self.confirming,
            "disconfirming": self.disconfirming,
            "neutral": self.neutral,
            "task_class": self.task_class,
            "notes": self.notes,
        }


class LoopCalibrationTrace:
    """Per-iteration confidence/evidence ledger for one question.

    Purpose: make "more confident" separable from "more accurate" so R1's
    harness can score confidence-per-iteration against resolved outcomes.
    An overconfidence-manufacturing loop shows up as confidence rising while
    disconfirming evidence is ignored or absent — visible here.
    """

    def __init__(self, subject: str = ""):
        self.subject = subject
        self.records: list[IterationRecord] = []

    def add_iteration(
        self,
        confidence: float,
        evidence_counts: dict[str, int],
        task_class: Optional[str] = None,
        notes: str = "",
    ) -> IterationRecord:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {confidence!r}")
        confirming = int(evidence_counts.get("confirming", 0))
        disconfirming = int(evidence_counts.get("disconfirming", 0))
        neutral = int(evidence_counts.get("neutral", 0))
        rec = IterationRecord(
            iteration=len(self.records) + 1,
            timestamp=_utc_now_iso(),
            confidence=float(confidence),
            evidence_total=confirming + disconfirming + neutral,
            confirming=confirming,
            disconfirming=disconfirming,
            neutral=neutral,
            task_class=task_class,
            notes=notes,
        )
        self.records.append(rec)
        return rec

    def to_records(self) -> list[dict[str, Any]]:
        """Stable export shape for the retrodiction harness."""
        return [r.to_dict() for r in self.records]

    def summary(self) -> dict[str, Any]:
        """Headline diagnostics: confidence drift vs evidence drift.

        ``overconfidence_suspected`` is True when confidence climbed
        materially while the disconfirming share of evidence never grew —
        the loop-level analogue of a lowered gate, and the thing the harness
        exists to catch.
        """
        if not self.records:
            return {"iterations": 0}
        first, last = self.records[0], self.records[-1]
        conf_gain = last.confidence - first.confidence
        evid_gain = last.evidence_total - first.evidence_total
        disc_first = first.disconfirming
        disc_last = sum(r.disconfirming for r in self.records[-3:])
        # Confidence rose ≥10pts while zero disconfirming evidence was ever
        # registered after iteration 1 → the loop may be flattering itself.
        overconfidence = conf_gain >= 0.10 and disc_last == 0
        # Simple least-squares slope of confidence across iterations.
        slope = _slope([r.confidence for r in self.records])
        return {
            "iterations": len(self.records),
            "subject": self.subject,
            "final_confidence": last.confidence,
            "confidence_gain": round(conf_gain, 4),
            "evidence_gain": evid_gain,
            "confidence_slope_per_iteration": round(slope, 6),
            "disconfirming_seen": sum(r.disconfirming for r in self.records),
            "overconfidence_suspected": bool(overconfidence),
        }


def _slope(ys: list[float]) -> float:
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


# ────────────────────────────────────────────────────────────────────────
# 3. State compaction between iterations
# ────────────────────────────────────────────────────────────────────────

# Item contract: {"id", "content", "stance": supporting|contradicting|neutral,
#                 "tier": 1..5 (source quality, lower = better), "iteration"}
# Unknown/missing stance → "neutral"; unknown tier → 4 (secondary analysis).


def compact_state(
    items: Iterable[dict],
    max_supporting: int = 8,
    max_neutral: int = 4,
) -> tuple[list[dict], list[dict]]:
    """Explicit iteration-boundary compaction, biased toward dissent.

    Policy (in order):
      1. EVERY item whose stance is "contradicting" survives verbatim —
         never dropped, never summarised away, however many there are.
         The one contradicting source is the most expensive thing to lose.
      2. Supporting items survive best-tier-first up to ``max_supporting``.
      3. Neutral items survive up to ``max_neutral``, best-tier-first.
      4. Everything dropped is returned in the second list WITH the reason,
         so a downstream conclusion can be audited against what was lost.

    Returns ``(kept, dropped)`` — both lists of item dicts; dropped items
    gain ``"dropped_reason"``. Pure: mutates nothing, returns copies.
    """
    kept: list[dict] = []
    dropped: list[dict] = []

    contradicting: list[dict] = []
    supporting: list[dict] = []
    neutral: list[dict] = []

    for raw in items:
        item = dict(raw)
        stance = str(item.get("stance") or "neutral").lower()
        if stance not in ("supporting", "contradicting", "neutral"):
            stance = "neutral"
        item["stance"] = stance
        try:
            tier = int(item.get("tier", 4))
        except (TypeError, ValueError):
            tier = 4
        item["tier"] = tier
        if "id" not in item:
            raise ValueError(f"compaction item missing 'id': {item!r}")
        if stance == "contradicting":
            contradicting.append(item)
        elif stance == "supporting":
            supporting.append(item)
        else:
            neutral.append(item)

    # Tier 1 first; id as tiebreaker for determinism.
    key = lambda it: (it["tier"], str(it.get("id")))

    for it in contradicting:
        kept.append(it)
    for it in sorted(supporting, key=key)[:max_supporting]:
        kept.append(it)
    for it in sorted(supporting, key=key)[max_supporting:]:
        it["dropped_reason"] = (
            f"supporting over budget ({max_supporting}) — lowest tiers "
            f"dropped first; contradicting items are never dropped"
        )
        dropped.append(it)
    for it in sorted(neutral, key=key)[:max_neutral]:
        kept.append(it)
    for it in sorted(neutral, key=key)[max_neutral:]:
        it["dropped_reason"] = f"neutral over budget ({max_neutral})"
        dropped.append(it)

    logger.info(
        "compact_state: kept %d (%d contradicting preserved verbatim), "
        "dropped %d with reasons logged",
        len(kept), len(contradicting), len(dropped),
    )
    return kept, dropped


# ────────────────────────────────────────────────────────────────────────
# 4. Per-phase task-class allocation
# ────────────────────────────────────────────────────────────────────────

# Phase → canonical task_class. Values MUST be declared in
# config/providers.yaml routing.task_classes — the ProviderRouter raises
# loudly on undeclared classes and we preserve that property rather than
# hiding it behind a fallback.
LOOP_PHASE_TASK_CLASSES: dict[str, str] = {
    # Framing / decomposition — first iteration. Bad framing dooms
    # everything downstream; capability pays most here.
    "framing": "promotion_judgment",
    # Evidence grind — the middle ~90% of volume. Extraction-class work;
    # local models do this well at zero marginal cost.
    "evidence_grind": "extraction",
    # Mid-loop synthesis checkpoints between grind waves.
    "interim_synthesis": "classification",
    # Final synthesis — assemble the conclusion from accumulated evidence.
    "synthesis": "research_synthesis",
    # Adversarial review — last iteration. Catching a subtle flaw is harder
    # than producing the conclusion; a weak critic rubber-stamps.
    "adversarial_review": "adversarial_review",
}

_KNOWN_PHASES = frozenset(LOOP_PHASE_TASK_CLASSES)


def task_class_for_phase(phase: str) -> str:
    """Canonical task_class for a loop phase.

    Raises KeyError on unknown phase — LOUD by design, same philosophy as
    ProviderRouter's rejection of undeclared task classes. Callers validate
    against their own providers.yaml via
    ``ProviderRouter.canonical_task_class(result)`` if they want the config
    cross-check.
    """
    tc = LOOP_PHASE_TASK_CLASSES.get(phase)
    if tc is None:
        raise KeyError(
            f"unknown loop phase {phase!r}; known: {sorted(_KNOWN_PHASES)}"
        )
    return tc


def phase_sequence(position: int, total: int) -> str:
    """Map a 0-based iteration position to a loop phase.

    First iteration frames, last adversarially reviews, and the middle is
    grind with periodic synthesis checkpoints (every 3rd middle iteration).
    total <= 1 collapses to framing alone.
    """
    if total < 1:
        raise ValueError("total must be >= 1")
    if not 0 <= position < total:
        raise ValueError(f"position {position} out of range for total {total}")
    if total <= 1:
        return "framing"
    if position == 0:
        return "framing"
    if position == total - 1:
        return "adversarial_review"
    middle_idx = position - 1
    middle_len = total - 2
    if middle_len >= 3 and middle_idx > 0 and middle_idx % 3 == 0:
        return "interim_synthesis"
    return "evidence_grind"


def task_class_for_iteration(position: int, total: int) -> str:
    """Convenience: task_class for iteration ``position`` of ``total``."""
    return task_class_for_phase(phase_sequence(position, total))


# ────────────────────────────────────────────────────────────────────────
# 5. Anti-thrash: shared progress-window logic (mirrors ResearchLoop's
#    _check_progress counters; extracted so it is unit-testable without a
#    live loop, DB, or Claude call).
# ────────────────────────────────────────────────────────────────────────


@dataclass
class ProgressVerdict:
    progressing: bool
    consecutive_no_progress: int
    spinning: bool
    diagnose: bool  # run the (once-per-episode) spinning diagnosis now
    detail: str


def evaluate_progress_window(
    prev_snapshot: Optional[dict],
    curr_snapshot: dict,
    consecutive_no_progress: int,
    already_diagnosed_this_episode: bool,
    *,
    stagnation_threshold: int = 3,
) -> ProgressVerdict:
    """Pure core of ResearchLoop._check_progress.

    Fixes over the inline version, behaviour-preserving otherwise:
      * diagnosis fires ONCE per spin episode (the inline version re-ran the
        Claude escalation on every subsequent no-progress check — spam).
      * DB-failure sentinels (-1) are treated as "unknown", never as
        negative progress.
    """
    if prev_snapshot is None:
        return ProgressVerdict(
            progressing=True, consecutive_no_progress=0, spinning=False,
            diagnose=False, detail="first snapshot — baseline",
        )

    new_promotions = curr_snapshot.get("promotions", 0) - prev_snapshot.get("promotions", 0)
    prev_signals = prev_snapshot.get("total_signals", 0)
    curr_signals = curr_snapshot.get("total_signals", 0)
    if prev_signals < 0 or curr_signals < 0:
        new_signals = 0
        signals_known = False
    else:
        new_signals = curr_signals - prev_signals
        signals_known = True
    cycles_elapsed = curr_snapshot.get("cycle", 0) - prev_snapshot.get("cycle", 0)

    progressing = new_promotions > 0 or (signals_known and new_signals > 0)

    if progressing:
        return ProgressVerdict(
            progressing=True, consecutive_no_progress=0, spinning=False,
            diagnose=False,
            detail=(f"+{new_signals} signals, +{new_promotions} promotions "
                    f"over {cycles_elapsed} cycles"),
        )

    streak = consecutive_no_progress + 1
    spinning = streak >= stagnation_threshold
    diagnose = spinning and not already_diagnosed_this_episode
    detail = (
        f"0 new signals, 0 promotions over {cycles_elapsed} cycles — "
        f"streak {streak}"
    )
    return ProgressVerdict(
        progressing=False, consecutive_no_progress=streak,
        spinning=spinning, diagnose=diagnose, detail=detail,
    )
