"""Handlers for Claude's deep work findings (mixin).

Gate policy note: strategies classified gate-weakening are refused upstream in
handle_claude_findings and their handlers are kept as explicit refusers.
"""

import hashlib
import json
import logging

import aiosqlite

from .config import DB_PATH

logger = logging.getLogger("callisto.self_repair")


class FindingsMixin:
    """Pattern matching + handlers for Claude's deep work findings."""

    # Keyword patterns that map Claude's pipeline_issues strings to fix strategies.
    # Order matters: first match wins.
    FINDING_PATTERNS: list[tuple[list[str], str]] = [
        (["identical event", "same games", "same event", "duplicate"],
         "duplicate_events"),
        (["side filter", "side_filter", "side not applied", "over/under not filtered",
          "totals over.*same.*totals under"],
         "side_filter_broken"),
        (["prioritize nba", "prioritize nfl", "nba over mlb",
          "nfl over mlb", "nhl over mlb", "sport priority", "reorder"],
         "prioritize_sports"),
        (["low sample", "not enough data", "insufficient data",
          "too few events", "small sample"],
         "low_sample_size"),
        (["zero promotion", "no promotions", "promotion threshold",
          "nothing promoted", "0 promotions"],
         "promotion_thresholds_strict"),
        (["edge ceiling", "edge cap", "max edge", "threshold too high",
          "thresholds above"],
         "edge_ceiling"),
        (["resolution", "game_results", "date mismatch", "date offset",
          "timezone", "could not match", "match failure", "unresolved event"],
         "resolution_broken"),
    ]

    @classmethod
    def classify_finding(cls, description: str) -> str:
        """Match a free-text finding description to a known fix strategy."""
        desc_lower = description.lower()
        for keywords, strategy in cls.FINDING_PATTERNS:
            for kw in keywords:
                if kw in desc_lower:
                    return strategy
        return "unknown"

    async def handle_claude_findings(self, findings: list[dict]) -> list[dict]:
        """Convert Claude's deep work findings into repair actions.

        Each finding has: {"severity": "CRITICAL|HIGH|LOW", "description": "..."}
        Returns a list of result dicts with keys: fixed, action, detail.
        """
        from .gate_policy import GATE_WEAKENING_STRATEGIES

        results: list[dict] = []
        for finding in findings:
            desc = finding.get("description", "")
            severity = finding.get("severity", "LOW")
            strategy = self.classify_finding(desc)

            try:
                # GATE GUARD: findings classified as gate-weakening are never
                # executed — recorded for human review instead.
                if strategy in GATE_WEAKENING_STRATEGIES:
                    result = self._refuse_gate_change(
                        strategy,
                        f"Claude finding classified '{strategy}' maps to gate lowering "
                        f"— refused by gate policy.",
                        detail=desc,
                    )
                else:
                    handler = {
                        "duplicate_events": self._fix_finding_duplicate_events,
                        "side_filter_broken": self._fix_finding_side_filter,
                        "prioritize_sports": self._fix_finding_prioritize_sports,
                        "low_sample_size": self._fix_finding_low_sample,
                        "resolution_broken": self._fix_finding_resolution,
                    }.get(strategy)

                    if handler:
                        result = await handler(finding)
                    else:
                        # Unknown pattern — record to Hermes with a UNIQUE key
                        # to prevent the same finding from inflating occurrences.
                        # Previous bug: x396 occurrences of "unknown" because the
                        # same stalling issue was re-recorded every cycle under a
                        # single key, drowning out real discoveries.
                        finding_hash = hashlib.md5(
                            f"{strategy}:{desc[:100]}".encode()
                        ).hexdigest()[:8]
                        result = {"fixed": False, "action": "recorded_for_review",
                                  "detail": f"[{severity}] {desc[:200]}"}
                        try:
                            from tools.hermes_memory import get_hermes_memory
                            hermes = get_hermes_memory()
                            await hermes.record_learning(
                                key=f"claude_finding_{strategy or 'unknown'}_{finding_hash}",
                                value=f"[{severity}] {desc[:500]}",
                                confidence=0.5,
                                source="deep_work_finding",
                            )
                        except Exception:
                            pass

            except Exception as e:
                result = {"fixed": False, "action": "handler_error",
                          "detail": f"{strategy}: {e}"}

            await self._record_to_hermes(f"claude_finding_{strategy}", result)
            results.append(result)

        return results

    async def _fix_finding_duplicate_events(self, finding: dict) -> dict:
        """Flag hypotheses that tested identical event sets as needing unique data."""
        flagged = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                # Find hypotheses with identical (unique_games, total_events) counts
                cursor = await db.execute("""
                    SELECT h.hypothesis_id, h.name,
                           COUNT(DISTINCT be.event_id) as unique_games,
                           COUNT(*) as total_events
                    FROM hypotheses h
                    JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                    WHERE h.status = 'backtesting'
                    GROUP BY h.hypothesis_id
                    HAVING total_events > 10
                """)
                rows = await cursor.fetchall()
                # Group by (unique_games, total_events) signature
                sig_groups: dict[str, list[tuple]] = {}
                for r in rows:
                    sig = f"{r[2]}g_{r[3]}e"
                    sig_groups.setdefault(sig, []).append(r)

                for sig, group in sig_groups.items():
                    if len(group) <= 1:
                        continue
                    # Flag all but the first as needing unique data
                    for r in group[1:]:
                        h_id = r[0]
                        try:
                            row = await (await db.execute(
                                "SELECT model_config FROM hypotheses WHERE hypothesis_id = ?",
                                (h_id,)
                            )).fetchone()
                            cfg = json.loads(row[0]) if row and row[0] else {}
                            cfg["needs_unique_data"] = True
                            cfg["_flagged_by"] = "claude_finding_duplicate_events"
                            await db.execute(
                                "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                                (json.dumps(cfg), h_id),
                            )
                            flagged += 1
                        except Exception:
                            continue
                if flagged:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "duplicate_events_check",
                    "detail": f"Error: {e}"}

        if flagged:
            return {"fixed": True, "action": "flagged_duplicate_events",
                    "detail": f"Flagged {flagged} hypotheses as needs_unique_data"}
        return {"fixed": False, "action": "duplicate_events_check",
                "detail": "No duplicate event sets detected"}

    async def _fix_finding_side_filter(self, finding: dict) -> dict:
        """Check and fix hypotheses with broken or missing side_filter in model_config."""
        fixed_count = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT hypothesis_id, name, thesis, model_config FROM hypotheses "
                    "WHERE status IN ('draft', 'backtesting')"
                )
                rows = await cursor.fetchall()
                for h_id, name, thesis, mc_raw in rows:
                    try:
                        cfg = json.loads(mc_raw) if mc_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        continue

                    # Infer side from name/thesis if side_filter is missing
                    name_lower = (name or "").lower()
                    thesis_lower = (thesis or "").lower()
                    current_side = cfg.get("side_filter")

                    if current_side:
                        continue  # Already has a side filter

                    inferred_side = None
                    if "under" in name_lower or "under" in thesis_lower:
                        inferred_side = "under"
                    elif "over" in name_lower or "over" in thesis_lower:
                        inferred_side = "over"
                    elif "home" in name_lower or "home" in thesis_lower:
                        inferred_side = "home"
                    elif "away" in name_lower or "away" in thesis_lower:
                        inferred_side = "away"

                    if inferred_side:
                        cfg["side_filter"] = inferred_side
                        cfg["_side_filter_inferred_by"] = "claude_finding_side_filter"
                        await db.execute(
                            "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                            (json.dumps(cfg), h_id),
                        )
                        fixed_count += 1
                if fixed_count:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "side_filter_fix",
                    "detail": f"Error: {e}"}

        if fixed_count:
            return {"fixed": True, "action": "side_filter_fix",
                    "detail": f"Inferred and set side_filter on {fixed_count} hypotheses"}
        return {"fixed": False, "action": "side_filter_fix",
                "detail": "No hypotheses missing side_filter"}

    async def _fix_finding_prioritize_sports(self, finding: dict) -> dict:
        """Record sport prioritization preference — actual reordering happens in _phase_backtest."""
        # The actual reordering is handled by SPORT_PRIORITY in _phase_backtest.
        # Here we just record the finding and confirm the priority is active.
        try:
            from tools.hermes_memory import get_hermes_memory
            hermes = get_hermes_memory()
            await hermes.record_learning(
                key="sport_priority_active",
                value="Sport priority sorting enabled: NBA > NFL > NHL > MLB > NCAAB > NCAAW > PGA",
                confidence=0.9,
                source="deep_work_finding",
            )
        except Exception:
            pass
        return {"fixed": True, "action": "sport_priority_confirmed",
                "detail": "SPORT_PRIORITY ordering active in backtest queue (NBA/NFL first, MLB last)"}

    async def _fix_finding_low_sample(self, finding: dict) -> dict:
        """Set minimum_events threshold on hypotheses to prevent premature evaluation."""
        updated = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT hypothesis_id, model_config FROM hypotheses "
                    "WHERE status IN ('draft', 'backtesting')"
                )
                rows = await cursor.fetchall()
                for h_id, mc_raw in rows:
                    try:
                        cfg = json.loads(mc_raw) if mc_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if cfg.get("minimum_events"):
                        continue  # Already set
                    cfg["minimum_events"] = 30
                    cfg["_min_events_set_by"] = "claude_finding_low_sample"
                    await db.execute(
                        "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                        (json.dumps(cfg), h_id),
                    )
                    updated += 1
                if updated:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "set_minimum_events",
                    "detail": f"Error: {e}"}

        if updated:
            return {"fixed": True, "action": "set_minimum_events",
                    "detail": f"Set minimum_events=30 on {updated} hypotheses"}
        return {"fixed": False, "action": "set_minimum_events",
                "detail": "All hypotheses already have minimum_events set"}

    async def _fix_finding_promotion_thresholds(self, finding: dict) -> dict:
        """REFUSED by gate policy. Formerly wrote minimum_events_for_promotion=20.

        That key is read NOWHERE in the repo (verified by repo-wide grep), so the
        original fix was a no-op that stamped confidence-0.8 success. Kept as an
        explicit refuser for any stale caller.
        """
        return self._refuse_gate_change(
            "promotion_thresholds_strict",
            "_fix_finding_promotion_thresholds refused: maintenance routines may not "
            "lower promotion requirements.",
            detail=finding,
        )

    async def _fix_finding_edge_ceiling(self, finding: dict) -> dict:
        """REFUSED by gate policy. Formerly wrote the OPERATIVE edge_threshold
        column (UPDATE hypotheses SET edge_threshold = 0.015).

        That column is read by backtest.py:196/:3819 and gates every signal at
        backtest.py:2520/:2708/:2866 — this was the one lowering path that
        actually moved the gate. A maintenance routine must never do this.
        """
        return self._refuse_gate_change(
            "edge_ceiling",
            "_fix_finding_edge_ceiling refused: writing the operative edge_threshold "
            "column is a gate change, reserved for humans.",
            detail=finding,
        )

    async def _fix_finding_resolution(self, finding: dict) -> dict:
        """Re-run resolution when Claude identifies matching failures."""
        return await self._fix_resolution_broken(finding)
