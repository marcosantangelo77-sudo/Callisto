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
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.embeddings import VectorStore, embed_text, embed_batch, cosine_similarity
from tools.hypothesis import HypothesisManager

load_dotenv()

logger = logging.getLogger("callisto.hypothesis_generator")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ──────────────────────────────────────────────────
# Wiki-grounded variance-enforced generator constants
# ──────────────────────────────────────────────────
# Candidate sim >= CANDIDATE_DEDUP_SIM  ⇒ drop the weaker of the two
CANDIDATE_DEDUP_SIM: float = 0.85
# Candidate sim >= PRIOR_CORPUS_SIM to any wiki/existing-hyp  ⇒ drop (already covered)
PRIOR_CORPUS_SIM: float = 0.80
# How many wiki articles to prime the LLM with
WIKI_CONTEXT_TOP_K: int = 8
# How many recent rejected hypotheses to show as negative examples
NEGATIVE_EXAMPLES_N: int = 4


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
            "min_edge": [1, 2, 3],
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
            "min_edge": [1, 2, 3],
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
            "min_edge": [1, 2, 3],
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
            "min_edge": [1.5, 3],
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
            "min_edge": [0.5, 1, 1.5],
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
            "min_edge": [0.5, 1, 1.5],
        },
    },
    {
        "id": "golf_course_horse",
        "name": "Course horse {finish_type} mispricing at {tournament}",
        "thesis": (
            "Players with {min_top_finishes}+ top-{finish_rank} finishes at {tournament} "
            "in the last {lookback_years} years have {finish_type} lines set too long. "
            "Course-specific institutional knowledge compounds at venues played annually "
            "(especially Augusta). Fair probability of {finish_type} exceeds book implied "
            "by {min_edge}%+."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "{finish_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["course_history", "recent_form"],
        },
        "variables": {
            "tournament": ["Masters", "US_Open", "Open_Championship", "PGA_Championship"],
            "finish_type": ["tournament_winner", "top_5_finish", "top_10_finish"],
            "finish_rank": [5, 10, 20],
            "min_top_finishes": [2, 3],
            "lookback_years": [5, 10],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "golf_age_discount",
        "name": "Age discount on {finish_type} for veterans at {tournament}",
        "thesis": (
            "Players aged {min_age}+ with strong course history are over-discounted "
            "by books due to age bias. At {tournament}, course knowledge degrades slower "
            "than raw athleticism — especially at Augusta where putting from memory and "
            "shot-shaping matter more than distance. {finish_type} odds are too long."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "{finish_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["player_age", "course_history", "sg_approach"],
        },
        "variables": {
            "tournament": ["Masters", "Open_Championship"],
            "finish_type": ["tournament_winner", "top_5_finish", "top_10_finish", "top_20_finish"],
            "min_age": [40, 43, 45],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "golf_recent_form_lag",
        "name": "Recent winner {finish_type} odds lag at majors",
        "thesis": (
            "Players who won a PGA Tour event within {weeks_since_win} weeks before a major "
            "have {finish_type} odds that don't fully reflect the form spike. Books adjust "
            "slowly for recency — the confidence and momentum carry forward. "
            "Fair probability exceeds book implied by {min_edge}%+."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "{finish_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["recent_win", "sg_total"],
        },
        "variables": {
            "finish_type": ["tournament_winner", "top_5_finish", "top_10_finish"],
            "weeks_since_win": [2, 4, 6, 8],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "golf_sg_approach_mispricing",
        "name": "SG:Approach elite players underpriced at approach-dominant courses",
        "thesis": (
            "Players ranked top-{sg_rank} in Strokes Gained: Approach over the last "
            "{lookback_events} events are underpriced at courses where approach play "
            "is the dominant success factor (Augusta, Pebble Beach, Muirfield Village). "
            "SG:Approach correlates most strongly with major wins — books weight "
            "overall rank too heavily vs. skill-specific fit."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "{finish_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["sg_approach_rank", "course_sg_correlation"],
        },
        "variables": {
            "finish_type": ["tournament_winner", "top_5_finish", "top_10_finish"],
            "sg_rank": [5, 10, 15],
            "lookback_events": [5, 10, 16],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "golf_first_round_leader",
        "name": "First-round leader tendency mispricing",
        "thesis": (
            "Players who have led after Round 1 at a specific venue {min_times}+ times "
            "in the last {lookback_years} years have first-round leader / top-5 R1 odds "
            "set too long. Early-round course comfort is a repeatable skill, not randomness. "
            "Fair probability exceeds book implied by {min_edge}%+."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "first_round_leader",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["r1_history", "course_familiarity"],
        },
        "variables": {
            "min_times": [2, 3],
            "lookback_years": [5, 10],
            "min_edge": [2, 3, 5],
        },
    },
    {
        "id": "golf_weather_round_scoring",
        "name": "Weather impact on tournament round scoring",
        "thesis": (
            "When {weather_condition} conditions are forecast for a tournament round, "
            "books underadjust round scoring props and matchup odds. Players with "
            "experience in adverse conditions gain a relative edge. "
            "Affected markets are mispriced by {min_edge}%+."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "round_score",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["weather_forecast", "player_weather_history"],
        },
        "variables": {
            "weather_condition": ["high wind (15+ mph)", "rain", "cold (<55F)"],
            "min_edge": [1, 2, 3],
        },
    },
    # ── MLB-specific templates ──
    {
        "id": "mlb_pitcher_prop_rest",
        "name": "Starting pitcher {prop_type} on {rest_days}+ days rest",
        "thesis": (
            "Starting pitchers on {rest_days}+ days rest have {prop_type} lines "
            "that don't fully account for the rest advantage. Extended rest improves "
            "velocity retention, spin rate, and command through later innings. "
            "Books set Over strikeout / Under earned run lines too conservatively. "
            "Fair probability of the favorable side exceeds book implied by {min_edge}%+."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["pitcher_rest_days", "pitch_count_recent"],
        },
        "variables": {
            "prop_type": ["strikeouts", "earned_runs", "hits_allowed", "outs_recorded"],
            "rest_days": [5, 6, 7],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "mlb_opening_week_totals",
        "name": "MLB opening week {weather_factor} total mispricing",
        "thesis": (
            "Early-season MLB games (first 2 weeks) in {weather_factor} conditions "
            "see inflated or deflated run totals that books don't fully adjust for. "
            "Pitchers are not fully stretched, bullpens are fresh, cold-weather parks "
            "suppress offense. Under is +EV when {weather_factor} is present."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["season_week", "weather", "park_factor"],
        },
        "variables": {
            "weather_factor": ["cold (<55F)", "wind (15+ mph)", "rain/drizzle"],
            "min_edge": [1, 1.5, 2],
        },
    },
    {
        "id": "mlb_schedule_spot",
        "name": "MLB schedule spot {spot_type} spread value",
        "thesis": (
            "Teams in {spot_type} schedule situations show ATS performance that "
            "diverges from book implied probability. Books underweight travel fatigue, "
            "timezone shifts, and letdown/lookahead dynamics in MLB where the 162-game "
            "schedule creates persistent schedule spot edges. ATS win rate exceeds "
            "implied by {min_edge}%+."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["schedule_spot", "travel_distance", "timezone_shift"],
        },
        "variables": {
            "spot_type": [
                "3+ game road trip finale", "home after 7+ road games",
                "day game after night game", "cross-country travel (3+ timezone shift)",
            ],
            "min_edge": [1, 1.5, 2],
        },
    },
    {
        "id": "mlb_park_factor_totals",
        "name": "MLB park factor mispricing on totals at {park_type} parks",
        "thesis": (
            "Games at {park_type} parks have totals that don't fully reflect "
            "park-specific run environment. Books adjust but lag behind the "
            "true park factor, especially early season when lines are calibrated "
            "to league-wide trends. Fair total probability diverges by {min_edge}%+."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["park_factor", "altitude", "dimensions"],
        },
        "variables": {
            "park_type": ["extreme hitter (Coors, Great American)", "extreme pitcher (Oracle, Petco)", "bandbox (Fenway, Yankee)"],
            "min_edge": [1, 1.5, 2],
        },
    },
    # ── NCAAW/WNBA identity/cohesion templates ──
    {
        "id": "ncaaw_cohesion_spread",
        "name": "NCAAW {cohesion_factor} cohesion spread advantage",
        "thesis": (
            "Teams with strong {cohesion_factor} cohesion outperform their spread "
            "implied probability. Thin NCAAW markets don't price intangible cohesion "
            "factors — regional identity, institutional values, coaching tenure, and "
            "roster stability create systematic edges. ATS win rate exceeds book "
            "implied by {min_edge}%+."
        ),
        "sport_filter": ["basketball_ncaaw"],
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["team_cohesion", "coaching_tenure", "roster_stability"],
        },
        "variables": {
            "cohesion_factor": ["regional identity", "coaching stability (10+ years)", "roster continuity (low transfer portal)", "institutional values alignment"],
            "min_edge": [1.5, 2, 3],
        },
    },
    {
        "id": "wnba_demographic_totals",
        "name": "WNBA {factor} demographic composition total mispricing",
        "thesis": (
            "WNBA teams with {factor} demographic composition have game totals "
            "that diverge from book expectations. Social cohesion drives pace, "
            "defensive intensity, and chemistry in ways that thin WNBA markets "
            "don't price. Fair total probability exceeds implied by {min_edge}%+."
        ),
        "sport_filter": ["basketball_wnba"],
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["demographic_composition", "team_cohesion", "pace"],
        },
        "variables": {
            "factor": ["high regional identity", "strong institutional alignment", "veteran-heavy roster"],
            "min_edge": [1.5, 2, 3],
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
                         "icehockey_nhl", "baseball_mlb", "golf_pga"],
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
            "min_edge": [0.5, 1, 2],
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
        await self._db.execute("PRAGMA busy_timeout = 60000")
        logger.info("Hypothesis generator initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

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

        # Compute temporal metadata
        today = datetime.now(timezone.utc).date()
        if training_cutoff_date:
            try:
                cutoff = datetime.strptime(training_cutoff_date, "%Y-%m-%d").date()
            except ValueError:
                cutoff = today - timedelta(days=30)
        else:
            cutoff = today - timedelta(days=30)

        training_period_start = "2023-01-01"
        training_period_end = str(cutoff)
        forward_test_start = str(cutoff + timedelta(days=1))

        created = []

        for template in HYPOTHESIS_TEMPLATES:
            if sport not in template["sport_filter"]:
                continue

            # Player prop templates now supported — prop_snapshots provides
            # multi-book data and BacktestEngine._process_prop_snapshots handles devig.

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

        result = await escalate_with_ladder(
            prompt=prompt,
            system_context="Callisto hypothesis generation — return structured JSON only.",
            task_type="hypothesis_gen",
            timeout=120,
            hermes_caller="hypothesis_gen",
        )

        if result.get("error"):
            logger.error(f"Hypothesis generation ladder failed: {result['error']}")
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

        # Temporal metadata for Claude-generated hypotheses
        today = datetime.now(timezone.utc).date()
        training_cutoff = today - timedelta(days=30)
        training_period_start = "2023-01-01"
        training_period_end = str(training_cutoff)
        forward_test_start = str(training_cutoff + timedelta(days=1))

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
                from tools.thesis_seeds import pick_unexplored_seeds
                existing_names = await self.hypothesis_manager.get_all_names()
                existing_theses = await self._recent_theses(sport)
                seeds = pick_unexplored_seeds(
                    existing_names, existing_theses, sport=sport, max_seeds=3,
                )
            except Exception as e:
                logger.debug(f"Seed retrieval failed (non-fatal): {e}")
                seeds = []

        # 4. Build the grounding prompt ------------------------------------
        prompt = self._build_grounded_prompt(
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
        if result.get("error"):
            logger.error(f"grounded generator ladder error: {result['error']}")
            return {
                "generated": [], "rejected": [],
                "wiki_context_topics": [a.get("topic") for a in wiki_articles],
                "seeds_used": [s["seed_id"] for s in seeds],
                "model_used": model_used, "diversity_metric": 0.0,
            }

        candidates = self._parse_candidates(content)
        if not candidates:
            logger.warning("grounded generator returned no parseable candidates")
            return {
                "generated": [], "rejected": [],
                "wiki_context_topics": [a.get("topic") for a in wiki_articles],
                "seeds_used": [s["seed_id"] for s in seeds],
                "model_used": model_used, "diversity_metric": 0.0,
            }

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
        kept_indices, drop_reasons = await self._enforce_variance(
            candidates, cand_embs, wiki_articles
        )
        kept_indices = kept_indices[:max_keep]

        # 8. Persist survivors as draft hypotheses ---------------------------
        today = datetime.now(timezone.utc).date()
        training_cutoff = today - timedelta(days=30)
        training_period_start = "2023-01-01"
        training_period_end = str(training_cutoff)
        forward_test_start = str(training_cutoff + timedelta(days=1))

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
        diversity = self._avg_pairwise_distance(kept_embs)

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
        try:
            from tools.knowledge_wiki import KnowledgeWiki
        except Exception as e:
            logger.debug(f"wiki import failed: {e}")
            return []

        query_parts = [sport.replace("_", " ")]
        if focus_market:
            query_parts.append(focus_market.replace("_", " "))
        query_parts.append("betting edge hypothesis")
        query = " ".join(query_parts)

        try:
            kw = KnowledgeWiki(self.db_path)
            # kw.search needs an aiosqlite connection; reuse our own.
            if self._db is None:
                await self.initialize()
            hits = await kw.search(self._db, query, top_k=WIKI_CONTEXT_TOP_K)
            return hits or []
        except Exception as e:
            logger.debug(f"wiki semantic search failed (non-fatal): {e}")
            return []

    # ── helper: rejected-hypothesis retrieval ─────────────────────
    async def _retrieve_rejection_examples(
        self, sport: str, focus_market: Optional[str], limit: int
    ) -> list[dict]:
        """Pull a few recent rejected hypotheses in the same cohort."""
        if self._db is None:
            await self.initialize()
        sql_parts = ["SELECT name, thesis, notes FROM hypotheses WHERE status='rejected'"]
        params: list = []
        if sport:
            sql_parts.append("AND sport = ?")
            params.append(sport)
        if focus_market:
            sql_parts.append("AND market_type = ?")
            params.append(focus_market)
        sql_parts.append("ORDER BY updated_at DESC LIMIT ?")
        params.append(limit)
        try:
            cur = await self._db.execute(" ".join(sql_parts), params)
            rows = await cur.fetchall()
            return [
                {"name": r[0], "thesis": r[1] or "", "notes": r[2] or ""}
                for r in rows
            ]
        except Exception as e:
            logger.debug(f"rejection-example retrieval failed: {e}")
            return []

    async def _recent_theses(self, sport: str, limit: int = 50) -> list[str]:
        if self._db is None:
            await self.initialize()
        try:
            cur = await self._db.execute(
                "SELECT thesis FROM hypotheses WHERE sport = ? ORDER BY created_at DESC LIMIT ?",
                (sport, limit),
            )
            return [r[0] or "" for r in await cur.fetchall()]
        except Exception:
            return []

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
        wiki_block = "\n".join(
            f"- [{a.get('topic')}] {a.get('title')}: "
            f"{(a.get('summary') or '')[:220]}"
            for a in wiki_articles[:WIKI_CONTEXT_TOP_K]
        ) or "(no prior wiki articles)"

        neg_block = "\n".join(
            f"- REJECTED: {r['name']} — {(r['thesis'] or '')[:180]}"
            for r in rejected_examples
        ) or "(no prior rejections for this cohort)"

        seed_block = "\n".join(
            f"- SEED {s['seed_id']} ({s['category']}, {s['market_type']}): "
            f"{s['thesis_template'][:180]}"
            for s in seeds
        ) or "(no seeds supplied)"

        mkt = focus_market or "any market (props/totals/spreads/h2h/live/parlay)"
        return (
            f"Sport: {sport}\nFocus market: {mkt}\n\n"
            f"THINGS THE WIKI ALREADY KNOWS "
            f"(do NOT propose re-discovery of these — propose COMPLEMENTARY "
            f"or ORTHOGONAL theses):\n{wiki_block}\n\n"
            f"RECENT FAILED HYPOTHESES IN THIS COHORT "
            f"(do NOT propose variations of these shape):\n{neg_block}\n\n"
            f"UNDEREXPLORED THESIS SPACES "
            f"(preferred starting points — specialize to a concrete "
            f"testable form using sport-specific names/markets):\n{seed_block}\n\n"
            f"Generate exactly {n_candidates} DISTINCT candidate hypotheses "
            f"as a JSON array. Each item MUST have:\n"
            f"  - name:               short unique slug\n"
            f"  - market:             specific market key\n"
            f"  - cohort_filter:      SQL-expressible WHERE clause over "
            f"game_contexts / player_stats\n"
            f"  - signal_logic:       why the edge exists, mechanism\n"
            f"  - min_signals:        integer ≥ 20\n"
            f"  - ic_prior_estimate:  float in [0.005, 0.08]\n"
            f"  - variance_justification: one sentence — why this is NOT a "
            f"duplicate of any wiki article or rejected hypothesis above\n"
            f"  - thesis_statement:   2-3 sentence backtestable claim\n"
            f"  - edge_threshold:     float (decimal, e.g., 0.02)\n"
            f"  - model_config:       dict (devig_method, target_book, "
            f"consensus_min_books, context_factors list)\n\n"
            f"HARD RULES:\n"
            f"1. Reject vague wording. 'Team plays better when rested' is "
            f"BANNED; say exactly which column, threshold, and side.\n"
            f"2. Every candidate must be DIFFERENT from the others — do not "
            f"vary only one numeric threshold.\n"
            f"3. Prefer specific official/umpire/ref/coach/venue/microstructure "
            f"triggers over blanket team-level claims.\n"
            f"4. Return ONLY the JSON array. No explanation text, no code "
            f"fences outside the JSON."
        )

    # ── helper: tolerant JSON extraction ──────────────────────────
    @staticmethod
    def _parse_candidates(content: str) -> list[dict]:
        if not content:
            return []
        # Strip code fences if present.
        txt = content.strip()
        if txt.startswith("```"):
            txt = txt.strip("`")
            # drop optional "json" language marker
            if txt.lstrip().lower().startswith("json"):
                txt = txt.split("\n", 1)[1] if "\n" in txt else txt
        start = txt.find("[")
        end = txt.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(txt[start:end + 1])
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [x for x in data if isinstance(x, dict)]

    # ── helper: variance enforcement ──────────────────────────────
    async def _enforce_variance(
        self,
        candidates: list[dict],
        cand_embs: list[list[float]],
        wiki_articles: list[dict],
    ) -> tuple[list[int], list[dict]]:
        """Greedy selection that drops:
          (a) near-duplicate candidates (sim >= CANDIDATE_DEDUP_SIM)
          (b) candidates that cluster against a wiki article
              (sim >= PRIOR_CORPUS_SIM)

        Returns (kept_indices, drop_reasons)."""
        if not cand_embs or len(cand_embs) != len(candidates):
            # Embeddings unavailable — trust the LLM and accept all.
            return list(range(len(candidates))), []

        # Load wiki embeddings for articles we have summaries for.
        # We embed summaries once per call (cheap — typically 8 items).
        wiki_texts = [
            (a.get("summary") or a.get("title") or a.get("topic") or "")[:500]
            for a in wiki_articles
        ]
        wiki_texts = [t for t in wiki_texts if t]
        try:
            wiki_embs = await embed_batch(wiki_texts) if wiki_texts else []
        except Exception as e:
            logger.debug(f"wiki embed_batch failed: {e}")
            wiki_embs = []

        kept: list[int] = []
        drop_reasons: list[dict] = []

        # Score candidates by ic_prior as a quality signal.
        def _q(i: int) -> float:
            try:
                return float(candidates[i].get("ic_prior_estimate", 0.0))
            except (TypeError, ValueError):
                return 0.0

        order = sorted(range(len(candidates)), key=_q, reverse=True)

        for i in order:
            emb_i = cand_embs[i]

            # Drop vs already-kept candidates.
            dup = False
            for j in kept:
                sim = cosine_similarity(emb_i, cand_embs[j])
                if sim >= CANDIDATE_DEDUP_SIM:
                    dup = True
                    drop_reasons.append({
                        "reason": f"near_duplicate_of_candidate_{j} (sim={sim:.3f})",
                        "candidate": candidates[i],
                    })
                    break
            if dup:
                continue

            # Drop vs wiki articles already in the corpus.
            prior_hit = False
            for w_emb, w_meta in zip(wiki_embs, wiki_articles):
                sim = cosine_similarity(emb_i, w_emb)
                if sim >= PRIOR_CORPUS_SIM:
                    prior_hit = True
                    drop_reasons.append({
                        "reason": (
                            f"overlaps_wiki_{w_meta.get('topic')} "
                            f"(sim={sim:.3f})"
                        ),
                        "candidate": candidates[i],
                    })
                    break
            if prior_hit:
                continue

            kept.append(i)

        # Sort kept back into original order for stable output.
        kept.sort()
        return kept, drop_reasons

    @staticmethod
    def _avg_pairwise_distance(embs: list[list[float]]) -> float:
        """1 - mean cosine similarity across all pairs (higher = more diverse)."""
        n = len(embs)
        if n < 2:
            return 0.0
        sims: list[float] = []
        for i in range(n):
            for j in range(i + 1, n):
                sims.append(cosine_similarity(embs[i], embs[j]))
        if not sims:
            return 0.0
        return 1.0 - (sum(sims) / len(sims))

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
        try:
            from tools.knowledge_wiki import KnowledgeWiki
        except Exception as e:
            logger.debug(f"wiki import for sharpening failed: {e}")
            return False

        hyp = await self.hypothesis_manager.get_hypothesis(hypothesis_id)
        if not hyp:
            logger.debug(f"sharpening: hypothesis {hypothesis_id} not found")
            return False

        topic = f"backtest_outcome_{hypothesis_id}"
        title = f"Backtest outcome: {hyp['name']} ({outcome})"
        stats_blob = json.dumps(stats or {}, default=str)
        summary = (
            f"Outcome={outcome}. Market={hyp['market_type']}, sport={hyp['sport']}. "
            f"Edge threshold={hyp['edge_threshold']}. "
            f"Thesis: {(hyp.get('thesis') or '')[:240]}"
        )
        content = (
            f"Hypothesis: {hyp['name']}\n"
            f"Thesis: {hyp.get('thesis', '')}\n"
            f"Outcome: {outcome}\n"
            f"Stats: {stats_blob}\n"
            f"Model config: {json.dumps(hyp.get('model_config') or {})[:1500]}\n"
        )

        try:
            kw = KnowledgeWiki(self.db_path)
            if self._db is None:
                await self.initialize()
            await kw.initialize(self._db)
            # Upsert path: use a minimal insert-or-replace so we don't
            # require the LLM-compiler for sharpening signals.
            now_iso = datetime.now(timezone.utc).isoformat()
            await self._db.execute(
                "INSERT OR REPLACE INTO wiki_articles "
                "(topic, title, content, summary, related_topics, "
                "source_sessions, source_entries, domain, confidence, "
                "created_at, updated_at, compile_count, content_hash) "
                "VALUES (?, ?, ?, ?, '[]', '[]', '[]', ?, ?, ?, ?, 1, ?)",
                (
                    topic, title, content, summary, "SIGNAL",
                    0.8 if outcome == "success" else 0.5,
                    now_iso, now_iso,
                    f"hypgen:{hypothesis_id}:{outcome}",
                ),
            )
            await self._db.commit()
            # Embed and stash for retrieval.
            try:
                emb = await embed_text(summary)
                store = VectorStore(self.db_path)
                await store.initialize()
                try:
                    await store.store(
                        "wiki_articles", summary, emb,
                        metadata={"topic": topic, "outcome": outcome,
                                  "hypothesis_id": hypothesis_id},
                    )
                finally:
                    await store.close()
            except Exception as e:
                logger.debug(f"sharpening: embed/store failed: {e}")
            return True
        except Exception as e:
            logger.warning(f"sharpening wiki write failed: {e}")
            return False
