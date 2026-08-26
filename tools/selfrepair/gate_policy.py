# ── GATE POLICY ──────────────────────────────────────────────────────────────
# Governing principle: a maintenance routine must NEVER weaken a gate.
#
# Gate-bearing state is any value that feeds promotion/rejection decisions:
#   - hypotheses.edge_threshold COLUMN (read by backtest.py:196/:3819, gates every
#     signal at backtest.py:2520/:2708/:2866)
#   - model_config keys consumed by evaluation/promotion logic
#   - hypotheses.status transitions that reverse a rejection or advance a stage
#
# Self-repair may DIAGNOSE gate problems and record them for human review.
# It may not WRITE to gate-bearing state. Enforced three ways:
#   1. GATE_WRITE_PATTERNS below — refused substrings for SQL/config writes;
#      every repair dispatch passes through SelfRepairEngine._gate_guard().
#   2. Strategies classified GATE_WEAKENING_STRATEGIES are routed to a refuser,
#      never executed, regardless of who asks (detector OR Claude findings).
#   3. tests/test_tier1_loop_self_repair_gate_policy.py statically re-checks
#      this policy so a future edit cannot reintroduce an operative gate write
#      without a loud, reviewable diff to the policy itself.
GATE_WRITE_PATTERNS: tuple[str, ...] = (
    # Operative threshold columns
    "SET edge_threshold",
    # Promotion/evaluation knobs wherever they might live
    "minimum_events_for_promotion",
    "_threshold_lowered_by",
    "_promotion_threshold_lowered_by",
    "_edge_ceiling_lowered_by",
)
# Status reversals that un-reject or advance stages are gate decisions too.
GATE_STATUS_TRANSITIONS = {("rejected", "draft")}

# Repair strategies whose entire purpose is to weaken a gate. These are never
# executed; matching issues/findings are recorded for human review instead.
GATE_WEAKENING_STRATEGIES: frozenset[str] = frozenset({
    "promotion_thresholds_strict",   # lowers minimum_events_for_promotion
    "edge_ceiling",                  # writes the operative edge_threshold column
})

# Env opt-in required for the premature-rejection requeue (rejected -> draft).
ALLOW_REQUEUE_ENV = "CALLISTO_ALLOW_PREMATURE_REQUEUE"
