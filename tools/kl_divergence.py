"""
KL-divergence on line movements — quantify market information flow.

Measures how much the betting market "learned" between opening and closing lines.
High KL = significant price discovery (sharp info flowed in or news broke).
Low KL = stable market (prices barely moved, less likely to find edges).

Two metrics:
  - KL(closing || opening): directed divergence from opening to closing
  - Jensen-Shannon: symmetric divergence (sqrt of JS divergence = JS distance)

Applied per-game per-market to classify market efficiency. Feeds into
edge_confidence as a feature: high-KL games have more informed markets,
so edges detected on those games are more likely already priced in.
"""

import logging
import math
import os
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.kl_divergence")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Epsilon for smoothing to avoid log(0)
_EPS = 1e-10


def kl_divergence(p: list[float], q: list[float]) -> float:
    """
    KL(P || Q) — Kullback-Leibler divergence from Q to P.

    Measures how much information is lost when Q is used to approximate P.
    P = closing line distribution (what the market settled on)
    Q = opening line distribution (where the market started)

    Both inputs are probability distributions (should sum to ~1.0).
    Uses epsilon smoothing to handle zeros.

    Returns:
        KL divergence in nats (>= 0). Higher = more information gained.
    """
    if len(p) != len(q) or not p:
        return 0.0
    total_p = sum(p)
    total_q = sum(q)
    if total_p <= 0 or total_q <= 0:
        return 0.0
    # Normalize
    p_norm = [x / total_p for x in p]
    q_norm = [x / total_q for x in q]
    # Smooth and compute
    kl = 0.0
    for pi, qi in zip(p_norm, q_norm):
        pi = max(pi, _EPS)
        qi = max(qi, _EPS)
        kl += pi * math.log(pi / qi)
    return max(0.0, kl)


def jensen_shannon(p: list[float], q: list[float]) -> float:
    """
    Jensen-Shannon divergence — symmetric version of KL.

    JS(P, Q) = 0.5 * KL(P || M) + 0.5 * KL(Q || M)
    where M = 0.5 * (P + Q)

    Returns:
        JS divergence (0 to ln(2) ≈ 0.693). 0 = identical distributions.
    """
    if len(p) != len(q) or not p:
        return 0.0
    total_p = sum(p)
    total_q = sum(q)
    if total_p <= 0 or total_q <= 0:
        return 0.0
    p_norm = [x / total_p for x in p]
    q_norm = [x / total_q for x in q]
    m = [0.5 * (pi + qi) for pi, qi in zip(p_norm, q_norm)]
    return 0.5 * kl_divergence(p_norm, m) + 0.5 * kl_divergence(q_norm, m)


def shannon_entropy(probs: list[float]) -> float:
    """Shannon entropy of a distribution (nats)."""
    total = sum(probs)
    if total <= 0:
        return 0.0
    normalized = [p / total for p in probs if p > 0]
    return -sum(p * math.log(p) for p in normalized if p > 0)


async def compute_game_kl(
    db_path: str,
    event_id: str,
    sport: str,
    market_type: str = "h2h",
) -> Optional[dict]:
    """
    Compute KL-divergence between opening and closing lines for a game.

    Uses odds_snapshots to find earliest (opening) and latest (closing)
    implied probability distributions across books.

    Returns dict with metrics, or None if insufficient data.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")

        # Find snapshots for this event — earliest and latest
        # MUST filter by event_id to avoid comparing different games' odds
        cursor = await db.execute(
            "SELECT snapshot_json, timestamp FROM odds_snapshots "
            "WHERE sport = ? AND event_id = ? ORDER BY timestamp ASC LIMIT 1",
            (sport, event_id),
        )
        opening_row = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT snapshot_json, timestamp FROM odds_snapshots "
            "WHERE sport = ? AND event_id = ? ORDER BY timestamp DESC LIMIT 1",
            (sport, event_id),
        )
        closing_row = await cursor.fetchone()

        if not opening_row or not closing_row:
            return None
        if opening_row[1] == closing_row[1]:
            return None  # Same snapshot, can't compute divergence

    import json

    # Extract implied probabilities per book from opening snapshot
    def extract_implied_probs(snapshot_json: str, game_id: str, mkt: str) -> list[float]:
        """Extract implied probabilities for home team from snapshot."""
        try:
            data = json.loads(snapshot_json)
        except (json.JSONDecodeError, TypeError):
            return []
        probs = []
        for game in data.get("games", []):
            if game.get("id") != game_id:
                continue
            for bm in game.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market.get("key") != mkt:
                        continue
                    outcomes = market.get("outcomes", [])
                    if outcomes:
                        # Use first outcome's price as implied prob
                        price = outcomes[0].get("price", 0)
                        if price != 0:
                            if price > 0:
                                prob = 100 / (price + 100)
                            else:
                                prob = abs(price) / (abs(price) + 100)
                            probs.append(prob)
        return probs

    opening_probs = extract_implied_probs(opening_row[0], event_id, market_type)
    closing_probs = extract_implied_probs(closing_row[0], event_id, market_type)

    if len(opening_probs) < 2 or len(closing_probs) < 2:
        return None

    # Normalize to same length (use min of both)
    n = min(len(opening_probs), len(closing_probs))
    opening_probs = sorted(opening_probs)[:n]
    closing_probs = sorted(closing_probs)[:n]

    kl = kl_divergence(closing_probs, opening_probs)
    js = jensen_shannon(closing_probs, opening_probs)

    return {
        "event_id": event_id,
        "sport": sport,
        "market_type": market_type,
        "kl_divergence": round(kl, 6),
        "js_divergence": round(js, 6),
        "n_books": n,
        "opening_entropy": round(shannon_entropy(opening_probs), 6),
        "closing_entropy": round(shannon_entropy(closing_probs), 6),
    }


async def store_kl_metrics(db_path: str, metrics: list[dict]) -> int:
    """Persist KL metrics to kl_metrics table. Returns count stored."""
    if not metrics:
        return 0
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        count = 0
        for m in metrics:
            try:
                await db.execute(
                    "INSERT OR REPLACE INTO kl_metrics "
                    "(sport, event_id, market_type, kl_divergence, js_divergence, "
                    "n_books, opening_entropy, closing_entropy) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        m["sport"], m["event_id"], m["market_type"],
                        m["kl_divergence"], m["js_divergence"],
                        m["n_books"], m["opening_entropy"], m["closing_entropy"],
                    ),
                )
                count += 1
            except Exception as e:
                logger.debug(f"KL metric store error: {e}")
        await db.commit()
    return count
