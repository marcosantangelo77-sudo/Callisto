"""Schema engine — connection handling and ensure_schema().

Split out of the old monolithic tools/schema.py. The DDL now lives in
tools/schema/core.py (domain-general) plus registered plugin schemas
(plugins/sports/schema.py); this module applies core first, then every
registered plugin, then runs the legacy column migrations.

ensure_schema() applies, in order:
  1. CORE_SCHEMA_SQL            — domain-general tables
  2. plugin_claim_extension_ddl — plugin side tables keyed to core claims
  3. plugin_schema_sql          — plugin-owned domain tables
  4. legacy column adds/backfills — unchanged from the pre-split file

On a fresh DB this produces the seam directly. On an existing DB every
statement is IF NOT EXISTS / idempotent, so nothing is touched that
migration 013 hasn't already rebuilt.
"""

import logging
import os

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

# Import core DDL + the plugin registry. The sports plugin itself is
# imported by tools/schema/__init__ (registration side effect); engine
# callers that import this module directly still need the registry.
from tools.schema.core import CORE_SCHEMA_SQL  # noqa: E402
from tools.schema.registry import (  # noqa: E402
    plugin_claim_extension_ddl,
    plugin_schema_sql,
)

logger = logging.getLogger("callisto.schema")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


async def open_db(db_path: str = None) -> aiosqlite.Connection:
    """Open a DB connection with WAL mode and busy_timeout.

    Use this instead of raw aiosqlite.connect() everywhere. The connection is
    tagged with ``_callisto_db_path`` so ``tools.db_utils.execute_with_retry``
    can route writes through the matching ``WriteCoordinator`` (single-writer
    pattern, see ``tools/db_writer.py``). When no coordinator is running the
    connection still works as a regular aiosqlite connection.
    """
    if db_path is None:
        db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    db = await aiosqlite.connect(db_path)
    # Tag the connection so coordinator routing works without a path lookup.
    try:
        db._callisto_db_path = os.path.abspath(db_path)
    except Exception:
        pass
    await db.execute("PRAGMA busy_timeout = 60000")   # 60s — prevents 'database is locked' during bulk writes
    await db.execute("PRAGMA journal_mode = WAL")      # WAL mode for concurrent reads during writes
    await db.execute("PRAGMA synchronous = NORMAL")    # Safe with WAL, reduces fsync overhead
    await db.execute("PRAGMA wal_autocheckpoint = 1000")  # Checkpoint after 1000 pages (~4MB) — prevents WAL bloat
    await db.execute("PRAGMA journal_size_limit = 67108864")  # 64MB WAL cap — SQLite tries harder to checkpoint
    await db.execute("PRAGMA cache_size = -512")        # 512KB page cache (default -2000 = 2MB) — reduces RSS per conn
    await db.execute("PRAGMA mmap_size = 0")           # Disable mmap — prevents WAL from being memory-mapped into RSS
    # Foreign keys are a per-connection pragma in SQLite. Enabling here
    # makes FOREIGN KEY declarations in the schema actually enforced for
    # inserts/updates/deletes via this connection. Audit found 1 orphan
    # row in hypothesis_stats pre-fix; migration 004 cleans existing
    # orphans so turning this on doesn't retroactively break writes.
    # Set CALLISTO_DISABLE_FK=1 to opt out (useful during bulk imports
    # that must temporarily bypass cascades).
    if os.getenv("CALLISTO_DISABLE_FK", "0") != "1":
        await db.execute("PRAGMA foreign_keys = ON")
    return db

async def _safe_add_column(
    db, table: str, column: str, coltype: str
) -> None:
    """Idempotent ADD COLUMN that distinguishes "already exists" from real errors.

    SQLite reports the already-exists case with a specific substring in the
    error message; anything else (permission denied, disk full, invalid type,
    missing table) is a real problem and must reach the logs instead of
    being swallowed as `except: pass` — that pattern silently leaves the
    schema incomplete and downstream writers fail with "no such column"
    hours later, far from the root cause.
    """
    from tools.db_utils import safe_ident
    tbl = safe_ident(table)
    col = safe_ident(column)
    try:
        await db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {coltype}")
        await db.commit()
        logger.info(f"Added {column} column to {table}")
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            # Expected on second+ runs; not worth logging at INFO level.
            return
        logger.warning(
            f"Failed to ADD COLUMN {column} {coltype} to {table}: {e!r}. "
            "Schema may be incomplete — check underlying cause before restarting."
        )


