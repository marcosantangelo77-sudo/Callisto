"""
market_regime — Market-regime detection for Callisto.

The same hypothesis can be +EV in NBA regular season and sharply -EV in the
playoffs (different rest patterns, different referee crews, sharper market).
Callisto currently treats every day identically, leaving systematic alpha
on the table whenever a regime shift happens.

This module is a STANDALONE read-only signal producer. It does not mutate
any table and it does not wire into decision-making — integration is a
later PR. Callers should import:

    from tools.market_regime import (
        detect_regime,
        MarketRegime,
        current_regime_multiplier,
        regime_safe_for_trading,
    )

Sport keys follow the odds-api convention already in use across Callisto:
``baseball_mlb``, ``basketball_nba``, ``icehockey_nhl``,
``americanfootball_nfl``. An ``mlb``/``nba``/``nhl``/``nfl`` shorthand is
also accepted and normalised.

No external network calls; only reads from the existing SQLite DB.
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Literal, Optional

logger = logging.getLogger("callisto.market_regime")

# ──────────────────────────────────────────────────────────────────────────
# Sport-key normalisation
# ──────────────────────────────────────────────────────────────────────────

_SPORT_ALIASES = {
    "mlb": "baseball_mlb",
    "baseball_mlb": "baseball_mlb",
    "nba": "basketball_nba",
    "basketball_nba": "basketball_nba",
    "nhl": "icehockey_nhl",
    "icehockey_nhl": "icehockey_nhl",
    "nfl": "americanfootball_nfl",
    "americanfootball_nfl": "americanfootball_nfl",
    "ncaab": "basketball_ncaab",
    "basketball_ncaab": "basketball_ncaab",
    "ncaaw": "basketball_ncaaw",
    "basketball_ncaaw": "basketball_ncaaw",
}


def _canonical_sport(sport: str) -> str:
    key = (sport or "").strip().lower()
    return _SPORT_ALIASES.get(key, key)


# ──────────────────────────────────────────────────────────────────────────
# Season-phase calendar (anchored to the current/next cycle). Boundaries
# are rough — good enough for regime bucketing. Each rule is (month, day).
# ``regular`` is the main season; ``playoffs`` covers post-season + finals
# unless a distinct ``championship`` phase is defined (NBA Finals, World
# Series). ``preseason`` is short. Everything else is ``offseason``.
#
# The calendar is evaluated in priority order: the first window that
# contains ``as_of`` wins. Windows are expressed as cyclic (month, day)
# ranges so we do not have to re-encode every year. NFL wraps over New
# Year — we handle that via per-sport helpers.
# ──────────────────────────────────────────────────────────────────────────

SeasonPhase = Literal[
    "preseason", "regular", "playoffs", "championship", "offseason"
]

_ALL_PHASES: tuple[SeasonPhase, ...] = (
    "preseason", "regular", "playoffs", "championship", "offseason",
)


@dataclass(frozen=True)
class _PhaseWindow:
    phase: SeasonPhase
    start_md: tuple[int, int]  # inclusive (month, day)
    end_md: tuple[int, int]    # inclusive (month, day)
    wraps_year: bool = False   # True if end is in the following calendar year


# Sport → ordered list of windows. Order matters: first match wins.
# Anything not matched defaults to ``offseason``.
_SPORT_CALENDAR: dict[str, list[_PhaseWindow]] = {
    "baseball_mlb": [
        _PhaseWindow("preseason", (3, 1), (3, 27)),
        _PhaseWindow("regular", (3, 28), (9, 30)),
        _PhaseWindow("playoffs", (10, 1), (10, 25)),
        _PhaseWindow("championship", (10, 26), (11, 5)),
    ],
    "basketball_nba": [
        _PhaseWindow("preseason", (9, 30), (10, 20)),
        _PhaseWindow("regular", (10, 21), (4, 15), wraps_year=True),
        _PhaseWindow("playoffs", (4, 16), (5, 31)),
        _PhaseWindow("championship", (6, 1), (6, 25)),
    ],
    "icehockey_nhl": [
        _PhaseWindow("preseason", (9, 20), (10, 5)),
        _PhaseWindow("regular", (10, 6), (4, 15), wraps_year=True),
        _PhaseWindow("playoffs", (4, 16), (6, 5)),
        _PhaseWindow("championship", (6, 6), (6, 25)),
    ],
    "americanfootball_nfl": [
        _PhaseWindow("preseason", (8, 1), (9, 3)),
        # Regular season runs Sep → early Jan of next year
        _PhaseWindow("regular", (9, 4), (1, 7), wraps_year=True),
        # Wild-card → conference championships → Super Bowl
        _PhaseWindow("playoffs", (1, 8), (2, 3), wraps_year=False),
        _PhaseWindow("championship", (2, 4), (2, 14)),
    ],
    "basketball_ncaab": [
        _PhaseWindow("preseason", (10, 25), (11, 5)),
        _PhaseWindow("regular", (11, 6), (3, 15), wraps_year=True),
        _PhaseWindow("playoffs", (3, 16), (4, 3)),
        _PhaseWindow("championship", (4, 4), (4, 10)),
    ],
    "basketball_ncaaw": [
        _PhaseWindow("preseason", (10, 25), (11, 5)),
        _PhaseWindow("regular", (11, 6), (3, 17), wraps_year=True),
        _PhaseWindow("playoffs", (3, 18), (4, 5)),
        _PhaseWindow("championship", (4, 6), (4, 12)),
    ],
}


# Noisy-window rules: phases (or specific (phase, last_N_days_of_phase))
# where Callisto should refuse new live bets. Kept conservative: we only
# flag regimes with well-known structural noise (lineup shenanigans,
# tank-heavy late regular season in the NBA, last week of MLB regular
# season where September call-ups and playoff-locked rest warp lineups).
# Preseason is also treated as too noisy to bet live.
_NOISY_RULES: dict[str, list[tuple[SeasonPhase, Optional[int]]]] = {
    "baseball_mlb": [
        ("preseason", None),
        # Last 7 days of regular season — Sept call-ups + rest for locked teams
        ("regular", 7),
    ],
    "basketball_nba": [
        ("preseason", None),
        # Last 10 days — tanking is structural once seeding is locked
        ("regular", 10),
    ],
    "icehockey_nhl": [
        ("preseason", None),
        ("regular", 7),
    ],
    "americanfootball_nfl": [
        ("preseason", None),
        # Week-18 (last 7 days of regular) resting regulars is systematic
        ("regular", 7),
    ],
    "basketball_ncaab": [("preseason", None)],
    "basketball_ncaaw": [("preseason", None)],
}


# ──────────────────────────────────────────────────────────────────────────
# Dataclass
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class MarketRegime:
    sport: str
    as_of: date
    season_phase: SeasonPhase
    week_of_season: Optional[int]
    days_into_phase: int
    phase_length_days: int
    num_games_today: int
    num_games_last_7d: int
    historical_roi_prior: Optional[float]
    historical_clv_prior: Optional[float]
    volatility_estimate: Optional[float]
    confidence: float
    # extras for downstream sanity-checking (not required by the spec but
    # cheap to include)
    noisy_window: bool = False
    sample_sizes: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        return d


# ──────────────────────────────────────────────────────────────────────────
# Phase classification
# ──────────────────────────────────────────────────────────────────────────

def _md_to_ord(m: int, d: int) -> int:
    """Return an ordinal in [1, 366] for (month, day) comparisons."""
    return m * 100 + d


def _within_window(win: _PhaseWindow, as_of: date) -> bool:
    """Return True if ``as_of`` falls inside ``win`` on the cyclic calendar."""
    lo = _md_to_ord(*win.start_md)
    hi = _md_to_ord(*win.end_md)
    today = _md_to_ord(as_of.month, as_of.day)
    if win.wraps_year:
        # e.g. Oct 21 → Apr 15: match if today >= lo OR today <= hi
        return today >= lo or today <= hi
    return lo <= today <= hi


def _phase_bounds_for(
    win: _PhaseWindow, as_of: date
) -> tuple[date, date]:
    """Concrete (start_date, end_date) for ``win`` anchored around ``as_of``.

    Needed so we can compute ``days_into_phase`` and ``phase_length_days``
    with real calendar arithmetic, not month/day tuples. Handles year wrap.
    """
    yr = as_of.year
    try:
        start = date(yr, *win.start_md)
    except ValueError:
        start = date(yr, win.start_md[0], 28)
    if win.wraps_year:
        try:
            end = date(yr + 1, *win.end_md)
        except ValueError:
            end = date(yr + 1, win.end_md[0], 28)
        if as_of < start:
            # we are inside the tail (Jan–Apr 15 of a season that began last year)
            start = date(yr - 1, *win.start_md)
            end = date(yr, *win.end_md)
    else:
        try:
            end = date(yr, *win.end_md)
        except ValueError:
            end = date(yr, win.end_md[0], 28)
    return start, end


def _classify_phase(
    sport: str, as_of: date
) -> tuple[SeasonPhase, Optional[_PhaseWindow], Optional[tuple[date, date]]]:
    """Return (phase, window, (start, end)) for ``sport`` on ``as_of``.

    Unknown sports return ``("offseason", None, None)``. Unmatched dates
    return ``("offseason", None, None)`` as well.
    """
    cal = _SPORT_CALENDAR.get(sport)
    if not cal:
        return "offseason", None, None
    for win in cal:
        if _within_window(win, as_of):
            return win.phase, win, _phase_bounds_for(win, as_of)
    return "offseason", None, None


def _week_of_season(
    phase: SeasonPhase, bounds: Optional[tuple[date, date]], as_of: date
) -> Optional[int]:
    if phase not in ("regular", "preseason") or bounds is None:
        return None
    start, _ = bounds
    delta = (as_of - start).days
    if delta < 0:
        return None
    return (delta // 7) + 1


# ──────────────────────────────────────────────────────────────────────────
# DB helpers (read-only)
# ──────────────────────────────────────────────────────────────────────────

def _db_path() -> str:
    return os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


def _open_readonly(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open the Callisto DB read-only via ``file:...?mode=ro`` URI."""
    path = db_path or _db_path()
    # Normalise to an absolute path so ?mode=ro works regardless of cwd.
    abs_path = os.path.abspath(path)
    uri = f"file:{abs_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _count_games(
    conn: sqlite3.Connection, sport: str, start: date, end: date
) -> int:
    """Count distinct (home,away,date) from game_contexts in [start,end]."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM (
            SELECT DISTINCT game_date, home_team, away_team
            FROM game_contexts
            WHERE sport = ?
              AND game_date BETWEEN ? AND ?
        )
        """,
        (sport, start.isoformat(), end.isoformat()),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def _prior_phase_window(
    phase: SeasonPhase, bounds: Optional[tuple[date, date]]
) -> Optional[tuple[date, date]]:
    """Return the same phase a year earlier (for ROI/CLV priors)."""
    if bounds is None or phase == "offseason":
        return None
    start, end = bounds
    try:
        return date(start.year - 1, start.month, start.day), date(
            end.year - 1, end.month, end.day
        )
    except ValueError:
        return None


