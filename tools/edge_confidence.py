"""
AGP-compliant confidence scoring for detected betting edges.

Maps quantitative edge evidence to the AGP SourceClass/ConfidenceTier framework.
Every edge gets a confidence score that is CAPPED by the quality of its evidence:

    PRIMARY   (sharp book present)    → max 1.0  (VERIFIED)
    SECONDARY (soft book cross-ref)   → max 0.75 (CORROBORATED)
    SIGNAL    (single source)         → max 0.55 (PROBABLE)
    INFERRED  (no live data)          → max 0.55 (PROBABLE)

Within each ceiling, the score is determined by evidence strength:
    - Book count and agreement
    - Edge magnitude vs noise threshold
    - Market type efficiency
    - Cross-method consistency
"""

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("callisto.edge_confidence")

_CALIBRATOR_PATH_ENV = "CALLISTO_EDGE_CALIBRATOR_PATH"
_DEFAULT_CALIBRATOR_PATH = "memory/edge_calibrator.json"
_CALIBRATOR_CACHE: Optional["_CalibratorBase"] = None
_CALIBRATOR_CACHE_MTIME: Optional[float] = None
_CALIBRATOR_LOAD_FAILED: bool = False

# AGP confidence ceilings (must match orchestrator.py)
CEILING_PRIMARY = 1.0
CEILING_SECONDARY = 0.75
CEILING_SIGNAL = 0.55
CEILING_INFERRED = 0.55

# Sharp book identifiers — matches both API keys and display titles (lowercase)
SHARP_BOOKS = {
    "pinnacle", "lowvig", "lowvig.ag", "lowvig.ag",
    "circa", "bookmaker.eu", "betonline", "betonline.ag",
    "betonlineag", "betcris",
}

# Edge must exceed this to be considered signal vs noise
NOISE_FLOOR_PCT = 0.5  # 0.5%

# Market efficiency tiers — less efficient markets can sustain edges longer
MARKET_EFFICIENCY = {
    "h2h": 0.95,           # Moneylines are highly efficient
    "spreads": 0.90,       # Spreads slightly less
    "totals": 0.85,        # Totals less efficient
    "player_points": 0.70, # Player props are where edges live
    "player_rebounds": 0.65,
    "player_assists": 0.65,
    "player_threes": 0.60,
    "player_points_rebounds_assists": 0.60,
    "alternate_spreads": 0.55,
    "alternate_totals": 0.55,
}


@dataclass
class EdgeConfidence:
    """AGP-scored confidence for a detected edge."""
    score: float
    tier: str           # VERIFIED, CORROBORATED, PROBABLE, SPECULATIVE, UNVERIFIED
    source_class: str   # PRIMARY, SECONDARY, SIGNAL, INFERRED
    ceiling: float
    factors: dict       # Breakdown of what contributed to the score
    reasoning: str      # Human-readable explanation
    raw_prob: Optional[float] = None
    calibrated_prob: Optional[float] = None
    calibrator_name: Optional[str] = None


class _CalibratorBase:
    kind = "base"

    def predict(self, p):
        raise NotImplementedError

    def to_dict(self) -> dict:
        raise NotImplementedError

    @staticmethod
    def from_dict(d: dict) -> "_CalibratorBase":
        kind = d.get("kind")
        if kind == "platt":
            return PlattCalibrator.from_dict(d)
        if kind == "isotonic":
            return IsotonicCalibrator.from_dict(d)
        if kind == "identity":
            return IdentityCalibrator()
        raise ValueError(f"Unknown calibrator kind: {kind}")


class IdentityCalibrator(_CalibratorBase):
    kind = "identity"

    def predict(self, p):
        try:
            import numpy as _np
            arr = _np.asarray(p, dtype=float)
            return _np.clip(arr, 0.0, 1.0)
        except Exception:
            if p is None:
                return None
            return max(0.0, min(1.0, float(p)))

    def to_dict(self) -> dict:
        return {"kind": "identity"}

    @staticmethod
    def from_dict(d: dict) -> "IdentityCalibrator":
        return IdentityCalibrator()


