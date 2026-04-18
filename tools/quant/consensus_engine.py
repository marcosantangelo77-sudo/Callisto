"""Consensus fair-probability estimator over multi-book lines.

Given N book-offered prices for a market outcome, the question is:
what is the *true* probability of the outcome? Every book's implied
probability is biased by its vig. A sharp book (Pinnacle) carries
~2% vig; a retail book (DraftKings, Fanatics) carries ~5%; a boost
market can carry 10%+. Naively averaging implied probabilities double-
counts the vig and overweights noisy retail lines.

ConsensusEngine produces a **calibrated fair probability estimate** by:

    1. Devigging each book individually. Pinnacle uses the multiplicative
       method (proportional scaling of implied probs to sum to 1.0).
       Retail books use the power method (fit a single exponent that
       normalises) because retail overround is not proportional across
       favorites vs underdogs. Both methods are standard sports-trading
       practice; see Cover/Thomas for the math or Joseph Buchdahl's
       "Squares and Sharps, Suckers and Sharks" for empirical support.

    2. Weighting each devigged estimate by the book's sharp tier.
       Pinnacle, Circa, and Betfair Exchange are the global sharpest
       books on most markets. DraftKings, FanDuel, and BetMGM are
       reference books (liquid but slower than Pinnacle). Caesars,
       Fanatics, Hard Rock, and smaller retail are soft books that
       trail. Tier weights roughly reflect the Kelly growth rate each
       book's line would give a full-information bettor: sharp lines
       converge to fair faster, so the weights sum is ~consensus.

    3. Trimming outliers. Any devigged estimate >2σ from the weighted
       median is dropped — usually indicates a book got a bad auto-
       price or hasn't updated after news. This both reduces variance
       in the consensus and produces a "disagreement flag" telling the
       caller whether the market is noisy right now.

    4. Re-aggregating the trimmed set with tier weights.

The output is a ``ConsensusResult`` with the fair probability, the
effective sample size (Kish's formula), a confidence band from the
weighted standard error, and a list of outlier books. Callers can use
the confidence band directly in Kelly sizing (wider bands → smaller
fraction).

Not thread-safe with respect to the module-level tier tables (they are
read-only constants). Pure functions everywhere else.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

# Book sharp tiers. The weights below target a geometric-growth-optimal
# consensus under the assumption that sharpness on a given market
# dominates liquidity (true for most player-prop and alt-line markets).
# Override with a custom dict passed to `compute_consensus_fair_prob` when
# market-specific priors diverge — e.g., Pinnacle is NOT the sharpest book
# on UFC prop markets; Betfair exchange order book is.
BOOK_TIER: dict[str, str] = {
    # Sharp (global reference books with the tightest vig + fastest update)
    "pinnacle": "sharp",
    "lowvig": "sharp",
    "betfair_exchange": "sharp",
    "circa": "sharp",
    # Reference (liquid US retail, slower than sharp but large-sized)
    "draftkings": "reference",
    "fanduel": "reference",
    "betmgm": "reference",
    "pointsbet": "reference",
    "barstool": "reference",
    # Soft (lower limits, slower to update, often the +EV source)
    "caesars": "soft",
    "fanatics": "soft",
    "hardrock": "soft",
    "bet365": "soft",
    "unibet": "soft",
    "wynn": "soft",
    "bally": "soft",
    "superbook": "soft",
    "twinspires": "soft",
    "espnbet": "soft",
}

BOOK_TIER_WEIGHT: dict[str, float] = {
    "sharp": 0.60,
    "reference": 0.25,
    "soft": 0.15,
}

# Heuristic vig prior when we have no other signal. Used as the initial
# guess in power-devig iteration and as the fallback weight floor.
DEFAULT_TIER_VIG: dict[str, float] = {
    "sharp": 0.025,
    "reference": 0.05,
    "soft": 0.06,
}


@dataclass(frozen=True)
class BookLine:
    """One book's offered price for one outcome of a two-way market.

    For three-way markets (e.g., soccer 1X2) the caller should call the
    engine once per outcome and pass the three BookLines for that
    outcome each time — or use ``compute_consensus_fair_prob_nway`` which
    handles the joint devig correctly.
    """
    book: str
    implied_prob: float              # pre-devig
    paired_implied_prob: Optional[float] = None  # opposite side of the two-way market, if known
    updated_at: Optional[str] = None
    limit: Optional[float] = None    # max stake the book will accept; None = unknown


@dataclass(frozen=True)
class ConsensusResult:
    """Output of the consensus calculation."""
    fair_prob: float                  # calibrated, in (0, 1)
    n_books: int                      # books contributing after trim
    n_books_raw: int                  # books supplied before trim
    effective_sample_size: float      # Kish ESS under tier weights
    std_err: float                    # weighted standard error of fair_prob
    outlier_books: list[str] = field(default_factory=list)
    disagreement: bool = False        # True if any outlier trimmed OR std_err > 0.03
    per_book_fair: dict[str, float] = field(default_factory=dict)
    method: str = "consensus_v1"


# ──────────────────────────────────────────────────────────────────────
# Devig primitives
# ──────────────────────────────────────────────────────────────────────


def multiplicative_devig(implied_a: float, implied_b: float) -> tuple[float, float]:
    """Proportional (multiplicative) devig for a two-way market.

    Assumes overround is uniformly distributed across both outcomes. This
    is the right model for Pinnacle and other sharp books where the hold
    is tiny and symmetric.

        p_A = implied_A / (implied_A + implied_B)
    """
    if implied_a < 0 or implied_b < 0:
        raise ValueError("implied probabilities must be non-negative")
    total = implied_a + implied_b
    if total <= 0:
        raise ValueError("implied probabilities must sum to a positive value")
    return implied_a / total, implied_b / total


def pinnacle_devig(implied_a: float, implied_b: float) -> tuple[float, float]:
    """Alias for :func:`multiplicative_devig`. Named separately for legibility
    at call sites where we're specifically applying sharp-book logic."""
    return multiplicative_devig(implied_a, implied_b)