def _prior_roi_clv(
    conn: sqlite3.Connection,
    sport: str,
    prior_window: Optional[tuple[date, date]],
) -> tuple[Optional[float], Optional[float], int]:
    """Compute mean ROI and mean CLV across backtest_events for the same phase
    a year earlier. Returns (roi, clv, n_events).

    ROI is approximated as per-event PnL where win pays ``book_odds_american``
    decimal profit and loss pays -1. We bucket by ``game_date``. CLV prior
    uses backtest_events.clv_implied - book_implied_prob (edge-preserving)
    when both are present; that is the existing convention in clv_log for
    aggregated metrics.
    """
    if prior_window is None:
        return None, None, 0
    start, end = prior_window
    rows = conn.execute(
        """
        SELECT book_odds_american, book_implied_prob, clv_implied, actual_result
        FROM backtest_events
        WHERE sport = ?
          AND actual_result IS NOT NULL
          AND actual_result != ''
          AND game_date BETWEEN ? AND ?
        """,
        (sport, start.isoformat(), end.isoformat()),
    ).fetchall()
    if not rows:
        return None, None, 0

    def _amer_to_profit(american: Optional[int]) -> float:
        if american is None:
            return 0.0
        if american >= 100:
            return american / 100.0
        if american <= -100:
            return 100.0 / abs(american)
        return 0.0

    # Callisto uses both 'win'/'loss' (paper_trades) and 'won'/'lost'
    # (backtest_events). Normalise so priors include the backtest history.
    _WIN = {"win", "won"}
    _LOSS = {"loss", "lost"}
    _PUSH = {"push", "pushed"}

    pnls: list[float] = []
    clv_deltas: list[float] = []
    for r in rows:
        res = (r["actual_result"] or "").lower()
        if res in _WIN:
            pnls.append(_amer_to_profit(r["book_odds_american"]))
        elif res in _LOSS:
            pnls.append(-1.0)
        elif res in _PUSH:
            pnls.append(0.0)
        # ignore everything else (void, pending)
        if r["clv_implied"] is not None and r["book_implied_prob"] is not None:
            clv_deltas.append(float(r["clv_implied"]) - float(r["book_implied_prob"]))
    roi = (sum(pnls) / len(pnls)) if pnls else None
    clv = (sum(clv_deltas) / len(clv_deltas)) if clv_deltas else None
    return roi, clv, len(rows)