async def ensure_schema(db_path: str = DB_PATH) -> None:
    """Create or upgrade all tables. Safe to call multiple times."""
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    # SECURITY (audit P2): warn loudly when the DB lives inside a OneDrive sync
    # folder. OneDrive holds file handles open while syncing, which corrupts WAL
    # writes and (with bankroll/bet data) replicates financial PII to Microsoft
    # cloud. Marco's current install IS inside OneDrive, so this is a warning
    # rather than a hard fail — but the path forward is to symlink the DB out.
    # Set CALLISTO_SILENCE_ONEDRIVE_WARNING=1 to suppress.
    if (
        "OneDrive" in os.path.abspath(db_path)
        and os.getenv("CALLISTO_SILENCE_ONEDRIVE_WARNING", "0") != "1"
    ):
        logger.warning(
            f"DB path {db_path!r} is inside a OneDrive sync folder. WAL + cloud sync "
            "can corrupt data; bankroll and bets replicate to Microsoft cloud. Move to "
            "a non-synced location (e.g. C:/CallistoLocal/callisto.db) when feasible. "
            "Set CALLISTO_SILENCE_ONEDRIVE_WARNING=1 to suppress this warning."
        )
    async with aiosqlite.connect(db_path) as db:
        # Set PRAGMAs before schema creation — these persist for the connection
        await db.execute("PRAGMA busy_timeout = 120000")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA wal_autocheckpoint = 1000")
        await db.execute("PRAGMA journal_size_limit = 67108864")
        await db.execute("PRAGMA synchronous = NORMAL")  # Safe with WAL, reduces fsync
        await db.commit()

        # SECURITY (audit P2): schema_migrations table tracks which one-time
        # migrations have been applied. Future migrations should INSERT a row
        # here so a failed/skipped migration is detectable instead of silent.
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version INTEGER PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        await db.commit()
        # Run schema statements individually instead of executescript() to
        # avoid the EXCLUSIVE lock executescript takes for the whole script.
        #
        # 2026-04-18: two bugs bit the naive `split(";")` approach and caused
        # tables to silently fail to materialize:
        #   1. `;` characters inside `-- ...` comments split the DDL mid-
        #      statement (statcast_pitches, nba_shot_events).
        #   2. A leading `-- header divider` comment on a chunk made the
        #      naive `startswith("--")` filter drop the whole CREATE TABLE.
        # Fix: strip every `-- line comment` (up to end-of-line) from the
        # entire SCHEMA_SQL body BEFORE splitting on `;`. Inline trailing
        # `-- ...` column comments survive as part of each column line until
        # stripped, which is fine because SQLite would accept them if kept
        # anyway. This is DDL-only; no string literals in SCHEMA_SQL depend
        # on retaining the `-- ` sequence.
        import re as _re_schema
        full_ddl = CORE_SCHEMA_SQL + "\n" + plugin_claim_extension_ddl() + "\n" + plugin_schema_sql()
        cleaned = _re_schema.sub(r"--[^\n]*", "", full_ddl)
        for raw in cleaned.split(";"):
            stmt = raw.strip()
            if not stmt:
                continue
            try:
                await db.execute(stmt)
            except Exception as e:
                # Pre-fix this was ``except Exception: pass`` which silently
                # dropped any DDL failure — including typos, wrong column
                # counts, referenced-but-missing tables. Downstream writes
                # then exploded hours later with confusing "no such column"
                # errors. Log the failing statement and the root cause;
                # IF NOT EXISTS / OR IGNORE duplicates are still tolerated
                # because SQLite reports them with a recognisable message.
                msg = str(e).lower()
                if (
                    "already exists" in msg
                    or "duplicate column" in msg
                ):
                    continue
                first_line = stmt.splitlines()[0][:140] if stmt else "<empty>"
                logger.error(
                    f"ensure_schema statement failed: {e!r} — "
                    f"first line: {first_line!r}. Downstream writers that "
                    f"depend on this table/column will fail."
                )
        await db.commit()

        # Migrations: add regime columns (safe if already exists)
        for tbl in ("historical_odds_cache", "game_results"):
            try:
                await db.execute(f"ALTER TABLE {tbl} ADD COLUMN regime TEXT")
                await db.commit()
                logger.info(f"Added regime column to {tbl}")
            except Exception:
                pass  # Column already exists

        # Migration: add binary embedding blob column for numpy storage
        await _safe_add_column(db, "embeddings", "embedding_blob", "BLOB")

        # Migration (2026-04-21): add model_name to embeddings so we don't mix
        # vectors from different embed models when the EMBED_MODEL env var
        # changes. Old rows default to NULL — retrieval treats NULL as "unknown,
        # logged-and-excluded" rather than silently comparing cross-model.
        await _safe_add_column(db, "embeddings", "model_name", "TEXT")

        # Migration (2026-04-21): wiki_articles gains source_task_id so
        # file_task_result can join back to the task_queue instead of minting
        # a fake "task_{int(time.time())}" id that can't be traced.
        await _safe_add_column(db, "wiki_articles", "source_task_id", "TEXT")

        # Migration: add microstructure metric columns to hypothesis_stats.
        # (Baseline schema now includes these; migration stays for old DBs.)
        for col in ("sortino", "brier_score", "information_coefficient"):
            await _safe_add_column(db, "hypothesis_stats", col, "REAL")

        # Migration: add microstructure metric columns to backtest_runs.
        for col in ("sortino_ratio_val", "brier_score", "information_coefficient"):
            await _safe_add_column(db, "backtest_runs", col, "REAL")

        # Migration: add home_team/away_team to paper_trades for resolution matching
        for col in ("home_team", "away_team"):
            await _safe_add_column(db, "paper_trades", col, "TEXT")

        # Migration (2026-04-18): add `source` to ev_opportunities. Before this,
        # line_monitor INSERTed with (game_id, bookmaker, team, edge) while
        # autonomous.py attempted INSERTs with (event_id, book, side, ev_pct)
        # against the same table — the autonomous writes silently dropped
        # because the table had no such columns, producing recurring
        # OperationalError("no column named event_id") in the WriteCoordinator.
        # autonomous.py is now remapped onto the canonical column names and
        # stamps `source` to distinguish signal provenance.
        await _safe_add_column(
            db, "ev_opportunities", "source", "TEXT DEFAULT 'line_movement'"
        )

        # Migration (audit 2026-04-21): allow 'paused' status for LIVE-hypothesis
        # demotion loop. Older DBs have a CHECK constraint that rejects 'paused';
        # SQLite cannot alter a CHECK in place so we rebuild the table.
        try:
            cur = await db.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 20260421"
            )
            if not await cur.fetchone():
                cur = await db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='hypotheses'"
                )
                row = await cur.fetchone()
                table_sql = row[0] if row else ""
                if table_sql and "'paused'" not in table_sql:
                    logger.info("Migration 20260421: rebuilding hypotheses table to add 'paused' status")
                    await db.execute("BEGIN")
                    try:
                        await db.execute("ALTER TABLE hypotheses RENAME TO hypotheses_old_20260421")
                        await db.execute("""
                            CREATE TABLE hypotheses (
                                hypothesis_id TEXT PRIMARY KEY,
                                name TEXT NOT NULL,
                                thesis TEXT NOT NULL,
                                sport TEXT NOT NULL,
                                market_type TEXT NOT NULL,
                                model_config TEXT NOT NULL,
                                edge_threshold REAL NOT NULL DEFAULT 0.01,
                                status TEXT NOT NULL DEFAULT 'draft'
                                    CHECK(status IN ('draft','backtesting','paper_trading','live','paused','retired','rejected')),
                                min_sample_size INTEGER NOT NULL DEFAULT 50,
                                significance_level REAL NOT NULL DEFAULT 0.05,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                promoted_at DATETIME,
                                promoted_by TEXT,
                                notes TEXT
                            )
                        """)
                        # Copy all existing rows (unchanged data).
                        await db.execute(
                            "INSERT INTO hypotheses "
                            "(hypothesis_id, name, thesis, sport, market_type, "
                            " model_config, edge_threshold, status, min_sample_size, "
                            " significance_level, created_at, updated_at, "
                            " promoted_at, promoted_by, notes) "
                            "SELECT hypothesis_id, name, thesis, sport, market_type, "
                            " model_config, edge_threshold, status, min_sample_size, "
                            " significance_level, created_at, updated_at, "
                            " promoted_at, promoted_by, notes "
                            "FROM hypotheses_old_20260421"
                        )
                        await db.execute("DROP TABLE hypotheses_old_20260421")
                        await db.execute(
                            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hypotheses_name ON hypotheses(name)"
                        )
                        await db.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES (20260421, 'add_paused_status')"
                        )
                        await db.commit()
                        logger.info("Migration 20260421 complete: 'paused' status now allowed")
                    except Exception as mig_err:
                        await db.rollback()
                        logger.error(f"Migration 20260421 failed: {mig_err}")
                else:
                    # Table already has 'paused' — record migration as complete.
                    await db.execute(
                        "INSERT OR IGNORE INTO schema_migrations (version, name) "
                        "VALUES (20260421, 'add_paused_status')"
                    )
                    await db.commit()
        except Exception as e:
            logger.warning(f"Could not evaluate migration 20260421: {e}")

        # Migration 20260422 (feat/portfolio-kelly-live-loop): allow
        # 'drawdown_paused' status so the drawdown kill-switch can flag LIVE
        # hypotheses distinctly from ordinary 'paused' demotion. Also create
        # the bankroll_peak table used by the kill switch.
        try:
            cur = await db.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 20260422"
            )
            if not await cur.fetchone():
                cur = await db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='hypotheses'"
                )
                row = await cur.fetchone()
                table_sql = row[0] if row else ""
                if table_sql and "'drawdown_paused'" not in table_sql:
                    logger.info("Migration 20260422: rebuilding hypotheses table to add 'drawdown_paused' status")
                    await db.execute("BEGIN")
                    try:
                        await db.execute("ALTER TABLE hypotheses RENAME TO hypotheses_old_20260422")
                        await db.execute("""
                            CREATE TABLE hypotheses (
                                hypothesis_id TEXT PRIMARY KEY,
                                name TEXT NOT NULL,
                                thesis TEXT NOT NULL,
                                sport TEXT NOT NULL,
                                market_type TEXT NOT NULL,
                                model_config TEXT NOT NULL,
                                edge_threshold REAL NOT NULL DEFAULT 0.01,
                                status TEXT NOT NULL DEFAULT 'draft'
                                    CHECK(status IN ('draft','backtesting','paper_trading','live','paused','drawdown_paused','retired','rejected')),
                                min_sample_size INTEGER NOT NULL DEFAULT 50,
                                significance_level REAL NOT NULL DEFAULT 0.05,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                promoted_at DATETIME,
                                promoted_by TEXT,
                                notes TEXT
                            )
                        """)
                        await db.execute(
                            "INSERT INTO hypotheses "
                            "(hypothesis_id, name, thesis, sport, market_type, "
                            " model_config, edge_threshold, status, min_sample_size, "
                            " significance_level, created_at, updated_at, "
                            " promoted_at, promoted_by, notes) "
                            "SELECT hypothesis_id, name, thesis, sport, market_type, "
                            " model_config, edge_threshold, status, min_sample_size, "
                            " significance_level, created_at, updated_at, "
                            " promoted_at, promoted_by, notes "
                            "FROM hypotheses_old_20260422"
                        )
                        await db.execute("DROP TABLE hypotheses_old_20260422")
                        await db.execute(
                            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hypotheses_name ON hypotheses(name)"
                        )
                        await db.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES (20260422, 'add_drawdown_paused_status')"
                        )
                        await db.commit()
                        logger.info("Migration 20260422 complete: 'drawdown_paused' status now allowed")
                    except Exception as mig_err:
                        await db.rollback()
                        logger.error(f"Migration 20260422 failed: {mig_err}")
                else:
                    await db.execute(
                        "INSERT OR IGNORE INTO schema_migrations (version, name) "
                        "VALUES (20260422, 'add_drawdown_paused_status')"
                    )
                    await db.commit()

            # bankroll_peak table (drawdown kill-switch state). Keyed by date
            # so we can see a rolling 30d peak via a simple MAX query.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bankroll_peak (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at DATETIME NOT NULL,
                    balance REAL NOT NULL,
                    note TEXT
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_bankroll_peak_ts ON bankroll_peak(observed_at)"
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"Could not evaluate migration 20260422: {e}")

        # Migration (odds-freshness audit): add ingestion-time stamp to
        # odds_snapshots so downstream consumers can compute freshness-weighted
        # consensus. The existing `timestamp` column records the row's write
        # time; `fetched_at` records when we *fetched* the odds (may differ if
        # a snapshot is re-processed, replayed from WS, or backfilled).
        # Books themselves emit `last_update` inside snapshot_json — that's
        # the book's own stamp and cannot be trusted for our freshness model.
        await _safe_add_column(db, "odds_snapshots", "fetched_at", "TEXT")

        # Migration (odds-freshness audit): event source so we can distinguish
        # scheduled snapshots (interval=15m), WebSocket deltas, and
        # incremental /odds/updated polls. Used for telemetry and for
        # replaying only the fresh slice.
        await _safe_add_column(
            db, "odds_snapshots", "source", "TEXT DEFAULT 'interval'"
        )

        # Migration (odds-freshness audit): add prob-basis-point CLV column
        # alongside legacy clv_cents (which was a mix of American cents and
        # prob×10000 depending on which code path wrote it — see
        # clv_tracker.py:414 vs :419). Going forward writers populate
        # clv_prob_bp unambiguously; readers should prefer it and treat
        # clv_cents as deprecated/mixed-units.
        await _safe_add_column(db, "clv_log", "clv_prob_bp", "REAL")

        # Migration (feat/regime-aware-sizing, 2026-04-22): stamp the
        # market regime (sport|season_phase) at placement time so CLV
        # analysis can bucket by regime. Future regime-bucket queries show
        # whether a hypothesis is regime-robust or regime-fragile.
        await _safe_add_column(db, "clv_log", "regime_phase_at_placement", "TEXT")

        # Migration (odds-freshness audit): gate flag for ev_opportunities.
        # An ev_opportunity with steam_only=1 means the row was surfaced by
        # line-movement consensus alone, NOT ratified by an independent model
        # (pace, props, sim). Kept so downstream filters can exclude
        # steam-only rows from Telegram alerts without losing them from
        # research backfill.
        await _safe_add_column(
            db, "ev_opportunities", "steam_only", "INTEGER DEFAULT 0"
        )

        # Migration (audit P2): add UNIQUE index on hypothesis_stats(hypothesis_id, stage)
        # so concurrent backtest writes can't insert competing rows for the same
        # hypothesis/stage. Existing duplicates (if any) are not removed here; the
        # CREATE UNIQUE INDEX call will fail loudly if duplicates exist, prompting a
        # one-time dedupe rather than silently masking the data corruption.
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_hypothesis_stats_unique ON hypothesis_stats(hypothesis_id, stage)"
            )
            await db.commit()
        except Exception as e:
            logger.error(
                f"Could not create UNIQUE index on hypothesis_stats: {e}. "
                "Existing duplicate rows must be deduplicated; run "
                "`DELETE FROM hypothesis_stats WHERE id NOT IN (SELECT MIN(id) "
                "FROM hypothesis_stats GROUP BY hypothesis_id, stage);` and retry."
            )

        # Backfill: convert existing JSON embeddings to binary blobs
        await _backfill_embedding_blobs(db)

        # One-time migration: backfill signals table from backtest_events
        await _backfill_signals_from_backtests(db)

        # One-time migration: tag existing data with regimes
        await _backfill_regimes(db)

    logger.info("Schema ensured")


