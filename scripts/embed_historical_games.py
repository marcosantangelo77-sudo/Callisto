"""
Embed ALL historical game_results (2023-2026) into the game_contexts embedding collection.

Current state: only 1,060 recent games (Feb 23 - Mar 23, 2026) are embedded.
game_results has 5,697 entries going back to 2023. This script embeds the ~4,637
missing games so the hypothesis generator can cluster on the full history.

Approach:
  1. Load all game_results from SQLite
  2. Generate embedding text in the same format as embed_game_context()
  3. Use content_hash dedup (INSERT OR IGNORE) to skip already-embedded games
  4. Embed via Ollama nomic-embed-text in batches of 50
  5. Tag metadata with data_period: "historical" (<2026-02-23) or "recent" (>=2026-02-23)
  6. Track and print progress every 100 games
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time

# Project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
BATCH_SIZE = 50  # Ollama batch size — safe for CPU/GPU memory
RECENT_CUTOFF = "2026-02-23"  # Games before this are "historical"


def content_hash(text: str) -> str:
    """SHA-256 hash of content for dedup — matches tools/embeddings.py _content_hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_embedding_text(game: dict) -> str:
    """
    Build the same text format as embed_game_context() in tools/embeddings.py.

    Historical games have: sport, game_date, home_team, away_team, home_score,
    away_score, total_score, spread_result. No venue, attendance, injuries, props.
    """
    sport = game["sport"]
    game_date = game["game_date"]
    home = game["home_team"]
    away = game["away_team"]

    parts = [f"{sport} game on {game_date}: {away} at {home}"]

    if game.get("home_score") is not None and game.get("away_score") is not None:
        parts.append(f"Final: {home} {game['home_score']}, {away} {game['away_score']}")

    if game.get("total_score") is not None:
        parts.append(f"Total: {game['total_score']}")

    if game.get("spread_result") is not None:
        parts.append(f"Spread: {home} {game['spread_result']}")

    return " | ".join(parts)


def build_metadata(game: dict) -> dict:
    """Build metadata dict for the embedding, matching existing format."""
    period = "historical" if game["game_date"] < RECENT_CUTOFF else "recent"
    meta = {
        "sport": game["sport"],
        "game_date": game["game_date"],
        "home_team": game["home_team"],
        "away_team": game["away_team"],
        "data_period": period,
    }
    if game.get("home_score") is not None:
        meta["home_score"] = game["home_score"]
    if game.get("away_score") is not None:
        meta["away_score"] = game["away_score"]
    if game.get("total_score") is not None:
        meta["total"] = game["total_score"]
    if game.get("spread_result") is not None:
        meta["spread"] = game["spread_result"]
    if game.get("winner"):
        meta["winner"] = game["winner"]
    return meta