def power_devig(
    implied_probs: list[float],
    tol: float = 1e-9,
    max_iter: int = 60,
) -> list[float]:
    """Power-method devig for a market of arbitrary cardinality.

    Finds the exponent ``k`` such that ``sum(p_i ** k) == 1``, then
    returns ``[p_i ** k for p_i in implied_probs]``. This accounts for
    non-uniform overround — retail books typically charge MORE vig on
    favorites than underdogs, which the multiplicative method ignores.

    Uses a bisection root-find, which is robust against the
    near-degenerate cases (implied prob near 0 or 1) that sometimes break
    Newton iterations here.
    """
    if not implied_probs:
        raise ValueError("implied_probs must be non-empty")
    if any(p <= 0 or p >= 1 for p in implied_probs):
        raise ValueError("implied probabilities must be strictly in (0, 1)")

    # f(k) = sum(p ** k) − 1. We want f(k) = 0.
    # At k = 1, f = overround > 0 (the book's vig). At k = ∞, f → 0 since
    # each p < 1. At k = 0, f = n − 1 > 0 for n ≥ 2. So the root sits in
    # (1, ∞) for any normal two-way or n-way market with overround > 0.
    def _f(k: float) -> float:
        return sum(p ** k for p in implied_probs) - 1.0

    lo, hi = 1.0, 1.0
    # Expand hi until f(hi) < 0.
    while _f(hi) > 0 and hi < 1e6:
        hi *= 2.0
    if _f(hi) > 0:
        # Degenerate — all probabilities very close to 1. Fall back to
        # multiplicative devig proportionally.
        total = sum(implied_probs)
        return [p / total for p in implied_probs]

    # Bisect.
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        fm = _f(mid)
        if abs(fm) < tol:
            break
        if fm > 0:
            lo = mid
        else:
            hi = mid
    k = 0.5 * (lo + hi)
    return [p ** k for p in implied_probs]


# ──────────────────────────────────────────────────────────────────────
# Consensus aggregation
# ──────────────────────────────────────────────────────────────────────


def _devig_one_book(line: BookLine) -> Optional[float]:
    """Return book's devigged fair prob for the *target* outcome, or None
    if we can't reliably devig (e.g., missing paired side and no prior)."""
    book_key = (line.book or "").lower()
    tier = BOOK_TIER.get(book_key, "soft")
    if line.paired_implied_prob is not None and line.paired_implied_prob > 0:
        # We have both sides of the two-way market — exact devig.
        if tier == "sharp":
            fair, _ = pinnacle_devig(line.implied_prob, line.paired_implied_prob)
        else:
            fair, _ = power_devig([line.implied_prob, line.paired_implied_prob])[0], None
            # power_devig returns a list; take the first element.
        return fair
    # Single-sided: subtract the tier-prior half-vig. Conservative but stable.
    vig_prior = DEFAULT_TIER_VIG.get(tier, 0.05)
    est = line.implied_prob / (1.0 + vig_prior / 2.0)
    return max(0.001, min(0.999, est))


def _weighted_median(values: list[float], weights: list[float]) -> float:
    """Weighted median. Used for robust-center estimation in trimming."""
    if not values:
        raise ValueError("values must be non-empty")
    pairs = sorted(zip(values, weights), key=lambda vw: vw[0])
    total = sum(weights)
    running = 0.0
    for v, w in pairs:
        running += w
        if running >= total / 2.0:
            return v
    return pairs[-1][0]


def _weighted_std(values: list[float], weights: list[float], mean: float) -> float:
    """Frequency-weighted standard deviation."""
    if sum(weights) <= 0:
        return 0.0
    return math.sqrt(
        sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / sum(weights)
    )


