# RED TEAM FINDINGS — confidence-inflation attack

**Claim under attack:** "there is no code path in this system that inflates a
confidence score."

**Verdict: FALSE.** Nine distinct inflation paths are demonstrated with
runnable failing tests:

- `tests/test_redteam_confidence_inflation.py` (boundary + random sweep)
- `tests/test_redteam_confidence_laundering.py` (adversarial constructions)

Run: `python3 -m pytest tests/test_redteam_confidence_*.py -q`
Current result: every finding below fails exactly as described. Honest
negative results (attacks that did NOT land) are listed at the end — those
tests pass and are kept as regression pins.

---

## CONFIRMED BREAKS

### F1 — `Adversary.apply_verdict` rounds UP on every path (HIGH)
`agp/adversary.py:489,491`. `round(max(0.0, score), 2)` on both the
no-objection path AND each penalty step. `apply_verdict(0.836, []) == 0.84`.
This is *exactly* the historical bug class (`round(0.836, 2) == 0.84`) that
MORNING_REPORT §process-2 said was fixed — the fix landed in
`clamp_with_ensemble` (which correctly uses `math.floor`) but **not** in the
sibling functions in the same module. The docstring says "There is NO bonus
path"; the rounding IS a bonus path of up to +0.005 per application.
Compounding test (`test_compounding_round_trip_creep`) shows ten honest
review round-trips lift 0.8351 → 0.84 through purely "neutral" code.

### F2 — `clamp_parent_confidence` rounds scores across tier boundaries (CRITICAL)
`tools/research_program.py:267`. `round(min(raw, ceil_), 2)`:
- raw 0.7499 (below CORROBORATED) → stored **0.75 / CORROBORATED**
- raw 0.5551 → stored **0.56**, promoted out of the floor band

The inheritance rule's entire point is that a parent never displays a tier it
has not earned; one line of round() defeats that at both the CORROBORATED and
PROBABLE boundaries. Boundary sweep: ~700 parametrized failures.

### F3 — `relabel_evidence` floors confidence UPWARD during demotion (HIGH)
`agp/provenance.py:164-166`. A PRIMARY-declared item with confidence 0.10
that provenance demotes to INFERRED comes back with confidence **0.30** — the
DB-floor clamp is applied as `max(floor, ...)` so the "demotion" triples the
score. Any downstream average over evidence confidence inherits invented mass.
Also `clamp_confidence_provenance(0.836, PRIMARY) == 0.84` (same round() defect).

### F4 — Evidence laundering via the citation rule (CRITICAL)
`ProvenanceLedger.assign_source_class` promotes ANY text to SECONDARY if it
contains a URL fetched at any point this session. Nothing binds evidence
content to tool output:
- One real fetch launders unlimited fabricated claims citing that URL,
  forever ("first fetch wins", no scoping/expiry).
- Verbatim re-emission of primary bytes re-classes as PRIMARY (exact-hash
  rule); engine.py passes `f.body[:4000]` straight into Evidence, so any
  summariser echoing a document converts hearsay handling into ceiling-1.0
  provenance without new verification.

### F5 — Synthesis group confidence takes MAX over mixed provenance (CRITICAL)
`synthesis.confidence_from_agreement` uses `best_class = max(items)`. A group
of one PRIMARY item plus two INFERRED gossip items scores **1.0 (VERIFIED)** —
above even the SECONDARY ceiling — because corroboration counting is per-group
and the ceiling is the group maximum. Corroboration does not exceed a single
class's ceiling (honest negative below), but mixed-provenance groups ride the
strongest member's ceiling while weaker members supply the voices.

### F6a — Self-review cap cannot engage on the pipeline path (CRITICAL)
`ReviewProvenance.independent` treats author_model="" as "nobody matches the
author", so with an unknown/empty author EVERY reviewer reads as independent.
`engine.py`'s adversary call does not pass `author_model` at all — the
SELF_REVIEW_CEILING (0.54) is structurally unreachable in the main pipeline.

