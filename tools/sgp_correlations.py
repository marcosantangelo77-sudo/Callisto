"""
Per-sport SGP leg-pair correlation table.

This module is the *configuration surface* for the SGP scanner. It wraps the
deeper correlation engines that already live in the repo (``tools.correlation``
and ``tools.sgp``) and adds two things on top:

  1. A small, explicit table of "canonical SGP leg-pair" correlations, keyed by
     tuples of abstract leg archetypes (e.g. ``("qb_pass_yds_over",
     "wr_rec_yds_over")``). These are the archetypes a caller passes the scanner
     — the scanner does NOT need to know about the ~80 raw Pearson priors in
     tools/correlation.py, it just needs a small menu of "named SGP legs".

  2. Two optional YAML overrides loaded lazily from the ``config/`` dir:
        - ``config/sgp_correlations.yaml`` — hand-authored overrides
        - ``config/sgp_correlations_empirical.yaml`` — emitted by the calibration
          script from real Callisto history
     The empirical file wins when both are present, because the whole point of
     the calibration step is to replace the priors with measured values.

The scanner and tests import ``get_correlation(sport, leg_a, leg_b)``. That
function returns a single float in ``[-1, 1]`` — it never raises on an unknown
pair, it just returns 0.0 (independence) and logs once per unknown pair. This
keeps the scanner robust: if a leg archetype isn't in the table we assume the
book's independent price is fair and we don't flag any edge.

The public API is intentionally small:

    seed_from_defaults() -> dict
        Load the hardcoded defaults. Pure, no I/O.

    load(config_dir: Path | None = None) -> CorrelationTable
        Merge defaults with YAML overrides. Cached per-process.

    get_correlation(sport, leg_a, leg_b) -> float
        Thin wrapper over the cached table.

    list_pairs(sport) -> list[(leg_a, leg_b, rho)]
        For debugging / calibration output.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("callisto.sgp_correlations")


# ---------------------------------------------------------------------------
# Canonical leg archetype names
# ---------------------------------------------------------------------------
# The scanner reasons about these abstract "legs", not raw Pearson market
# names. This keeps the YAML file small and readable and decouples the SGP
# catalog from the ~80 Pearson priors in tools/correlation.py.
#
# Format: <actor>_<stat>_<side>
#   actor ∈ {team, opp, game, qb, wr, rb, te, pitcher, batter, skater, goalie,
#            player}
#   stat  ∈ a short lowercase token (pass_yds, pts, rec_yds, ks, ...)
#   side  ∈ {over, under, win, cover}
#
# These are the LEGS that the scanner enumerates; the correlation table says
# how any pair of legs within the same game correlates.
# ---------------------------------------------------------------------------

# Defaults seeded from:
#   - PFF / nflfastR public research (NFL)
#   - Cleaning the Glass / pbpstats (NBA)
#   - Baseball Savant (MLB)
#   - Natural Stat Trick / MoneyPuck (NHL)
#   - Callisto's own tools/correlation.py and tools/sgp.py priors
#
# All values are mid-estimates. The scanner is conservative by default —
# callers can pass their own overrides or the empirical YAML file.

_DEFAULTS: dict[str, dict[tuple[str, str], float]] = {
    "nfl": {
        # Same-team QB/receiver stack — the classic SGP correlation
        ("qb_pass_yds_over", "wr_rec_yds_over"): 0.47,
        ("qb_pass_yds_over", "te_rec_yds_over"): 0.38,
        ("qb_pass_tds_over", "wr_rec_tds_over"): 0.42,
        ("qb_pass_yds_over", "qb_pass_tds_over"): 0.60,
        # Team-scoring stacks
        ("team_ml_win", "team_total_over"): 0.55,
        ("qb_pass_yds_over", "team_total_over"): 0.50,
        ("wr_rec_yds_over", "team_total_over"): 0.45,
        ("rb_rush_yds_over", "team_ml_win"): 0.35,
        ("rb_rush_yds_over", "team_total_over"): 0.30,
        # Game script anti-correlations
        ("qb_pass_yds_over", "rb_rush_yds_over"): -0.15,
        ("team_total_over", "opp_total_over"): -0.10,
        # Game-level
        ("team_total_over", "game_total_over"): 0.65,
        ("team_ml_win", "team_spread_cover"): 0.70,
    },
    "nba": {
        # Player-scoring to team/game totals
        ("player_pts_over", "team_total_over"): 0.50,
        ("player_pts_over", "game_total_over"): 0.35,
        ("player_ast_over", "game_total_over"): 0.40,
        ("player_reb_over", "game_pace_over"): 0.35,
        ("player_threes_over", "player_pts_over"): 0.55,
        # Same-team teammate stacks — small positive (team scoring env)
        ("player_pts_over", "teammate_pts_over"): 0.15,
        # Team-scoring + game script
        ("team_ml_win", "team_total_over"): 0.40,
        ("team_spread_cover", "team_total_over"): 0.35,
        ("team_total_over", "opp_total_over"): 0.30,  # pace-correlated
        ("team_total_over", "game_total_over"): 0.70,
        # Blowout anti — stars sit
        ("player_pts_over", "team_blowout_cover"): -0.15,
    },
    "mlb": {
        # Batter + team-total
        ("batter_hits_over", "team_total_over"): 0.30,
        ("batter_tb_over", "team_total_over"): 0.40,
        ("batter_rbi_over", "team_total_over"): 0.55,
        ("batter_hr_over", "team_total_over"): 0.45,
        # Pitcher Ks + opposing-team under (Ks reduce opponent offense)
        ("pitcher_ks_over", "opp_total_under"): 0.30,
        ("pitcher_ks_over", "game_total_under"): 0.22,
        ("pitcher_er_over", "opp_total_over"): 0.55,  # reversed perspective
        # Team ML + team total
        ("team_ml_win", "team_total_over"): 0.40,
        # Anti-correlations
        ("pitcher_ks_over", "batter_hits_over"): -0.20,
    },
    "nhl": {
        ("player_goals_over", "team_total_over"): 0.45,
        ("player_shots_over", "player_goals_over"): 0.40,
        ("team_ml_win", "team_total_over"): 0.35,
        ("goalie_saves_over", "opp_shots_over"): 0.80,
        ("goalie_saves_over", "team_total_over"): -0.15,
    },
    # Women's basketball / soccer — sparse defaults, fall back to 0.0
    "wnba": {
        ("player_pts_over", "team_total_over"): 0.50,
        ("team_ml_win", "team_total_over"): 0.40,
    },
    "soccer": {
        ("team_ml_win", "team_goals_over"): 0.55,
        ("both_teams_score", "game_total_over"): 0.45,
    },
}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

@dataclass
class CorrelationTable:
    """In-memory correlation table with provenance tracking.

    Each pair tracks where its value came from so downstream code (and the CLI)
    can show "seeded default" vs "yaml override" vs "empirical". That matters
    for confidence: a value we calibrated from 500 historical games is much
    more trustworthy than a seeded research estimate.
    """

    by_sport: dict[str, dict[tuple[str, str], float]] = field(default_factory=dict)
    provenance: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def get(self, sport: str, leg_a: str, leg_b: str) -> float:
        sport_key = sport.strip().lower()
        mat = self.by_sport.get(sport_key)
        if not mat:
            return 0.0
        rho = mat.get((leg_a, leg_b))
        if rho is None:
            rho = mat.get((leg_b, leg_a))
        return float(rho) if rho is not None else 0.0

    def source(self, sport: str, leg_a: str, leg_b: str) -> str:
        sport_key = sport.strip().lower()
        return (
            self.provenance.get((sport_key, leg_a, leg_b))
            or self.provenance.get((sport_key, leg_b, leg_a))
            or "missing"
        )

    def set(
        self,
        sport: str,
        leg_a: str,
        leg_b: str,
        rho: float,
        source: str,
    ) -> None:
        sport_key = sport.strip().lower()
        mat = self.by_sport.setdefault(sport_key, {})
        mat[(leg_a, leg_b)] = float(rho)
        self.provenance[(sport_key, leg_a, leg_b)] = source

    def list_pairs(self, sport: str) -> list[tuple[str, str, float, str]]:
        sport_key = sport.strip().lower()
        mat = self.by_sport.get(sport_key, {})
        out = []
        for (a, b), rho in mat.items():
            out.append((a, b, float(rho), self.source(sport_key, a, b)))
        out.sort(key=lambda r: abs(r[2]), reverse=True)
        return out


def seed_from_defaults() -> CorrelationTable:
    """Build a table from the hardcoded defaults. Pure, no I/O."""
    tbl = CorrelationTable()
    for sport, pairs in _DEFAULTS.items():
        for (a, b), rho in pairs.items():
            tbl.set(sport, a, b, rho, "seeded_default")
    return tbl


def _default_config_dir() -> Path:
    """Resolve config/ next to the tools/ package (repo-root/config)."""
    here = Path(__file__).resolve().parent
    # tools/ -> repo root
    return here.parent / "config"


def _load_yaml(path: Path) -> Optional[dict]:
    """Load a YAML file if it exists. Falls back to a tiny hand-rolled parser
    when PyYAML isn't installed — good enough for our flat
    ``{sport: {leg_a|leg_b: rho}}`` shape."""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else None
    except ImportError:
        logger.info("PyYAML not available, using minimal parser for %s", path)
        return _parse_flat_yaml(text)


def _parse_flat_yaml(text: str) -> dict:
    """Minimal YAML parser for our flat schema.

    Supported shape (2-space indent, no anchors / lists):
        nfl:
          qb_pass_yds_over|wr_rec_yds_over: 0.47
          team_ml_win|team_total_over: 0.55
        nba:
          ...

    This is a deliberately dumb parser — we don't need round-tripping, just a
    graceful fallback so the module stays importable without PyYAML.
    """
    out: dict[str, dict[str, float]] = {}
    current_sport: Optional[str] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            # Top-level key
            stripped = line.rstrip(":").strip()
            if stripped:
                current_sport = stripped
                out.setdefault(current_sport, {})
            continue
        if current_sport is None:
            continue
        # Indented "pair: rho"
        body = line.strip()
        if ":" not in body:
            continue
        k, v = body.split(":", 1)
        try:
            out[current_sport][k.strip()] = float(v.strip())
        except ValueError:
            continue
    return out


def _merge_yaml(tbl: CorrelationTable, data: dict, source_label: str) -> int:
    """Merge a YAML-loaded dict into a CorrelationTable. Returns count merged."""
    merged = 0
    if not isinstance(data, dict):
        return 0
    for sport, pairs in data.items():
        if not isinstance(pairs, dict):
            continue
        for key, rho in pairs.items():
            if isinstance(key, (list, tuple)) and len(key) == 2:
                a, b = key
            elif isinstance(key, str) and "|" in key:
                a, b = [p.strip() for p in key.split("|", 1)]
            else:
                continue
            try:
                r = float(rho)
            except (TypeError, ValueError):
                continue
            r = max(-1.0, min(1.0, r))
            tbl.set(sport, a, b, r, source_label)
            merged += 1
    return merged


# Module-level cache. The scanner calls get_correlation() a LOT; we don't want
# to re-read YAML on every call. Thread-safe lazy init.
_table_lock = threading.Lock()
_table: Optional[CorrelationTable] = None


def load(config_dir: Optional[Path] = None, *, force_reload: bool = False) -> CorrelationTable:
    """Return the cached CorrelationTable, loading YAML overrides on first call.

    Order of precedence (later wins):
        1. _DEFAULTS (always)
        2. config/sgp_correlations.yaml (hand-authored overrides)
        3. config/sgp_correlations_empirical.yaml (calibrated from history)
    """
    global _table
    with _table_lock:
        if _table is not None and not force_reload:
            return _table

        tbl = seed_from_defaults()
        cfg_dir = config_dir or Path(os.getenv("CALLISTO_CONFIG_DIR", "")) or _default_config_dir()
        if isinstance(cfg_dir, str):
            cfg_dir = Path(cfg_dir)

        try:
            manual = _load_yaml(cfg_dir / "sgp_correlations.yaml")
            if manual:
                n = _merge_yaml(tbl, manual, "yaml_override")
                logger.info("Loaded %d SGP correlation overrides from yaml_override", n)
        except Exception as e:
            logger.warning("Could not load sgp_correlations.yaml: %s", e)

        try:
            empirical = _load_yaml(cfg_dir / "sgp_correlations_empirical.yaml")
            if empirical:
                n = _merge_yaml(tbl, empirical, "empirical")
                logger.info("Loaded %d SGP correlation values from empirical calibration", n)
        except Exception as e:
            logger.warning("Could not load sgp_correlations_empirical.yaml: %s", e)

        _table = tbl
        return tbl


def get_correlation(sport: str, leg_a: str, leg_b: str) -> float:
    """Return the correlation between two canonical SGP leg archetypes.

    Returns 0.0 (independence) when the pair is not in the table. Never raises.
    """
    return load().get(sport, leg_a, leg_b)


def get_source(sport: str, leg_a: str, leg_b: str) -> str:
    """Return the provenance tag for a pair (for debugging / CLI output)."""
    return load().source(sport, leg_a, leg_b)


def list_pairs(sport: str) -> list[tuple[str, str, float, str]]:
    """Return ``[(leg_a, leg_b, rho, source)]`` sorted by |rho| desc."""
    return load().list_pairs(sport)


def reset_cache() -> None:
    """Invalidate the module cache — useful in tests that mutate YAML."""
    global _table
    with _table_lock:
        _table = None
