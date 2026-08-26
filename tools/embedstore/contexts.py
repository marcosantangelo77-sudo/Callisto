"""High-level convenience embedders for the autonomous research loop.

Extracted from tools/embeddings.py.
"""

from typing import Optional

from tools.embedstore.client import embed_text
from tools.embedstore.store import VectorStore


async def embed_game_context(
    store: VectorStore,
    sport: str,
    game_date: str,
    home_team: str,
    away_team: str,
    context: dict,
) -> int:
    """
    Embed a game context into the 'game_contexts' collection.

    The context dict should contain structured info like:
      - injuries, rest days, travel, pace, defensive rating, etc.
      - prop lines and devigged fair values
      - final scores and key player stats

    We serialize it into a natural language description for embedding.
    """
    # Build a textual representation for embedding
    parts = [
        f"{sport} game on {game_date}: {away_team} at {home_team}",
    ]
    if context.get("home_score") is not None:
        parts.append(
            f"Final: {home_team} {context['home_score']}, "
            f"{away_team} {context['away_score']}"
        )
    if context.get("total"):
        parts.append(f"Total: {context['total']}")
    if context.get("spread"):
        parts.append(f"Spread: {home_team} {context['spread']}")
    if context.get("injuries"):
        parts.append(f"Injuries: {', '.join(context['injuries'][:5])}")
    if context.get("rest_days_home") is not None:
        parts.append(
            f"Rest: {home_team} {context['rest_days_home']}d, "
            f"{away_team} {context.get('rest_days_away', '?')}d"
        )
    if context.get("key_props"):
        for prop in context["key_props"][:5]:
            parts.append(
                f"Prop: {prop['player']} {prop['market']} "
                f"{prop.get('line', '?')} (fair={prop.get('fair_prob', '?')})"
            )

    text = " | ".join(parts)
    embedding = await embed_text(text)

    # Classify as historical or recent based on date
    data_period = "recent" if game_date >= "2026-02-23" else "historical"

    metadata = {
        "sport": sport,
        "game_date": game_date,
        "home_team": home_team,
        "away_team": away_team,
        "data_period": data_period,
        **{k: v for k, v in context.items() if k not in ("key_props", "injuries")},
    }

    return await store.store("game_contexts", text, embedding, metadata)


async def embed_prop_outcome(
    store: VectorStore,
    sport: str,
    game_date: str,
    player: str,
    market: str,
    line: float,
    fair_prob_over: float,
    book_implied_over: float,
    actual_stat: Optional[float] = None,
    context: Optional[dict] = None,
) -> int:
    """
    Embed a prop outcome for pattern discovery.

    Encodes: player, market type, line value, edge size, and whether
    the prop hit. This lets us find clusters of similar prop situations.
    """
    edge = fair_prob_over - book_implied_over
    hit = actual_stat > line if actual_stat is not None and line else None

    parts = [
        f"{sport} prop {game_date}: {player} {market} {line}",
        f"Fair over: {fair_prob_over:.3f}, Book implied: {book_implied_over:.3f}",
        f"Edge: {edge:.3f} ({edge*100:.1f}%)",
    ]
    if actual_stat is not None:
        parts.append(f"Actual: {actual_stat}, Hit: {hit}")
    if context:
        if context.get("minutes"):
            parts.append(f"Minutes: {context['minutes']}")
        if context.get("opponent"):
            parts.append(f"vs {context['opponent']}")
        if context.get("home_away"):
            parts.append(f"({context['home_away']})")

    text = " | ".join(parts)
    embedding = await embed_text(text)

    metadata = {
        "sport": sport,
        "game_date": game_date,
        "player": player,
        "market": market,
        "line": line,
        "fair_prob_over": fair_prob_over,
        "book_implied_over": book_implied_over,
        "edge": round(edge, 4),
        "actual_stat": actual_stat,
        "hit": hit,
        **(context or {}),
    }

    return await store.store("prop_outcomes", text, embedding, metadata)
