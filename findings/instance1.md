# Instance 1 findings — the unattended loop (tools/autonomous.py, tools/self_repair.py, orchestrator.py)

Session opened 2026-08-22 on branch audit/tier1-loop.
Method: START_HERE brief; AUDIT_MANDATE §2 protocol; ROADMAP treated as unverified prior claims.
Peer status at open: instance 3 (gate) substantially done and read; instances 2/4 not started.

---

## WORK UNIT 1 — self_repair.py gate-lowering paths, re-verified against current code

## [VERIFIED] self_repair.py:991 — ROADMAP §3.1 is WRONG that two of three lowering paths are no-ops: `_fix_finding_edge_ceiling` writes the OPERATIVE column
Blast radius: SILENT (this one actually moves the gate)
Evidence: ROADMAP claims "`_fix_thresholds` writes a JSON key nothing reads" and groups
the edge-ceiling path with it as cosmetic. Re-derived: backtest.py reads
`h["edge_threshold"]` — the COLUMN — at backtest.py:196 and :3819, and gates every
signal on `edge >= edge_threshold` at :2520/:2708/:2866. self_repair.py:991 executes
`UPDATE hypotheses SET edge_threshold = 0.015 ...` on every draft/backtesting row above
2%. That is an operative gate change made by a maintenance routine, stamped
confidence-0.8 into hermes learnings (:1021). Only `_fix_thresholds` (:556 writes the
model_config JSON key, which backtest never reads) and
`_fix_finding_promotion_thresholds` (:953 writes `minimum_events_for_promotion`, which
is read NOWHERE in the repo — verified by repo-wide grep) are no-ops.
Falsifier: find a code path reading model_config["edge_threshold"] or
minimum_events_for_promotion; or show backtest signal gating reads something else.
For: me (enforcement design, WORK UNIT 3)

## [VERIFIED] Repo-wide grep confirms `minimum_events` and `minimum_events_for_promotion` are written by self-repair and read by nothing
Blast radius: SILENT (false success telemetry)
Evidence: grep across the whole worktree: only hits are self_repair.py:898-968 and the
ROADMAP itself. Both `_fix_finding_low_sample` and `_fix_finding_promotion_thresholds`
report `"fixed": True` and record confidence-0.8/learnings for keys no consumer exists
for. Q5 finding: unreachable-by-consumption code manufacturing false confidence.
Falsifier: any reader of those keys appearing in a future commit.
For: me

## [VERIFIED] self_repair.py:318-347 + :570-591 — `_fix_premature_rejection` un-rejects hypotheses, weakening a *decision* gate, not just a numeric knob
Blast radius: LOUD-ish (moves rows rejected→draft wholesale)
Evidence: detector flags ALL rejected hypotheses with zero backtest_events whose sport
appears in historical_odds_cache (no check of WHY rejected — could be duplicate-filter,
thesis-quality, or dedup rejection); fixer bulk-updates status='rejected' → 'draft'
without recording which mechanism rejected them or honoring retry budgets. Combined
with the >95% rejection-rate trigger (_det_rejection :288-303 classifies near-total
rejection as repairable), this is a standing pressure to recycle everything through
draft until something passes. ROADMAP §3.1 noted the treadmill; the requeue path is the
concrete ratchet-release.
Falsifier: show rejections-with-0-events are always data-availability failures (e.g.
rejection reason logged and checked); none is.
For: me

## [VERIFIED] self_repair.py:676-707 — keyword classifier routes "zero promotion" findings to threshold-lowering
Blast radius: ARMING (depends on Claude deep-work output text)
Evidence: `_FINDING_PATTERNS` maps ["zero promotion","no promotions","promotion
threshold",...] → strategy `promotion_thresholds_strict`, handled by
`_fix_finding_promotion_thresholds`; ["edge ceiling","max edge","threshold too high"]
→ `_fix_finding_edge_ceiling` (operative, see above). Any free-text Claude finding
containing these phrases mechanically triggers gate-weakening. Given instance 3's
verified result that zero promotions is the DESIGNED outcome of the Šidák gate, this
classifier guarantees the loop responds to correct gate behavior by attacking the gate.
Falsifier: demonstrate a path where "zero promotion" text does not reach the lowering
handler (e.g. severity filter) — there is none; all severities route identically (:716-733).
For: me

## [VERIFIED] self_repair.py:443-462 — `_try_betmgm_subdomains` monkeypatches a module global and can leak the patched value
Blast radius: SILENT
Evidence: sets `mod._BASE_URL` then restores in a loop; if an await inside the try
raises between set and restore… actually restore runs before each next iteration and
after loop exit via `mod._BASE_URL = orig` placement — but on exception INSIDE
`scrape_betmgm_odds` the restore at :459 still runs (inside the for). Real leak: if
`asyncio.wait_for` raises TimeoutError the except catches it and restore already ran…
re-checked: restore is the last statement in the try, so a timeout skips it and the
except at :457 swallows without restoring. **The patched subdomain persists** until the
next successful call. Also betmgm is commented out of SCRAPERS (:28) so this handler is
currently unreachable (Q5) — dead code with a latent state-corruption bug.
Falsifier: trace showing wait_for timeout reaches line 459.
For: me

---
