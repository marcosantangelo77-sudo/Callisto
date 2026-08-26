"""Empirical (measured) candidate reordering for ProviderRouter.

With empirical routing disabled or zero measurements, behaviour is
byte-identical to the configured tier list. Identity grouping keeps
proxy/CLI rails of the same model contiguous.
"""

from __future__ import annotations

from typing import Optional

from inference_kernel import logger


class EmpiricalRoutingMixin:
    """Methods mixed into ProviderRouter — extracted, not rewritten."""

    def _candidates_as_models(self, names: list[str]) -> list:
        from tools.routing.policy import CandidateModel
        out = []
        seen_identities: set[str] = set()
        for rank, n in enumerate(names):
            ep = self.endpoints.get(n)
            if ep is None:
                continue
            model_name = self.scoring_model_name(n)
            # Dedupe ONLY rails with an explicit canonical model identity.
            # Identity-less endpoints keep legacy standalone behaviour even
            # when their display `model` labels collide.
            if ep.model_identity:
                if ep.model_identity in seen_identities:
                    # Same canonical model via another transport rail: ONE
                    # scoring candidate, not several.
                    continue
                seen_identities.add(ep.model_identity)
            out.append(CandidateModel(
                name=model_name,
                tier=n,
                cost_per_1k_input=ep.cost_per_1k_input,
                cost_per_1k_output=ep.cost_per_1k_output,
                config_rank=rank,
            ))
        return out

    def route_order(self, task_class: str,
                    candidate_names: list[str],
                    role: Optional[str] = None) -> tuple[list[str], dict]:
        """Apply the empirical policy to one candidate list.

        Returns (reordered_names, honesty_metadata). With empirical routing
        disabled or zero measurements anywhere for this role, returns
        (candidate_names unchanged, {"basis": "configured"}) — exact
        degradation to today's configured behaviour.
        """
        meta: dict = {"basis": "configured", "role": role or task_class}
        if not self.empirical_routing_enabled or len(candidate_names) < 2:
            return candidate_names, meta
        try:
            from tools.routing.policy import ThompsonRoutingPolicy
            if self._routing_policy is None:
                self._routing_policy = ThompsonRoutingPolicy(
                    store=self.score_store,
                    cost_weight=self.empirical_cost_weight,
                    usd_per_brier_point=self.empirical_usd_per_brier_point)
            cands = self._candidates_as_models(candidate_names)
            if not cands:
                return candidate_names, meta
            decision = self._routing_policy.decide(role or task_class, cands)
        except Exception as e:  # never let measurement break a live call
            logger.warning(f"Empirical routing failed ({e}) — using config order")
            return candidate_names, {**meta, "error": str(e)}
        meta.update({
            "basis": decision.basis,
            "chosen_model": decision.model,
            "sampled_effective_loss": decision.sampled_effective_loss,
            "scores": decision.scores_used,
        })
        winner_identity: Optional[str] = None
        tier_ep = self.endpoints.get(decision.tier)
        if tier_ep is not None and tier_ep.model_identity:
            winner_identity = tier_ep.model_identity

        def _is_winner_rail(n: str) -> bool:
            if n == decision.tier:
                return True
            if winner_identity is None:
                return False
            ep = self.endpoints.get(n)
            return ep is not None and ep.model_identity == winner_identity

        if any(_is_winner_rail(n) for n in candidate_names):
            # Chosen model's ENTIRE rail group moves to the front as one
            # contiguous block (configured order preserved), so a proxy/CLI
            # failover pair is never separated. The rest keep their failover
            # order so a dead winner still degrades exactly as before.
            winners = [n for n in candidate_names if _is_winner_rail(n)]
            rest = [n for n in candidate_names if not _is_winner_rail(n)]
            return winners + rest, meta
        return candidate_names, meta

    def _group_by_identity(self, names: list[str]) -> list[str]:
        """Collapse rails that share a canonical model identity so the same
        physical model is ONE candidate, not several. The first-declared rail
        keeps its position (preserving configured transport priority); later
        rails of the same identity move to directly after it as failovers.
        Endpoints WITHOUT model_identity keep legacy per-endpoint behaviour."""
        if len(names) < 2:
            return names
        # group index per identity / standalone endpoint, assigned at FIRST
        # appearance in the configured order.
        group_of: dict[str, int] = {}  # identity -> group index
        standalone_group: dict[str, int] = {}  # endpoint -> group index
        next_group = 0
        for n in names:
            ident = self.endpoints[n].model_identity if n in self.endpoints else None
            if ident is None:
                standalone_group[n] = next_group
                next_group += 1
            elif ident not in group_of:
                group_of[ident] = next_group
                next_group += 1
        # Stable sort by group index preserves configured order WITHIN every
        # group while keeping each identity contiguous at its first
        # appearance; no-identity endpoints remain standalone.
        return sorted(names, key=lambda n: (
            group_of[self.endpoints[n].model_identity]
            if n in self.endpoints and self.endpoints[n].model_identity
            else standalone_group[n]))

    def scoring_model_name(self, endpoint_name: str) -> str:
        """Canonical name to record/lookup in the score store for an endpoint.
        Rails sharing a model identity share one scoring candidate; without
        an identity the display model label is used (legacy behaviour)."""
        ep = self.endpoints.get(endpoint_name)
        if ep is not None and ep.model_identity:
            return ep.model_identity
        return ep.model if ep is not None else endpoint_name
