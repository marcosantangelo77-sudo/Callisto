"""
Single-writer coordinator for SQLite — eliminates writer-writer lock contention.

Problem this solves
-------------------
SQLite WAL allows many concurrent readers but only ONE writer at a time across
ALL connections to a database file. Callisto has ~100 call sites across ~25
modules each opening their own ``aiosqlite.connect()`` to the same DB. Whenever
two of them try to write at the same time, one loses and gets ``database is
locked``. Every prior fix (busy_timeout↑, exponential-backoff retries,
``BEGIN IMMEDIATE``, ``executescript`` → ``execute``, snapshot-drain dance)
treated a symptom of the same disease — N connections × M writers all racing
for ONE writer lock with no global coordinator.

This module is the architectural fix. There is exactly ONE writer connection
per DB path, owned by the coordinator. Every write goes onto an ``asyncio.Queue``
and is drained serially by a single consumer task on that owned connection.
The single connection always has the writer lock when it wants it — no other
in-process connection ever competes — so retries, backoff, and ``BEGIN
IMMEDIATE`` band-aids become unnecessary.

API contract
------------
- ``await get_writer(db_path).execute(sql, params)`` — single statement, returns
  ``lastrowid`` (or ``rowcount`` for non-INSERT).
- ``await get_writer(db_path).executemany(sql, list_of_params)`` — bulk write,
  returns rowcount. The whole batch runs inside one transaction.
- ``await get_writer(db_path).transaction([(sql, params), ...])`` — multi-statement
  atomic transaction. Returns list of per-statement results (lastrowid/rowcount).

Producers see latency = (queue wait) + (single statement run). Under steady
load the queue stays ~0; under bursty backtest writes it grows briefly and
drains fast because the writer never has to retry.

Reads are NOT routed through the coordinator. Reads use any connection (WAL
allows unlimited concurrent readers), so existing read paths stay as-is.

Lifecycle
---------
``start()`` opens the writer connection and launches the drain task.
``stop()`` flushes the queue, cancels the drain task, closes the connection.
Both are called from ``api.py`` lifespan startup/shutdown.

Failure semantics
-----------------
If the underlying ``execute`` raises, the future for that operation is
rejected with the original exception. Other queued operations are unaffected
— the drain loop continues. The writer never crashes the producer's task.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.db_writer")

# Sentinel for shutdown.
_SHUTDOWN = object()

# Per-DB-path coordinator registry.
_coordinators: dict[str, "WriteCoordinator"] = {}
_registry_lock: Optional[asyncio.Lock] = None


def _registry_lock_get() -> asyncio.Lock:
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


class WriteCoordinator:
    """Single-writer coordinator for one SQLite database file.

    Owns one ``aiosqlite.Connection``. All writes for the DB go through this
    coordinator's ``asyncio.Queue`` and are applied serially by one consumer
    task. The owned connection is the only writer for the DB inside this
    process, so writer-writer contention disappears.

    Construct via the module-level ``get_writer(db_path)`` factory rather than
    directly so the registry stays in sync.
    """

    # Tunables (env-configurable).
    QUEUE_MAXSIZE = int(os.getenv("CALLISTO_WRITER_QUEUE_MAX", "10000"))
    BUSY_TIMEOUT_MS = int(os.getenv("CALLISTO_WRITER_BUSY_TIMEOUT_MS", "30000"))
    SLOW_OP_WARN_S = float(os.getenv("CALLISTO_WRITER_SLOW_OP_WARN_S", "2.0"))

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        # Producers currently blocked in _submit() awaiting Queue.put() on a
        # full queue. stop() must settle these too, not only queued entries.
        self._pending_puts: set[asyncio.Future] = set()
        self._running = False
        # Counters for /health visibility.
        self._writes_total = 0
        self._writes_failed = 0
        self._queue_high_water = 0
        self._slowest_op_seconds = 0.0
        self._last_failure: str = ""

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def start(self) -> None:
        """Open the writer connection and launch the drain task."""
        if self._running:
            return
        self._db = await aiosqlite.connect(self.db_path)
        # SECURITY: mark this connection as the routing SINK so the patched
        # aiosqlite.Connection.execute does not loop writes back into the
        # coordinator's own queue (self-routing deadlock).
        try:
            self._db._callisto_is_coord_writer = True
        except Exception:
            pass
        # WAL + sane defaults. busy_timeout still matters for cross-process
        # contention from ad-hoc query scripts; the in-process side won't
        # contend at all because there's only one writer.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA wal_autocheckpoint=1000")
        await self._db.execute("PRAGMA journal_size_limit=67108864")
        await self._db.commit()
        self._queue = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
        self._running = True
        self._task = asyncio.create_task(self._drain_loop(), name=f"db-writer:{self.db_path}")
        logger.info(f"WriteCoordinator started for {self.db_path}")

    async def stop(self, drain_timeout_s: float = 10.0) -> None:
        """Drain the queue and close the connection."""
        if not self._running:
            return
        self._running = False
        # Send a shutdown sentinel so the drain loop exits cleanly after
        # processing any already-queued work. The sentinel enqueue is bounded
        # by the CALLER'S drain timeout — never a hard-coded delay — so a full
        # queue under backpressure cannot inflate stop() beyond the budget.
        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.put((_SHUTDOWN, None, None, None)), timeout=drain_timeout_s)
            except asyncio.TimeoutError:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=drain_timeout_s)
            except asyncio.TimeoutError:
                logger.warning(f"WriteCoordinator drain timed out after {drain_timeout_s}s; cancelling")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        # Settle producers blocked in _submit() on Queue.put(): cancel their
        # admission tasks first (so nothing can enqueue after the sweep below)
        # and let each blocked producer observe CancelledError. Any put that
        # had already landed before cancellation took effect is picked up by
        # the queue sweep that follows.
        if self._pending_puts:
            for put_fut in list(self._pending_puts):
                put_fut.cancel()
            await asyncio.gather(*list(self._pending_puts), return_exceptions=True)
            self._pending_puts.clear()
        # Forced-shutdown settlement: if the drain task was cancelled/timed
        # out, anything still sitting in the queue has a future that no one
        # will ever resolve — its caller would hang forever after stop().
        # Sweep the queue and settle every remaining entry with an explicit
        # shutdown error (never silently swallowed: callers see it raise).
        if self._queue is not None:
            shutdown_err = RuntimeError(
                "WriteCoordinator shut down before this write could be applied"
            )
            abandoned = 0
            while True:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(item, tuple) and len(item) >= 4:
                    fut = item[3]
                    if isinstance(fut, asyncio.Future) and not fut.done():
                        fut.set_exception(shutdown_err)
                        abandoned += 1
            if abandoned:
                logger.warning(
                    f"WriteCoordinator stopped with {abandoned} queued op(s) "
                    f"unsettled; rejected each caller with a shutdown error"
                )
            self._writes_failed += abandoned
        # Clear lifecycle references so a later start() is a clean restart.
        self._task = None
        self._queue = None
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                pass
        logger.info(
            f"WriteCoordinator stopped for {self.db_path}: "
            f"writes={self._writes_total} failed={self._writes_failed} "
            f"queue_hi={self._queue_high_water} slowest={self._slowest_op_seconds:.2f}s"
        )

    # -----------------------------------------------------------------
    # Public write API
    # -----------------------------------------------------------------

    async def execute(self, sql: str, params: tuple = ()) -> int:
        """Run a single write. Returns lastrowid for INSERT, rowcount otherwise."""
        return await self._submit("execute", sql, params)

    async def executemany(self, sql: str, params_list: list) -> int:
        """Run a bulk write. Returns rowcount across the batch."""
        return await self._submit("executemany", sql, params_list)

    async def transaction(self, ops: list[tuple[str, tuple]]) -> list[int]:
        """Run a multi-statement atomic transaction.

        ``ops`` is a list of ``(sql, params)`` pairs. All run inside one
        BEGIN/COMMIT. Returns a list of per-op lastrowid/rowcount values.
        """
        if not ops:
            return []
        return await self._submit("transaction", None, ops)

    async def _submit(self, op_type: str, sql: Optional[str], payload):
        if not self._running or self._queue is None:
            raise RuntimeError(
                "WriteCoordinator not started. Call start() in app lifespan, or "
                "fall through to direct DB writes for one-off scripts."
            )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        # Admission runs as a tracked task so stop() can settle producers
        # blocked here on a full queue (cancel them) and so no entry can
        # land in the queue after stop()'s settlement sweep.
        put_task = loop.create_task(self._queue.put((op_type, sql, payload, fut)))
        self._pending_puts.add(put_task)
        try:
            # Raises CancelledError if stop() settles this producer during
            # shutdown; otherwise admission completed normally.
            await put_task
        except asyncio.CancelledError:
            if self._running:
                # Cancellation came from outside (the producer itself), not
                # from stop(); don't mask it.
                raise
            raise RuntimeError(
                "WriteCoordinator shut down before this write could be admitted"
            )
        finally:
            self._pending_puts.discard(put_task)
        # Track high-water mark for visibility.
        qsize = self._queue.qsize() if self._queue is not None else 0
        if qsize > self._queue_high_water:
            self._queue_high_water = qsize
        return await fut

    # -----------------------------------------------------------------
    # Visibility
    # -----------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "db_path": self.db_path,
            "running": self._running,
            "queue_depth": self._queue.qsize() if self._queue else 0,
            "queue_high_water": self._queue_high_water,
            "writes_total": self._writes_total,
            "writes_failed": self._writes_failed,
            "slowest_op_seconds": round(self._slowest_op_seconds, 3),
            "last_failure": self._last_failure,
        }

    # -----------------------------------------------------------------
    # Drain loop (internal)
    # -----------------------------------------------------------------

    async def _drain_loop(self) -> None:
        """Pull (op_type, sql, payload, fut) tuples and apply serially."""
        assert self._queue is not None and self._db is not None
        while True:
            item = await self._queue.get()
            try:
                if item[0] is _SHUTDOWN:
                    return
                op_type, sql, payload, fut = item
                started = time.monotonic()
                try:
                    result = await self._apply(op_type, sql, payload)
                    if not fut.done():
                        fut.set_result(result)
                    self._writes_total += 1
                except Exception as e:
                    self._writes_failed += 1
                    self._last_failure = f"{type(e).__name__}: {e}"
                    logger.error(
                        f"WriteCoordinator op_type={op_type} failed: {e!r}"
                    )
                    if not fut.done():
                        fut.set_exception(e)
                finally:
                    elapsed = time.monotonic() - started
                    if elapsed > self._slowest_op_seconds:
                        self._slowest_op_seconds = elapsed
                    if elapsed > self.SLOW_OP_WARN_S:
                        logger.warning(
                            f"WriteCoordinator slow op ({op_type}, {elapsed:.2f}s): "
                            f"{(sql or '')[:120]}"
                        )
            except asyncio.CancelledError:
                # Reject the in-flight future so its caller doesn't hang.
                try:
                    if isinstance(item, tuple) and len(item) >= 4:
                        f = item[3]
                        if isinstance(f, asyncio.Future) and not f.done():
                            f.cancel()
                except Exception:
                    pass
                raise
            except Exception as e:  # pragma: no cover - defensive
                logger.exception(f"WriteCoordinator drain loop unexpected error: {e}")

    async def _apply(self, op_type: str, sql, payload):
        assert self._db is not None
        # Defensive: VACUUM must NEVER be routed through the coordinator. The
        # coordinator's sink connection is in aiosqlite's deferred-transaction
        # mode, and SQLite refuses "VACUUM from within a transaction". This is
        # the silent-failure fix: raise loudly so the caller (or test) can see
        # the misuse instead of letting it hide as `writes_failed += 1`.
        if op_type in ("execute", "executemany") and _is_vacuum_sql(sql):
            raise RuntimeError(
                "VACUUM cannot run through the WriteCoordinator — use "
                "tools.schema.vacuum_db() which opens a dedicated autocommit "
                "connection. See tools/db_writer.py _WRITE_RE comment."
            )
        # DDL guard: ALTER/CREATE/DROP/TRUNCATE/REINDEX/ATTACH/DETACH must
        # reach the coordinator only if something upstream bypassed the
        # migration framework. Fail loud so the caller sees which statement
        # escaped instead of it hiding as ``writes_failed += 1``.
        if op_type in ("execute", "executemany") and _is_ddl_sql(sql):
            raise RuntimeError(
                f"DDL statement {sql.strip().split()[0].upper()!r} cannot run "
                f"through the WriteCoordinator. Move it into "
                f"tools/migrations/NNN_*.py and call apply_pending_migrations() "
                f"at startup. Offending SQL: {sql[:200]!r}"
            )
        if op_type == "transaction":
            for _s, _p in (payload or []):
                if _is_vacuum_sql(_s):
                    raise RuntimeError(
                        "VACUUM cannot appear inside a WriteCoordinator "
                        "transaction — use tools.schema.vacuum_db()."
                    )
                if _is_ddl_sql(_s):
                    raise RuntimeError(
                        f"DDL inside a WriteCoordinator transaction is "
                        f"forbidden. Move into tools/migrations/. "
                        f"Offending SQL: {_s[:200]!r}"
                    )
        if op_type == "execute":
            cursor = await self._db.execute(sql, payload or ())
            await self._db.commit()
            # INSERT returns lastrowid; UPDATE/DELETE returns rowcount.
            lastrow = getattr(cursor, "lastrowid", None) or 0
            rowcount = getattr(cursor, "rowcount", -1)
            return lastrow if lastrow else rowcount
        if op_type == "executemany":
            cursor = await self._db.executemany(sql, payload or [])
            await self._db.commit()
            return getattr(cursor, "rowcount", 0)
        if op_type == "transaction":
            results: list = []
            try:
                # aiosqlite is in autocommit-ish "deferred" mode; an explicit
                # BEGIN starts a transaction whose commit happens at .commit().
                await self._db.execute("BEGIN")
                for s, p in payload:
                    cursor = await self._db.execute(s, p or ())
                    lastrow = getattr(cursor, "lastrowid", None) or 0
                    rowcount = getattr(cursor, "rowcount", -1)
                    results.append(lastrow if lastrow else rowcount)
                await self._db.commit()
                return results
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        raise ValueError(f"unknown op_type {op_type!r}")


# ---------------------------------------------------------------------
# Module-level factory + helpers
# ---------------------------------------------------------------------


async def get_writer(db_path: str) -> WriteCoordinator:
    """Return the coordinator for ``db_path``, starting it on first use."""
    db_path = os.path.abspath(db_path)
    coord = _coordinators.get(db_path)
    if coord is not None and coord._running:
        return coord
    lock = _registry_lock_get()
    async with lock:
        coord = _coordinators.get(db_path)
        if coord is None or not coord._running:
            coord = WriteCoordinator(db_path)
            await coord.start()
            _coordinators[db_path] = coord
    return coord


def get_writer_if_running(db_path: str) -> Optional[WriteCoordinator]:
    """Synchronous accessor: return the coordinator only if it's already started.

    Used by ``db_utils.execute_with_retry`` to opt into the coordinator if it
    happens to be available, without forcing an async startup from a sync hot
    path.
    """
    coord = _coordinators.get(os.path.abspath(db_path))
    return coord if (coord is not None and coord._running) else None


async def stop_all() -> None:
    """Stop every coordinator (called from app shutdown)."""
    for coord in list(_coordinators.values()):
        try:
            await coord.stop()
        except Exception:
            logger.exception(f"Error stopping coordinator for {coord.db_path}")
    _coordinators.clear()


def all_stats() -> list[dict]:
    return [c.stats() for c in _coordinators.values()]


def tag_connection(db, db_path: str):
    """Tag an aiosqlite connection with its DB path so ``execute_with_retry``
    routes it through the matching ``WriteCoordinator`` automatically.

    Use right after ``aiosqlite.connect(path)`` in modules that don't go
    through ``tools.schema.open_db`` (which already tags). No-op if the
    connection rejects attribute assignment.
    """
    try:
        db._callisto_db_path = os.path.abspath(db_path)
    except Exception:
        pass
    return db


# ---------------------------------------------------------------------
# Process-wide aiosqlite routing (the actual root-cause fix)
# ---------------------------------------------------------------------
#
# Tagging connections only helps callers that funnel writes through
# ``db_utils.execute_with_retry``. Most modules call ``await db.execute(sql)``
# / ``await db.commit()`` directly on their own connections — those bypass the
# coordinator and contend for SQLite's writer lock against it. Surgically
# tagging ~50 untagged sites is whack-a-mole.
#
# ``install_aiosqlite_routing()`` instead patches ``aiosqlite.connect`` and
# the resulting ``Connection.execute`` / ``executemany`` / ``commit`` once at
# process startup. Every connection thereafter:
#   1. Is auto-tagged with its absolute DB path.
#   2. On ``execute``: if the SQL is a write AND a coordinator is running for
#      that path, the write is routed through the coordinator and a synthetic
#      cursor (lastrowid / rowcount) is returned. Reads pass through directly.
#   3. On ``commit``: a no-op when the matching coordinator is running
#      (the coordinator commits internally), otherwise legacy commit.
#
# This makes the single-writer pattern *transparent* to every module —
# including third-party scripts and future code — without per-call-site edits.

_INSTALLED = False

# Heuristic: detect SQL statements that take the writer lock. Anything that
# isn't clearly a SELECT/PRAGMA-read/EXPLAIN we treat as a write to be safe.
#
# DDL IS DELIBERATELY EXCLUDED. VACUUM, ALTER, CREATE TABLE/INDEX, DROP,
# REINDEX, TRUNCATE, ATTACH/DETACH must NEVER route through the coordinator:
#
#   * VACUUM: SQLite refuses "VACUUM from within a transaction"; aiosqlite's
#     default isolation opens an implicit tx around every execute().
#   * ALTER TABLE ADD COLUMN: idempotent callers (cache_manager.rotate_caches,
#     ensure_schema._safe_add_column fallback) run ALTER every startup. The
#     second run fails with "duplicate column name"; the coordinator ate the
#     exception and incremented writes_failed. Accounted for 23 of 28,394
#     silent failures in the data-layer audit window. DDL now lives in
#     ``tools/migrations/*.py`` and runs on a dedicated autocommit stdlib
#     connection (see ``apply_pending_migrations``).
#   * CREATE/DROP TABLE/INDEX: same reasoning — belongs in a migration.
#
# INSERT/UPDATE/DELETE/REPLACE are the ONLY things that should hit the
# writer queue. Everything else — transaction boundaries (BEGIN/COMMIT/…)
# is controlled by the coordinator itself and must also be excluded.
#
# If DDL is ever submitted through this path, ``_apply`` raises loudly rather
# than letting it hide as ``writes_failed += 1`` (silent-failure → loud-
# failure upgrade).
import re as _re_writer
_WRITE_RE = _re_writer.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b",
    _re_writer.IGNORECASE,
)
_VACUUM_RE = _re_writer.compile(r"^\s*VACUUM\b", _re_writer.IGNORECASE)
# DDL statements that must bypass the coordinator completely. Used by
# ``_apply`` as a loud-failure guard: if one of these ever reaches the
# drain loop, something re-enabled DDL routing and must be fixed upstream.
_DDL_RE = _re_writer.compile(
    r"^\s*(ALTER|CREATE|DROP|TRUNCATE|REINDEX|ATTACH|DETACH)\b",
    _re_writer.IGNORECASE,
)


def _is_write_sql(sql) -> bool:
    if not isinstance(sql, str):
        return False
    return bool(_WRITE_RE.match(sql))


def _is_vacuum_sql(sql) -> bool:
    if not isinstance(sql, str):
        return False
    return bool(_VACUUM_RE.match(sql))


def _is_ddl_sql(sql) -> bool:
    if not isinstance(sql, str):
        return False
    return bool(_DDL_RE.match(sql))


def install_aiosqlite_routing() -> None:
    """Monkey-patch aiosqlite so every Connection auto-routes writes through
    the matching ``WriteCoordinator``. Idempotent. Call once at app startup
    BEFORE any module opens a connection.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    import aiosqlite as _aios
    _orig_connect = _aios.connect
    _orig_conn_execute = _aios.Connection.execute
    _orig_conn_executemany = _aios.Connection.executemany
    _orig_conn_commit = _aios.Connection.commit

    # Wrap connect: tag the resulting Connection with its absolute path.
    # aiosqlite.connect returns a Connection instance that's used both as
    # `async with ctx as db:` and `db = await ctx`. In both cases __aenter__
    # / __await__ resolve to the SAME object — so tagging the ctx itself is
    # sufficient and survives the connect handshake. (Instance-method
    # patching of dunders does NOT work because Python looks them up on the
    # class, not the instance.)
    def _patched_connect(database, *args, **kwargs):
        ctx = _orig_connect(database, *args, **kwargs)
        try:
            tag_connection(ctx, str(database))
        except Exception:
            pass
        return ctx

    async def _patched_execute(self, sql, parameters=None):
        # Skip routing when called on the coordinator's OWN sink connection
        # (avoids self-routing deadlock).
        if getattr(self, "_callisto_is_coord_writer", False):
            return await _orig_conn_execute(self, sql, parameters or ())
        # Route writes through the coordinator if one is running for our DB.
        if _is_write_sql(sql):
            db_path = getattr(self, "_callisto_db_path", None)
            if db_path:
                coord = get_writer_if_running(db_path)
                if coord is not None:
                    value = await coord.execute(sql, parameters or ())
                    return _RoutedCursor(value)
        return await _orig_conn_execute(self, sql, parameters or ())

    async def _patched_executemany(self, sql, parameters):
        if getattr(self, "_callisto_is_coord_writer", False):
            return await _orig_conn_executemany(self, sql, parameters)
        if _is_write_sql(sql):
            db_path = getattr(self, "_callisto_db_path", None)
            if db_path:
                coord = get_writer_if_running(db_path)
                if coord is not None:
                    rc = await coord.executemany(sql, list(parameters))
                    return _RoutedCursor(rc)
        return await _orig_conn_executemany(self, sql, parameters)

    async def _patched_commit(self):
        # The coordinator's own sink commits internally (and must, to flush
        # WAL). Other connections' commits become no-ops when a coordinator
        # is in charge of the DB they target.
        if getattr(self, "_callisto_is_coord_writer", False):
            return await _orig_conn_commit(self)
        db_path = getattr(self, "_callisto_db_path", None)
        if db_path and get_writer_if_running(db_path) is not None:
            return None
        return await _orig_conn_commit(self)

    _aios.connect = _patched_connect
    _aios.Connection.execute = _patched_execute
    _aios.Connection.executemany = _patched_executemany
    _aios.Connection.commit = _patched_commit
    _INSTALLED = True
    logger.info(
        "aiosqlite routing installed — every Connection write will route "
        "through the matching WriteCoordinator transparently."
    )


class _RoutedCursor:
    """Cursor shim returned by the routed Connection.execute path.

    Exposes ``lastrowid`` and ``rowcount``; raises if used as an iterable
    (writes don't iterate, so this catches misuse loudly).
    """

    def __init__(self, value: int):
        self.lastrowid = value if value > 0 else 0
        self.rowcount = value

    async def fetchone(self):
        raise RuntimeError(
            "Cursor returned by routed-write path supports lastrowid/rowcount only. "
            "If you need to fetch rows after a write, issue a separate SELECT."
        )

    async def fetchall(self):
        raise RuntimeError(
            "Cursor returned by routed-write path supports lastrowid/rowcount only."
        )

    def __aiter__(self):
        raise RuntimeError(
            "Cursor returned by routed-write path is not iterable."
        )
