"""Migration discovery, locking, and apply loop.

Design invariants:
- Migrations run on a **dedicated stdlib sqlite3 connection in autocommit
  mode** (``isolation_level=None``). aiosqlite's default deferred-transaction
  mode + the process-wide routing patch (``db_writer.install_aiosqlite_routing``)
  would both interfere with DDL. The stdlib connection is also UNTAGGED, so
  the aiosqlite monkey-patch can't route these calls through the
  WriteCoordinator.
- Each migration's ``up()`` runs inside a single explicit transaction
  (``BEGIN IMMEDIATE`` … ``COMMIT``). On exception we ``ROLLBACK``, log, and
  halt the run — later migrations are not attempted. SQLite DDL is
  transactional (unlike MySQL), so this gives atomic per-migration apply.
- Concurrent processes serialize via ``_migration_lock`` table + ``BEGIN
  EXCLUSIVE`` on it. The second process blocks until the first commits, then
  sees the first's ``schema_migrations`` rows and applies nothing.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import logging
import os
import pkgutil
import re
import sqlite3
import time
from typing import Callable, Optional

logger = logging.getLogger("callisto.migrations")

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.py$")


@dataclasses.dataclass(frozen=True)
class Migration:
    version: int
    name: str
    module_name: str
    up: Callable[[sqlite3.Connection], None]
    down: Optional[Callable[[sqlite3.Connection], None]]
    source_checksum: str


def _migration_source_checksum(module) -> str:
    """SHA-256 of the migration module's source file.

    Stored in ``schema_migrations.checksum`` so a later audit can detect
    "someone edited 002_add_archived after it was applied, the DB may not
    match the committed code."
    """
    try:
        src_path = getattr(module, "__file__", None)
        if not src_path or not os.path.exists(src_path):
            return ""
        with open(src_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return ""


def discover_migrations() -> list[Migration]:
    """Import every ``NNN_name.py`` file under ``tools/migrations/`` and
    return them sorted by version.

    A migration module MUST expose ``up(conn)`` as a top-level function.
    ``down(conn)`` is optional.
    """
    # Local import to dodge circular import during package __init__.
    from . import __path__ as _pkg_path
    pkg_name = __name__.rsplit(".", 1)[0]  # "tools.migrations"

    found: list[Migration] = []
    for _finder, mod_name, _is_pkg in pkgutil.iter_modules(_pkg_path):
        m = _MIGRATION_FILENAME_RE.match(f"{mod_name}.py")
        if not m:
            continue
        version = int(m.group(1))
        short_name = m.group(2)
        full_mod = f"{pkg_name}.{mod_name}"
        module = importlib.import_module(full_mod)
        up_fn = getattr(module, "up", None)
        if not callable(up_fn):
            logger.error(f"Migration {full_mod} has no callable up(conn); skipping")
            continue
        down_fn = getattr(module, "down", None)
        found.append(
            Migration(
                version=version,
                name=short_name,
                module_name=full_mod,
                up=up_fn,
                down=down_fn if callable(down_fn) else None,
                source_checksum=_migration_source_checksum(module),
            )
        )
    found.sort(key=lambda x: x.version)

    # Duplicate-version guard — two files with the same numeric prefix would
    # be ambiguous. Fail loud rather than silently run only one.
    seen: set[int] = set()
    for mig in found:
        if mig.version in seen:
            raise RuntimeError(
                f"Duplicate migration version {mig.version:03d}; check "
                f"tools/migrations/ for two files sharing that prefix."
            )
        seen.add(mig.version)
    return found


def _connect_autocommit(db_path: str) -> sqlite3.Connection:
    """Dedicated stdlib connection in autocommit mode for DDL.

    Why not aiosqlite? The process-wide ``install_aiosqlite_routing`` patch
    would try to route CREATE/ALTER/DROP through the WriteCoordinator, which
    is the exact bug this module replaces. An untagged stdlib connection is
    invisible to the patch.
    """
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=120.0)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # foreign_keys pragma is a per-connection setting; we explicitly LEAVE IT OFF
    # for the migration connection because migrations may need to rewrite data
    # that would trigger FK cascades. ensure_schema.open_db sets it ON for
    # normal app connections — that's the right place.
    return conn


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Create ``schema_migrations`` and ``_migration_lock`` if missing.

    Upgrades the existing ``schema_migrations`` table (which only had
    version/name/applied_at) by adding ``checksum`` and ``bootstrap`` columns
    idempotently.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT,
            checksum TEXT,
            bootstrap INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Idempotent column add for pre-existing tables that only had the
    # v1 shape (version, name, applied_at).
    for col, ddl in (
        ("checksum", "ALTER TABLE schema_migrations ADD COLUMN checksum TEXT"),
        ("bootstrap", "ALTER TABLE schema_migrations ADD COLUMN bootstrap INTEGER NOT NULL DEFAULT 0"),
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migration_lock (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            locked_at TEXT,
            locked_by TEXT
        )
        """
    )
    # Seed the single-row lock table.
    conn.execute("INSERT OR IGNORE INTO _migration_lock (id) VALUES (1)")