class PlattCalibrator(_CalibratorBase):
    """Logistic regression on a single input feature (logit of raw prob).

    Transforms raw probability p -> sigmoid(a * logit(p) + b). Fitted by
    minimizing negative log-likelihood via gradient descent (pure numpy —
    no sklearn). Numerically stable with logit clipping on the boundary.
    """
    kind = "platt"

    def __init__(self, a: float = 1.0, b: float = 0.0, n_train: int = 0):
        self.a = float(a)
        self.b = float(b)
        self.n_train = int(n_train)

    def predict(self, p):
        import numpy as _np
        arr = _np.asarray(p, dtype=float)
        eps = 1e-6
        clipped = _np.clip(arr, eps, 1.0 - eps)
        logit = _np.log(clipped / (1.0 - clipped))
        z = self.a * logit + self.b
        out = 1.0 / (1.0 + _np.exp(-z))
        if arr.ndim == 0:
            return float(out)
        return out

    @classmethod
    def fit(cls, probs, outcomes, *, lr: float = 0.1, max_iter: int = 2000, tol: float = 1e-7) -> "PlattCalibrator":
        import numpy as _np
        p = _np.asarray(probs, dtype=float)
        y = _np.asarray(outcomes, dtype=float)
        if p.shape != y.shape:
            raise ValueError(f"probs and outcomes shape mismatch: {p.shape} vs {y.shape}")
        if len(p) < 4:
            return cls(a=1.0, b=0.0, n_train=int(len(p)))
        eps = 1e-6
        p_clipped = _np.clip(p, eps, 1.0 - eps)
        x = _np.log(p_clipped / (1.0 - p_clipped))
        n_pos = float(y.sum())
        n_neg = float(len(y) - n_pos)
        if n_pos == 0 or n_neg == 0:
            return cls(a=1.0, b=0.0, n_train=int(len(p)))
        y_smooth = _np.where(y > 0.5, (n_pos + 1.0) / (n_pos + 2.0), 1.0 / (n_neg + 2.0))
        a, b = 1.0, 0.0
        prev_loss = float("inf")
        for _ in range(max_iter):
            z = a * x + b
            z = _np.clip(z, -30.0, 30.0)
            pred = 1.0 / (1.0 + _np.exp(-z))
            diff = pred - y_smooth
            grad_a = float(_np.mean(diff * x))
            grad_b = float(_np.mean(diff))
            a -= lr * grad_a
            b -= lr * grad_b
            loss = float(-_np.mean(y_smooth * _np.log(pred + 1e-12) + (1.0 - y_smooth) * _np.log(1.0 - pred + 1e-12)))
            if abs(prev_loss - loss) < tol:
                break
            prev_loss = loss
        return cls(a=a, b=b, n_train=int(len(p)))

    def to_dict(self) -> dict:
        return {"kind": "platt", "a": self.a, "b": self.b, "n_train": self.n_train}

    @staticmethod
    def from_dict(d: dict) -> "PlattCalibrator":
        return PlattCalibrator(a=d.get("a", 1.0), b=d.get("b", 0.0), n_train=d.get("n_train", 0))


class IsotonicCalibrator(_CalibratorBase):
    """Monotonic step-function calibration via Pool Adjacent Violators."""
    kind = "isotonic"

    def __init__(self, x_thresholds=None, y_values=None, n_train: int = 0):
        import numpy as _np
        self.x = _np.asarray(x_thresholds if x_thresholds is not None else [], dtype=float)
        self.y = _np.asarray(y_values if y_values is not None else [], dtype=float)
        self.n_train = int(n_train)

    def predict(self, p):
        import numpy as _np
        arr = _np.asarray(p, dtype=float)
        if len(self.x) == 0:
            out = _np.clip(arr, 0.0, 1.0)
            return float(out) if arr.ndim == 0 else out
        out = _np.interp(arr, self.x, self.y, left=float(self.y[0]), right=float(self.y[-1]))
        out = _np.clip(out, 0.0, 1.0)
        return float(out) if arr.ndim == 0 else out

    @classmethod
    def fit(cls, probs, outcomes) -> "IsotonicCalibrator":
        import numpy as _np
        p = _np.asarray(probs, dtype=float)
        y = _np.asarray(outcomes, dtype=float)
        if p.shape != y.shape:
            raise ValueError("probs and outcomes shape mismatch")
        if len(p) < 2:
            return cls(x_thresholds=p.tolist(), y_values=y.tolist(), n_train=int(len(p)))
        order = _np.argsort(p, kind="mergesort")
        p_sorted = p[order]
        y_sorted = y[order]
        values = y_sorted.astype(float).copy()
        weights = _np.ones_like(values, dtype=float)
        i = 0
        while i < len(values) - 1:
            if values[i] > values[i + 1]:
                total_w = weights[i] + weights[i + 1]
                merged = (values[i] * weights[i] + values[i + 1] * weights[i + 1]) / total_w
                values[i] = merged
                weights[i] = total_w
                values = _np.delete(values, i + 1)
                weights = _np.delete(weights, i + 1)
                p_sorted = _np.delete(p_sorted, i + 1)
                if i > 0:
                    i -= 1
            else:
                i += 1
        x_thresholds = p_sorted
        y_values = _np.clip(values, 0.0, 1.0)
        return cls(x_thresholds=x_thresholds.tolist(), y_values=y_values.tolist(), n_train=int(len(p)))

    def to_dict(self) -> dict:
        return {
            "kind": "isotonic",
            "x_thresholds": [float(v) for v in self.x.tolist()],
            "y_values": [float(v) for v in self.y.tolist()],
            "n_train": self.n_train,
        }

    @staticmethod
    def from_dict(d: dict) -> "IsotonicCalibrator":
        return IsotonicCalibrator(
            x_thresholds=d.get("x_thresholds", []),
            y_values=d.get("y_values", []),
            n_train=d.get("n_train", 0),
        )


