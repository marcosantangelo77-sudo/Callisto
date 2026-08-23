"""Routing policy over measured (role, model) scores.

## Why Thompson sampling, not epsilon-greedy

Epsilon-greedy explores uniformly forever: it keeps paying full exploration
cost on a model that has already proven 20% worse, and it explores a model
with n=1 exactly as hard as one with n=200. Thompson sampling draws each
candidate's quality from its own posterior, so:

- exploration is automatic and self-annealing — a model with few observations
  has a wide posterior and gets sampled often; a model with many consistent
  observations is sampled almost always;
- the exploration rate scales with UNCERTAINTY, not a fixed constant;
- a newly appearing model starts from an uninformative prior and is explored
  immediately without ever inheriting another model's track record.

## Posterior model

Each candidate's mean Brier loss is drawn from a Normal posterior centred on
its RECENCY-WEIGHTED mean (exponential decay; the store is append-only, so
staleness is handled at read time, never by rewriting history) with spread
sd/sqrt(effective_n), widened when recent form disagrees with the lifetime
mean — a model whose record shifted is explored more, not silently trusted.

Quality compares MAGNITUDE, not a coarse hit/miss encoding, so a model 2%
better in mean loss is measurably 2% better in the draw.

## Cost awareness

Cost enters the comparison in Brier-equivalent units through an explicit
exchange rate: `usd_per_brier_point` (default 5.0) — paying $5 for one full
point of mean-Brier improvement is break-even. Effective loss is

    sampled_brier + cost_weight * (est_call_usd / usd_per_brier_point)

so a model scoring 2% better at 50x the price usually loses, and setting
cost_weight = 0 recovers pure-quality routing. Local endpoints are free at
the margin (unit cost 0), so exploring them is nearly costless — measurements
on free compute are the cheapest knowledge the system can buy.

## Honesty contract

Every decision returns which BASIS it used — "configured" (no data),
"unmeasured" (winner had no record), or "sparse"/"provisional"/"measured" —
plus the sampled effective loss and every candidate's aggregate. Callers can
always see whether tonight's routing was science or config.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from tools.routing.scores import ModelScoreStore


@dataclass
class CandidateModel:
    """One model eligible for a role, with its unit costs."""
    name: str                 # model identifier as recorded in the score store
    tier: str                 # configured endpoint/tier name (for the router)
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    # Preference order from providers.yaml (lower = configured first choice).
    config_rank: int = 0


@dataclass
class RoutingDecision:
    """What was chosen, and — crucially — ON WHAT BASIS."""
    tier: str
    model: str
    basis: str                        # configured | unmeasured | sparse |
                                      # provisional | measured
    sampled_effective_loss: Optional[float] = None
    scores_used: dict = field(default_factory=dict)   # {model: aggregate}


class ThompsonRoutingPolicy:
    """Thompson sampling over per-(role, model) Brier posteriors + cost."""

    CHANCE_BASELINE = 0.25     # Brier of a coin flip; the uninformed prior
    NEW_MODEL_PRIOR_SD = 0.15  # wide prior -> new models get explored
    MIN_SD = 0.02              # observation-level sd floor (honest variance)
    MIN_POSTERIOR_SD = 0.01    # posterior sd floor (no zero-width certainty)
    HALF_LIFE_RECORDS = 60.0   # recency decay, in observations
    # Estimated tokens for cost comparison; coarse by design.
    EST_INPUT_TOKENS = 1000
    EST_OUTPUT_TOKENS = 500

    def __init__(self,
                 store: Optional[ModelScoreStore] = None,
                 cost_weight: float = 0.5,
                 rng: Optional[random.Random] = None,
                 usd_per_brier_point: float = 5.0):
        self.store = store or ModelScoreStore()
        self.cost_weight = float(cost_weight)
        self.rng = rng or random.Random()
        self.usd_per_brier_point = float(usd_per_brier_point)

    # ── posterior ──

    def _records_for(self, role: str, task_class: str,
                     model: str) -> tuple[list[dict], bool]:
        """Records for (role, task_class, model), plus whether the slice is
        measured.

        W8 fix: routing used to pool every task_class under a role, so a
        model measured only on classification could win synthesis draws it
        was never measured on. When the task-class slice is empty the
        candidate is treated as UNMEASURED for this call — it gets the wide
        chance-centred draw (explored, never trusted) and inherits nothing
        from measurements taken on other task classes. That is the honest
        reading: "this model is great at classification" is simply not
        evidence about how it does synthesis.
        """
        recs = [r for r in self.store.load_all()
                if r.get("role") == role
                and r.get("task_class") == task_class
                and r.get("model") == model]
        return recs, bool(recs)

    def _sample_loss(self, records: list[dict]) -> float:
        """One draw of the model's true mean loss from its posterior.

        Recency-weighted: the store is APPEND-ONLY (history is never
        rewritten), but a model that improved or collapsed mid-record must be
        judged on recent form. Exponential decay downweights stale
        observations at READ time — disclosed here, applied uniformly.
        """
        n = len(records)
        decay = 0.5 ** (1.0 / self.HALF_LIFE_RECORDS)
        # records are in append order == chronological order
        weights = [decay ** (n - 1 - i) for i in range(n)]
        wsum = sum(weights)
        wmean = sum(w * r["brier"] for w, r in zip(weights, records)) / wsum
        var = sum(w * (r["brier"] - wmean) ** 2
                  for w, r in zip(weights, records)) / wsum
        sd = max(var ** 0.5, self.MIN_SD)
        raw_lifetime = sum(r["brier"] for r in records) / n
        # Centre on recent form; disagreement between recent form and the
        # LIFETIME (unshrunk) mean widens the posterior — genuine
        # distribution-shift signal. Shrinkage is deliberately NOT part of
        # this term: a steady model's shrinkage gap is not instability.
        spread = max(sd / wsum ** 0.5, abs(raw_lifetime - wmean),
                     self.MIN_POSTERIOR_SD)
        return self.rng.gauss(wmean, spread)

    # ── cost ──

    def _unit_cost(self, c: CandidateModel) -> float:
        return (c.cost_per_1k_input * self.EST_INPUT_TOKENS / 1000.0
                + c.cost_per_1k_output * self.EST_OUTPUT_TOKENS / 1000.0)

    def _cost_penalty(self, c: CandidateModel) -> float:
        """Call cost converted to Brier-equivalent units."""
        if self.usd_per_brier_point <= 0:
            return 0.0
        return (self.cost_weight * self._unit_cost(c)
                / self.usd_per_brier_point)

    # ── decision ──

    def decide(self, role: str,
               candidates: list[CandidateModel],
               task_class: Optional[str] = None) -> RoutingDecision:
        """Pick a tier for this role's next call.

        No candidate has any measurement -> basis="configured", best-ranked
        candidate (exact degradation to today's behaviour). Otherwise every
        candidate competes on its sampled effective loss; candidates with NO
        observations get a wide chance-centred Thompson draw — explored, never
        trusted, and inheriting nothing from any other model's record.

        `task_class` (W8 fix): when given, each candidate is judged on its
        (role, task_class) slice — falling back to the role-wide record only
        when the slice is empty — so a classification specialist cannot win
        synthesis calls it was never measured on.
        """
        summary = self.store.summary(role)

        if not any(c.name in summary for c in candidates):
            ordered = sorted(candidates, key=lambda c: c.config_rank)
            chosen = ordered[0]
            return RoutingDecision(tier=chosen.tier, model=chosen.name,
                                   basis="configured", scores_used={})

        best_name, best_loss = None, math.inf
        details: dict[str, dict] = {}
        for c in candidates:
            agg = summary.get(c.name)
            if task_class:
                recs, measured = self._records_for(role, task_class, c.name)
            elif agg is not None:
                recs, measured = self.store.records_for(role, c.name), True
            else:
                recs, measured = [], False
            if measured:
                sampled = self._sample_loss(recs)
            else:
                # Unmeasured model: Thompson draw from the wide uninformed
                # prior. Real cost still applies.
                sampled = self.rng.gauss(self.CHANCE_BASELINE,
                                         self.NEW_MODEL_PRIOR_SD)
            eff = sampled + self._cost_penalty(c)
            if agg is not None:
                details[c.name] = {**agg,
                                   "sampled_effective_loss": round(eff, 6)}
            else:
                details[c.name] = {"n": 0, "basis": "unmeasured",
                                   "sampled_effective_loss": round(eff, 6)}
            if eff < best_loss:
                best_name, best_loss = c, eff

        assert best_name is not None
        winner_n = details[best_name.name].get("n", 0)
        basis = ("unmeasured" if not winner_n
                 else self.store.basis_label(winner_n))
        return RoutingDecision(
            tier=best_name.tier, model=best_name.name, basis=basis,
            sampled_effective_loss=round(best_loss, 6),
            scores_used=details,
        )