def get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of versions present in ``schema_migrations``.

    Includes bootstrap rows (``applied_at IS NULL``) — bootstrapped migrations
    must NOT be re-run against an existing DB.
    """
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(r[0]) for r in rows}


def bootstrap_existing_db(
    conn: sqlite3.Connection, migrations: list[Migration]
) -> int:
    """Seed ``schema_migrations`` with all known migrations marked as
    ``bootstrap=1, applied_at=NULL`` — but ONLY if the DB already has
    Callisto's core tables and the migrations table is still empty.

    The heuristic for "existing DB": the ``hypotheses`` table exists. That's
    the oldest canonical table; if it's present, ``ensure_schema`` has run
    at least once before this migration framework was introduced, so all
    pre-existing migrations are implicitly satisfied.

    Returns the number of rows seeded.
    """
    existing_rows = conn.execute(
        "SELECT COUNT(*) FROM schema_migrations"
    ).fetchone()[0]
    if existing_rows > 0:
        return 0

    hyp_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hypotheses'"
    ).fetchone()
    if not hyp_exists:
        # Fresh DB — let migrations run normally; don't bootstrap.
        return 0

    inserted = 0
    for mig in migrations:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations "
            "(version, name, applied_at, checksum, bootstrap) "
            "VALUES (?, ?, NULL, ?, 1)",
            (mig.version, mig.name, mig.source_checksum),
        )
        inserted += 1
    logger.info(
        f"Bootstrapped schema_migrations with {inserted} pre-existing "
        f"migrations (existing DB detected: 'hypotheses' table present)."
    )
    return inserted


def _acquire_exclusive_lock(
    conn: sqlite3.Connection, timeout_s: float = 60.0
) -> None:
    """Acquire an exclusive transaction on ``_migration_lock``.

    Two processes both calling ``apply_pending_migrations`` at the same
    moment will serialize here: ``BEGIN IMMEDIATE`` grabs the RESERVED lock;
    the ``UPDATE`` then takes the writer lock. SQLite's ``busy_timeout``
    (set in ``_connect_autocommit``) makes the second process wait up to 60s
    rather than immediately failing with ``database is locked``.
    """
    deadline = time.monotonic() + timeout_s
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE _migration_lock SET locked_at = ?, locked_by = ? WHERE id = 1",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), f"pid:{os.getpid()}"),
            )
            return
        except sqlite3.OperationalError as e:
            last_err = e
            # BEGIN IMMEDIATE failed because another tx holds the lock;
            # sleep briefly and retry within the deadline.
            time.sleep(0.25)
    raise TimeoutError(
        f"Could not acquire _migration_lock within {timeout_s}s: {last_err!r}"
    )


def _release_lock(conn: sqlite3.Connection, *, commit: bool) -> None:
    try:
        if commit:
            conn.execute("COMMIT")
        else:
            conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        # Not in a transaction — nothing to release.
        pass


def apply_pending_migrations(db_path: str) -> dict:
    """Apply every migration whose version isn't already in
    ``schema_migrations``. Idempotent, safe to call at every startup.

    Returns a status dict: ``{applied: [versions], skipped: [versions],
    bootstrapped: N}``.

    Raises on the first migration that fails. Previously-applied migrations
    stay durable; the failing one is rolled back.
    """
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    migrations = discover_migrations()

    # Phase 1: open a short-lived setup connection to create the bookkeeping
    # tables and bootstrap if needed. No migrations run here.
    setup = _connect_autocommit(db_path)
    try:
        ensure_migration_table(setup)
        bootstrapped = bootstrap_existing_db(setup, migrations)
    finally:
        setup.close()

    applied: list[int] = []
    skipped: list[int] = []

    # Phase 2: one connection does both the locking and the applying. SQLite's
    # writer lock is per-DB-file (not per-connection); once we BEGIN IMMEDIATE
    # on it we own the only writer slot for the whole DB. A SECOND connection
    # trying BEGIN IMMEDIATE would block until we COMMIT. So migrations run
    # on this same connection, each inside its own tx; between migrations we
    # briefly drop to autocommit but a concurrent process can't observe a
    # half-applied schema because each migration's DDL + the matching
    # schema_migrations INSERT commit together.
    #
    # To handle concurrent-process serialization: we acquire the advisory
    # lock once here, re-check applied versions INSIDE each migration tx so
    # a peer that applied the same migration between our checks is detected
    # via the PRIMARY KEY conflict on schema_migrations.version — we catch
    # that specific case as "already applied by peer" and continue.
    conn = _connect_autocommit(db_path)
    try:
        _acquire_exclusive_lock(conn)
        _release_lock(conn, commit=True)

        applied_now = get_applied_versions(conn)

        for mig in migrations:
            if mig.version in applied_now:
                skipped.append(mig.version)
                continue
            logger.info(f"Applying migration {mig.version:03d}_{mig.name}")
            conn.execute("BEGIN IMMEDIATE")
            # Re-check inside the tx: a peer process may have applied the
            # same migration between our applied_now snapshot and now.
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (mig.version,),
            ).fetchone()
            if already:
                conn.execute("COMMIT")
                skipped.append(mig.version)
                continue
            try:
                mig.up(conn)
                conn.execute(
                    "INSERT INTO schema_migrations "
                    "(version, name, applied_at, checksum, bootstrap) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (
                        mig.version,
                        mig.name,
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        mig.source_checksum,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            applied.append(mig.version)
            logger.info(f"Applied migration {mig.version:03d}_{mig.name}")
    finally:
        conn.close()

    logger.info(
        f"Migrations complete: applied={applied} skipped={skipped} "
        f"bootstrapped={bootstrapped}"
    )
    return {
        "applied": applied,
        "skipped": skipped,
        "bootstrapped": bootstrapped,
        "total_versions": [m.version for m in migrations],
    }
