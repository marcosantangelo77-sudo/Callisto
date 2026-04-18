"""Sharp-market microstructure classifiers over per-book odds time series.

A professional sports-betting trader learns a set of heuristics that
distinguish *sharp* line movement from *public* line movement:

    • **First mover** — which book's line moved first on a given move?
      When Pinnacle moves before anyone else, the move is almost certainly
      sharp-driven; when DraftKings moves first on a side everyone is
      betting, the move is almost certainly public-money-driven.

    • **Steam** — when multiple books move in the same direction within a
      narrow time window (<60s typical). Steam almost always reflects
      real information (sharp syndicate, news, injury) because the
      alternative — that all books coincidentally misprice the same way
      at the same moment — is astronomically unlikely.

    • **Reverse line movement** (RLM) — the line moves *against* where the
      public is betting. The canonical example: 70% of tickets on the
      favourite, but the line drifts off the favourite. This means the
      sharp money is on the *other* side in dollar terms, and the books
      trust that money enough to move the line despite public action.

    • **Limit-down** — a book reducing its maximum accepted bet on a
      market, visible in odds-api.io Pro as a drop in the `limit` field.
      When a book limits down, it has either (a) decided the market is
      too efficient to quote aggressively, or (b) taken a one-sided hit
      and is de-risking. Both are information.

This module takes a list of ``LineTick`` records — one tick per
(book × outcome × timestamp) — and returns ``SharpSignal`` objects
describing any of the above events it detects. Pure functions, no I/O.

References:
  Buchdahl, J. "Squares and Sharps, Suckers and Sharks" (2016)
  Miller and Davidow, "The Logic of Sports Betting" (2019)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional


@dataclass(frozen=True)
class LineTick:
    """One snapshot of one book's price for one outcome."""
    book: str
    market_key: str                 # stable ID, e.g. f"{event_id}|{market}|{team}"
    implied_prob: float
    ts: datetime                    # timezone-aware
    limit: Optional[float] = None


@dataclass(frozen=True)
class SharpSignal:
    """One detected microstructure event."""
    kind: str                       # 'first_mover' | 'steam' | 'rlm' | 'limit_down'
    market_key: str
    direction: int                  # +1 if implied prob rose (team got shorter); -1 if fell
    first_book: Optional[str] = None
    participating_books: tuple[str, ...] = ()
    magnitude: float = 0.0          # size of move in implied-prob space
    ts_start: Optional[datetime] = None
    ts_end: Optional[datetime] = None
    note: str = ""


# ──────────────────────────────────────────────────────────────────────
# Primitive helpers
# ──────────────────────────────────────────────────────────────────────


def _sorted_by_ts(ticks: list[LineTick]) -> list[LineTick]:
    return sorted(ticks, key=lambda t: t.ts)