def _prior_volatility(
    conn: sqlite3.Connection,
    sport: str,
    prior_window: Optional[tuple[date, date]],
) -> tuple[Optional[float], int]:
    """Stdev of per-day ROI across paper_trades during the prior phase."""
    if prior_window is None:
        return None, 0
    start, end = prior_window
    rows = conn.execute(
        """
        SELECT game_date, hypothetical_pnl
        FROM paper_trades
        WHERE sport = ?
          AND hypothetical_pnl IS NOT NULL
          AND game_date BETWEEN ? AND ?
        """,
        (sport, start.isoformat(), end.isoformat()),
    ).fetchall()
    if not rows:
        return None, 0
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["game_date"], []).append(float(r["hypothetical_pnl"]))
    daily = [sum(v) / len(v) for v in by_day.values() if v]
    if len(daily) < 2:
        return None, len(rows)
    mean = sum(daily) / len(daily)
    var = sum((x - mean) ** 2 for x in daily) / (len(daily) - 1)
    return math.sqrt(var), len(rows)


# ──────────────────────────────────────────────────────────────────────────
# Confidence
# ──────────────────────────────────────────────────────────────────────────

def _confidence(
    phase: SeasonPhase,
    days_into_phase: int,
    phase_length_days: int,
    n_prior_events: int,
    n_games_7d: int,
) -> float:
    """Heuristic in [0, 1].

    Drops below 0.5 in the first ~14 days of a new phase (spec requirement),
    scales with how much prior-year data we have for the same phase, and
    scales with current-phase activity (games in the last 7 days).
    """
    if phase == "offseason":
        return 0.25

    # Early-phase penalty: linear ramp from 0.3 → 1.0 over the first 14 days
    ramp_days = 14
    if days_into_phase < 0:
        ramp_factor = 0.3
    elif days_into_phase >= ramp_days:
        ramp_factor = 1.0
    else:
        ramp_factor = 0.3 + 0.7 * (days_into_phase / ramp_days)

    # Prior sample weight: cap at 200 events for full credit
    prior_weight = min(1.0, n_prior_events / 200.0)
    # Activity weight: even a handful of games this week saturates it fast
    activity_weight = min(1.0, n_games_7d / 20.0)

    # Compose; early-phase penalty dominates by design so the spec's
    # "first two weeks → confidence < 0.5" holds even with heavy priors.
    base = 0.3 + 0.5 * prior_weight + 0.2 * activity_weight
    conf = base * ramp_factor
    return max(0.0, min(1.0, conf))


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