def _calibrator_path() -> str:
    return os.environ.get(_CALIBRATOR_PATH_ENV, _DEFAULT_CALIBRATOR_PATH)


def save_calibrator(calibrator: _CalibratorBase, path: Optional[str] = None, *, metadata: Optional[dict] = None) -> str:
    path = path or _calibrator_path()
    payload = {
        "calibrator": calibrator.to_dict(),
        "metadata": metadata or {},
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, p)
    global _CALIBRATOR_CACHE, _CALIBRATOR_CACHE_MTIME, _CALIBRATOR_LOAD_FAILED
    _CALIBRATOR_CACHE = None
    _CALIBRATOR_CACHE_MTIME = None
    _CALIBRATOR_LOAD_FAILED = False
    return str(p)


def load_calibrator(path: Optional[str] = None) -> Optional[_CalibratorBase]:
    """Load calibrator from JSON file. Returns None if file missing or invalid."""
    path = path or _calibrator_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    try:
        return _CalibratorBase.from_dict(payload.get("calibrator", {}))
    except (ValueError, KeyError) as e:
        logger.warning("edge_confidence: failed to parse calibrator at %s: %s", path, e)
        return None


def _get_active_calibrator() -> Optional[_CalibratorBase]:
    global _CALIBRATOR_CACHE, _CALIBRATOR_CACHE_MTIME, _CALIBRATOR_LOAD_FAILED
    path = _calibrator_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if _CALIBRATOR_CACHE is not None:
            _CALIBRATOR_CACHE = None
            _CALIBRATOR_CACHE_MTIME = None
        return None
    if _CALIBRATOR_CACHE is not None and _CALIBRATOR_CACHE_MTIME == mtime:
        return _CALIBRATOR_CACHE
    if _CALIBRATOR_LOAD_FAILED and _CALIBRATOR_CACHE_MTIME == mtime:
        return None
    cal = load_calibrator(path)
    if cal is None:
        _CALIBRATOR_LOAD_FAILED = True
        _CALIBRATOR_CACHE_MTIME = mtime
        return None
    _CALIBRATOR_CACHE = cal
    _CALIBRATOR_CACHE_MTIME = mtime
    _CALIBRATOR_LOAD_FAILED = False
    return cal


def calibrate_probability(raw_prob: Optional[float], calibrator: Optional[_CalibratorBase] = None) -> Optional[float]:
    """Apply calibrator to a raw probability. Returns None if raw_prob is None.

    If no calibrator is provided and none is loadable from disk, returns the
    raw probability clipped to [0, 1].
    """
    if raw_prob is None:
        return None
    try:
        p = float(raw_prob)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p):
        return None
    p = max(0.0, min(1.0, p))
    cal = calibrator if calibrator is not None else _get_active_calibrator()
    if cal is None:
        return p
    try:
        return max(0.0, min(1.0, float(cal.predict(p))))
    except Exception as e:
        logger.warning("edge_confidence: calibrator.predict failed: %s", e)
        return p


def brier_score(probs, outcomes) -> float:
    """Mean squared error between predicted probs and observed binary outcomes."""
    import numpy as _np
    p = _np.asarray(probs, dtype=float)
    y = _np.asarray(outcomes, dtype=float)
    if len(p) == 0:
        return float("nan")
    return float(_np.mean((p - y) ** 2))


def expected_calibration_error(probs, outcomes, *, n_bins: int = 10) -> float:
    """Weighted absolute gap between predicted prob and observed frequency."""
    import numpy as _np
    p = _np.asarray(probs, dtype=float)
    y = _np.asarray(outcomes, dtype=float)
    if len(p) == 0:
        return float("nan")
    edges = _np.linspace(0.0, 1.0, n_bins + 1)
    total = len(p)
    ece = 0.0
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        if i == n_bins - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        if not _np.any(mask):
            continue
        bin_pred = float(_np.mean(p[mask]))
        bin_obs = float(_np.mean(y[mask]))
        weight = float(_np.sum(mask)) / total
        ece += weight * abs(bin_pred - bin_obs)
    return float(ece)


