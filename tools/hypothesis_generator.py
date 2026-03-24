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
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.embeddings import VectorStore, embed_text, cosine_similarity
from tools.hypothesis import HypothesisManager

load_dotenv()

logger = logging.getLogger("callisto.hypothesis_generator")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


# ──────────────────────────────────────────────────
# HYPOTHESIS TEMPLATES
# ──────────────────────────────────────────────────
# Each template defines a class of edge to test.
# The generator fills in sport-specific and context-specific parameters.

HYPOTHESIS_TEMPLATES = [
    {
        "id": "rest_advantage_props",
        "name": "Rest advantage {prop_type} mispricing",
        "thesis": (
            "Players on {rest_days}+ days rest have {prop_type} lines set too low "
            "by books that don't fully account for rest effects on {stat_category}. "
            "Fair probability of Over is higher than book implied by {min_edge}%+."
        ),
        "sport_filter": ["basketball_nba", "basketball_ncaab"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["rest_days"],
        },
        "variables": {
            "rest_days": [2, 3, 4],
            "prop_type": ["points", "rebounds", "assists", "threes"],
            "stat_category": ["scoring", "rebounding", "passing", "three-point shooting"],
            "min_edge": [2, 3],
        },
    },
    {
        "id": "back_to_back_unders",
        "name": "Back-to-back {prop_type} unders",
        "thesis": (
            "Players on the second night of a back-to-back have reduced {stat_category} "
            "output. Books adjust lines but not enough — Under is +EV at {min_edge}%+."
        ),
        "sport_filter": ["basketball_nba"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["back_to_back"],
            "side_filter": "Under",
        },
        "variables": {
            "prop_type": ["points", "rebounds", "assists", "points_rebounds_assists"],
            "stat_category": ["scoring", "rebounding", "passing", "combined stats"],
            "min_edge": [2, 3],
        },
    },
    {
        "id": "pace_mismatch_overs",
        "name": "Pace mismatch {prop_type} overs",
        "thesis": (
            "When a slow-pace team faces a fast-pace team, books underestimate the "
            "pace-up effect on player {stat_category}. {prop_type} Overs are +EV "
            "when pace differential exceeds {pace_diff} possessions."
        ),
        "sport_filter": ["basketball_nba"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["pace_differential"],
            "side_filter": "Over",
        },
        "variables": {
            "prop_type": ["points", "assists", "points_rebounds_assists"],
            "stat_category": ["scoring", "passing", "combined stats"],
            "pace_diff": [4, 6, 8],
            "min_edge": [2, 3],
        },
    },
    {
        "id": "injury_role_boost",
        "name": "Teammate injury {prop_type} boost",
        "thesis": (
            "When a team's top {role} is injured, the backup/next-man-up sees increased "
            "{stat_category}. Books are slow to adjust {prop_type} lines upward, "
            "creating Over edges of {min_edge}%+."
        ),
        "sport_filter": ["basketball_nba", "basketball_ncaab"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["teammate_injury"],
            "side_filter": "Over",
        },
        "variables": {
            "prop_type": ["points", "rebounds", "assists", "threes"],
            "stat_category": ["scoring", "rebounding", "passing", "three-point shooting"],
            "role": ["scorer", "rebounder", "playmaker"],
            "min_edge": [3, 5],
        },
    },
    {
        "id": "home_underdog_spread",
        "name": "Home underdog spread value",
        "thesis": (
            "Home underdogs of {spread_range} points receive insufficient home-court "
            "adjustment from books. ATS win rate exceeds implied probability by {min_edge}%+."
        ),
        "sport_filter": ["basketball_nba", "basketball_ncaab"],
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["home_underdog"],
        },
        "variables": {
            "spread_range": ["1-4", "4-7", "7-10"],
            "min_edge": [2, 3],
        },
    },
    {
        "id": "total_weather_impact",
        "name": "Weather impact on {sport} totals",
        "thesis": (
            "Games played in {weather_condition} conditions see reduced scoring. "
            "Books don't fully adjust totals for weather — Under is +EV when "
            "{weather_metric} exceeds {threshold}."
        ),
        "sport_filter": ["americanfootball_nfl", "baseball_mlb"],
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["weather"],
            "side_filter": "Under",
        },
        "variables": {
            "sport": ["NFL", "MLB"],
            "weather_condition": ["high wind", "heavy rain", "extreme cold"],
            "weather_metric": ["wind_mph", "precipitation_mm", "temp_f"],
            "threshold": [15, 5, 32],
            "min_edge": [2, 3],
        },
    },
    {
        "id": "consensus_divergence",
        "name": "Cross-book consensus divergence on {market_type}",
        "thesis": (
            "When the devigged consensus fair probability from {min_books}+ books "
            "diverges from the target book's implied by {min_edge}%+, the consensus "
            "is correct more often than the target book. This is the core model."
        ),
        "sport_filter": ["basketball_nba", "basketball_ncaab", "americanfootball_nfl",
                         "icehockey_nhl", "baseball_mlb"],
        "market_type": "{market_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": "{min_books}",
        },
        "variables": {
            "market_type": ["spreads", "totals", "h2h",
                           "player_points", "player_rebounds", "player_assists"],
            "min_books": [3, 4, 5],
            "min_edge": [2, 3, 5],
        },
    },
]


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
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        logger.info("Hypothesis generator initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def generate_from_templates(
        self,
        sport: str,
        max_hypotheses: int = 50,
    ) -> list[dict]:
        """
        Generate hypotheses from templates for a given sport.
        Expands variable combinations and creates draft hypotheses.
        Skips combinations that already exist.

        Returns list of created hypothesis summaries.
        """
        existing = await self.hypothesis_manager.list_hypotheses()
        existing_names = {h["name"] for h in existing}

        created = []

        for template in HYPOTHESIS_TEMPLATES:
            if sport not in template["sport_filter"]:
                continue

            # Generate all variable combinations
            combos = self._expand_variables(template["variables"])

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

                # Build model config
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

                try:
                    hid = await self.hypothesis_manager.create_hypothesis(
                        name=name,
                        thesis=thesis,
                        sport=sport,
                        market_type=market_type,
                        model_config=model_config,
                        edge_threshold=edge_threshold,
                        notes=f"Auto-generated from template '{template['id']}'",
                    )
                    created.append({
                        "hypothesis_id": hid,
                        "name": name,
                        "template": template["id"],
                        "variables": combo,
                    })
                    existing_names.add(name)
                except Exception as e:
                    logger.warning(f"Failed to create hypothesis '{name}': {e}")

        logger.info(
            f"Generated {len(created)} hypotheses for {sport} "
            f"from {len(HYPOTHESIS_TEMPLATES)} templates"
        )
        return created

    async def generate_from_clusters(
        self,
        collection: str = "prop_outcomes",
        similarity_threshold: float = 0.85,
        min_cluster_size: int = 10,
        min_hit_rate_delta: float = 0.05,
    ) -> list[dict]:
        """
        Analyze embedding clusters to discover data-driven hypotheses.

        For each cluster of similar prop outcomes:
          1. Check if the cluster has a statistically interesting hit rate
          2. If hit rate diverges from expected, generate a hypothesis
          3. Extract common features from the cluster as context factors

        Returns list of created hypothesis summaries.
        """
        clusters = await self.vector_store.cluster_by_similarity(
            collection, threshold=similarity_threshold
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
                    },
                    edge_threshold=abs(delta),
                    notes=f"Auto-discovered from {collection} cluster (N={len(cluster)})",
                )
                created.append({
                    "hypothesis_id": hid,
                    "name": name,
                    "cluster_size": len(cluster),
                    "hit_rate": round(hit_rate, 4),
                    "expected_rate": round(expected_rate, 4),
                    "delta": round(delta, 4),
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
        Ask Claude Code to generate novel hypotheses from a data summary.
        Returns structured hypotheses for creation.

        This is the escalation path: local models do pattern detection,
        Claude does creative hypothesis formulation.
        """
        from tools.claude_code import claude_code_query

        prompt = (
            f"You are Callisto's hypothesis engine. Given the following data summary "
            f"for {sport}, generate 3-5 novel, testable betting hypotheses.\n\n"
            f"DATA SUMMARY:\n{data_summary}\n\n"
            f"For each hypothesis, return JSON with:\n"
            f"- name: short descriptive name\n"
            f"- thesis: detailed testable claim\n"
            f"- market_type: one of (spreads, totals, h2h, player_points, "
            f"player_rebounds, player_assists, player_threes, "
            f"player_points_rebounds_assists)\n"
            f"- edge_threshold: minimum edge to flag (decimal, e.g., 0.03)\n"
            f"- model_config: dict with devig_method, target_book, "
            f"consensus_min_books, and any context_factors\n\n"
            f"Return ONLY a JSON array. No explanation text."
        )

        result = await claude_code_query(
            prompt=prompt,
            system_context="Callisto hypothesis generation — return structured JSON only.",
            timeout=120,
        )

        if result.get("error"):
            logger.error(f"Claude Code hypothesis generation failed: {result['error']}")
            return []

        # Parse response
        content = result.get("content", "")
        try:
            # Try to extract JSON from response
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                hypotheses_raw = json.loads(content[start:end])
            else:
                logger.warning("Could not find JSON array in Claude response")
                return []
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse Claude hypotheses: {e}")
            return []

        created = []
        for h_raw in hypotheses_raw:
            try:
                hid = await self.hypothesis_manager.create_hypothesis(
                    name=h_raw.get("name", "Unnamed"),
                    thesis=h_raw.get("thesis", ""),
                    sport=sport,
                    market_type=h_raw.get("market_type", "spreads"),
                    model_config=h_raw.get("model_config", {
                        "type": "consensus_devig",
                        "devig_method": "power",
                        "target_book": "draftkings",
                        "consensus_min_books": 3,
                    }),
                    edge_threshold=float(h_raw.get("edge_threshold", 0.02)),
                    notes="Auto-generated by Claude Code hypothesis engine",
                )
                created.append({
                    "hypothesis_id": hid,
                    "name": h_raw.get("name"),
                    "source": "claude_code",
                })
            except Exception as e:
                logger.warning(f"Failed to create Claude hypothesis: {e}")

        logger.info(f"Claude Code generated {len(created)} hypotheses for {sport}")
        return created

    def _expand_variables(self, variables: dict) -> list[dict]:
        """Expand variable dict into list of all combinations."""
        if not variables:
            return [{}]

        keys = list(variables.keys())
        values = list(variables.values())

        combos = [{}]
        for key, vals in zip(keys, values):
            new_combos = []
            for combo in combos:
                if isinstance(vals, list):
                    for v in vals:
                        new_combo = combo.copy()
                        new_combo[key] = v
                        new_combos.append(new_combo)
                else:
                    combo[key] = vals
                    new_combos.append(combo)
            combos = new_combos

        return combos

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