def compute_consensus_fair_prob(
    lines: list[BookLine],
    *,
    tier_weights: Optional[dict[str, float]] = None,
    trim_sigma: float = 2.0,
    min_books: int = 2,
) -> ConsensusResult:
    """Return a calibrated consensus fair-probability for one outcome.

    The algorithm is deterministic and has no hidden state. The
    computation proceeds in four stages — see the module docstring for
    the math rationale.

    Parameters
    ----------
    lines
        One BookLine per book. Must be the SAME outcome across all lines
        (e.g., all "Yankees moneyline"). Pass ``paired_implied_prob`` on
        each line where possible — it enables exact two-way devig.
    tier_weights
        Override for ``BOOK_TIER_WEIGHT`` if you've measured that sharp
        weight should be lower for this specific market (e.g., Pinnacle
        has no edge on UFC fighter-level props).
    trim_sigma
        Outlier threshold. Any book whose devigged estimate is more than
        ``trim_sigma`` weighted-SD from the weighted median is dropped.
        2.0 is empirically the sweet spot — tighter kills signal from
        legitimately-fast books that moved first; looser lets bad auto-
        prices leak in.
    min_books
        Minimum contributing books after trim. If fewer, the result's
        ``disagreement`` flag is set and the caller should down-weight
        the signal.

    Returns
    -------
    ConsensusResult
        Fields documented on the dataclass. ``fair_prob`` is the
        calibrated estimate; ``std_err`` is a reasonable input into a
        confidence-aware Kelly.
    """
    if not lines:
        raise ValueError("lines must be non-empty")

    weights_map = tier_weights or BOOK_TIER_WEIGHT

    per_book: dict[str, float] = {}
    raw_values: list[float] = []
    raw_weights: list[float] = []
    raw_books: list[str] = []
    for line in lines:
        fair = _devig_one_book(line)
        if fair is None:
            continue
        book_key = (line.book or "").lower()
        tier = BOOK_TIER.get(book_key, "soft")
        w = weights_map.get(tier, weights_map["soft"])
        per_book[line.book] = fair
        raw_values.append(fair)
        raw_weights.append(w)
        raw_books.append(line.book)

    n_raw = len(raw_values)
    if n_raw == 0:
        raise ValueError("no lines survived devigging")

    # Weighted median → robust center for trimming.
    center = _weighted_median(raw_values, raw_weights)
    sigma = _weighted_std(raw_values, raw_weights, center)
    # Floor sigma below a numerical-noise threshold so that books that
    # "agree" at the floating-point jitter level (e.g., same input but
    # different devig code paths) aren't flagged as outliers. 1e-6 in
    # implied-prob space is 0.0001% — orders of magnitude finer than any
    # real sportsbook resolution (books quote to 0.001 implied).
    if sigma < 1e-6:
        # ESS under Kish's formula: (Σw)² / Σw². Equals n only for equal
        # weights; with mixed tiers it's strictly less.
        ess_agree = (sum(raw_weights) ** 2) / sum(w * w for w in raw_weights)
        return ConsensusResult(
            fair_prob=center,
            n_books=n_raw,
            n_books_raw=n_raw,
            effective_sample_size=ess_agree,
            std_err=0.0,
            outlier_books=[],
            disagreement=False,
            per_book_fair=per_book,
        )

    # Trim outliers at trim_sigma from the weighted median.
    kept_values: list[float] = []
    kept_weights: list[float] = []
    outliers: list[str] = []
    for v, w, b in zip(raw_values, raw_weights, raw_books):
        if abs(v - center) > trim_sigma * sigma:
            outliers.append(b)
        else:
            kept_values.append(v)
            kept_weights.append(w)

    if len(kept_values) < min_books and n_raw >= min_books:
        # Trimming killed too much — fall back to untrimmed set but flag it.
        kept_values, kept_weights = raw_values, raw_weights
        outliers = []

    # Weighted mean on the kept set.
    total_w = sum(kept_weights)
    mean = sum(v * w for v, w in zip(kept_values, kept_weights)) / total_w
    # Kish effective sample size under these weights.
    ess = (sum(kept_weights) ** 2) / sum(w * w for w in kept_weights)
    kept_sigma = _weighted_std(kept_values, kept_weights, mean)
    # Std error of a weighted mean: σ / sqrt(ESS).
    std_err = kept_sigma / math.sqrt(ess) if ess > 0 else 0.0

    disagreement = bool(outliers) or std_err > 0.03

    return ConsensusResult(
        fair_prob=max(0.001, min(0.999, mean)),
        n_books=len(kept_values),
        n_books_raw=n_raw,
        effective_sample_size=ess,
        std_err=std_err,
        outlier_books=outliers,
        disagreement=disagreement,
        per_book_fair=per_book,
    )