def _as_utc(dt: datetime) -> datetime:
    """Normalize to UTC; tolerate naive inputs by assuming they're UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _group_by_book(ticks: list[LineTick]) -> dict[str, list[LineTick]]:
    out: dict[str, list[LineTick]] = {}
    for t in ticks:
        out.setdefault(t.book, []).append(t)
    for k in out:
        out[k] = _sorted_by_ts(out[k])
    return out


def _diff_series(per_book: dict[str, list[LineTick]], min_move: float) -> list[tuple[str, datetime, float, int]]:
    """Return every (book, ts, magnitude, direction) move > min_move for any book.

    A "move" is one tick's implied prob differing from the prior tick's
    implied prob for the same book by more than ``min_move``. This ignores
    sub-resolution jitter that occurs when books re-round but don't
    actually change the line.
    """
    events: list[tuple[str, datetime, float, int]] = []
    for book, ticks in per_book.items():
        for i in range(1, len(ticks)):
            prev_p = ticks[i - 1].implied_prob
            curr_p = ticks[i].implied_prob
            diff = curr_p - prev_p
            if abs(diff) <= min_move:
                continue
            direction = 1 if diff > 0 else -1
            events.append((book, _as_utc(ticks[i].ts), abs(diff), direction))
    return events


# ──────────────────────────────────────────────────────────────────────
# First-mover detection
# ──────────────────────────────────────────────────────────────────────


def detect_first_mover(
    ticks: list[LineTick],
    *,
    min_move: float = 0.005,          # 0.5 implied-prob points
    lookback_window_s: int = 300,     # 5 min
) -> list[SharpSignal]:
    """Find cases where one book moved and others followed within
    ``lookback_window_s``.

    A first-mover event is emitted for the FIRST book to move whenever
    ≥ 1 other book subsequently moves in the same direction within the
    window. The signal is sharp if the first-mover is a known sharp
    book; it's still informative either way (you now know which book is
    fastest for this market).
    """
    if not ticks:
        return []
    per_book = _group_by_book(ticks)
    events = _diff_series(per_book, min_move)
    events.sort(key=lambda e: e[1])
    if len(events) < 2:
        return []

    window = timedelta(seconds=lookback_window_s)
    signals: list[SharpSignal] = []
    claimed_indices: set[int] = set()

    for i, (book_a, ts_a, mag_a, dir_a) in enumerate(events):
        if i in claimed_indices:
            continue
        followers: list[tuple[str, datetime, float]] = []
        for j in range(i + 1, len(events)):
            book_b, ts_b, mag_b, dir_b = events[j]
            if ts_b - ts_a > window:
                break
            if book_b == book_a:
                continue
            if dir_b != dir_a:
                continue
            followers.append((book_b, ts_b, mag_b))
            claimed_indices.add(j)
        if not followers:
            continue
        claimed_indices.add(i)
        books = (book_a,) + tuple(b for b, _, _ in followers)
        market_key = ticks[0].market_key
        signals.append(SharpSignal(
            kind="first_mover",
            market_key=market_key,
            direction=dir_a,
            first_book=book_a,
            participating_books=books,
            magnitude=mag_a + sum(m for _, _, m in followers) / max(len(followers), 1),
            ts_start=ts_a,
            ts_end=followers[-1][1],
            note=f"{book_a} moved first; {len(followers)} follower(s)",
        ))
    return signals


# ──────────────────────────────────────────────────────────────────────
# Steam detection
# ──────────────────────────────────────────────────────────────────────


def detect_steam_event(
    ticks: list[LineTick],
    *,
    min_move: float = 0.005,
    window_s: int = 60,
    min_books: int = 3,
) -> list[SharpSignal]:
    """Identify clusters where ≥ ``min_books`` distinct books move in the
    same direction within ``window_s``. This is the canonical steam
    definition used by professional sports traders.
    """
    if not ticks:
        return []
    per_book = _group_by_book(ticks)
    events = _diff_series(per_book, min_move)
    events.sort(key=lambda e: e[1])
    if len(events) < min_books:
        return []

    window = timedelta(seconds=window_s)
    signals: list[SharpSignal] = []
    i = 0
    while i < len(events):
        cluster: list[tuple[str, datetime, float, int]] = [events[i]]
        j = i + 1
        while j < len(events) and events[j][1] - cluster[0][1] <= window:
            cluster.append(events[j])
            j += 1
        same_dir = [c for c in cluster if c[3] == cluster[0][3]]
        unique_books = {c[0] for c in same_dir}
        if len(unique_books) >= min_books:
            signals.append(SharpSignal(
                kind="steam",
                market_key=ticks[0].market_key,
                direction=cluster[0][3],
                participating_books=tuple(sorted(unique_books)),
                magnitude=statistics.mean(c[2] for c in same_dir),
                ts_start=same_dir[0][1],
                ts_end=same_dir[-1][1],
                note=f"{len(unique_books)} books moved within {window_s}s",
            ))
            i = j                     # don't re-flag overlapping clusters
        else:
            i += 1
    return signals


# ──────────────────────────────────────────────────────────────────────
# Reverse line movement
# ──────────────────────────────────────────────────────────────────────


def detect_reverse_line_movement(
    ticks: list[LineTick],
    public_pct_on_side: float,
    *,
    min_move: float = 0.005,
) -> Optional[SharpSignal]:
    """Detect RLM: public money is >60% on one side, but the line drifted
    off that side (the opposite-side implied prob *rose*).

    ``public_pct_on_side`` is the fraction of *ticket* count on the
    outcome that the ``ticks`` are for. Source is typically Action
    Network or a book's "consensus" page.

    Returns at most one signal per call — the biggest net RLM in the
    supplied tick window.
    """
    if not ticks:
        return None
    if public_pct_on_side < 0.60:
        return None                                  # not enough lopsided public action to call this RLM

    per_book = _group_by_book(ticks)
    # Net drift across books. If the line drifted AWAY from the public side
    # (i.e., this outcome's implied prob dropped), RLM is firing.
    drifts: list[float] = []
    for book, book_ticks in per_book.items():
        if len(book_ticks) < 2:
            continue
        drifts.append(book_ticks[-1].implied_prob - book_ticks[0].implied_prob)
    if not drifts:
        return None
    net = statistics.mean(drifts)
    if abs(net) <= min_move:
        return None
    if net < 0:
        # Public-backed side lost implied prob → RLM.
        return SharpSignal(
            kind="rlm",
            market_key=ticks[0].market_key,
            direction=-1,
            participating_books=tuple(sorted(per_book.keys())),
            magnitude=abs(net),
            ts_start=_as_utc(min(t.ts for t in ticks)),
            ts_end=_as_utc(max(t.ts for t in ticks)),
            note=(
                f"Public {public_pct_on_side:.0%} on this side but line "
                f"drifted off by {abs(net):.4f} implied-prob points"
            ),
        )
    return None


# ──────────────────────────────────────────────────────────────────────
# Composite scan
# ──────────────────────────────────────────────────────────────────────


def scan_market_movement(
    ticks: list[LineTick],
    *,
    public_pct_on_side: Optional[float] = None,
    min_move: float = 0.005,
    steam_window_s: int = 60,
    steam_min_books: int = 3,
    first_mover_window_s: int = 300,
) -> list[SharpSignal]:
    """One-shot scan that returns every microstructure event in one call.

    Callers get a flat list of ``SharpSignal`` events which they can
    persist, use to re-score edges, or emit as real-time alerts.
    """
    signals: list[SharpSignal] = []
    signals.extend(detect_steam_event(
        ticks,
        min_move=min_move,
        window_s=steam_window_s,
        min_books=steam_min_books,
    ))
    signals.extend(detect_first_mover(
        ticks,
        min_move=min_move,
        lookback_window_s=first_mover_window_s,
    ))
    if public_pct_on_side is not None:
        rlm = detect_reverse_line_movement(
            ticks,
            public_pct_on_side=public_pct_on_side,
            min_move=min_move,
        )
        if rlm is not None:
            signals.append(rlm)

    # Limit-down: emit a signal whenever the latest tick's limit is a
    # material fraction lower than the earliest tick's limit for the same
    # book. Useful for detecting "book knows something" moments.
    per_book = _group_by_book(ticks)
    for book, book_ticks in per_book.items():
        limits = [t.limit for t in book_ticks if t.limit is not None]
        if len(limits) >= 2 and limits[0] > 0:
            drop = (limits[0] - limits[-1]) / limits[0]
            if drop >= 0.5:
                signals.append(SharpSignal(
                    kind="limit_down",
                    market_key=ticks[0].market_key,
                    direction=0,
                    first_book=book,
                    participating_books=(book,),
                    magnitude=drop,
                    ts_start=_as_utc(book_ticks[0].ts),
                    ts_end=_as_utc(book_ticks[-1].ts),
                    note=f"{book} reduced limit by {drop:.0%}",
                ))
    return signals