def score_edge(
    edge_pct: float,
    books_compared: int,
    book_names: list[str],
    market: str = "h2h",
    has_sharp_book: Optional[bool] = None,
    cross_method_confirmed: bool = False,
    is_live: bool = False,
    hours_to_game: Optional[float] = None,
    market_hhi: Optional[float] = None,
    market_entropy: Optional[float] = None,
    regime_data: Optional[dict] = None,
    kl_divergence: Optional[float] = None,
    js_divergence: Optional[float] = None,
    number_shading_detected: bool = False,
    shading_value_side: Optional[str] = None,
    trap_line_confidence: Optional[float] = None,
    trap_actionable_side: Optional[str] = None,
    attention_opportunity: Optional[float] = None,
    # --- Line analysis / dead number signals ---
    rlm_detected: bool = False,
    rlm_confidence: float = 0.0,
    rlm_edge_on_sharp_side: bool = False,
    steam_detected: bool = False,
    steam_confidence: float = 0.0,
    steam_edge_on_steam_side: bool = False,
    is_dead_number: bool = False,
    key_number_value: float = 0.0,
    contrarian_value_score: float = 0.0,
    contrarian_edge_pct: float = 0.0,
    public_side_edge: bool = False,
    # --- Injury model signals ---
    injury_market_adjustment: Optional[float] = None,
    injury_is_contrarian: bool = False,
    # --- Probability calibration ---
    model_fair_prob: Optional[float] = None,
    calibrator: Optional[_CalibratorBase] = None,
) -> EdgeConfidence:
    """
    Score a detected edge using AGP confidence methodology.

    Args:
        edge_pct: Edge as a percentage (e.g., 3.2 for 3.2%)
        books_compared: Number of books in the devig/comparison
        book_names: List of book names/keys used
        market: Market type key (h2h, spreads, player_points, etc.)
        has_sharp_book: Override sharp detection (None = auto-detect from book_names)
        cross_method_confirmed: Edge found by multiple methods (devig + cross-book + simulation)
        is_live: Whether this is a live/in-play market
        hours_to_game: Hours until game starts (None if unknown)
        regime_data: Optional dict from full_regime_analysis() for the team.
            Used to adjust confidence based on regime changes, recency bias,
            and mean reversion signals.
        kl_divergence: KL divergence between opening and current lines for this game.
            High KL = active price discovery, edges in this game are higher quality.
            Low KL = stale/thin market, edge may be noise from illiquidity.
        js_divergence: Jensen-Shannon divergence (symmetric KL). Used as secondary
            signal when KL is available.
        number_shading_detected: True if the line sits on a public-magnet number.
            Book is exploiting public clustering; the opposite side has value.
        shading_value_side: 'opposite' or 'this_side' — which side benefits
            from the shading.  Boosts confidence when edge aligns with value side.
        trap_line_confidence: 0-1 confidence that the line is a trap (from
            detect_trap_line).  High confidence trap = reduce trust in the
            "obvious" public side, boost contrarian.
        trap_actionable_side: 'opposite_public' if contrarian side has value.
        attention_opportunity: 0-1 score from attention_arbitrage.  Higher means
            the market is thin (marquee events drawing eyeballs elsewhere),
            so edges may persist longer.
        rlm_detected: True if reverse line movement detected on this game.
        rlm_confidence: 0-1 confidence score from detect_rlm().
        rlm_edge_on_sharp_side: True if our edge is on the sharp/RLM side.
        steam_detected: True if a steam move was detected for this game.
        steam_confidence: 0-1 confidence score from detect_steam().
        steam_edge_on_steam_side: True if our edge is on the same side as steam.
        is_dead_number: True if the spread sits on a dead number (low importance).
        key_number_value: 0-1 importance of the spread number from dead_numbers module.
        contrarian_value_score: Historical ROI from fading the public at this %.
        contrarian_edge_pct: Estimated probability edge from contrarian position.
        public_side_edge: True if edge is on the public side with no sharp confirmation.

    Returns:
        EdgeConfidence with AGP-compliant score, tier, and reasoning.
    """
    # Step 1: Determine source class from book quality
    if has_sharp_book is None:
        book_keys_lower = {b.lower() for b in book_names}
        has_sharp_book = bool(book_keys_lower & SHARP_BOOKS)

    if has_sharp_book:
        source_class = "PRIMARY"
        ceiling = CEILING_PRIMARY
    elif books_compared >= 2:
        source_class = "SECONDARY"
        ceiling = CEILING_SECONDARY
    elif books_compared == 1:
        source_class = "SIGNAL"
        ceiling = CEILING_SIGNAL
    else:
        source_class = "INFERRED"
        ceiling = CEILING_INFERRED

    factors = {}
    reasons = []

    # Step 2: Base score from edge magnitude
    if edge_pct >= 5.0:
        base = 0.90
        reasons.append(f"Strong edge ({edge_pct:.1f}%) — well above noise")
    elif edge_pct >= 3.0:
        base = 0.75
        reasons.append(f"Solid edge ({edge_pct:.1f}%) — clear signal")
    elif edge_pct >= 2.0:
        base = 0.60
        reasons.append(f"Moderate edge ({edge_pct:.1f}%) — actionable but monitor")
    elif edge_pct >= 1.0:
        base = 0.45
        reasons.append(f"Thin edge ({edge_pct:.1f}%) — could be noise")
    elif edge_pct >= NOISE_FLOOR_PCT:
        base = 0.30
        reasons.append(f"Marginal edge ({edge_pct:.1f}%) — likely noise")
    else:
        base = 0.15
        reasons.append(f"Sub-noise edge ({edge_pct:.1f}%) — not actionable")
    factors["edge_magnitude"] = round(base, 3)

    # Step 3: Book count adjustment
    if books_compared >= 5:
        book_adj = 0.10
        reasons.append(f"{books_compared} books — strong consensus")
    elif books_compared >= 3:
        book_adj = 0.05
        reasons.append(f"{books_compared} books — adequate sample")
    elif books_compared == 2:
        book_adj = 0.0
        reasons.append("2 books — minimum for cross-reference")
    else:
        book_adj = -0.10
        reasons.append("Single book — no cross-reference possible")
    factors["book_count"] = round(book_adj, 3)

    # Step 4: Sharp book bonus
    if has_sharp_book:
        sharp_adj = 0.10
        reasons.append("Sharp book (Pinnacle/LowVig) present — PRIMARY evidence")
    else:
        sharp_adj = 0.0
        reasons.append("No sharp books — capped at SECONDARY")
    factors["sharp_book"] = round(sharp_adj, 3)

    # Step 5: Market efficiency adjustment
    efficiency = MARKET_EFFICIENCY.get(market, 0.80)
    # Less efficient markets = edges more likely to be real
    market_adj = (1.0 - efficiency) * 0.15
    if efficiency <= 0.70:
        reasons.append(f"Prop market ({market}) — less efficient, edges persist longer")
    elif efficiency >= 0.90:
        reasons.append(f"Main line ({market}) — highly efficient, edge may close fast")
    factors["market_efficiency"] = round(market_adj, 3)

    # Step 6: Cross-method confirmation
    if cross_method_confirmed:
        method_adj = 0.08
        reasons.append("Edge confirmed by multiple methods — high conviction")
    else:
        method_adj = 0.0
    factors["cross_method"] = round(method_adj, 3)

    # Step 7: Live market penalty
    if is_live:
        live_adj = -0.10
        reasons.append("Live market — prices move fast, edge may be stale")
    else:
        live_adj = 0.0
    factors["live_penalty"] = round(live_adj, 3)

    # Step 8: Time-to-game adjustment
    time_adj = 0.0
    if hours_to_game is not None:
        if hours_to_game < 0.5:
            time_adj = 0.03  # Near tip — lines are sharp, edge is more meaningful
            reasons.append("Near game start — lines are sharpest, edge is meaningful")
        elif hours_to_game > 24:
            time_adj = -0.05  # Early line — may move
            reasons.append("24+ hours out — line may still move")
    factors["time_to_game"] = round(time_adj, 3)

    # Step 9: Market concentration (HHI)
    hhi_adj = 0.0
    if market_hhi is not None:
        if market_hhi < 1500:  # Competitive — books agree, divergence is meaningful
            hhi_adj = 0.05
            reasons.append(f"Competitive market (HHI={market_hhi:.0f}) — divergence is signal")
        elif market_hhi > 4000:  # Concentrated — fewer books, easier to be noise
            hhi_adj = -0.05
            reasons.append(f"Concentrated market (HHI={market_hhi:.0f}) — edge may be noise")
    factors["market_hhi"] = round(hhi_adj, 3)

    # Step 10: Market entropy
    entropy_adj = 0.0
    if market_entropy is not None:
        if market_entropy > 2.0:  # High disagreement — genuine opportunity
            entropy_adj = 0.05
            reasons.append(f"High book disagreement (entropy={market_entropy:.2f}) — opportunity window")
        elif market_entropy < 0.5:  # Total agreement — edge is closing or noise
            entropy_adj = -0.03
            reasons.append(f"Books in strong agreement (entropy={market_entropy:.2f}) — edge may be stale")
    factors["market_entropy"] = round(entropy_adj, 3)

    # Step 11: Regime analysis adjustments
    regime_adj = 0.0
    if regime_data and isinstance(regime_data, dict):
        # 11a: Regime change detected — edge aligns with unpriced shift
        power_rating = regime_data.get("power_rating", {})
        regime_label = power_rating.get("regime", "stable") if isinstance(power_rating, dict) else "stable"
        if regime_label in ("improving", "declining"):
            regime_adj += 0.05
            reasons.append(f"Regime change detected ({regime_label}) — public may not have priced shift")

        # 11b: Recency bias — public overreacting to recent streak
        recency = regime_data.get("recency_bias")
        if recency and isinstance(recency, dict):
            bias_mag = recency.get("bias_magnitude", 0)
            bias_dir = recency.get("bias_direction", "neutral")
            if bias_mag > 0.4:
                regime_adj += 0.05
                reasons.append(
                    f"High recency bias ({bias_dir}, magnitude={bias_mag:.2f}) — "
                    f"public likely overweighting recent streak"
                )
            elif bias_mag > 0.2:
                regime_adj += 0.02
                reasons.append(f"Moderate recency bias ({bias_dir}, magnitude={bias_mag:.2f})")

        # 11c: Mean reversion signal — penalize edges that bet WITH a trend expected to revert
        mean_rev = regime_data.get("mean_reversion")
        if mean_rev and isinstance(mean_rev, dict):
            if mean_rev.get("reversion_expected") and mean_rev.get("confidence", 0) > 0.6:
                # If the team is performing far above/below mean and reversion is expected,
                # edges betting on continuation of the trend are less reliable
                z = mean_rev.get("current_zscore", 0)
                if abs(z) > 1.5:
                    regime_adj -= 0.03
                    direction = "downward" if z > 0 else "upward"
                    reasons.append(
                        f"Mean reversion expected ({direction}, z={z:.1f}) — "
                        f"trend continuation edges are riskier"
                    )

        # Clamp regime adjustment to reasonable range
        regime_adj = max(-0.08, min(0.10, regime_adj))
    factors["regime_analysis"] = round(regime_adj, 3)

    # Step 12: KL divergence — market information flow
    # High KL = significant price discovery between snapshots, meaning the market
    # is actively processing information. Edges surviving active price discovery
    # are higher quality signals. Low KL = stale/unchanged lines, could indicate
    # thin market with no information flow (edge may be illiquidity artifact).
    kl_adj = 0.0
    if kl_divergence is not None:
        if kl_divergence > 0.05:
            # Strong price discovery — edges here are battle-tested
            kl_adj = 0.06
            reasons.append(
                f"High KL divergence ({kl_divergence:.4f}) — active price discovery, "
                f"edge survived informed market"
            )
        elif kl_divergence > 0.01:
            # Moderate price discovery — market is moving, edge is plausible
            kl_adj = 0.03
            reasons.append(
                f"Moderate KL divergence ({kl_divergence:.4f}) — market actively processing info"
            )
        elif kl_divergence < 0.001:
            # Near-zero KL — lines haven't moved, could be thin/stale market
            kl_adj = -0.04
            reasons.append(
                f"Very low KL divergence ({kl_divergence:.4f}) — stale lines, "
                f"edge may be illiquidity artifact"
            )
        # JS divergence provides a secondary symmetric signal
        if js_divergence is not None and js_divergence > 0.03:
            kl_adj += 0.02
            reasons.append(
                f"High JS divergence ({js_divergence:.4f}) — symmetric price movement confirms info flow"
            )
    factors["kl_divergence"] = round(kl_adj, 3)

    # Step 13: Market psychology — number shading
    # When a line sits on a public-magnet number (NFL -3, -7; NBA round totals),
    # books shade juice toward the popular side. The opposite side carries value.
    # If our edge aligns with the value side, boost confidence; if it's on the
    # public side, reduce it.
    shading_adj = 0.0
    if number_shading_detected:
        if shading_value_side == "opposite":
            shading_adj = 0.06
            reasons.append(
                "Number shading detected — line sits on public magnet, "
                "opposite side (our edge) has value"
            )
        elif shading_value_side == "this_side":
            shading_adj = 0.03
            reasons.append(
                "Line near key number but off the magnet — less public "
                "clustering, slight value on this side"
            )
        else:
            # Shading detected but edge is on the public side
            shading_adj = -0.04
            reasons.append(
                "Number shading detected — edge is on the shaded (public) "
                "side, book may be exploiting public money"
            )
    factors["number_shading"] = round(shading_adj, 3)

    # Step 14: Market psychology — trap line detection
    # A trap line hasn't moved despite heavy one-sided public action.
    # The book (and sharps) are comfortable on the opposite side.
    # Boost contrarian edges, penalize edges aligned with the public trap side.
    trap_adj = 0.0
    if trap_line_confidence is not None and trap_line_confidence > 0.30:
        if trap_actionable_side == "opposite_public":
            # Our edge is contrarian (aligned with book/sharps against public)
            trap_adj = min(0.08, trap_line_confidence * 0.10)
            reasons.append(
                f"Trap line detected (confidence {trap_line_confidence:.0%}) — "
                f"edge is contrarian, aligned with book/sharps"
            )
        else:
            # Our edge is on the public side of a trap — reduce confidence
            trap_adj = -min(0.06, trap_line_confidence * 0.08)
            reasons.append(
                f"Trap line detected (confidence {trap_line_confidence:.0%}) — "
                f"edge is on the public side, book is comfortable against us"
            )
    factors["trap_line"] = round(trap_adj, 3)

    # Step 15: Market psychology — attention arbitrage
    # When marquee events dominate attention, thin markets are less monitored.
    # Edges in those thin markets may persist longer, giving more time to act.
    attention_adj = 0.0
    if attention_opportunity is not None and attention_opportunity > 0.3:
        attention_adj = min(0.06, attention_opportunity * 0.08)
        reasons.append(
            f"Attention arbitrage ({attention_opportunity:.2f}) — thin market "
            f"while marquee events dominate, edge may persist longer"
        )
    factors["attention_arbitrage"] = round(attention_adj, 3)

    # Step 16: Reverse line movement (RLM) — strongest sharp money indicator
    # RLM = line moves AGAINST the public side, meaning sharp money is driving it.
    # If our edge is on the sharp (RLM) side, big confidence boost.
    rlm_adj = 0.0
    if rlm_detected and rlm_confidence > 0.1:
        if rlm_edge_on_sharp_side:
            rlm_adj = min(0.08, 0.08 * rlm_confidence)
            reasons.append(
                f"RLM detected (confidence {rlm_confidence:.0%}) — edge is on the "
                f"sharp side (line moving against public). Strong confirmation."
            )
        else:
            # Edge is on the public side of an RLM — reduce confidence
            rlm_adj = -min(0.06, 0.06 * rlm_confidence)
            reasons.append(
                f"RLM detected (confidence {rlm_confidence:.0%}) — edge is on the "
                f"public side (sharp money going the other way). Caution."
            )
    factors["rlm"] = round(rlm_adj, 3)

    # Step 17: Steam move — coordinated sharp action across multiple books
    # Steam is the highest-conviction sharp signal. Multiple books moving
    # simultaneously means a syndicate is acting on information.
    steam_adj = 0.0
    if steam_detected and steam_confidence > 0.1:
        if steam_edge_on_steam_side:
            steam_adj = min(0.10, 0.10 * steam_confidence)
            reasons.append(
                f"STEAM MOVE detected (confidence {steam_confidence:.0%}) — edge "
                f"aligned with coordinated sharp action. Highest-conviction signal."
            )
        else:
            # Edge is against steam — significant red flag
            steam_adj = -min(0.08, 0.08 * steam_confidence)
            reasons.append(
                f"STEAM MOVE detected (confidence {steam_confidence:.0%}) — edge is "
                f"AGAINST the steam direction. Sharps are on the other side."
            )
    factors["steam"] = round(steam_adj, 3)

    # Step 18: Dead number / key number analysis
    # Dead numbers = low-importance spreads where the book has less risk.
    # Key numbers = high-importance spreads (3, 7, 10 in NFL) where
    # crossing the number has huge probability impact.
    dead_num_adj = 0.0
    if is_dead_number:
        dead_num_adj = 0.02
        reasons.append(
            "Spread is on a dead number — book has less risk here, "
            "edge may persist longer"
        )
    elif key_number_value > 0.6:
        dead_num_adj = 0.04
        reasons.append(
            f"Spread near high-value key number (importance {key_number_value:.2f}) — "
            f"crossing this number has significant probability impact"
        )
    elif key_number_value > 0.3:
        dead_num_adj = 0.02
        reasons.append(
            f"Spread near moderate key number (importance {key_number_value:.2f})"
        )
    factors["dead_number"] = round(dead_num_adj, 3)

    # Step 19: Contrarian value — fading the public
    # Historical data shows fading lopsided public action is +EV,
    # especially in football and when combined with sharp signals.
    contrarian_adj = 0.0
    if contrarian_edge_pct > 0 and contrarian_value_score > 1.0:
        # Meaningful contrarian edge (historical ROI > 1%)
        contrarian_adj = min(0.06, contrarian_edge_pct / 100.0 * 3.0)
        reasons.append(
            f"Contrarian value: historical ROI {contrarian_value_score:+.1f}% "
            f"at this public %, edge estimate +{contrarian_edge_pct:.1f}%"
        )
    elif public_side_edge and not rlm_edge_on_sharp_side and not steam_edge_on_steam_side:
        # Edge is on the public side with no sharp confirmation — penalty
        contrarian_adj = -0.04
        reasons.append(
            "Edge is on the public side with no sharp confirmation "
            "(no RLM, no steam) — higher risk of being wrong"
        )
    factors["contrarian"] = round(contrarian_adj, 3)

    # Step: Injury model adjustment
    # When the injury model detects that the market hasn't fully priced an
    # injury AND our edge aligns with the expected adjustment direction,
    # boost confidence.  When the model flags a contrarian opportunity
    # (market over-adjusted to star name), boost contrarian edges.
    injury_adj = 0.0
    if injury_market_adjustment is not None and injury_market_adjustment != 0:
        injury_adj = injury_market_adjustment  # already capped at +/-0.10 by caller
        if injury_market_adjustment > 0:
            reasons.append(
                f"Injury model: market under-adjusted (modifier {injury_market_adjustment:+.3f}). "
                f"Edge aligns with expected adjustment direction."
            )
        else:
            reasons.append(
                f"Injury model: edge may oppose adjustment direction "
                f"(modifier {injury_market_adjustment:+.3f})."
            )
    if injury_is_contrarian:
        injury_adj += 0.04
        reasons.append(
            "Injury model CONTRARIAN: public over-reacted to star injury. "
            "Market moved more than model impact suggests — value on injured team."
        )
    injury_adj = max(-0.10, min(0.10, injury_adj))
    factors["injury_model"] = round(injury_adj, 3)

    # Compute raw score
    line_analysis_adj = rlm_adj + steam_adj + dead_num_adj + contrarian_adj
    psych_adj = shading_adj + trap_adj + attention_adj
    raw = base + book_adj + sharp_adj + market_adj + method_adj + live_adj + time_adj + hhi_adj + entropy_adj + regime_adj + kl_adj + psych_adj + line_analysis_adj + injury_adj
    # Clamp to [0, ceiling]
    score = round(max(0.0, min(raw, ceiling)), 3)
    factors["raw_total"] = round(raw, 3)
    factors["ceiling_applied"] = ceiling

    # Determine tier
    if score >= 0.90:
        tier = "VERIFIED"
    elif score >= 0.75:
        tier = "CORROBORATED"
    elif score >= 0.55:
        tier = "PROBABLE"
    elif score >= 0.30:
        tier = "SPECULATIVE"
    else:
        tier = "UNVERIFIED"

    reasoning = f"Source: {source_class} (ceiling {ceiling}). " + " | ".join(reasons)

    raw_prob: Optional[float] = None
    calibrated_prob: Optional[float] = None
    calibrator_name: Optional[str] = None
    if model_fair_prob is not None:
        try:
            rp = float(model_fair_prob)
            if math.isfinite(rp):
                raw_prob = max(0.0, min(1.0, rp))
        except (TypeError, ValueError):
            raw_prob = None
    if raw_prob is not None:
        active = calibrator if calibrator is not None else _get_active_calibrator()
        if active is None:
            calibrated_prob = raw_prob
            calibrator_name = "identity"
        else:
            try:
                calibrated_prob = max(0.0, min(1.0, float(active.predict(raw_prob))))
                calibrator_name = getattr(active, "kind", "unknown")
            except Exception as e:
                logger.warning("edge_confidence: calibrator.predict failed: %s", e)
                calibrated_prob = raw_prob
                calibrator_name = "identity_fallback"

    return EdgeConfidence(
        score=score,
        tier=tier,
        source_class=source_class,
        ceiling=ceiling,
        factors=factors,
        reasoning=reasoning,
        raw_prob=raw_prob,
        calibrated_prob=calibrated_prob,
        calibrator_name=calibrator_name,
    )


