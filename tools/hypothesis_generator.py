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
  This module is now a thin facade over tools/hypgen/:
    templates.py    — HYPOTHESIS_TEMPLATES, constants, expand_variables
    prompts.py      — prompt assembly, candidate parsing, variance enforcement
    seeds.py        — underexplored-seed selection
    persistence.py  — DB lifecycle, retrieval helpers, sharpening wiki write-back
    generation.py   — generation pipelines (templates, clusters, ladder,
                      wiki-grounded variance-enforced) and cluster analysis

  Write-safety contract: neither this facade nor any tools.hypgen module
  issues `signal_generated` or `edge_threshold` UPDATE statements against
  the hypotheses table. Hypothesis creation goes exclusively through
  HypothesisManager.create_hypothesis; the only direct SQL write in the
  package is the documented sharpening-loop INSERT OR REPLACE into
  wiki_articles (see tools/hypgen/persistence.py).
"""

import logging
from typing import Optional

from dotenv import load_dotenv

from tools.embeddings import VectorStore, embed_batch  # noqa: F401
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
from tools.hypgen.generation import (  # noqa: F401
    analyze_cluster as _analyze_cluster_fn,
    generate_from_clusters as _generate_from_clusters_impl,
    generate_from_ladder as _generate_from_ladder_impl,
    generate_from_templates as _generate_from_templates_impl,
    generate_wiki_grounded as _generate_wiki_grounded_impl,
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
        """Generate hypotheses from templates. See tools/hypgen/generation.py."""
        return await _generate_from_templates_impl(
            self, sport, max_hypotheses=max_hypotheses,
            training_cutoff_date=training_cutoff_date,
        )

    async def generate_from_clusters(
        self,
        collection: str = "prop_outcomes",
        similarity_threshold: float = 0.85,
        min_cluster_size: int = 10,
        min_hit_rate_delta: float = 0.05,
        data_period: str | None = None,
    ) -> list[dict]:
        """Discover data-driven hypotheses from embedding clusters.
        See tools/hypgen/generation.py."""
        return await _generate_from_clusters_impl(
            self,
            collection=collection,
            similarity_threshold=similarity_threshold,
            min_cluster_size=min_cluster_size,
            min_hit_rate_delta=min_hit_rate_delta,
            data_period=data_period,
        )

    async def generate_from_claude(
        self,
        sport: str,
        data_summary: str,
    ) -> list[dict]:
        """
        Ask the hypothesis_gen ladder to generate novel hypotheses from a
        data summary. Historical name kept for call-site compatibility;
        see tools/hypgen/generation.py::generate_from_ladder.
        """
        return await _generate_from_ladder_impl(self, sport, data_summary)

    def _expand_variables(self, variables: dict) -> list[dict]:
        """Expand variable dict into list of all combinations."""
        return expand_variables(variables)

    def _analyze_cluster(self, cluster: list[dict]) -> Optional[dict]:
        """
        Analyze a cluster of prop outcomes to find patterns.
        Returns analysis dict with hit_rate, expected_rate, common_features.
        """
        return _analyze_cluster_fn(cluster)

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
        """Wiki-grounded generation pipeline. See tools/hypgen/generation.py."""
        return await _generate_wiki_grounded_impl(
            self,
            sport,
            focus_market=focus_market,
            n_candidates=n_candidates,
            max_keep=max_keep,
            include_seeds=include_seeds,
        )

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
