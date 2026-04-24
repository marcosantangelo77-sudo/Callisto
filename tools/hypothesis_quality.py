"""
Hypothesis quality gate for Callisto's autonomous hypothesis generator.

Enforces a falsifiability + specificity schema on every hypothesis proposal
BEFORE it is persisted to the hypotheses table. This is the "never LLM gut
picks" line in the sand — vague, under-specified, and duplicate proposals
are rejected with a logged reason, and rolling metrics are exposed via
/system/full-status so regressions are immediately visible.

Schema (each proposal must satisfy all):
  - sport:            non-empty; matches VALID_SPORT_PREFIXES
  - market_type:      non-empty; matches VALID_MARKET_PATTERNS
  - direction:        one of VALID_SIDES (over/under/home/away/yes/no/
                      nrfi/yrfi/favorite/underdog/winner/top_5/top_10/...)
                      OR thesis text contains an unambiguous side token
  - condition_set:    at least one concrete context factor in model_config
                      OR a cohort_filter / SQL WHERE-style clause
  - expected_edge:    numeric edge_threshold in [MIN_EDGE, MAX_EDGE]
  - min_sample_size:  >= MIN_SAMPLE
  - significance_level:<= MAX_PVALUE
  - stat_test:        one of VALID_STAT_TESTS  (falls back to binomial
                      for spread/total/ML/prop-over-under markets)

Additional rejection gates:
  - banned phrasing ("gut feeling", "teams usually", "tend to", ...)
  - too-short thesis (< MIN_THESIS_LEN characters)
  - unquantified thesis (no numeric threshold anywhere in thesis text
                        AND no numeric threshold in model_config factors)

Semantic dedup: when an embedding of the new thesis is supplied together
with the embeddings of the most recent N prior hypotheses, a cosine
similarity >= DUPLICATE_SIM threshold triggers rejection.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Optional, Sequence

logger = logging.getLogger("callisto.hypothesis_quality")


MIN_EDGE: float = 0.003
MAX_EDGE: float = 0.30
MIN_SAMPLE: int = 50
MAX_PVALUE: float = 0.10
MIN_THESIS_LEN: int = 60
DUPLICATE_SIM: float = 0.88
RECENT_N_FOR_DEDUP: int = 500
METRICS_WINDOW: int = 500


VALID_SPORT_PREFIXES: tuple[str, ...] = (
    "basketball_",
    "baseball_",
    "americanfootball_",
    "icehockey_",
    "soccer_",
    "golf_",
    "tennis_",
    "mma_",
    "boxing_",
)

VALID_MARKET_PATTERNS: tuple[str, ...] = (
    "spreads", "totals", "h2h", "moneyline", "runline", "puckline",
    "player_", "pitcher_", "batter_", "goalie_", "skater_",
    "first_inning_nrfi_yrfi", "nrfi", "yrfi", "first_round_leader",
    "tournament_winner", "top_5_finish", "top_10_finish",
    "top_20_finish", "round_score", "team_total",
)

VALID_SIDES: tuple[str, ...] = (
    "over", "under", "home", "away", "yes", "no",
    "nrfi", "yrfi", "favorite", "underdog", "winner",
    "top_5", "top_10", "top_20", "first_round_leader",
)

VALID_STAT_TESTS: tuple[str, ...] = (
    "binomial", "binomial_two_sided", "chi_squared", "chi_sq",
    "fisher_exact", "logistic_regression", "poisson",
    "t_test", "ttest", "z_test", "mann_whitney", "wilcoxon",
    "kolmogorov_smirnov", "bootstrap",
)

DEFAULT_STAT_TEST_FOR_MARKET: dict[str, str] = {
    "spreads": "binomial",
    "totals": "binomial",
    "h2h": "binomial",
    "moneyline": "binomial",
    "runline": "binomial",
    "puckline": "binomial",
    "first_inning_nrfi_yrfi": "binomial",
    "round_score": "t_test",
}

BANNED_PHRASES: tuple[str, ...] = (
    "gut feeling",
    "gut pick",
    "i think",
    "i believe",
    "seems like",
    "feels like",
    "teams usually",
    "players usually",
    "generally tend",
    "tend to underperform",
    "tend to overperform",
    "magic",
    "vibe",
    "destined",
    "obviously",
    "clearly wins",
    "should just",
    "always covers",
    "never loses",
    "sure thing",
    "lock",
    "hunch",
)


class RejectReason:
    MISSING_FIELD = "missing_required_field"
    INVALID_SPORT = "invalid_sport"
    INVALID_MARKET = "invalid_market"
    MISSING_DIRECTION = "missing_direction"
    MISSING_CONDITIONS = "missing_condition_set"
    EDGE_OUT_OF_RANGE = "edge_threshold_out_of_range"
    SAMPLE_TOO_SMALL = "min_sample_size_below_floor"
    PVALUE_TOO_LOOSE = "significance_level_above_ceiling"
    INVALID_STAT_TEST = "invalid_stat_test"
    BANNED_PHRASE = "banned_phrase"
    THESIS_TOO_SHORT = "thesis_too_short"
    UNQUANTIFIED_THESIS = "thesis_has_no_numeric_threshold"
    DUPLICATE_SEMANTIC = "duplicate_near_prior_hypothesis"


@dataclass
class QualityResult:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    normalized: dict = field(default_factory=dict)

    @property
    def primary_reason(self) -> Optional[str]:
        return self.reasons[0] if self.reasons else None


class HypothesisQualityMetrics:
    """Process-wide rolling counters for hypothesis-gen quality telemetry."""

    def __init__(self, window: int = METRICS_WINDOW):
        self._lock = threading.Lock()
        self._window = window
        self._events: deque[tuple[float, bool, Optional[str]]] = deque(maxlen=window)
        self._recent_rejections: deque[dict] = deque(maxlen=50)
        self._total_accepted: int = 0
        self._total_rejected: int = 0

    def record(self, accepted: bool, reason: Optional[str],
               name: Optional[str] = None, sport: Optional[str] = None,
               market_type: Optional[str] = None) -> None:
        with self._lock:
            self._events.append((time.time(), accepted, reason))
            if accepted:
                self._total_accepted += 1
            else:
                self._total_rejected += 1
                self._recent_rejections.append({
                    "ts": time.time(),
                    "reason": reason,
                    "name": name,
                    "sport": sport,
                    "market_type": market_type,
                })

    def snapshot(self) -> dict:
        with self._lock:
            window = list(self._events)
            recent = list(self._recent_rejections)
            total_accepted = self._total_accepted
            total_rejected = self._total_rejected

        n = len(window)
        accepted_in_window = sum(1 for _, a, _ in window if a)
        rejected_in_window = n - accepted_in_window
        reason_histogram: Counter[str] = Counter()
        for _, a, r in window:
            if not a and r:
                reason_histogram[r] += 1

        rejection_rate = (rejected_in_window / n) if n else 0.0
        return {
            "window": self._window,
            "last_n": n,
            "accepted_in_window": accepted_in_window,
            "rejected_in_window": rejected_in_window,
            "rejection_rate": round(rejection_rate, 4),
            "reason_histogram": dict(reason_histogram),
            "total_accepted": total_accepted,
            "total_rejected": total_rejected,
            "recent_rejections": recent[-10:],
        }

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._recent_rejections.clear()
            self._total_accepted = 0
            self._total_rejected = 0


_METRICS = HypothesisQualityMetrics()


def get_metrics() -> HypothesisQualityMetrics:
    return _METRICS


_NUMERIC_TOKEN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:%|percent|bp|bps|games|events|points|runs|"
    r"days|rounds|minutes|shots|saves|strikeouts|rebounds|assists|threes|"
    r"mph|inches|feet|mm|degrees|hr|homeruns|\+|x|times)?",
    re.IGNORECASE,
)


def _has_numeric_threshold(text: str) -> bool:
    if not text:
        return False
    return bool(_NUMERIC_TOKEN.search(text))


def _has_valid_sport(sport: Optional[str]) -> bool:
    if not sport:
        return False
    s = sport.lower()
    return any(s.startswith(p) for p in VALID_SPORT_PREFIXES)


def _has_valid_market(market: Optional[str]) -> bool:
    if not market:
        return False
    m = market.lower()
    return any(p in m for p in VALID_MARKET_PATTERNS)


def _detect_direction(h: dict) -> Optional[str]:
    for key in ("direction", "side", "side_filter"):
        v = h.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    mc = h.get("model_config") or {}
    v = mc.get("side_filter")
    if isinstance(v, str) and v.strip():
        return v.strip().lower()
    thesis = (h.get("thesis") or h.get("thesis_statement") or "").lower()
    for side in VALID_SIDES:
        token = side.replace("_", " ")
        if re.search(rf"\b{re.escape(token)}\b", thesis):
            return side
    return None


def _detect_conditions(h: dict) -> bool:
    mc = h.get("model_config") or {}
    factors = mc.get("context_factors") or mc.get("factors") or []
    if isinstance(factors, list) and len(factors) >= 1 and all(
        isinstance(f, str) and f.strip() for f in factors
    ):
        return True
    cohort = (h.get("cohort_filter") or mc.get("cohort_filter") or "").strip()
    if cohort:
        return True
    variables = h.get("variables") or {}
    if isinstance(variables, dict) and variables:
        return True
    return False


def _detect_stat_test(h: dict) -> Optional[str]:
    for key in ("stat_test", "statistical_test", "test"):
        v = h.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    mc = h.get("model_config") or {}
    for key in ("stat_test", "statistical_test", "test"):
        v = mc.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    market = (h.get("market_type") or "").lower()
    for key, default in DEFAULT_STAT_TEST_FOR_MARKET.items():
        if key in market:
            return default
    if market.startswith("player_") or market.startswith("pitcher_") \
            or market.startswith("batter_") or market.startswith("skater_") \
            or market.startswith("goalie_"):
        return "binomial"
    return None


def _contains_banned_phrase(text: str) -> Optional[str]:
    low = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in low:
            return phrase
    return None


def check_schema(h: dict) -> QualityResult:
    """Validate schema of a single hypothesis proposal.

    h may be either the raw LLM candidate (with keys like 'thesis_statement',
    'cohort_filter', 'ic_prior_estimate') or an internal-format dict (with
    'thesis', 'market_type', 'model_config'). Both shapes are supported.
    """
    reasons: list[str] = []

    thesis = (h.get("thesis_statement") or h.get("thesis") or "").strip()
    name = (h.get("name") or "").strip()
    sport = (h.get("sport") or "").strip()
    market = (h.get("market_type") or h.get("market") or "").strip()
    mc = h.get("model_config") or {}

    if not name:
        reasons.append(RejectReason.MISSING_FIELD + ":name")
    if len(thesis) < MIN_THESIS_LEN:
        reasons.append(RejectReason.THESIS_TOO_SHORT)
    if not _has_valid_sport(sport):
        reasons.append(RejectReason.INVALID_SPORT)
    if not _has_valid_market(market):
        reasons.append(RejectReason.INVALID_MARKET)

    direction = _detect_direction(h)
    if not direction:
        reasons.append(RejectReason.MISSING_DIRECTION)

    if not _detect_conditions(h):
        reasons.append(RejectReason.MISSING_CONDITIONS)

    edge = h.get("edge_threshold")
    if edge is None:
        edge = h.get("ic_prior_estimate")
    try:
        edge_f = float(edge) if edge is not None else None
    except (TypeError, ValueError):
        edge_f = None
    if edge_f is None:
        reasons.append(RejectReason.MISSING_FIELD + ":edge_threshold")
    elif edge_f < MIN_EDGE or edge_f > MAX_EDGE:
        reasons.append(RejectReason.EDGE_OUT_OF_RANGE)

    min_sample = h.get("min_sample_size")
    if min_sample is None:
        min_sample = h.get("min_signals")
    if min_sample is None:
        min_sample = mc.get("min_sample_size") if isinstance(mc, dict) else None
    try:
        min_sample_i = int(min_sample) if min_sample is not None else None
    except (TypeError, ValueError):
        min_sample_i = None
    if min_sample_i is None:
        min_sample_i = MIN_SAMPLE
    if min_sample_i < MIN_SAMPLE:
        reasons.append(RejectReason.SAMPLE_TOO_SMALL)

    sig_level = h.get("significance_level")
    if sig_level is None and isinstance(mc, dict):
        sig_level = mc.get("significance_level")
    try:
        sig_f = float(sig_level) if sig_level is not None else 0.05
    except (TypeError, ValueError):
        sig_f = 0.05
    if sig_f > MAX_PVALUE or sig_f <= 0.0:
        reasons.append(RejectReason.PVALUE_TOO_LOOSE)

    stat_test = _detect_stat_test(h)
    if not stat_test or stat_test not in VALID_STAT_TESTS:
        reasons.append(RejectReason.INVALID_STAT_TEST)

    banned = _contains_banned_phrase(thesis) or _contains_banned_phrase(name)
    if banned:
        reasons.append(f"{RejectReason.BANNED_PHRASE}:{banned}")

    if not _has_numeric_threshold(thesis):
        reasons.append(RejectReason.UNQUANTIFIED_THESIS)

    normalized = {
        "name": name,
        "sport": sport,
        "market_type": market,
        "direction": direction,
        "edge_threshold": edge_f if edge_f is not None else None,
        "min_sample_size": min_sample_i,
        "significance_level": sig_f,
        "stat_test": stat_test,
        "thesis": thesis,
    }
    return QualityResult(accepted=len(reasons) == 0, reasons=reasons,
                         normalized=normalized)


def check_semantic_duplicate(
    candidate_emb: Sequence[float],
    prior_embs: Sequence[Sequence[float]],
    threshold: float = DUPLICATE_SIM,
) -> tuple[bool, float, int]:
    """Return (is_duplicate, max_similarity, index_of_match).

    Uses cosine similarity; returns (False, 0.0, -1) when no prior_embs are
    supplied so callers can skip the check gracefully when embeddings aren't
    available.
    """
    from tools.embeddings import cosine_similarity

    if not candidate_emb or not prior_embs:
        return (False, 0.0, -1)
    best_sim = -1.0
    best_idx = -1
    for i, e in enumerate(prior_embs):
        if not e:
            continue
        s = cosine_similarity(list(candidate_emb), list(e))
        if s > best_sim:
            best_sim = s
            best_idx = i
    return (best_sim >= threshold, best_sim, best_idx)


def hypothesis_quality_check(
    h: dict,
    candidate_emb: Optional[Sequence[float]] = None,
    prior_embs: Optional[Sequence[Sequence[float]]] = None,
    duplicate_threshold: float = DUPLICATE_SIM,
    record_metric: bool = True,
) -> QualityResult:
    """Entry point — schema check then optional semantic-dedup check.

    If `record_metric` is True (default) the outcome is appended to the
    process-wide rolling metrics counter so /system/full-status can expose
    it.
    """
    result = check_schema(h)

    if result.accepted and candidate_emb is not None and prior_embs:
        is_dup, sim, idx = check_semantic_duplicate(
            candidate_emb, prior_embs, threshold=duplicate_threshold
        )
        if is_dup:
            result.accepted = False
            result.reasons.append(
                f"{RejectReason.DUPLICATE_SEMANTIC}:sim={sim:.3f}:idx={idx}"
            )

    if record_metric:
        _METRICS.record(
            accepted=result.accepted,
            reason=result.primary_reason,
            name=h.get("name"),
            sport=h.get("sport"),
            market_type=(h.get("market_type") or h.get("market")),
        )
        if not result.accepted:
            logger.info(
                "hypothesis_quality REJECT name=%r sport=%r market=%r reasons=%s",
                h.get("name"), h.get("sport"),
                (h.get("market_type") or h.get("market")),
                result.reasons,
            )

    return result