def score_parlay(leg_confidences: list[EdgeConfidence]) -> EdgeConfidence:
    """
    Score a parlay's overall confidence.

    Parlay confidence is limited by its weakest leg — a chain is only
    as strong as its weakest link. The score is the minimum leg score
    weighted by the geometric mean to account for cumulative risk.
    """
    if not leg_confidences:
        return EdgeConfidence(
            score=0.0, tier="UNVERIFIED", source_class="INFERRED",
            ceiling=0.55, factors={}, reasoning="No legs to score",
        )

    scores = [lc.score for lc in leg_confidences]
    min_score = min(scores)
    # Geometric mean biases toward the weakest leg
    product = 1.0
    for s in scores:
        product *= max(s, 0.01)  # avoid zero
    geo_mean = product ** (1.0 / len(scores))

    # Weighted: 60% weakest leg, 40% geometric mean
    combined = 0.6 * min_score + 0.4 * geo_mean
    # Parlay ceiling: lowest leg's ceiling
    ceiling = min(lc.ceiling for lc in leg_confidences)
    score = round(min(combined, ceiling), 3)

    # Determine tier
    if score >= 0.90:
        tier = "VERIFIED"
    elif score >= 0.75:
        tier = "CORROBORATED"
    elif score >= 0.55:
        tier = "PROBABLE"
    elif score >= 0.30:
        tier = "SPECULATIVE"
    else:
        tier = "UNVERIFIED"

    # Weakest leg source class
    source_class = min(leg_confidences, key=lambda lc: lc.score).source_class

    factors = {
        "leg_scores": scores,
        "min_score": round(min_score, 3),
        "geo_mean": round(geo_mean, 3),
        "leg_count": len(scores),
        "ceiling": ceiling,
    }

    weakest = min(leg_confidences, key=lambda lc: lc.score)
    reasoning = (
        f"Parlay ({len(scores)} legs). Weakest leg: {weakest.tier} ({weakest.score:.2f}). "
        f"Combined: {score:.2f} ({tier}). Source: {source_class} ceiling {ceiling}."
    )

    raw_prob: Optional[float] = None
    calibrated_prob: Optional[float] = None
    calibrator_name: Optional[str] = None
    raw_legs = [lc.raw_prob for lc in leg_confidences if lc.raw_prob is not None]
    if raw_legs and len(raw_legs) == len(leg_confidences):
        prod = 1.0
        for rp in raw_legs:
            prod *= max(0.0, min(1.0, float(rp)))
        raw_prob = prod
    cal_legs = [lc.calibrated_prob for lc in leg_confidences if lc.calibrated_prob is not None]
    if cal_legs and len(cal_legs) == len(leg_confidences):
        prod = 1.0
        for cp in cal_legs:
            prod *= max(0.0, min(1.0, float(cp)))
        calibrated_prob = prod
        names = {lc.calibrator_name for lc in leg_confidences if lc.calibrator_name}
        calibrator_name = "mixed" if len(names) > 1 else (next(iter(names)) if names else None)

    return EdgeConfidence(
        score=score, tier=tier, source_class=source_class,
        ceiling=ceiling, factors=factors, reasoning=reasoning,
        raw_prob=raw_prob, calibrated_prob=calibrated_prob, calibrator_name=calibrator_name,
    )
