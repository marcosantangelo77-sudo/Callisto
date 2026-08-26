"""
Autonomous hypothesis generator — turns embedded data into testable betting theses.

This is Callisto's creative engine. It:
  1. Analyzes clusters of similar game/prop contexts from the vector store
  2. Detects statistical anomalies within clusters (hit rates, edge persistence)
  3. Generates testable hypotheses with specific model configs
  4. Creates them as drafts in the HypothesisManager for backtesting

Hypothesis templates encode domain knowledge about WHERE edges exist:
  - Props: situational mispricing (rest, pace, matchup, minutes changes)
  - Lines: key number value, stale line detection, reverse movement
  - Boosts: structural +EV from operator promotions

The local models (Architect/Manager) drive this autonomously. Claude Code
escalation handles the heavy statistical analysis when needed.

SPLIT NOTE (tools.hypgen):
  This module is now a facade over tools/hypgen/:
    templates.py    — HYPOTHESIS_TEMPLATES, constants, expand_variables
    prompts.py      — prompt assembly, candidate parsing, variance enforcement
    seeds.py        — underexplored-seed selection
    persistence.py  — DB lifecycle, retrieval helpers, sharpening wiki write-back

  Write-safety contract: neither this facade nor any tools.hypgen module
  issues `signal_generated` or `edge_threshold` UPDATE statements against
  the hypotheses table. Hypothesis creation goes exclusively through
  HypothesisManager.create_hypothesis; the only direct SQL write in the
  package is the documented sharpening-loop INSERT OR REPLACE into
  wiki_articles (see tools/hypgen/persistence.py).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

from tools.embeddings import VectorStore, embed_batch
from tools.hypothesis import HypothesisManager

# Facade re-exports — keeps historical attribute access working
# (e.g. `hypothesis_generator.HYPOTHESIS_TEMPLATES`, module-level constants).
from tools.hypgen import (  # noqa: F401
    CANDIDATE_DEDUP_SIM,
    NEGATIVE_EXAMPLES_N,
    PRIOR_CORPUS_SIM,
    WIKI_CONTEXT_TOP_K,
    HYPOTHESIS_TEMPLATES,
)
from tools.hypgen.persistence import DB_PATH  # noqa: F401
from tools.hypgen.prompts import (
    avg_pairwise_distance,
    build_claude_prompt,
    build_grounded_prompt,
    enforce_variance,
    parse_candidates,
    parse_json_array,
)
from tools.hypgen.seeds import pick_unexplored_seeds
from tools.hypgen.templates import expand_variables
from tools.hypgen.persistence import (
    HypgenDB,
    compute_temporal_metadata,
    recent_theses as _recent_theses,
    record_backtest_outcome_to_wiki as _record_backtest_outcome_to_wiki,
    retrieve_rejection_examples as _retrieve_rejection_examples,
    retrieve_wiki_context as _retrieve_wiki_context,
)

load_dotenv()

logger = logging.getLogger("callisto.hypothesis_generator")


class HypothesisGenerator:
    """Generates testable hypotheses from data patterns and templates."""

    def __init__(
        self,
        hypothesis_manager: HypothesisManager,
        vector_store: VectorStore,
        db_path: str = DB_PATH,
    ):
        self.hypothesis_manager = hypothesis_manager
        self.vector_store = vector_store
        self.db_path = db_path
        self._dbstore = HypgenDB(db_path)

    @property
    def _db(self):
        """Direct aiosqlite connection (compat for callers/tests)."""
        return self._dbstore._db

    @_db.setter
    def _db(self, value):
        """Compat: existing tests/callers inject a raw connection."""
        self._dbstore._db = value

    async def initialize(self) -> None:
        await self._dbstore.initialize()

    async def close(self) -> None:
        await self._dbstore.close()

    async def generate_from_templates(
        self,
        sport: str,
        max_hypotheses: int = 50,
        training_cutoff_date: Optional[str] = None,
    ) -> list[dict]:
        """
        Generate hypotheses from templates for a given sport.
        Expands variable combinations and creates draft hypotheses.
        Skips combinations that already exist.

        Args:
            sport: Sport key (e.g., "basketball_nba")
            max_hypotheses: Max hypotheses to create this call
            training_cutoff_date: ISO date string (YYYY-MM-DD). Data up to this
                date is the training set; backtests will use data after this date.
                Defaults to 30 days before today.

        Returns list of created hypothesis summaries.
        """
        existing_names = await self.hypothesis_manager.get_all_names()

        temporal = compute_temporal_metadata(training_cutoff_date)
        training_period_start = temporal["training_period_start"]
        training_period_end = temporal["training_period_end"]
        forward_test_start = temporal["forward_test_start"]

        created = []

        for template in HYPOTHESIS_TEMPLATES:
            if sport not in template["sport_filter"]:
                continue

            # Player prop templates now supported — prop_snapshots provides
            # multi-book data and BacktestEngine._process_prop_snapshots handles devig.

            # Generate all variable combinations
            combos = expand_variables(template["variables"])

            for combo in combos:
                if len(created) >= max_hypotheses:
                    break

                # Fill template
                name = template["name"].format(**combo)
                if name in existing_names:
                    continue

                thesis = template["thesis"].format(**combo)
                market_type = template["market_type"].format(**combo)
                edge_threshold = combo.get("min_edge", 2) / 100.0

                # Build model config with temporal metadata
                model_config = {}
                for k, v in template["model_config"].items():
                    if isinstance(v, str) and "{" in v:
                        model_config[k] = v.format(**combo)
                    else:
                        model_config[k] = v

                # Convert string numbers to int
                if "consensus_min_books" in model_config:
                    try:
                        model_config["consensus_min_books"] = int(
                            model_config["consensus_min_books"]
                        )
                    except (ValueError, TypeError):
                        pass

                # Attach temporal isolation metadata
                model_config["training_period_start"] = training_period_start
                model_config["training_period_end"] = training_period_end
                model_config["forward_test_start"] = forward_test_start

                try:
                    hid = await self.hypothesis_manager.create_hypothesis(
                        name=name,
                        thesis=thesis,
                        sport=sport,
                        market_type=market_type,
                        model_config=model_config,
                        edge_threshold=edge_threshold,
                        notes=(
                            f"Auto-generated from template '{template['id']}'. "
                            f"Train: [{training_period_start}..{training_period_end}], "
                            f"forward-test from {forward_test_start}."
                        ),
                    )
                    created.append({
                        "hypothesis_id": hid,
                        "name": name,
                        "template": template["id"],
                        "variables": combo,
                        "training_period_end": training_period_end,
                        "forward_test_start": forward_test_start,
                    })
                    existing_names.add(name)
                except Exception as e:
                    logger.warning(f"Failed to create hypothesis '{name}': {e}")

        logger.info(
            f"Generated {len(created)} hypotheses for {sport} "
            f"from {len(HYPOTHESIS_TEMPLATES)} templates "
            f"(training cutoff: {training_period_end})"
        )
        return created

    async def generate_from_clusters(
        self,
        collection: str = "prop_outcomes",
        similarity_threshold: float = 0.85,
        min_cluster_size: int = 10,
        min_hit_rate_delta: float = 0.05,
        data_period: str | None = None,
    ) -> list[dict]:
        """
        Analyze embedding clusters to discover data-driven hypotheses.

        For each cluster of similar prop outcomes:
          1. Check if the cluster has a statistically interesting hit rate
          2. If hit rate diverges from expected, generate a hypothesis
          3. Extract common features from the cluster as context factors

        Args:
            collection: which embedding collection to cluster
            similarity_threshold: min cosine similarity for clustering
            min_cluster_size: ignore clusters smaller than this
            min_hit_rate_delta: min deviation from expected to generate hypothesis
            data_period: 'historical' = cluster only on historical data (for backtesting),
                         'recent' = only recent data, None = all data (for live trading)

        Returns list of created hypothesis summaries.
        """
        clusters = await self.vector_store.cluster_by_similarity(
            collection, threshold=similarity_threshold, data_period=data_period
        )

        created = []
        for cluster in clusters:
            if len(cluster) < min_cluster_size:
                continue

            # Analyze cluster
            analysis = self._analyze_cluster(cluster)
            if not analysis:
                continue

            hit_rate = analysis["hit_rate"]
            expected_rate = analysis["expected_rate"]
            delta = hit_rate - expected_rate

            if abs(delta) < min_hit_rate_delta:
                continue

            # Generate hypothesis from cluster pattern
            side = "Over" if delta > 0 else "Under"
            common = analysis["common_features"]
            sport = common.get("sport", "basketball_nba")
            market = common.get("market", "player_points")

            name = (
                f"Cluster-discovered: {market.replace('player_', '')} "
                f"{side} edge ({common.get('pattern_desc', 'unknown pattern')})"
            )

            thesis = (
                f"In situations matching this cluster pattern "
                f"(N={len(cluster)}, hit_rate={hit_rate:.1%} vs "
                f"expected {expected_rate:.1%}), {side} bets on "
                f"{market} show a {abs(delta)*100:.1f}% edge. "
                f"Pattern features: {common.get('pattern_desc', 'see metadata')}."
            )

            # Tag which embedding data the hypothesis was derived from
            period_label = data_period or "all"

            # Compute temporal isolation metadata for cluster-derived hypotheses
            today = datetime.now(timezone.utc).date()
            training_cutoff = today - timedelta(days=30)
            training_period_start = "2023-01-01"
            training_period_end = str(training_cutoff)
            forward_test_start = str(training_cutoff + timedelta(days=1))

            try:
                hid = await self.hypothesis_manager.create_hypothesis(
                    name=name,
                    thesis=thesis,
                    sport=sport,
                    market_type=market,
                    model_config={
                        "type": "cluster_derived",
                        "devig_method": "power",
                        "target_book": "draftkings",
                        "consensus_min_books": 3,
                        "cluster_features": common,
                        "source_cluster_size": len(cluster),
                        "source_data_period": period_label,
                        "training_period_start": training_period_start,
                        "training_period_end": training_period_end,
                        "forward_test_start": forward_test_start,
                    },
                    edge_threshold=abs(delta),
                    notes=(
                        f"Auto-discovered from {collection} cluster "
                        f"(N={len(cluster)}, data_period={period_label}). "
                        f"Train: [{training_period_start}..{training_period_end}], "
                        f"forward-test from {forward_test_start}."
                    ),
                )
                created.append({
                    "hypothesis_id": hid,
                    "name": name,
                    "cluster_size": len(cluster),
                    "hit_rate": round(hit_rate, 4),
                    "expected_rate": round(expected_rate, 4),
                    "delta": round(delta, 4),
                    "data_period": period_label,
                    "training_period_end": training_period_end,
                    "forward_test_start": forward_test_start,
                })
            except Exception as e:
                logger.warning(f"Failed to create cluster hypothesis: {e}")

        logger.info(
            f"Generated {len(created)} hypotheses from {len(clusters)} clusters "
            f"in '{collection}'"
        )
        return created

    async def generate_from_claude(
        self,
        sport: str,
        data_summary: str,
    ) -> list[dict]:
        """
        Ask the hypothesis_gen ladder (qwen36 primary, Claude last) to
        generate novel hypotheses from a data summary.

        The function keeps its historical name for call-site compatibility,
        but the ladder picks the best available model per task_type and
        respects CALLISTO_LOCAL_ONLY + Claude Max hours demotion.
        """
        from inference import escalate_with_ladder

        result = await escalate_with_ladder(
            prompt=build_claude_prompt(sport, data_summary),
            system_context="Callisto hypothesis generation — return structured JSON only.",
            task_type="hypothesis_gen",
            timeout=120,
            hermes_caller="hypothesis_gen",
        )

        if result.get("error"):
            logger.error(f"Hypothesis generation ladder failed: {result['error']}")
            return []

        # Parse response
        hypotheses_raw = parse_json_array(result.get("content", ""))
        if not hypotheses_raw:
            logger.warning("Could not find JSON array in Claude response")
            return []

        # Temporal metadata for Claude-generated hypotheses
        temporal = compute_temporal_metadata(None)
        training_period_start = temporal["training_period_start"]
        training_period_end = temporal["training_period_end"]
        forward_test_start = temporal["forward_test_start"]

        created = []
        for h_raw in hypotheses_raw:
            try:
                mc = h_raw.get("model_config", {
                    "type": "consensus_devig",
                    "devig_method": "power",
                    "target_book": "draftkings",
                    "consensus_min_books": 3,
                })
                # Inject temporal isolation metadata
                mc["training_period_start"] = training_period_start
                mc["training_period_end"] = training_period_end
                mc["forward_test_start"] = forward_test_start

                hid = await self.hypothesis_manager.create_hypothesis(
                    name=h_raw.get("name", "Unnamed"),
                    thesis=h_raw.get("thesis", ""),
                    sport=sport,
                    market_type=h_raw.get("market_type", "spreads"),
                    model_config=mc,
                    edge_threshold=float(h_raw.get("edge_threshold", 0.02)),
                    notes=(
                        f"Auto-generated by Claude Code hypothesis engine. "
                        f"Train: [{training_period_start}..{training_period_end}], "
                        f"forward-test from {forward_test_start}."
                    ),
                )
                created.append({
                    "hypothesis_id": hid,
                    "name": h_raw.get("name"),
                    "source": "claude_code",
                    "training_period_end": training_period_end,
                    "forward_test_start": forward_test_start,
                })
            except Exception as e:
                logger.warning(f"Failed to create Claude hypothesis: {e}")

        logger.info(f"Claude Code generated {len(created)} hypotheses for {sport}")
        return created

    def _expand_variables(self, variables: dict) -> list[dict]:
        """Expand variable dict into list of all combinations."""
        return expand_variables(variables)

    def _analyze_cluster(self, cluster: list[dict]) -> Optional[dict]:
        """
        Analyze a cluster of prop outcomes to find patterns.
        Returns analysis dict with hit_rate, expected_rate, common_features.
        """
        hits = 0
        total = 0
        edges = []
        sports = []
        markets = []
        players = []

        for item in cluster:
            meta = item.get("metadata") or {}
            if meta.get("hit") is not None:
                total += 1
                if meta["hit"]:
                    hits += 1
            if meta.get("edge") is not None:
                edges.append(meta["edge"])
            if meta.get("sport"):
                sports.append(meta["sport"])
            if meta.get("market"):
                markets.append(meta["market"])
            if meta.get("player"):
                players.append(meta["player"])

        if total < 5:
            return None

        hit_rate = hits / total
        # Expected rate from book implied probabilities
        expected_probs = [
            item.get("metadata", {}).get("book_implied_over", 0.5)
            for item in cluster
            if item.get("metadata", {}).get("book_implied_over") is not None
        ]
        expected_rate = (
            sum(expected_probs) / len(expected_probs)
            if expected_probs
            else 0.5
        )

        # Find most common features
        def mode(lst):
            if not lst:
                return None
            return max(set(lst), key=lst.count)

        common_sport = mode(sports)
        common_market = mode(markets)

        # Build pattern description
        pattern_parts = []
        if common_sport:
            pattern_parts.append(common_sport.replace("basketball_", ""))
        if common_market:
            pattern_parts.append(common_market.replace("player_", ""))
        avg_edge = sum(edges) / len(edges) if edges else 0
        if avg_edge:
            pattern_parts.append(f"avg_edge={avg_edge:.1%}")
        pattern_desc = " ".join(pattern_parts) if pattern_parts else "mixed"

        return {
            "hit_rate": hit_rate,
            "expected_rate": expected_rate,
            "total_resolved": total,
            "avg_edge": avg_edge,
            "common_features": {
                "sport": common_sport,
                "market": common_market,
                "pattern_desc": pattern_desc,
                "unique_players": len(set(players)),
            },
        }

    # ──────────────────────────────────────────────────────────────
    # WIKI-GROUNDED, VARIANCE-ENFORCED GENERATOR
    # (additive — existing generate_* methods unchanged)
    # ──────────────────────────────────────────────────────────────

    async def generate_wiki_grounded(
        self,
        sport: str,
        focus_market: Optional[str] = None,
        n_candidates: int = 8,
        max_keep: int = 5,
        include_seeds: bool = True,
    ) -> dict:
        """
        Retrieve wiki articles + rejection examples, then call the hypothesis_gen
        ladder to produce N candidates, embed them, enforce diversity vs each
        other AND vs prior corpus, and persist the survivors as draft hypotheses.

        Returns a dict:
          {
            "generated": [<hyp dict>, ...],     # final survivors, persisted
            "rejected": [{"reason", "candidate"}, ...],
            "wiki_context_topics": [...],
            "seeds_used": [...],
            "model_used": <str>,
            "diversity_metric": float,          # avg pairwise cosine distance
                                                # (1 - sim) among survivors
          }
        """
        from inference import escalate_with_ladder

        # 1. Retrieve wiki articles related to the sport/market ------------
        wiki_articles = await self._retrieve_wiki_context(sport, focus_market)

        # 2. Pull a handful of rejected hypotheses as negative examples ----
        rejected_examples = await self._retrieve_rejection_examples(
            sport, focus_market, limit=NEGATIVE_EXAMPLES_N
        )

        # 3. Pick underexplored seeds --------------------------------------
        seeds: list[dict] = []
        if include_seeds:
            try:
                existing_names = await self.hypothesis_manager.get_all_names()
                existing_theses = await self._recent_theses(sport)
                seeds = pick_unexplored_seeds(
                    existing_names, existing_theses, sport=sport, max_seeds=3,
                )
            except Exception as e:
                logger.debug(f"Seed retrieval failed (non-fatal): {e}")
                seeds = []

        # 4. Build the grounding prompt ------------------------------------
        prompt = build_grounded_prompt(
            sport, focus_market, wiki_articles, rejected_examples, seeds, n_candidates
        )

        # 5. Call the ladder ------------------------------------------------
        result = await escalate_with_ladder(
            prompt=prompt,
            system_context=(
                "You are Callisto's hypothesis engine. Produce specific, "
                "SQL-filterable, backtest-able hypotheses. Return JSON ONLY."
            ),
            task_type="hypothesis_gen",
            timeout=180,
            hermes_caller="hypothesis_gen_wiki",
        )
        model_used = result.get("model_used", "unknown")
        content = result.get("content", "")
        empty_result = {
            "generated": [], "rejected": [],
            "wiki_context_topics": [a.get("topic") for a in wiki_articles],
            "seeds_used": [s["seed_id"] for s in seeds],
            "model_used": model_used, "diversity_metric": 0.0,
        }
        if result.get("error"):
            logger.error(f"grounded generator ladder error: {result['error']}")
            return dict(empty_result)

        candidates = parse_candidates(content)
        if not candidates:
            logger.warning("grounded generator returned no parseable candidates")
            return dict(empty_result)

        # 6. Embed candidate thesis statements in ONE batch -----------------
        thesis_texts = [
            (c.get("thesis_statement")
             or c.get("thesis")
             or c.get("name")
             or "").strip()
            for c in candidates
        ]
        try:
            cand_embs = await embed_batch(thesis_texts)
        except Exception as e:
            logger.warning(f"embed_batch failed ({e}); skipping variance step")
            cand_embs = []

        # 7. Variance-enforce vs each other and vs prior corpus --------------
        kept_indices, drop_reasons = await enforce_variance(
            candidates, cand_embs, wiki_articles
        )
        kept_indices = kept_indices[:max_keep]

        # 8. Persist survivors as draft hypotheses ---------------------------
        temporal = compute_temporal_metadata(None)
        training_period_start = temporal["training_period_start"]
        training_period_end = temporal["training_period_end"]
        forward_test_start = temporal["forward_test_start"]

        created: list[dict] = []
        rejected_log = drop_reasons[:]
        for i in kept_indices:
            c = candidates[i]
            try:
                mc = c.get("model_config") or {
                    "type": "consensus_devig",
                    "devig_method": "power",
                    "target_book": "draftkings",
                    "consensus_min_books": 3,
                }
                mc["training_period_start"] = training_period_start
                mc["training_period_end"] = training_period_end
                mc["forward_test_start"] = forward_test_start
                mc["grounding"] = {
                    "source": "wiki_grounded_v1",
                    "wiki_topics": [a.get("topic") for a in wiki_articles][:5],
                    "seed_ids": [s["seed_id"] for s in seeds],
                    "ladder_model": model_used,
                }

                thesis_txt = (
                    c.get("thesis_statement")
                    or c.get("thesis")
                    or c.get("signal_logic")
                    or ""
                )
                name = c.get("name", f"auto_{sport}_{i}")
                market = (c.get("market_type") or c.get("market")
                          or focus_market or "spreads")
                edge = c.get("edge_threshold")
                if edge is None:
                    edge = c.get("ic_prior_estimate", 0.02)
                try:
                    edge = float(edge)
                except (TypeError, ValueError):
                    edge = 0.02

                hid = await self.hypothesis_manager.create_hypothesis(
                    name=name,
                    thesis=thesis_txt,
                    sport=sport,
                    market_type=market,
                    model_config=mc,
                    edge_threshold=edge,
                    notes=(
                        f"Wiki-grounded generation (model={model_used}). "
                        f"Train: [{training_period_start}..{training_period_end}], "
                        f"forward-test from {forward_test_start}."
                    ),
                )
                created.append({
                    "hypothesis_id": hid,
                    "name": name,
                    "thesis": thesis_txt,
                    "market_type": market,
                    "source": "wiki_grounded",
                })
            except Exception as e:
                logger.warning(f"grounded generator persist failed: {e}")
                rejected_log.append({"reason": f"persist_error: {e}",
                                     "candidate": c})

        # 9. Diversity metric on the survivors ------------------------------
        kept_embs = [cand_embs[i] for i in kept_indices if i < len(cand_embs)]
        diversity = avg_pairwise_distance(kept_embs)

        logger.info(
            f"grounded generator: sport={sport} survivors={len(created)} "
            f"dropped={len(rejected_log)} diversity={diversity:.3f} "
            f"model={model_used}"
        )
        return {
            "generated": created,
            "rejected": rejected_log,
            "wiki_context_topics": [a.get("topic") for a in wiki_articles],
            "seeds_used": [s["seed_id"] for s in seeds],
            "model_used": model_used,
            "diversity_metric": round(diversity, 4),
        }

    # ── helper: wiki retrieval ────────────────────────────────────
    async def _retrieve_wiki_context(
        self, sport: str, focus_market: Optional[str]
    ) -> list[dict]:
        """Semantic-search the wiki for articles related to sport/market."""
        return await _retrieve_wiki_context(
            self._dbstore, sport, focus_market, top_k=WIKI_CONTEXT_TOP_K
        )

    # ── helper: rejected-hypothesis retrieval ─────────────────────
    async def _retrieve_rejection_examples(
        self, sport: str, focus_market: Optional[str], limit: int
    ) -> list[dict]:
        """Pull a few recent rejected hypotheses in the same cohort."""
        return await _retrieve_rejection_examples(
            self._dbstore, sport, focus_market, limit
        )

    async def _recent_theses(self, sport: str, limit: int = 50) -> list[str]:
        return await _recent_theses(self._dbstore, sport, limit)

    # ── helper: prompt construction ───────────────────────────────
    def _build_grounded_prompt(
        self,
        sport: str,
        focus_market: Optional[str],
        wiki_articles: list[dict],
        rejected_examples: list[dict],
        seeds: list[dict],
        n_candidates: int,
    ) -> str:
        return build_grounded_prompt(
            sport, focus_market, wiki_articles, rejected_examples, seeds, n_candidates
        )

    # ── helper: tolerant JSON extraction ──────────────────────────
    @staticmethod
    def _parse_candidates(content: str) -> list[dict]:
        return parse_candidates(content)

    # ── helper: variance enforcement ──────────────────────────────
    async def _enforce_variance(
        self,
        candidates: list[dict],
        cand_embs: list[list[float]],
        wiki_articles: list[dict],
    ) -> tuple[list[int], list[dict]]:
        """Greedy selection dropping near-duplicates and wiki overlaps.

        Returns (kept_indices, drop_reasons)."""
        return await enforce_variance(candidates, cand_embs, wiki_articles)

    @staticmethod
    def _avg_pairwise_distance(embs: list[list[float]]) -> float:
        """1 - mean cosine similarity across all pairs (higher = more diverse)."""
        return avg_pairwise_distance(embs)

    # ──────────────────────────────────────────────────────────────
    # SHARPENING LOOP: post-backtest wiki article
    # ──────────────────────────────────────────────────────────────
    async def record_backtest_outcome_to_wiki(
        self,
        hypothesis_id: str,
        outcome: str,   # "success" | "failure" | "inconclusive"
        stats: Optional[dict] = None,
    ) -> bool:
        """Write a wiki article summarizing why a hypothesis did or didn't work.

        Called from hypothesis_mgr post-backtest hook. Next generation cycle
        will retrieve the article via semantic search, so the LLM can avoid
        re-proposing near-duplicates.

        Returns True on wiki write, False on any error (non-fatal).
        """
        return await _record_backtest_outcome_to_wiki(
            self._dbstore, self.hypothesis_manager, hypothesis_id, outcome, stats
        )