### F6b — Model identity is spelling, not weights (HIGH)
`normalize_model` strips provider prefixes/date tags only. The same weights
behind a proxy alias ("gpt-4o-proxy-alias") count as a genuinely independent
reviewer; the 0.54 self-review cap evaporates. (Honest negative: the
"(unattributed)" substitution IS handled conservatively.)

### F6c — Empty/failed panel reads as approval, not veto (HIGH)
`PanelVerdict(objections=[], backend_failures=3).apply(0.99) == (0.99, "")`.
A verdict whose critics ALL failed carries zero epistemic weight yet clamps
nothing; silence is indistinguishable from "attacked and withstood".
`backend_failures` is recorded but `apply()` ignores it. (Single-adversary
backend failure inside `attack()` does fail closed with a BLOCKING objection —
the hole is any caller assembling/applying a verdict without a completed
attack.) Related: the VETO path also round()s up — 0.836 veto'd reports 0.84.

### F7 — Inheritance-rule credit from non-evidence (HIGH ×3)
In `tools/research_program.py`:
1. **Stale resolutions count toward the lift gate.** `outcome="stale"` means
   the descendant NEVER RESOLVED, yet `counted=True` and it satisfies
   `MIN_RESOLVED_FOR_LIFT`. Four lucky hits + one abandoned question lift a
   parent's ceiling from 0.55 to **0.7095** — nearly CORROBORATED on credit
   from a question that was never answered.
2. **Resolver labels are trusted wholesale.** A record claiming `outcome=
   "hit"` with `pinball_score=None` earns zero error regardless of reality;
   whoever writes ResolutionRecords controls calibration credit entirely.
   Even an honest flat 50% hit/miss record lifts the ceiling to 0.6426
   (PROBABLE) via size_factor — two bands for coin-flip accuracy.
3. **`best_source_class` on resolution records has NO seal/provenance check**
   (contrast `memory_epistemics.admit_learning`, which demands HMAC seals).
   One unverified dict field unlocks the 0.90 inherited ceiling for every
   parent above it. Laundering at the record layer beats laundering at the
   evidence layer.

---

## HONEST NEGATIVES — attacks that did NOT break anything

These pass and are kept as regression pins:
- `ensemble_ceiling` / `clamp_with_ensemble`: genuinely downward-only
  (floor-rounding, correct).
- `PanelVerdict.apply` non-veto paths: floor-rounding correct; no raise found
  across full sweep.
- Single-class synthesis groups cannot exceed `MAX_CONFIDENCE_BY_SOURCE` —
  the escape is F4/F5 (provenance laundering), not the corroboration formula.
- Void resolutions are correctly excluded from track record; <5 resolved
  descendants cap at SPECULATIVE as documented.
- `memory_epistemics.admit_learning` / decay: replace-not-ratchet, seal-gated
  class claims, decay monotone downward — no inflation found. The trust-
  escalator fix appears sound.
- `base_rate_relative_floor`: guarantees hold across the sweep; NaN/negative/
  oversized base rates all fall back to legacy floor.

## WHAT TO FIX (ordered by leverage)

1. Replace every `round(x, 2)` on a clamp path with
   `math.floor(x * 100) / 100` (the pattern already used in
   `clamp_with_ensemble`). Fixes F1, F2, F3-rounding, F6c-veto in one rule:
   **a clamp may only ever move a score DOWN, so it must round DOWN.**
2. Make `relabel_evidence` demotion pure-down: `min(old, ceiling)` without
   the floor, or keep floor but never above the incoming value.
3. Bind SECONDARY-by-citation to content overlap between evidence and the
   fetched bytes for that URL, or drop the citation rule entirely.
4. Synthesis: compute per-class voice counts; score = max over classes of
   `class_ceiling * frac(class_voices)` — never let weak voices borrow a
   strong member's ceiling.
5. Engine must thread `author_model` into the panel call; treat empty author
   as self_review (conservative), and treat `backend_failures > 0` (or a
   verdict assembled without an attack) as refusal, not approval.
6. Exclude `stale` from `MIN_RESOLVED_FOR_LIFT` counting; verify
   `best_source_class` against seals like memory_epistemics does.