def detect_regime(
    sport: str,
    as_of: Optional[date] = None,
    *,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> MarketRegime:
    """Return the current :class:`MarketRegime` for ``sport`` on ``as_of``.

    ``conn`` lets callers (and tests) inject a pre-built read-only
    connection. Otherwise the function opens the Callisto DB in
    read-only mode and closes it before returning.
    """
    as_of = as_of or date.today()
    sport_norm = _canonical_sport(sport)

    phase, win, bounds = _classify_phase(sport_norm, as_of)
    if bounds is None:
        days_into = 0
        phase_len = 0
    else:
        start, end = bounds
        days_into = (as_of - start).days
        phase_len = (end - start).days + 1

    owns_conn = conn is None
    if owns_conn:
        try:
            conn = _open_readonly(db_path)
        except sqlite3.Error as exc:
            logger.warning("market_regime: DB open failed (%s); returning zeros", exc)
            conn = None

    num_games_today = 0
    num_games_last_7d = 0
    roi_prior: Optional[float] = None
    clv_prior: Optional[float] = None
    vol_est: Optional[float] = None
    sample_sizes: dict[str, int] = {}

    if conn is not None:
        try:
            num_games_today = _count_games(conn, sport_norm, as_of, as_of)
            num_games_last_7d = _count_games(
                conn, sport_norm, as_of - timedelta(days=7), as_of
            )
            prior_win = _prior_phase_window(phase, bounds)
            roi_prior, clv_prior, n_prior_events = _prior_roi_clv(
                conn, sport_norm, prior_win
            )
            vol_est, n_prior_paper = _prior_volatility(
                conn, sport_norm, prior_win
            )
            sample_sizes = {
                "prior_backtest_events": n_prior_events,
                "prior_paper_trades": n_prior_paper,
                "games_last_7d": num_games_last_7d,
            }
        except sqlite3.Error as exc:
            logger.warning("market_regime: DB query failed (%s)", exc)
        finally:
            if owns_conn:
                try:
                    conn.close()
                except Exception:
                    pass

    n_prior_events = sample_sizes.get("prior_backtest_events", 0)
    conf = _confidence(
        phase, days_into, phase_len, n_prior_events, num_games_last_7d
    )

    regime = MarketRegime(
        sport=sport_norm,
        as_of=as_of,
        season_phase=phase,
        week_of_season=_week_of_season(phase, bounds, as_of),
        days_into_phase=max(0, days_into),
        phase_length_days=phase_len,
        num_games_today=num_games_today,
        num_games_last_7d=num_games_last_7d,
        historical_roi_prior=roi_prior,
        historical_clv_prior=clv_prior,
        volatility_estimate=vol_est,
        confidence=conf,
        noisy_window=_is_noisy_phase(sport_norm, phase, days_into, phase_len),
        sample_sizes=sample_sizes,
    )
    return regime


def _is_noisy_phase(
    sport: str, phase: SeasonPhase, days_into: int, phase_len: int
) -> bool:
    rules = _NOISY_RULES.get(sport)
    if not rules:
        return False
    for rule_phase, last_n in rules:
        if rule_phase != phase:
            continue
        if last_n is None:
            return True
        # flag only during the last `last_n` days of the phase
        if phase_len > 0 and days_into >= max(0, phase_len - last_n):
            return True
    return False


def current_regime_multiplier(
    sport: str,
    as_of: Optional[date] = None,
    *,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> float:
    """Return a scalar in [0.5, 1.5] that a portfolio sizer *could* use.

    Heuristic:
        * Offseason / preseason → 0.5 (too noisy / thin)
        * Noisy-window regimes → 0.5
        * Low confidence (< 0.4) → 0.7
        * Favourable regime: prior ROI > 2% AND prior CLV > 0.005 → 1.25
        * Very favourable (ROI > 5%) → 1.5
        * Otherwise → 1.0
    """
    r = detect_regime(sport, as_of, db_path=db_path, conn=conn)
    if r.season_phase in ("offseason", "preseason"):
        return 0.5
    if r.noisy_window:
        return 0.5
    if r.confidence < 0.4:
        return 0.7
    roi = r.historical_roi_prior
    clv = r.historical_clv_prior
    if roi is not None and clv is not None:
        if roi >= 0.05 and clv >= 0.005:
            return 1.5
        if roi >= 0.02 and clv >= 0.005:
            return 1.25
        if roi <= -0.03:
            return 0.75
    return 1.0


def regime_safe_for_trading(
    sport: str,
    as_of: Optional[date] = None,
    *,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Return ``False`` during known-noisy regimes.

    Conservative by construction: we only flag windows with well-known
    structural noise. A regime with low prior ROI is *not* automatically
    unsafe — sizing takes care of that via :func:`current_regime_multiplier`.
    """
    r = detect_regime(sport, as_of, db_path=db_path, conn=conn)
    if r.season_phase == "offseason":
        return False
    if r.season_phase == "preseason":
        return False
    if r.noisy_window:
        return False
    return True


__all__ = [
    "MarketRegime",
    "SeasonPhase",
    "detect_regime",
    "current_regime_multiplier",
    "regime_safe_for_trading",
]
