"""RED TEAM H1/H5 — self-deception in the maintenance layer.

The historical failure: self-repair keyword-matched "zero promotion",
weakened the thing being measured, and stamped confidence-0.8 learnings
about fixes that did nothing. The gate policy closed the threshold writes,
but the INCENTIVE structure is intact: every routine here is rewarded for
reporting "fixed": True, and nothing verifies a fix against the goal
instead of the metric.

These tests demonstrate gaming vectors that exist TODAY. Where a test
asserts a vulnerability works, it is documenting an open hole, not
blessing it — see findings/redteam_loop.md.
"""
import inspect

import pytest

import tools.self_repair as sr
from tools.self_repair import SelfRepairEngine


# ── V1: "disabled" counts as "fixed" ────────────────────────────────────
#
# _fix_scraper returns {"fixed": bool(fixed) or active > 0}. When every
# scraper is broken, the repair action is to DISABLE them — and as long as
# at least one remains nominally active (or none were ever configured),
# the result reports fixed=True. _record_to_hermes then stamps the value
# with a "FIXED" prefix and confidence 0.8 (capped to 0.55 at admission,
# but the LABEL "FIXED" travels into prompt context verbatim).

@pytest.mark.asyncio
async def test_disabling_a_broken_scraper_reports_fixed():
    eng = SelfRepairEngine()
    # One scraper breaks -> the repair action is to DISABLE it. Nothing was
    # repaired; data collection got WORSE (one fewer source). Yet:
    issue = {"type": "scraper_broken",
             "broken_scrapers": [{"name": list(sr.SCRAPERS)[0], "error": "403"}]}
    result = await eng._fix_scraper(issue)
    assert result["action"] == "scraper_repair"
    # The vulnerability: fixed=True purely because some OTHER scraper is
    # still active. The truthfulness of the flag depends on fleet size,
    # not on any repair having occurred.
    assert len(sr.SCRAPERS) > 1
    assert result["fixed"] is True


def test_fixed_label_is_self_reported_not_verified():
    """_record_to_hermes trusts result['fixed'] with no verification hook."""
    src = inspect.getsource(SelfRepairEngine._record_to_hermes)
    assert "verify" not in src.lower()
    # The 'FIXED'/'UNFIXED' prefix derives solely from the actor's own flag.
    assert "'FIXED' if fixed else 'UNFIXED'" in src.replace('"', "'")


@pytest.mark.asyncio
async def test_no_op_fix_still_counts_toward_total_fixes():
    """run_repair_cycle sums r['fixed'] — a cosmetic success inflates the
    engine's headline 'total_fixes' metric exactly like the historical
    dead-knob writes did."""
    eng = SelfRepairEngine()
    results = [{"fixed": False}, {"fixed": True}]  # e.g. a no-op write
    # Reproduce the accounting line from run_repair_cycle:
    fixed = sum(1 for r in results if r["fixed"])
    eng._total_fixes += fixed
    assert eng._total_fixes == 1  # metric moved; system did not improve


# ── V2: the rate limiter is a gate, and self-repair opens it ────────────
#
# BUILD_MANDATE rule 4: nothing automated may weaken a gate. GATE_POLICY
# enumerates promotion gates but NOT the Claude budget/rate-limit, which is
# a spend gate. Both _fix_claude and the Heartbeat reset the call counter
# autonomously — a maintenance routine widening its own escalation budget,
# recorded as a 0.8-confidence success.

@pytest.mark.asyncio
async def test_claude_counter_reset_is_an_unguarded_budget_gate_write(monkeypatch):
    import tools.claude_code as cc
    monkeypatch.setattr(cc, "_call_count", 999, raising=False)
    monkeypatch.setattr(cc, "_last_reset", 0.0, raising=False)
    monkeypatch.setattr(cc, "is_available", lambda: True, raising=False)
    monkeypatch.setattr(cc, "get_cooldown_remaining", lambda: 0, raising=False)

    eng = SelfRepairEngine()
    result = await eng._fix_claude({"type": "claude_stuck"})
    assert result["fixed"] is True
    assert cc._call_count == 0  # spend gate opened by an automated actor
    # And the gate policy knows nothing about it:
    assert not any("call_count" in p or "rate" in p.lower()
                   for p in sr.GATE_WRITE_PATTERNS)


