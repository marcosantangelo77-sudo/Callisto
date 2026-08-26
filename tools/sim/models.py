"""Data classes shared by the simulation engine."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TeamProfile:
    """Team statistical profile for simulation input."""
    name: str
    # Offensive/defensive efficiency (points per 100 possessions for basketball)
    offensive_efficiency: float = 100.0
    defensive_efficiency: float = 100.0
    # Pace (possessions per game)
    pace: float = 70.0
    # Variance factor (higher = more volatile outcomes)
    variance: float = 1.0
    # Home court advantage (points added when home)
    home_advantage: float = 3.0
    # Adjustments
    injuries_impact: float = 0.0  # negative = weakened
    rest_days: int = 1
    back_to_back: bool = False


@dataclass
class SimulationResult:
    """Results from a Monte Carlo simulation."""
    home_team: str
    away_team: str
    iterations: int
    sport: str = "basketball"
    # Score distributions
    home_avg_score: float = 0.0
    away_avg_score: float = 0.0
    home_score_std: float = 0.0
    away_score_std: float = 0.0
    # Spread analysis
    fair_spread: float = 0.0  # Positive = home favored
    spread_distribution: dict = field(default_factory=dict)
    # Total analysis
    fair_total: float = 0.0
    total_distribution: dict = field(default_factory=dict)
    # Win probabilities
    home_win_pct: float = 0.0
    away_win_pct: float = 0.0
    draw_pct: float = 0.0
    # Spread cover probabilities at various lines
    spread_cover_probs: dict = field(default_factory=dict)
    # Over/under probabilities at various totals
    over_probs: dict = field(default_factory=dict)
    # Raw score arrays for downstream analysis
    home_scores: list = field(default_factory=list)
    away_scores: list = field(default_factory=list)
    # Exact score probabilities (mainly useful for low-scoring)
    exact_score_probs: dict = field(default_factory=dict)


@dataclass
class PropSimResult:
    """Results from a player prop simulation."""
    player: str
    stat: str
    iterations: int
    mean: float = 0.0
    median: float = 0.0
    std: float = 0.0
    values: list = field(default_factory=list)
    percentiles: dict = field(default_factory=dict)
    over_probs: dict = field(default_factory=dict)  # line -> P(over)


@dataclass
class EdgeResult:
    """Comparison of simulated probability vs book line."""
    simulated_prob: float
    book_prob: float
    edge: float
    edge_pct: float
    confidence_interval: tuple  # (lower, upper) 95% CI
    kelly_fraction: float
    kelly_half: float  # Half-Kelly for conservative sizing
    ev_per_100: float
    is_positive_ev: bool
    rating: str  # "STRONG", "MODERATE", "THIN", "NO_EDGE"
    confidence: Optional[object] = None  # EdgeConfidence from score_edge
