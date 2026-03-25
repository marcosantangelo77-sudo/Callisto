"""
End-to-end test for the free prop scraper cascade.

Tests:
1. DraftKings Nash prop extraction
2. FanDuel prop extraction
3. BetMGM prop extraction
4. Unified cascade merge
5. Database storage and retrieval
6. Conversion to prop_scanner format

Run: python -m pytest tests/test_prop_scraper_free.py -v
  or: python tests/test_prop_scraper_free.py
"""

import asyncio
import json
import os
import sys
import tempfile

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def test_dk_props():
    """Test DraftKings Nash prop scraping for NBA."""
    from tools.prop_scraper_free import scrape_dk_props

    print("\n=== DK Props (NBA) ===")
    result = await scrape_dk_props("basketball_nba")

    if result.get("error"):
        print(f"  ERROR: {result['error']}")
        return result

    props = result.get("props", [])
    print(f"  Prop lines: {len(props)}")
    print(f"  Markets found: {result.get('market_count', 0)}")

    if props:
        # Show market breakdown
        markets = {}
        for p in props:
            m = p["market"]
            markets[m] = markets.get(m, 0) + 1
        print(f"  Market breakdown:")
        for m, count in sorted(markets.items(), key=lambda x: -x[1]):
            print(f"    {m}: {count} lines")

        # Show sample props
        print(f"  Sample props:")
        for p in props[:5]:
            print(f"    {p['player']} | {p['market']} {p['side']} {p['line']} @ {p['price']}")

    return result


async def test_fd_props():
    """Test FanDuel prop scraping for NBA."""
    from tools.prop_scraper_free import scrape_fd_props

    print("\n=== FanDuel Props (NBA) ===")
    result = await scrape_fd_props("basketball_nba")

    if result.get("error"):
        print(f"  ERROR: {result['error']}")
        return result

    props = result.get("props", [])
    print(f"  Prop lines: {len(props)}")

    if props:
        markets = {}
        for p in props:
            m = p["market"]
            markets[m] = markets.get(m, 0) + 1
        print(f"  Market breakdown:")
        for m, count in sorted(markets.items(), key=lambda x: -x[1]):
            print(f"    {m}: {count} lines")

        print(f"  Sample props:")
        for p in props[:5]:
            print(f"    {p['player']} | {p['market']} {p['side']} {p['line']} @ {p['price']}")

    return result


async def test_mgm_props():
    """Test BetMGM prop scraping for NBA."""
    from tools.prop_scraper_free import scrape_mgm_props

    print("\n=== BetMGM Props (NBA) ===")
    result = await scrape_mgm_props("basketball_nba")

    if result.get("error"):
        print(f"  ERROR: {result['error']}")
        return result

    props = result.get("props", [])
    print(f"  Prop lines: {len(props)}")

    if props:
        markets = {}
        for p in props:
            m = p["market"]
            markets[m] = markets.get(m, 0) + 1
        print(f"  Market breakdown:")
        for m, count in sorted(markets.items(), key=lambda x: -x[1]):
            print(f"    {m}: {count} lines")

        print(f"  Sample props:")
        for p in props[:5]:
            print(f"    {p['player']} | {p['market']} {p['side']} {p['line']} @ {p['price']}")

    return result


async def test_cascade():
    """Test the full free cascade for NBA."""
    from tools.prop_scraper_free import scrape_all_props

    print("\n=== Full Prop Cascade (NBA) ===")
    result = await scrape_all_props("basketball_nba")

    if result.get("error"):
        print(f"  ERROR: {result['error']}")
        return result

    props = result.get("props", [])
    print(f"  Total prop lines: {len(props)}")
    print(f"  Sources: {result.get('sources', [])}")
    print(f"  Unique player/market/lines: {result.get('unique_player_markets', 0)}")
    print(f"  Multi-book coverage: {result.get('multi_book_count', 0)}")

    if props:
        # Book breakdown
        books = {}
        for p in props:
            b = p["book"]
            books[b] = books.get(b, 0) + 1
        print(f"  Book breakdown:")
        for b, count in sorted(books.items(), key=lambda x: -x[1]):
            print(f"    {b}: {count} lines")

        # Market breakdown
        markets = {}
        for p in props:
            m = p["market"]
            markets[m] = markets.get(m, 0) + 1
        print(f"  Market breakdown:")
        for m, count in sorted(markets.items(), key=lambda x: -x[1]):
            print(f"    {m}: {count} lines")

    return result