async def embed_batch_ollama(
    client: httpx.AsyncClient, texts: list[str]
) -> list[list[float]]:
    """Embed a batch of texts via Ollama REST API."""
    resp = await client.post(
        f"{OLLAMA_BASE}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json().get("embeddings", [])


async def main():
    print("=" * 70)
    print("HISTORICAL GAME EMBEDDING — Full 2023-2026 Coverage")
    print("=" * 70)

    # ── Load all game_results ──
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, sport, game_date, home_team, away_team, "
        "home_score, away_score, total_score, spread_result, winner "
        "FROM game_results ORDER BY game_date ASC"
    )
    all_games = [dict(row) for row in cursor.fetchall()]
    print(f"Total game_results: {len(all_games)}")

    # ── Check existing embeddings for dedup ──
    cursor.execute(
        "SELECT content_hash FROM embeddings WHERE collection = 'game_contexts'"
    )
    existing_hashes = {row[0] for row in cursor.fetchall()}
    print(f"Existing game_contexts embeddings: {len(existing_hashes)}")

    # ── Filter to games not yet embedded ──
    to_embed = []
    for game in all_games:
        text = build_embedding_text(game)
        ch = content_hash(text)
        if ch not in existing_hashes:
            to_embed.append((game, text, ch))

    print(f"Games needing embedding: {len(to_embed)}")
    if not to_embed:
        print("All games already embedded. Nothing to do.")
        conn.close()
        return

    # ── Also update existing embeddings to add data_period metadata ──
    print("\nUpdating existing embeddings with data_period metadata...")
    cursor.execute(
        "SELECT id, metadata_json FROM embeddings WHERE collection = 'game_contexts'"
    )
    updated_count = 0
    for row_id, meta_json in cursor.fetchall():
        if not meta_json:
            continue
        meta = json.loads(meta_json)
        if "data_period" in meta:
            continue  # Already has it
        game_date = meta.get("game_date", "")
        meta["data_period"] = "historical" if game_date < RECENT_CUTOFF else "recent"
        cursor.execute(
            "UPDATE embeddings SET metadata_json = ? WHERE id = ?",
            (json.dumps(meta), row_id),
        )
        updated_count += 1
    conn.commit()
    print(f"Updated {updated_count} existing embeddings with data_period tag")

    # ── Embed in batches ──
    print(f"\nEmbedding {len(to_embed)} games in batches of {BATCH_SIZE}...")
    start_time = time.time()
    embedded_count = 0
    failed_count = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        for batch_start in range(0, len(to_embed), BATCH_SIZE):
            batch = to_embed[batch_start : batch_start + BATCH_SIZE]
            texts = [item[1] for item in batch]

            try:
                embeddings = await embed_batch_ollama(client, texts)

                if len(embeddings) != len(texts):
                    print(
                        f"  WARNING: batch {batch_start} got {len(embeddings)} "
                        f"embeddings for {len(texts)} texts"
                    )
                    failed_count += len(texts) - len(embeddings)

                # Store in DB
                for i, (game, text, ch) in enumerate(batch):
                    if i >= len(embeddings):
                        break
                    metadata = build_metadata(game)
                    cursor.execute(
                        "INSERT OR IGNORE INTO embeddings "
                        "(collection, content_hash, content_text, embedding_json, metadata_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            "game_contexts",
                            ch,
                            text,
                            json.dumps(embeddings[i]),
                            json.dumps(metadata),
                        ),
                    )
                    if cursor.rowcount > 0:
                        embedded_count += 1

                conn.commit()

            except httpx.HTTPStatusError as e:
                print(f"  HTTP error at batch {batch_start}: {e}")
                failed_count += len(batch)
            except httpx.ConnectError:
                print("  ERROR: Cannot connect to Ollama. Is it running?")
                conn.close()
                sys.exit(1)
            except Exception as e:
                print(f"  Error at batch {batch_start}: {e}")
                failed_count += len(batch)

            # Progress report every 100 games
            total_processed = batch_start + len(batch)
            if total_processed % 100 < BATCH_SIZE or total_processed == len(to_embed):
                elapsed = time.time() - start_time
                rate = total_processed / elapsed if elapsed > 0 else 0
                eta = (len(to_embed) - total_processed) / rate if rate > 0 else 0
                print(
                    f"  Progress: {total_processed}/{len(to_embed)} "
                    f"({total_processed * 100 / len(to_embed):.1f}%) | "
                    f"Embedded: {embedded_count} | Failed: {failed_count} | "
                    f"Rate: {rate:.0f} games/sec | ETA: {eta:.0f}s"
                )

    conn.close()
    elapsed = time.time() - start_time

    # ── Final stats ──
    print("\n" + "=" * 70)
    print("EMBEDDING COMPLETE")
    print(f"  New embeddings stored: {embedded_count}")
    print(f"  Failed/skipped: {failed_count}")
    print(f"  Time: {elapsed:.1f}s ({elapsed / 60:.1f} min)")

    # Verify final count
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM embeddings WHERE collection = 'game_contexts'")
    total = cursor.fetchone()[0]
    print(f"  Total game_contexts embeddings: {total}")

    # Count by period
    cursor.execute(
        "SELECT "
        "SUM(CASE WHEN json_extract(metadata_json, '$.data_period') = 'historical' THEN 1 ELSE 0 END) as historical, "
        "SUM(CASE WHEN json_extract(metadata_json, '$.data_period') = 'recent' THEN 1 ELSE 0 END) as recent, "
        "SUM(CASE WHEN json_extract(metadata_json, '$.data_period') IS NULL THEN 1 ELSE 0 END) as untagged "
        "FROM embeddings WHERE collection = 'game_contexts'"
    )
    row = cursor.fetchone()
    print(f"  Historical: {row[0]} | Recent: {row[1]} | Untagged: {row[2]}")

    # Date range
    cursor.execute(
        "SELECT MIN(json_extract(metadata_json, '$.game_date')), "
        "MAX(json_extract(metadata_json, '$.game_date')) "
        "FROM embeddings WHERE collection = 'game_contexts'"
    )
    dates = cursor.fetchone()
    print(f"  Date range: {dates[0]} to {dates[1]}")
    conn.close()

    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
