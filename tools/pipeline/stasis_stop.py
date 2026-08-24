"""JOB 3 — the stop rule, derived from the measured distribution.

The measurement (data/stopping_rules/round_distribution.json) shows:

  Round 1: always conclusion-moving (the first evidence IS the conclusion).
  Rounds that add a new independent key or a better source class: moving.
  Rounds after which NEITHER tier-determining class NOR stance inputs
  changed: pure cost — and in the golden corpus, once a round is pure cost
  the remaining rounds are pure cost too (state never recovers: refine_query
  re-queries with overlapping tokens against already-exhausted sources).

THE RULE (not a tuned saturation threshold — a state-change rule):
  stop when the just-finished round changed neither the independent-key set
  nor the admitted-body set. The next model call would receive an identical
  evidence payload, so no fetch in any further round can alter the sealed
  tier, stance, or confidence.

GUARD RAIL vs honest nulls (tools/gaps): the rule must NOT collapse "more
evidence stopped helping" into "there is nothing there". Two provisions:

  1. The stop fires only AFTER at least one full round has run and only on
     STATE STASIS. It never inspects confidence, tiers, or thresholds, so it
     cannot raise anything.
  2. A leaf stopped by this rule still goes through classify_null_kind()
     unchanged. If sources were never reached properly, the null is still
     labelled retrieval_failure; if they were reached and answered, it stays
     honest_null. The stop reason records WHICH rule fired ("stasis" vs
     "sufficient"), preserving the distinction in the trace.

This module provides StasisStop, an optional IterativeRetriever collaborator.
Wiring it in is opt-in per retriever instance; default behaviour is
byte-identical to pre-change code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("callisto.retrieval")


@dataclass
class _State:
    indep_keys: frozenset = frozenset()
    sha_set: frozenset = frozenset()

    def fingerprint(self) -> tuple:
        return (self.indep_keys, self.sha_set)


@dataclass
class StasisStop:
    """Stop when a round left the conclusion-relevant state unchanged.

    Attach as ``retriever.stasis_stop``. The retriever consults
    :meth:`record` after each round; when it returns True the loop breaks
    with stop_reason "stasis: ...". min_round=1 means the rule can fire as
    early as after round 1 IF round 1 moved nothing (a total miss), but the
    engine's own sufficiency check still runs first — this rule only ever
    stops EARLIER than budget exhaustion on evidence that provably cannot
    change the conclusion.
    """

    #: how many rounds must have run before the rule may fire
    min_round: int = 1
    _last_fp: tuple = field(default=None, init=False)
    _fired_at: int = field(default=0, init=False)

    def record(self, rnd: int, indep_keys, admitted_pairs) -> bool:
        """Feed one round's cumulative state; True => stop now."""
        fp = (frozenset(indep_keys), frozenset(admitted_pairs))
        if rnd < self.min_round:
            self._last_fp = fp
            return False
        stop = self._last_fp is not None and fp == self._last_fp
        # NOTE: also fires when round 1 == round 0 baseline? No — _last_fp
        # starts None, so the first recorded round can never fire. The rule
        # needs TWO consecutive identical states, i.e. one wasted round.
        if not stop:
            self._last_fp = fp
        else:
            self._fired_at = rnd
            logger.info("stasis stop at round %d: state unchanged", rnd)
        return stop

    @property
    def fired_at(self) -> int:
        return self._fired_at


def observe_with_stasis(state: dict, stasis: StasisStop) -> bool:
    """Adapter from the round_observer payload to StasisStop.record."""
    return stasis.record(state["round"], state["indep_keys"],
                         [sha for _, sha in state["admitted"]])