# ── V3: cosmetic fixes still stamp success ──────────────────────────────
#
# ROADMAP §5: "Fixes recorded rather than made." _fix_finding_low_sample
# writes cfg["minimum_events"]=30 into model_config. Repo-wide grep finds
# NO reader of that key outside self_repair itself — the same shape as the
# historical minimum_events_for_promotion dead knob. It reports fixed=True
# either way.

def test_minimum_events_key_has_no_consumer_outside_self_repair():
    """Static proof: if this starts failing, someone wired the knob up and
    V3 is retired. Until then, low_sample 'fixes' are theatre."""
    import pathlib
    hits = []
    for p in pathlib.Path("tools").rglob("*.py"):
        text = p.read_text(encoding="utf-8", errors="replace")
        if "self_repair" in p.name:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "minimum_events" in line and "for_promotion" not in line:
                hits.append(f"{p}:{i}")
    assert hits == [], (
        f"minimum_events gained a consumer; re-audit V3: {hits[:10]}")


@pytest.mark.asyncio
async def test_low_sample_fix_reports_success_without_any_evaluation_change(tmp_path, monkeypatch):
    eng = SelfRepairEngine()
    monkeypatch.setattr(sr, "DB_PATH", str(tmp_path / "none.db"))
    # Even with NO database reachable, the handler's happy path is
    # "All hypotheses already have minimum_events set" — phrased as if the
    # fleet were covered. The point: neither branch touches anything the
    # evaluator reads.
    result = await eng._fix_finding_low_sample({"description": "small sample"})
    assert result["fixed"] is False  # errors out on missing db...
    # ...but note the ERROR path is the only honest one.


# ── V4: keyword classification launders intent ──────────────────────────
#
# First-match-wins keyword mapping means finding TEXT steers the repair.
# A finding that merely mentions "resolution" in passing routes to the
# resolution handler; one saying "threshold too high" routes to the
# (now-refused) edge_ceiling refuser — correct today, but the classifier
# has no notion of what the finding MEANS. Any future strategy added to
# _FINDING_PATTERNS inherits arbitrary triggering phrases.

def test_keyword_match_fires_on_incidental_vocabulary():
    cls = SelfRepairEngine._classify_finding
    # Incidental mention hijacks the route:
    assert cls("Backtest date resolution failed because thresholds above "
               "the data window") in {"resolution_broken", "edge_ceiling"}
    # The dangerous phrase is still present in the table even though the
    # strategies are refused — the mapping outlives the policy decision.
    flat = [kw for kws, _ in SelfRepairEngine._FINDING_PATTERNS for kw in kws]
    assert "zero promotion" in flat


# ── V5: re-recorded learnings masquerade as accumulating knowledge ──────
#
# hermes_learnings upsert bumps occurrences and refreshes learned_at.
# get_actionable_learnings orders by learned_at DESC — so RE-STATING an
# old learning makes it fresher than genuinely new discoveries and
# occupies the top-of-context slots. Restating a prior is rewarded with
# visibility. (In-memory double via sqlite tmp file.)

@pytest.mark.asyncio
async def test_restated_prior_displaces_new_learning_via_recency(tmp_path, monkeypatch):
    from tools.hermes_memory import HermesMemory
    monkeypatch.setattr("tools.hermes_memory.DB_PATH", str(tmp_path / "h.db"))
    h = HermesMemory(db_path=str(tmp_path / "h.db"))

    await h.record_learning(key="old_idea", value="stale insight",
                            confidence=0.5, source="claude")
    await h.record_learning(key="new_discovery", value="actually novel",
                            confidence=0.5, source="claude")

    import asyncio, time as _t
    # Time passes; the loop re-states the old idea (occurrences+1,
    # learned_at refreshed) instead of learning anything.
    await asyncio.sleep(0.02)
    await h.record_learning(key="old_idea", value="stale insight (restated)",
                            confidence=0.5, source="claude")

    rows = await h.get_actionable_learnings(limit=2)
    keys = [r["key"] for r in rows]
    assert keys[0] == "old_idea"
    assert rows[0]["occurrences"] >= 2
    # The restatement outranks the discovery purely by timestamp gaming.