async def vacuum_db(db_path: str = DB_PATH) -> dict:
    """Run VACUUM + WAL checkpoint to reclaim space (audit P2).

    Call from a periodic task (e.g. weekly). VACUUM rewrites the entire DB so it
    holds an EXCLUSIVE lock — schedule it during a quiet window (overnight) and
    after wait_for_drain() so backtest/line_monitor writers are paused.

    Implementation note (vacuum-in-tx fix):
    SQLite refuses ``VACUUM`` when any transaction is active on the connection,
    and aiosqlite's default isolation_level opens an *implicit* transaction
    around every write. That manifested as the silent
    ``OperationalError: cannot VACUUM from within a transaction`` hidden behind
    the WriteCoordinator's ``writes_failed`` counter.

    The correct call path for VACUUM is therefore a dedicated, autocommit,
    UNTAGGED stdlib ``sqlite3`` connection on a worker thread:
      * stdlib sqlite3 with ``isolation_level=None`` ⇒ true autocommit, no
        implicit BEGIN around VACUUM.
      * Not tagged with ``_callisto_db_path`` ⇒ the aiosqlite monkey-patch in
        ``tools.db_writer.install_aiosqlite_routing`` can never re-route VACUUM
        through the coordinator (which would re-introduce the bug).
      * Run inside ``asyncio.to_thread`` so we don't block the event loop for
        the minutes VACUUM can take on a multi-GB DB.
    """
    import os as _os
    import sqlite3 as _sqlite3
    import asyncio as _asyncio

    before = _os.path.getsize(db_path) if _os.path.exists(db_path) else 0

    def _run_vacuum_sync() -> None:
        # isolation_level=None ⇒ autocommit. No implicit BEGIN is issued by
        # the driver, so VACUUM runs on a connection with no active tx.
        conn = _sqlite3.connect(db_path, isolation_level=None, timeout=300.0)
        try:
            # 5-minute busy timeout for the EXCLUSIVE lock contention window.
            conn.execute("PRAGMA busy_timeout = 300000")
            # Truncate WAL first so VACUUM's new DB is as small as possible.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # Invariant check: silent-failure → loud-failure upgrade. If this
            # connection somehow has an open transaction we refuse to VACUUM
            # rather than letting SQLite surface the confusing error string.
            if conn.in_transaction:
                raise RuntimeError(
                    "vacuum_db invariant violated: dedicated connection has "
                    "an open transaction before VACUUM. Refusing to VACUUM."
                )
            conn.execute("VACUUM")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    await _asyncio.to_thread(_run_vacuum_sync)

    after = _os.path.getsize(db_path) if _os.path.exists(db_path) else 0
    reclaimed = max(0, before - after)
    logger.info(
        f"VACUUM complete: {before/1e6:.1f}MB -> {after/1e6:.1f}MB "
        f"(reclaimed {reclaimed/1e6:.1f}MB)"
    )
    return {"before_bytes": before, "after_bytes": after, "reclaimed_bytes": reclaimed}


