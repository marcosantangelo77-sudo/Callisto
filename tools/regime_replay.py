"""
regime_replay — Historical per-regime performance replay.

Most hypotheses look profitable in aggregate but the edge lives in one
specific regime (e.g. MLB regular season) and vanishes (or reverses) in
another (playoffs, early-season calibration window). This module takes
a hypothesis_id and buckets its historical paper/backtest trades by
regime key, returning per-regime ROI / hit-rate / sample size.

Read-only. No schema changes. No integration.

Usage::

    from tools.regime_replay import replay_hypothesis
    stats = replay_hypothesis("23e25b03-d03")
    # {'baseball_mlb:regular': {'roi': 0.04, 'hit_rate': 0.55, 'n': 137},
    #  'baseball_mlb:playoffs': {'roi': -0.11, 'hit_rate': 0.43, 'n': 14}, ...}

By default the function reads both :table:`backtest_events` and
:table:`paper_trades` and concatenates them — paper trades are scored the
same way (win = +decimal_profit, loss = -1). Callers can restrict the
source via ``sources=("backtest",)`` or ``sources=("paper",)``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from tools.market_regime import _canonical_sport, _classify_phase

logger = logging.getLogger("callisto.regime_replay")


def _open_readonly(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    abs_path = os.path.abspath(path)
    conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _amer_to_profit(american: Optional[int]) -> float:
    if american is None:
        return 0.0
    if american >= 100:
        return american / 100.0
    if american <= -100:
        return 100.0 / abs(american)
    return 0.0


@dataclass
class _Trade:
    sport: str
    game_date: date
    odds_american: Optional[int]
    result: str  # 'win' | 'loss' | 'push' | other


def _fetch_trades(
    conn: sqlite3.Connection,
    hypothesis_id: str,
    sources: Iterable[str],
) -> list[_Trade]:
    out: list[_Trade] = []
    sources = tuple(s.lower() for s in sources)

    if "backtest" in sources:
        rows = conn.execute(
            """
            SELECT sport, game_date, book_odds_american, actual_result
            FROM backtest_events
            WHERE hypothesis_id = ?
              AND signal_generated = 1
              AND actual_result IS NOT NULL
            """,
            (hypothesis_id,),
        ).fetchall()
        for r in rows:
            try:
                gd = date.fromisoformat(str(r["game_date"])[:10])
            except ValueError:
                continue
            out.append(
                _Trade(
                    sport=_canonical_sport(r["sport"] or ""),
                    game_date=gd,
                    odds_american=r["book_odds_american"],
                    result=(r["actual_result"] or "").lower(),
                )
            )

    if "paper" in sources:
        rows = conn.execute(
            """
            SELECT sport, game_date, signal_odds_american, actual_result
            FROM paper_trades
            WHERE hypothesis_id = ?
              AND actual_result IS NOT NULL
            """,
            (hypothesis_id,),
        ).fetchall()
        for r in rows:
            try:
                gd = date.fromisoformat(str(r["game_date"])[:10])
            except ValueError:
                continue
            out.append(
                _Trade(
                    sport=_canonical_sport(r["sport"] or ""),
                    game_date=gd,
                    odds_american=r["signal_odds_american"],
                    result=(r["actual_result"] or "").lower(),
                )
            )

    return out


def _bucket_key(trade: _Trade) -> str:
    phase, _, _ = _classify_phase(trade.sport, trade.game_date)
    return f"{trade.sport}:{phase}"


def replay_hypothesis(
    hypothesis_id: str,
    *,
    db_path: Optional[str] = None,
    sources: Iterable[str] = ("backtest", "paper"),
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, dict[str, float]]:
    """Return ``{regime_key: {'roi', 'hit_rate', 'n'}}`` for ``hypothesis_id``.

    ``regime_key`` has the form ``"<sport>:<phase>"``, e.g.
    ``"baseball_mlb:regular"``. Rows with unknown sport or unparseable date
    are skipped. Pushes count in ``n`` but contribute 0 to ROI and are
    excluded from the hit-rate denominator.
    """
    owns = conn is None
    if owns:
        try:
            conn = _open_readonly(db_path)
        except sqlite3.Error as exc:
            logger.warning("regime_replay: DB open failed (%s)", exc)
            return {}

    try:
        trades = _fetch_trades(conn, hypothesis_id, sources)
    except sqlite3.Error as exc:
        logger.warning("regime_replay: query failed (%s)", exc)
        trades = []
    finally:
        if owns and conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    buckets: dict[str, list[_Trade]] = {}
    for t in trades:
        buckets.setdefault(_bucket_key(t), []).append(t)

    # Callisto uses both 'win'/'loss' (paper_trades) and 'won'/'lost'
    # (backtest_events) — normalise here so downstream buckets merge.
    _WIN = {"win", "won"}
    _LOSS = {"loss", "lost"}
    _PUSH = {"push", "pushed"}

    out: dict[str, dict[str, float]] = {}
    for key, items in buckets.items():
        pnls: list[float] = []
        wins = 0
        resolved = 0  # wins + losses, for hit-rate denominator
        for t in items:
            if t.result in _WIN:
                pnls.append(_amer_to_profit(t.odds_american))
                wins += 1
                resolved += 1
            elif t.result in _LOSS:
                pnls.append(-1.0)
                resolved += 1
            elif t.result in _PUSH:
                pnls.append(0.0)
            # ignore pending/void
        roi = (sum(pnls) / len(pnls)) if pnls else 0.0
        hit_rate = (wins / resolved) if resolved else 0.0
        out[key] = {
            "roi": round(roi, 4),
            "hit_rate": round(hit_rate, 4),
            "n": len(items),
            "resolved": resolved,
        }
    return out


__all__ = ["replay_hypothesis"]