async def test_storage(cascade_result: dict):
    """Test database storage and retrieval."""
    from tools.prop_scraper_free import store_prop_snapshot, ensure_prop_schema

    import aiosqlite

    print("\n=== Database Storage Test ===")

    # Use temp DB for test
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db = f.name

    try:
        await ensure_prop_schema(test_db)
        print(f"  Schema created: {test_db}")

        props = cascade_result.get("props", [])
        if not props:
            print("  SKIP: No props to store")
            return

        stored = await store_prop_snapshot(props, "basketball_nba", test_db)
        print(f"  Stored: {stored} rows")

        # Verify retrieval
        async with aiosqlite.connect(test_db) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM prop_snapshots")
            row = await cursor.fetchone()
            count = row[0]
            print(f"  Retrieved: {count} rows")
            assert count == stored, f"Mismatch: stored {stored} but got {count}"

            # Check distinct players
            cursor = await db.execute("SELECT COUNT(DISTINCT player) FROM prop_snapshots")
            row = await cursor.fetchone()
            print(f"  Distinct players: {row[0]}")

            # Check distinct books
            cursor = await db.execute("SELECT book, COUNT(*) FROM prop_snapshots GROUP BY book")
            rows = await cursor.fetchall()
            for book, cnt in rows:
                print(f"    {book}: {cnt} lines")

            # Check distinct markets
            cursor = await db.execute("SELECT market, COUNT(*) FROM prop_snapshots GROUP BY market ORDER BY COUNT(*) DESC")
            rows = await cursor.fetchall()
            for mkt, cnt in rows:
                print(f"    {mkt}: {cnt} lines")

        print("  PASS: Storage test passed")

    finally:
        os.unlink(test_db)


async def test_scanner_format(cascade_result: dict):
    """Test conversion to prop_scanner.py format."""
    from tools.prop_scraper_free import props_to_scanner_format

    print("\n=== Scanner Format Conversion ===")

    props = cascade_result.get("props", [])
    if not props:
        print("  SKIP: No props to convert")
        return

    formatted = props_to_scanner_format(props)
    bookmakers = formatted.get("bookmakers", [])
    print(f"  Bookmakers: {len(bookmakers)}")

    for bm in bookmakers:
        markets = bm.get("markets", [])
        total_outcomes = sum(len(m.get("outcomes", [])) for m in markets)
        print(f"    {bm['title']}: {len(markets)} markets, {total_outcomes} outcomes")

    # Verify structure matches what prop_scanner expects
    for bm in bookmakers:
        assert "key" in bm, "Missing bookmaker key"
        assert "title" in bm, "Missing bookmaker title"
        assert "markets" in bm, "Missing markets"
        for mkt in bm["markets"]:
            assert "key" in mkt, "Missing market key"
            assert "outcomes" in mkt, "Missing outcomes"
            for oc in mkt["outcomes"]:
                assert "name" in oc, "Missing outcome name"
                assert "price" in oc, "Missing outcome price"
                assert "point" in oc, "Missing outcome point"
                assert "description" in oc, "Missing outcome description (player name)"

    print("  PASS: Format matches prop_scanner expectations")


async def test_mlb_props():
    """Test prop scraping for MLB (if games available)."""
    from tools.prop_scraper_free import scrape_all_props

    print("\n=== Full Prop Cascade (MLB) ===")
    result = await scrape_all_props("baseball_mlb")

    props = result.get("props", [])
    print(f"  Total prop lines: {len(props)}")
    print(f"  Sources: {result.get('sources', [])}")

    if props:
        markets = {}
        for p in props:
            m = p["market"]
            markets[m] = markets.get(m, 0) + 1
        print(f"  Market breakdown:")
        for m, count in sorted(markets.items(), key=lambda x: -x[1]):
            print(f"    {m}: {count} lines")

    return result


async def test_production_db_storage(cascade_result: dict):
    """Store props in the actual Callisto production database."""
    from tools.prop_scraper_free import store_prop_snapshot, ensure_prop_schema

    import aiosqlite

    db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    print(f"\n=== Production DB Storage ({db_path}) ===")

    if not os.path.exists(db_path):
        print(f"  SKIP: Production DB not found at {db_path}")
        return

    props = cascade_result.get("props", [])
    if not props:
        print("  SKIP: No props to store")
        return

    await ensure_prop_schema(db_path)
    stored = await store_prop_snapshot(props, "basketball_nba", db_path)
    print(f"  Stored: {stored} rows to production DB")

    # Verify
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM prop_snapshots")
        row = await cursor.fetchone()
        print(f"  Total rows in prop_snapshots: {row[0]}")

    print("  PASS: Production storage confirmed")


async def main():
    print("=" * 60)
    print("FREE PROP SCRAPER CASCADE — END-TO-END TEST")
    print("=" * 60)

    # Test individual scrapers
    dk = await test_dk_props()
    fd = await test_fd_props()
    mgm = await test_mgm_props()

    # Test cascade
    cascade = await test_cascade()

    # Test storage (temp DB)
    await test_storage(cascade)

    # Test format conversion
    await test_scanner_format(cascade)

    # Test MLB
    mlb = await test_mlb_props()

    # Store to production DB
    await test_production_db_storage(cascade)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    dk_count = len(dk.get("props", []))
    fd_count = len(fd.get("props", []))
    mgm_count = len(mgm.get("props", []))
    total = len(cascade.get("props", []))
    multi = cascade.get("multi_book_count", 0)

    print(f"  DraftKings:  {dk_count:>5} prop lines")
    print(f"  FanDuel:     {fd_count:>5} prop lines")
    print(f"  BetMGM:      {mgm_count:>5} prop lines")
    print(f"  ─────────────────────────")
    print(f"  Total:       {total:>5} prop lines")
    print(f"  Multi-book:  {multi:>5} player/market/lines")
    print(f"  MLB cascade: {len(mlb.get('props', [])):>5} prop lines")

    if total > 0:
        print(f"\n  STATUS: OPERATIONAL — prop pipeline is live")
    else:
        print(f"\n  STATUS: NO DATA — check if games are scheduled today")

    return total > 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