async def _backfill_embedding_blobs(db) -> None:
    """Convert existing JSON-serialized embeddings to numpy binary blobs.

    Idempotent — only processes rows where embedding_blob IS NULL.
    Runs in batches of 500 to avoid holding the DB lock too long.
    """
    import json
    import numpy as np

    cursor = await db.execute(
        "SELECT COUNT(*) FROM embeddings WHERE embedding_blob IS NULL"
    )
    pending = (await cursor.fetchone())[0]
    if pending == 0:
        return

    logger.info(f"Backfilling {pending} embedding blobs from JSON...")
    total = 0
    while True:
        cursor = await db.execute(
            "SELECT id, embedding_json FROM embeddings "
            "WHERE embedding_blob IS NULL LIMIT 500"
        )
        rows = await cursor.fetchall()
        if not rows:
            break
        for row_id, emb_json in rows:
            blob = np.array(json.loads(emb_json), dtype=np.float32).tobytes()
            await db.execute(
                "UPDATE embeddings SET embedding_blob = ? WHERE id = ?",
                (blob, row_id),
            )
        await db.commit()
        total += len(rows)
        logger.info(f"  Backfilled {total}/{pending} embedding blobs")

    logger.info(f"Embedding blob backfill complete: {total} rows converted")


async def _backfill_regimes(db) -> None:
    """Tag existing historical_odds_cache and game_results rows with regime."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM game_results WHERE regime IS NULL"
    )
    untagged = (await cursor.fetchone())[0]
    if untagged == 0:
        return

    # Load regime rules
    cursor = await db.execute("SELECT sport, regime_name, start_date, end_date FROM regime_rules")
    rules = await cursor.fetchall()

    from tools.db_utils import safe_ident
    for sport, regime_name, start_date, end_date in rules:
        for tbl, date_col in [("game_results", "game_date"), ("historical_odds_cache", "snapshot_date")]:
            tbl_q = safe_ident(tbl)
            col_q = safe_ident(date_col)
            # SECURITY (audit C-5): parameterize end_date instead of inlining a quoted
            # string literal. Even though end_date originates from regime_rules (an
            # internal table), splicing a quoted string into SQL is the same anti-pattern
            # the rest of the audit closed.
            if end_date:
                await db.execute(
                    f"UPDATE {tbl_q} SET regime = ? "
                    f"WHERE sport = ? AND {col_q} >= ? AND {col_q} <= ? AND regime IS NULL",
                    (regime_name, sport, start_date, end_date),
                )
            else:
                await db.execute(
                    f"UPDATE {tbl_q} SET regime = ? "
                    f"WHERE sport = ? AND {col_q} >= ? AND regime IS NULL",
                    (regime_name, sport, start_date),
                )
    await db.commit()
    logger.info(f"Backfilled regimes for {untagged} untagged rows")


async def _backfill_signals_from_backtests(db) -> None:
    """Copy backtest_events with signal_generated=1 into signals table.

    Idempotent — uses INSERT OR IGNORE and checks if backfill already ran.
    """
    from tools.backtest import _signal_confidence

    # Check if we already have backtest-type signals (skip if already backfilled)
    row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM signals WHERE signal_type = 'backtest'"
    )
    if row and row[0][0] > 0:
        return  # Already backfilled

    # Count what needs backfilling
    row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM backtest_events WHERE signal_generated = 1"
    )
    total = row[0][0] if row else 0
    if total == 0:
        return

    rows = await db.execute_fetchall(
        "SELECT event_id, sport, side, market, book, book_odds_american, "
        "model_fair_prob, edge, ev_pct, kelly_fraction, hypothesis_id, run_id "
        "FROM backtest_events "
        "WHERE signal_generated = 1"
    )

    inserted = 0
    for r in rows:
        edge_val = r[7] or 0
        confidence = _signal_confidence(edge_val)
        await db.execute(
            "INSERT OR IGNORE INTO signals "
            "(event_id, sport, signal_type, team, market, book, "
            "odds_american, fair_probability, fair_prob_source, "
            "edge_pct, ev_pct, confidence, kelly_fraction, "
            "recommended_stake, status, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r[0],        # event_id
                r[1],        # sport
                "backtest",  # signal_type
                r[2],        # side/team
                r[3],        # market
                r[4],        # book
                r[5] or 0,   # odds_american
                r[6] or 0,   # fair_probability
                "cross_book_devig",
                edge_val,
                r[8] or 0,   # ev_pct
                confidence,
                r[9],        # kelly_fraction
                None,        # recommended_stake
                "historical",
                f"hypothesis_id={r[10]}, run_id={r[11]}",
            ),
        )
        inserted += 1

    await db.commit()
    logger.info(f"Backfill migration: inserted {inserted} backtest signals into signals table")


async def get_book_tier(db_path: str = DB_PATH, book_key: str = "") -> str:
    """Look up a book's tier (sharp/retail/reference)."""
    async with aiosqlite.connect(db_path) as db:
        row = await db.execute_fetchall(
            "SELECT tier FROM books WHERE book_id = ?", (book_key.lower(),)
        )
        return row[0][0] if row else "retail"
