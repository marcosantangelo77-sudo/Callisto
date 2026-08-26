"""Movement tracking for the line monitor.

Extracted from tools/line_monitor.py:
- significant-movement filtering against configured thresholds
- movement persistence to the line_movements table
- KL divergence computation between consecutive snapshots

LineMonitor delegates here; the import path tools.line_monitor.LineMonitor
is unchanged.
"""

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from tools.kl_divergence import kl_divergence, jensen_shannon, shannon_entropy, store_kl_metrics

logger = logging.getLogger("callisto.lines.movement")

# Movement thresholds — what counts as "significant"
# Tightened from 10/1.0: at -110, +5 is ~2% implied prob change which
# is a meaningful sharp move. 0.5 captures key-number crosses (3, 7) that
# can flip cover probability by 5-10%.
PRICE_MOVEMENT_THRESHOLD = 5     # American odds points (~2% implied prob)
POINT_MOVEMENT_THRESHOLD = 0.5   # Spread/total half-points (key number sensitivity)


def filter_significant(movements: list[dict]) -> list[dict]:
    """Keep only movements exceeding price or point movement thresholds."""
    return [
        m for m in movements
        if abs(m["price_movement"]) >= PRICE_MOVEMENT_THRESHOLD
        or abs(m["point_movement"]) >= POINT_MOVEMENT_THRESHOLD
    ]


class MovementRecorder:
    """Persist significant line movements and keep an in-memory alert ring."""

    def __init__(self, execute_with_retry, commit_with_retry):
        # Injected to avoid a hard tools.db_utils dependency at import time.
        self._exec = execute_with_retry
        self._commit = commit_with_retry
        self.alerts: list[dict] = []

    def _append_alert(self, alert: dict) -> None:
        self.alerts.append(alert)
        if len(self.alerts) > 100:
            self.alerts = self.alerts[-100:]

    async def record(self, db, sport: str, movement: dict) -> None:
        """Record a line movement to the database."""
        now = datetime.now(timezone.utc).isoformat()
        await self._exec(
            db,
            "INSERT INTO line_movements "
            "(sport, detected_at, team, market, bookmaker, old_price, new_price, "
            "price_movement, old_point, new_point, point_movement, direction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sport, now, movement["team"], movement["market"],
                movement["bookmaker"], movement["old_price"], movement["new_price"],
                movement["price_movement"], movement.get("old_point"),
                movement.get("new_point"), movement.get("point_movement", 0),
                movement["direction"],
            ),
            max_retries=5,
            operation=f"line_movement insert {sport}",
        )
        await self._commit(db, max_retries=5, operation=f"line_movement commit {sport}")

        self._append_alert({
            "sport": sport,
            "detected_at": now,
            **movement,
        })


class KLDivergenceTracker:
    """Compute and cache KL divergence between consecutive snapshots."""

    def __init__(self, db_path: str, cache_max: int = 2000):
        self.db_path = db_path
        self.cache: dict[str, dict] = {}
        self.CACHE_MAX = cache_max

    async def compute_and_store(
        self, sport: str, old_snapshot: dict, new_snapshot: dict,
    ) -> int:
        """Compute KL divergence between two consecutive snapshots per game.

        For each game present in both snapshots, extract implied probability
        distributions from each bookmaker and compute KL(new || old) and
        Jensen-Shannon divergence. High KL = significant price discovery
        between snapshots. Stores results in kl_metrics table.

        Also caches latest KL per (sport, event_id) in memory for fast
        lookups by edge_confidence scoring. Returns number of metrics stored.
        """
        try:
            old_games = {g.get("id"): g for g in old_snapshot.get("games", []) if g.get("id")}
            new_games = {g.get("id"): g for g in new_snapshot.get("games", []) if g.get("id")}

            common_ids = set(old_games.keys()) & set(new_games.keys())
            if not common_ids:
                return 0

            metrics_batch = []
            for event_id in common_ids:
                old_game = old_games[event_id]
                new_game = new_games[event_id]

                for market_type in ("h2h", "spreads", "totals"):
                    old_probs = extract_probs(old_game, market_type)
                    new_probs = extract_probs(new_game, market_type)

                    if len(old_probs) < 2 or len(new_probs) < 2:
                        continue

                    # Normalize to same length (use min of both)
                    n = min(len(old_probs), len(new_probs))
                    old_sorted = sorted(old_probs)[:n]
                    new_sorted = sorted(new_probs)[:n]

                    kl = kl_divergence(new_sorted, old_sorted)
                    js = jensen_shannon(new_sorted, old_sorted)

                    # Only store if there's meaningful divergence
                    if kl < 1e-8 and js < 1e-8:
                        continue

                    metric = {
                        "event_id": event_id,
                        "sport": sport,
                        "market_type": market_type,
                        "kl_divergence": round(kl, 6),
                        "js_divergence": round(js, 6),
                        "n_books": n,
                        "opening_entropy": round(shannon_entropy(old_sorted), 6),
                        "closing_entropy": round(shannon_entropy(new_sorted), 6),
                    }
                    metrics_batch.append(metric)

                    # Cache in memory for edge_confidence lookups (capped)
                    cache_key = f"{sport}:{event_id}:{market_type}"
                    if len(self.cache) >= self.CACHE_MAX:
                        # Evict ~20% oldest entries
                        evict_n = self.CACHE_MAX // 5
                        for _ in range(evict_n):
                            try:
                                self.cache.pop(next(iter(self.cache)))
                            except (StopIteration, KeyError):
                                break
                    self.cache[cache_key] = metric

            if metrics_batch:
                stored = await store_kl_metrics(self.db_path, metrics_batch)
                logger.info(
                    f"KL metrics {sport}: {stored} game-markets computed "
                    f"(max KL={max(m['kl_divergence'] for m in metrics_batch):.4f})"
                )
                return stored
            return 0

        except Exception as e:
            logger.warning(f"KL divergence computation failed for {sport}: {e}")
            return 0

    def get_for_game(self, sport: str, event_id: str, market_type: str = "h2h") -> Optional[dict]:
        """Look up cached KL metrics for a game. Used by edge_confidence scoring."""
        cache_key = f"{sport}:{event_id}:{market_type}"
        return self.cache.get(cache_key)


def extract_probs(game: dict, market_type: str) -> list[float]:
    """Extract implied probabilities for the first outcome across all bookmakers."""
    probs = []
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_type:
                continue
            outcomes = mkt.get("outcomes", [])
            if not outcomes:
                continue
            price = outcomes[0].get("price", 0)
            if price == 0:
                continue
            if price > 0:
                prob = 100.0 / (price + 100.0)
            else:
                prob = abs(price) / (abs(price) + 100.0)
            probs.append(prob)
    return probs


__all__ = [
    "PRICE_MOVEMENT_THRESHOLD",
    "POINT_MOVEMENT_THRESHOLD",
    "filter_significant",
    "MovementRecorder",
    "KLDivergenceTracker",
    "extract_probs",
]

# Type alias kept for signature documentation purposes only.
AsyncInsert = Callable[..., Awaitable[None]]
