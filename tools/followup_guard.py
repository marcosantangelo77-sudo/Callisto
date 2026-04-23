"""Guards for Callisto's task auto-followup mechanism.

Background
----------
``api._maybe_auto_followup`` used to re-enqueue tasks whenever a
task's result contained the literal string "Next step:" and >20 chars of
trailing text. There was no depth cap, no dedup, no quality gate, no
chain-budget. A hallucinated next-step could spawn a runaway chain or,
in the worst case, a recursive loop that burned the entire Claude hourly
budget.

This module provides the hardened replacement. It is deliberately a
separate module so:
  1. ``api.py`` stays readable — the guard logic is testable in isolation.
  2. Migrations live alongside the guards that consume the new columns.
  3. Tests can exercise each guard independently without spinning up
     the full FastAPI app.

Guards (all toggleable via env; all default-on):
  - ``CALLISTO_MAX_FOLLOWUP_DEPTH``     hard cap on follow-up nesting (default 5)
  - ``CALLISTO_FOLLOWUP_DEDUP``         semantic dedup within a 1h window
  - ``CALLISTO_FOLLOWUP_QUALITY_GATE``  reject vague / verbatim / entity-free followups
  - ``CALLISTO_MAX_FOLLOWUP_FANOUT``    direct followups per parent (default 3)
  - ``CALLISTO_MAX_CHAIN_BUDGET_USD``   cumulative cost ceiling per chain (default 1.00)
  - ``CALLISTO_FOLLOWUP_DEDUP_WINDOW_S``  dedup lookback window (default 3600)
  - ``CALLISTO_FOLLOWUP_DEDUP_THRESHOLD`` cosine threshold (default 0.95)

The schema migration adds three columns to ``task_queue``:
  - ``followup_depth``  (INTEGER DEFAULT 0)  — 0 for user-initiated
  - ``parent_task_id``  (INTEGER)            — direct parent, or NULL
  - ``root_task_id``    (INTEGER)            — 0-depth ancestor, always self for depth=0
  - ``cost_usd``        (REAL DEFAULT 0)     — Claude escalation cost for this task
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.followup_guard")


# ── Env toggles ──────────────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def max_depth() -> int:
    return _env_int("CALLISTO_MAX_FOLLOWUP_DEPTH", 5)


def max_fanout() -> int:
    return _env_int("CALLISTO_MAX_FOLLOWUP_FANOUT", 3)


def max_chain_budget_usd() -> float:
    return _env_float("CALLISTO_MAX_CHAIN_BUDGET_USD", 1.00)


def dedup_enabled() -> bool:
    return _env_bool("CALLISTO_FOLLOWUP_DEDUP", True)


def quality_gate_enabled() -> bool:
    return _env_bool("CALLISTO_FOLLOWUP_QUALITY_GATE", True)


def dedup_window_seconds() -> int:
    return _env_int("CALLISTO_FOLLOWUP_DEDUP_WINDOW_S", 3600)


def dedup_threshold() -> float:
    return _env_float("CALLISTO_FOLLOWUP_DEDUP_THRESHOLD", 0.95)


# ── Cost model ───────────────────────────────────────────────────────────
# Rough per-task cost estimate used for chain-budget accounting. We don't
# have a live per-request cost hook yet, so we use a conservative bucket:
# one Claude escalation averages ~$0.10 on Opus 4.6 at typical token
# counts, and most tasks escalate once. Callers that have a precise cost
# can pass it via ``record_task_cost``.
DEFAULT_TASK_COST_USD = _env_float("CALLISTO_DEFAULT_TASK_COST_USD", 0.10)


# ── Outcome codes ────────────────────────────────────────────────────────

@dataclass
class FollowupDecision:
    """Result of evaluating whether a followup should be enqueued.

    ``allowed=True``   → caller should proceed to ``queue.submit_task``
                         using ``query`` and associated metadata.
    ``allowed=False``  → caller should LOG ``reason`` and drop the
                         followup. ``merge_target_id`` is set when the
                         rejection is a dedup-merge (the caller may want
                         to attach context to that existing task).
    """
    allowed: bool
    reason: str
    query: str = ""
    parent_task_id: Optional[int] = None
    root_task_id: Optional[int] = None
    depth: int = 0
    merge_target_id: Optional[int] = None


# ── Schema migration ─────────────────────────────────────────────────────

async def ensure_followup_columns(db: aiosqlite.Connection) -> None:
    """Idempotently add followup bookkeeping columns to ``task_queue``.

    Safe to call on every startup — identical contract to
    ``tools.schema._safe_add_column``. We intentionally re-implement the
    "already exists" swallow here rather than importing to avoid a circular
    import between ``tools.schema`` (which calls into multiple subsystems)
    and the followup guard.
    """
    cols = [
        ("followup_depth", "INTEGER NOT NULL DEFAULT 0"),
        ("parent_task_id", "INTEGER"),
        ("root_task_id", "INTEGER"),
        ("cost_usd", "REAL NOT NULL DEFAULT 0"),
    ]
    for col, coltype in cols:
        try:
            await db.execute(f"ALTER TABLE task_queue ADD COLUMN {col} {coltype}")
            await db.commit()
            logger.info("followup_guard: added task_queue.%s", col)
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            logger.warning(
                "followup_guard: failed to add task_queue.%s: %r", col, e
            )

    # Back-fill root_task_id for depth=0 rows where it's NULL so the chain
    # queries can rely on root_task_id being populated. This is a one-shot
    # no-op once the column is already filled.
    try:
        await db.execute(
            "UPDATE task_queue SET root_task_id = task_id "
            "WHERE root_task_id IS NULL AND followup_depth = 0"
        )
        await db.commit()
    except Exception as e:
        logger.debug("followup_guard: root backfill skipped: %r", e)

    # Helpful index for chain lookups. CREATE IF NOT EXISTS is safe.
    try:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_queue_chain "
            "ON task_queue(root_task_id, followup_depth)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_queue_parent "
            "ON task_queue(parent_task_id)"
        )
        await db.commit()
    except Exception as e:
        logger.debug("followup_guard: index create skipped: %r", e)


# ── Row lookup ───────────────────────────────────────────────────────────

async def get_task_meta(
    db: aiosqlite.Connection, task_id: int
) -> Optional[dict]:
    """Fetch the followup bookkeeping fields for ``task_id``.

    Returns a dict with keys ``task_id, query, followup_depth,
    parent_task_id, root_task_id, cost_usd, created_at`` or ``None`` if
    the row doesn't exist / predates the migration.
    """
    try:
        # Refresh WAL snapshot so we see rows committed by the worker
        # coordinator.
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT task_id, query, followup_depth, parent_task_id, "
            "root_task_id, cost_usd, created_at "
            "FROM task_queue WHERE task_id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "task_id": row[0],
            "query": row[1],
            "followup_depth": row[2] or 0,
            "parent_task_id": row[3],
            "root_task_id": row[4] or row[0],
            "cost_usd": row[5] or 0.0,
            "created_at": row[6],
        }
    except Exception as e:
        # Pre-migration DB — columns don't exist yet. Treat the task as
        # a depth-0 root so followup semantics still work. The migration
        # is run at startup so this should only fire during tests that
        # skip ensure_schema().
        logger.debug("get_task_meta fallback (pre-migration?): %r", e)
        try:
            cur = await db.execute(
                "SELECT task_id, query, created_at "
                "FROM task_queue WHERE task_id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {
                "task_id": row[0],
                "query": row[1],
                "followup_depth": 0,
                "parent_task_id": None,
                "root_task_id": row[0],
                "cost_usd": 0.0,
                "created_at": row[2],
            }
        except Exception:
            return None


# ── Quality gate ─────────────────────────────────────────────────────────

# Vague phrases that commonly appear in low-value LLM "next step" output.
# A followup whose query (after stripping the AUTO-FOLLOWUP header) is
# dominated by these gets rejected.
_VAGUE_PHRASES = (
    "investigate further",
    "look into this",
    "look into it",
    "dig deeper",
    "explore this",
    "more research needed",
    "further analysis",
    "to be determined",
    "tbd",
    "follow up",
    "follow-up",
    "keep monitoring",
    "keep watching",
    "revisit later",
    "needs more data",
)

# Patterns that indicate a concrete entity/reference in the query.
# The quality gate requires at least ONE of these. Matches are case-
# insensitive. A hit = "this followup is about a real thing".
_ENTITY_PATTERNS = (
    # Team/game/event IDs (numeric or alphanumeric)
    re.compile(r"\bevent[_\s-]?id[:\s=]*([a-z0-9_-]{3,})", re.I),
    re.compile(r"\bgame[_\s-]?id[:\s=]*([a-z0-9_-]{3,})", re.I),
    re.compile(r"\bhypothesis[_\s-]?id[:\s=]*([a-z0-9_-]{3,})", re.I),
    re.compile(r"\bplayer[_\s-]?id[:\s=]*([a-z0-9_-]{3,})", re.I),
    re.compile(r"\bsession[_\s-]?id[:\s=]*([a-z0-9-]{8,})", re.I),
    # Capitalised proper nouns (two+ Title-Case words = likely a player
    # or team name, e.g. "Jayson Tatum", "Atlanta Braves"). Weak signal
    # but catches most real entities.
    re.compile(r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}"),
    # Known hypothesis naming convention — ``mlb_early_home_fav`` etc.
    re.compile(r"\b[a-z]+_[a-z]+_[a-z]+(?:_[a-z]+)*\b"),
    # Dollar amounts, ML prices with sign, over/under lines
    re.compile(r"\b[+-]\d{2,4}\b"),
    re.compile(r"\b(?:over|under)\s+\d+(?:\.\d+)?", re.I),
)


def _strip_followup_header(query: str) -> str:
    """Remove the leading ``AUTO-FOLLOWUP from task N:`` wrapper for comparison."""
    m = re.match(
        r"^\s*AUTO-FOLLOWUP\s+from\s+task\s+\d+\s*:\s*", query, flags=re.I
    )
    return query[m.end():].strip() if m else query.strip()


def _token_edit_distance_ratio(a: str, b: str) -> float:
    """Ratio of differing tokens to total. 0 = identical, 1 = fully disjoint.

    Cheap proxy for Levenshtein — we care about "are these meaningfully
    different queries" not about exact distance. Tokenisation is whitespace
    + punctuation splitting; casing is normalised.
    """
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta and not tb:
        return 0.0
    union = ta | tb
    common = ta & tb
    if not union:
        return 0.0
    return 1.0 - (len(common) / len(union))


def evaluate_quality(parent_query: str, followup_query: str) -> tuple[bool, str]:
    """Return (passes_gate, reason).

    Rejects a followup query when ANY of:
      - shorter than 20 non-header characters (already length-gated upstream
        but we double-check here so direct test callers see consistent logic)
      - dominated by vague phrases with no concrete entity
      - identical (post-normalisation) to the parent
      - <30% token-level difference from the parent
      - contains no extractable entity pattern

    The gate is intentionally conservative: false-negatives (useful
    followups rejected) are cheap — the user can re-submit. False-positives
    (garbage followups accepted) cost credits and pollute the queue.
    """
    payload = _strip_followup_header(followup_query)
    parent_payload = _strip_followup_header(parent_query)

    if len(payload) < 20:
        return False, "query_too_short"

    # Verbatim or near-verbatim to parent.
    if payload.lower().strip() == parent_payload.lower().strip():
        return False, "verbatim_duplicate_of_parent"

    diff_ratio = _token_edit_distance_ratio(parent_payload, payload)
    if diff_ratio < 0.30:
        return False, f"too_similar_to_parent(diff_ratio={diff_ratio:.2f})"

    # Vague-phrase check. If the query IS one of the vague phrases (or
    # contains nothing beyond vague-phrase tokens), reject.
    low = payload.lower()
    vague_hit = any(phrase in low for phrase in _VAGUE_PHRASES)
    entity_hit = any(p.search(payload) for p in _ENTITY_PATTERNS)

    if vague_hit and not entity_hit:
        return False, "vague_language_no_entity"

    if not entity_hit:
        # Even without explicit vague phrases, no entity means we have
        # nothing concrete to research against.
        return False, "no_extractable_entity"

    return True, "ok"


# ── Dedup (semantic) ─────────────────────────────────────────────────────

async def find_near_duplicate(
    db: aiosqlite.Connection,
    followup_query: str,
    window_seconds: Optional[int] = None,
    threshold: Optional[float] = None,
) -> Optional[int]:
    """Return the task_id of a near-duplicate task in the recent window, or None.

    The check embeds ``followup_query`` and compares against recent queries
    embedded on the fly (no persistent store for followup dedup — the
    cardinality is low so a linear scan over last-hour rows is cheap).

    Any failure in embedding/DB path returns ``None`` (dedup best-effort;
    never block a followup on vector-store flakes).
    """
    window = window_seconds if window_seconds is not None else dedup_window_seconds()
    thr = threshold if threshold is not None else dedup_threshold()

    try:
        # Refresh WAL
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT task_id, query FROM task_queue "
            "WHERE created_at > datetime('now', ?) "
            "ORDER BY created_at DESC LIMIT 200",
            (f"-{int(window)} seconds",),
        )
        rows = await cur.fetchall()
    except Exception as e:
        logger.debug("dedup: task_queue scan failed: %r", e)
        return None

    if not rows:
        return None

    # First pass: exact-normalised-string match. Catches the
    # "LLM emitted literally the same next-step twice" pattern without
    # any embedding calls.
    target_norm = _strip_followup_header(followup_query).lower().strip()
    for task_id, q in rows:
        if _strip_followup_header(q or "").lower().strip() == target_norm:
            return int(task_id)

    # Second pass: embedding cosine. Lazy-import to keep followup_guard
    # import-cheap for test paths that don't need Ollama.
    try:
        from tools.embeddings import embed_text, cosine_similarity
    except Exception as e:
        logger.debug("dedup: embeddings unavailable: %r", e)
        return None

    try:
        target_vec = await embed_text(target_norm)
    except Exception as e:
        logger.debug("dedup: embed(target) failed: %r", e)
        return None

    best_id = None
    best_sim = 0.0
    for task_id, q in rows:
        if not q:
            continue
        try:
            cand_vec = await embed_text(_strip_followup_header(q))
        except Exception:
            continue
        sim = cosine_similarity(target_vec, cand_vec)
        if sim > best_sim:
            best_sim = sim
            best_id = int(task_id)

    if best_id is not None and best_sim >= thr:
        logger.info(
            "dedup: followup matched task %d at cosine=%.3f (>= %.2f)",
            best_id, best_sim, thr,
        )
        return best_id
    return None


# ── Fan-out & budget checks ──────────────────────────────────────────────

async def count_direct_followups(
    db: aiosqlite.Connection, parent_task_id: int
) -> int:
    """Return how many direct-child followups this parent has already spawned."""
    try:
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT COUNT(*) FROM task_queue WHERE parent_task_id = ?",
            (parent_task_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("count_direct_followups failed: %r", e)
        return 0


async def chain_cost_usd(
    db: aiosqlite.Connection, root_task_id: int
) -> float:
    """Return the sum of ``cost_usd`` across the chain rooted at ``root_task_id``."""
    try:
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM task_queue "
            "WHERE root_task_id = ?",
            (root_task_id,),
        )
        row = await cur.fetchone()
        return float(row[0]) if row else 0.0
    except Exception as e:
        logger.debug("chain_cost_usd failed: %r", e)
        return 0.0


# ── Cost recording ───────────────────────────────────────────────────────

async def record_task_cost(
    db: aiosqlite.Connection, task_id: int, cost_usd: float
) -> None:
    """Idempotently set cost_usd on a task row. Safe on pre-migration DBs."""
    try:
        await db.execute(
            "UPDATE task_queue SET cost_usd = ? WHERE task_id = ?",
            (float(cost_usd), int(task_id)),
        )
        await db.commit()
    except Exception as e:
        logger.debug("record_task_cost skipped for %d: %r", task_id, e)


# ── Orchestration ────────────────────────────────────────────────────────

async def evaluate_followup(
    db: aiosqlite.Connection,
    parent_task_id: int,
    proposed_query: str,
) -> FollowupDecision:
    """Run every guard and return a single Decision.

    Order matters — cheap checks first so noisy rejects don't cost
    embedding calls:
      1. Load parent meta (must exist).
      2. Depth cap.
      3. Fan-out cap.
      4. Quality gate.
      5. Chain budget.
      6. Semantic dedup (most expensive).

    The caller (``api._maybe_auto_followup``) is responsible for actually
    enqueuing when ``allowed`` is True, using the returned ``query`` plus
    ``parent_task_id`` / ``root_task_id`` / ``depth`` for the insert.
    """
    parent = await get_task_meta(db, parent_task_id)
    if parent is None:
        return FollowupDecision(
            allowed=False, reason="parent_not_found", query=proposed_query
        )

    parent_depth = int(parent["followup_depth"])
    parent_query = parent["query"] or ""
    root_id = int(parent["root_task_id"] or parent_task_id)
    new_depth = parent_depth + 1

    # 1) Depth
    cap = max_depth()
    if new_depth > cap:
        logger.warning(
            "followup_depth_exceeded: parent=%d depth=%d cap=%d",
            parent_task_id, new_depth, cap,
        )
        return FollowupDecision(
            allowed=False,
            reason="followup_depth_exceeded",
            query=proposed_query,
            parent_task_id=parent_task_id,
            root_task_id=root_id,
            depth=new_depth,
        )

    # 2) Fan-out
    fanout_cap = max_fanout()
    direct = await count_direct_followups(db, parent_task_id)
    if direct >= fanout_cap:
        logger.warning(
            "followup_fanout_exceeded: parent=%d existing=%d cap=%d",
            parent_task_id, direct, fanout_cap,
        )
        return FollowupDecision(
            allowed=False,
            reason="followup_fanout_exceeded",
            query=proposed_query,
            parent_task_id=parent_task_id,
            root_task_id=root_id,
            depth=new_depth,
        )

    # 3) Quality
    if quality_gate_enabled():
        passed, reason = evaluate_quality(parent_query, proposed_query)
        if not passed:
            logger.info(
                "followup_quality_rejected: parent=%d reason=%s",
                parent_task_id, reason,
            )
            return FollowupDecision(
                allowed=False,
                reason=f"quality_gate:{reason}",
                query=proposed_query,
                parent_task_id=parent_task_id,
                root_task_id=root_id,
                depth=new_depth,
            )

    # 4) Chain budget
    budget_cap = max_chain_budget_usd()
    spent = await chain_cost_usd(db, root_id)
    if spent >= budget_cap:
        logger.warning(
            "followup_chain_budget_exceeded: root=%d spent=%.4f cap=%.4f",
            root_id, spent, budget_cap,
        )
        return FollowupDecision(
            allowed=False,
            reason="chain_budget_exceeded",
            query=proposed_query,
            parent_task_id=parent_task_id,
            root_task_id=root_id,
            depth=new_depth,
        )

    # 5) Dedup — merge into an existing recent task when too similar.
    if dedup_enabled():
        dup = await find_near_duplicate(db, proposed_query)
        if dup is not None:
            logger.info(
                "followup_dedup_merge: parent=%d → existing task %d",
                parent_task_id, dup,
            )
            return FollowupDecision(
                allowed=False,
                reason="dedup_merge",
                query=proposed_query,
                parent_task_id=parent_task_id,
                root_task_id=root_id,
                depth=new_depth,
                merge_target_id=dup,
            )

    return FollowupDecision(
        allowed=True,
        reason="ok",
        query=proposed_query,
        parent_task_id=parent_task_id,
        root_task_id=root_id,
        depth=new_depth,
    )


# ── Insert helper ────────────────────────────────────────────────────────

async def insert_followup(
    db: aiosqlite.Connection,
    query: str,
    priority: int,
    parent_task_id: int,
    root_task_id: int,
    depth: int,
    cost_usd: float = 0.0,
) -> int:
    """Insert a task row with the followup bookkeeping populated. Returns task_id.

    Callers that want the WriteCoordinator path should use the TaskQueue
    abstraction instead; this is here for tests and for direct use from
    the worker when the coordinator isn't running.
    """
    cur = await db.execute(
        "INSERT INTO task_queue "
        "(query, priority, created_at, followup_depth, parent_task_id, root_task_id, cost_usd) "
        "VALUES (?, ?, datetime('now'), ?, ?, ?, ?)",
        (query, priority, depth, parent_task_id, root_task_id, cost_usd),
    )
    await db.commit()
    return int(cur.lastrowid)


# ── Chain tree (for /task/{id}/chain) ────────────────────────────────────

async def get_chain_tree(
    db: aiosqlite.Connection, task_id: int
) -> dict:
    """Return the full task tree rooted at ``task_id``'s root ancestor.

    Shape:
      {
        "root_task_id": int,
        "task_count": int,
        "total_cost_usd": float,
        "max_depth": int,
        "tasks": [
          {task_id, query, status, followup_depth, parent_task_id,
           cost_usd, created_at, completed_at}, ...
        ]
      }
    """
    meta = await get_task_meta(db, task_id)
    if meta is None:
        return {"error": "task_not_found", "task_id": task_id}

    root_id = int(meta["root_task_id"] or task_id)
    try:
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT task_id, query, status, followup_depth, parent_task_id, "
            "cost_usd, created_at, completed_at "
            "FROM task_queue WHERE root_task_id = ? "
            "ORDER BY followup_depth ASC, task_id ASC",
            (root_id,),
        )
        rows = await cur.fetchall()
    except Exception as e:
        logger.warning("get_chain_tree failed: %r", e)
        return {"error": "chain_query_failed", "root_task_id": root_id}

    tasks = []
    max_d = 0
    total_cost = 0.0
    for r in rows:
        depth = int(r[3] or 0)
        cost = float(r[5] or 0.0)
        max_d = max(max_d, depth)
        total_cost += cost
        tasks.append({
            "task_id": r[0],
            "query": r[1],
            "status": r[2],
            "followup_depth": depth,
            "parent_task_id": r[4],
            "cost_usd": cost,
            "created_at": r[6],
            "completed_at": r[7],
        })

    return {
        "root_task_id": root_id,
        "task_count": len(tasks),
        "total_cost_usd": round(total_cost, 6),
        "max_depth": max_d,
        "tasks": tasks,
    }
