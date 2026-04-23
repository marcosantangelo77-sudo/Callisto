"""
Back-fill existing wiki_articles rows into the ``wiki_articles`` vector
collection so semantic retrieval works on day one instead of only for
articles written AFTER the feat/wiki-semantic-retrieval deploy.

Safe to re-run: idempotent on ``topic`` key via content_hash + near-dup
merge. Rows already embedded are skipped.

Usage:
    python scripts/backfill_wiki_embeddings.py                # live DB
    python scripts/backfill_wiki_embeddings.py --db /tmp/x.db # specific DB
    python scripts/backfill_wiki_embeddings.py --dry-run      # no writes
    python scripts/backfill_wiki_embeddings.py --limit 50     # cap articles

The ``--dry-run`` flag resolves the plan (what would be embedded) without
touching Ollama or the vector store. Useful for pre-flight.
"""

import argparse
import asyncio
import logging
import os
import sys

# Make the parent dir importable when run as a script.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import aiosqlite  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_wiki")


async def _plan(db_path: str, limit: int | None) -> list[dict]:
    """Read wiki_articles and return the payloads that WOULD be embedded."""
    async with aiosqlite.connect(db_path) as db:
        sql = (
            "SELECT topic, title, summary, content, domain, confidence, updated_at "
            "FROM wiki_articles ORDER BY updated_at DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()

    articles = []
    for r in rows:
        topic, title, summary, content, domain, confidence, updated_at = r
        articles.append({
            "topic": topic,
            "title": title,
            "summary": summary or "",
            "content": content or "",
            "domain": domain,
            "confidence": confidence,
            "updated_at": updated_at,
        })
    return articles


async def _already_embedded(db_path: str, topic: str) -> bool:
    """Check whether a topic already has a row in the wiki_articles vector
    collection. Keyed by metadata_json->$.topic so re-runs are idempotent.
    """
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM embeddings WHERE collection = 'wiki_articles' "
            "AND json_extract(metadata_json, '$.topic') = ? LIMIT 1",
            (topic,),
        )
        row = await cursor.fetchone()
    return row is not None


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Back-fill wiki_articles into the vector store."
    )
    parser.add_argument(
        "--db",
        default=os.getenv("CALLISTO_DB_PATH", "memory/callisto.db"),
        help="Path to the Callisto SQLite DB (default: memory/callisto.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be embedded, no Ollama calls, no writes.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of articles embedded (for smoke tests).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed articles that already have a vector row.",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        logger.error(f"DB not found: {args.db}")
        return 1

    articles = await _plan(args.db, args.limit)
    logger.info(f"Found {len(articles)} wiki articles in {args.db}")

    if args.dry_run:
        print("=== DRY RUN ===")
        print(f"DB: {args.db}")
        print(f"Total articles: {len(articles)}")
        for a in articles[:10]:
            print(
                f"  - {a['topic']:40s}  domain={a['domain']:10s}  "
                f"conf={a['confidence']:.2f}  updated={a['updated_at']}"
            )
        if len(articles) > 10:
            print(f"  ... and {len(articles) - 10} more")
        print(
            "\n(dry-run) Would embed each article as:\n"
            "    Topic: <slug>\\nTitle: <title>\\nSummary: <summary>\\n"
            "    Content: <content[:2000]>\n"
        )
        print("No writes performed.")
        return 0

    # Real embed path.
    try:
        from tools.embeddings import (
            VectorStore, embed_text, EMBED_MODEL, NEAR_DUP_THRESHOLD,
        )
        from tools.knowledge_wiki import WIKI_COLLECTION
    except Exception as e:
        logger.error(f"Failed to import embeddings stack: {e}")
        return 2

    store = VectorStore(args.db)
    await store.initialize()

    inserted = 0
    merged = 0
    skipped = 0
    failed = 0

    try:
        for i, a in enumerate(articles):
            topic = a["topic"]
            if not args.force and await _already_embedded(args.db, topic):
                skipped += 1
                continue

            text = (
                f"Topic: {topic.replace('_', ' ')}\n"
                f"Title: {a['title']}\n"
                f"Summary: {a['summary']}\n"
                f"Content: {a['content'][:2000]}"
            )
            metadata = {
                "topic": topic,
                "title": a["title"],
                "domain": a["domain"],
                "confidence": a["confidence"],
                "updated_at": a["updated_at"],
                "backfilled": True,
            }

            try:
                embedding = await asyncio.wait_for(embed_text(text), timeout=60.0)
            except Exception as e:
                logger.warning(f"[{i+1}/{len(articles)}] embed failed for '{topic}': {e}")
                failed += 1
                continue

            try:
                result = await store.store_or_merge(
                    WIKI_COLLECTION, text, embedding, metadata,
                    model_name=EMBED_MODEL,
                    near_dup_threshold=NEAR_DUP_THRESHOLD,
                )
                if result["action"] == "inserted":
                    inserted += 1
                elif result["action"] == "merged":
                    merged += 1
                else:  # duplicate
                    skipped += 1
                if (i + 1) % 25 == 0:
                    logger.info(
                        f"Progress {i+1}/{len(articles)}: "
                        f"+{inserted} inserted, ~{merged} merged, "
                        f"={skipped} skipped, !{failed} failed"
                    )
            except Exception as e:
                logger.warning(f"[{i+1}/{len(articles)}] store failed for '{topic}': {e}")
                failed += 1

    finally:
        await store.close()

    print("=== BACKFILL COMPLETE ===")
    print(f"DB: {args.db}")
    print(f"Articles scanned: {len(articles)}")
    print(f"  Inserted: {inserted}")
    print(f"  Merged (near-duplicate): {merged}")
    print(f"  Skipped (already embedded): {skipped}")
    print(f"  Failed: {failed}")
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
