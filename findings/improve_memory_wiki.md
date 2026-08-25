# MEMORY AND WIKI LAYER — improvement pass (improve/rotating-0824-194545)

**Area chosen: the memory and wiki layer** — tools/hermes_memory.py,
tools/knowledge_wiki.py, tools/memory_epistemics.py (the trust-policy core
they share).

Why this one: no improve run has owned it (CLI ×2, artifacts/sandbox,
retrodiction/calibration taken; retrieval-starvation, seal-contract and
worldbank-planner branches landed since). It decides what the model sees on
every iteration, carries the lowest coverage in the EPISTEMICS tier
(29% / 46% at map time), and the P4 trust-escalator fix landed here — so per
PATTERNS family #2 ("a fix lands in one copy while another keeps the bug")
I hunted siblings of that rule in the same subsystem.

**Families hunted:** #1 (a policy layer that never actually runs — TWO hits),
#2 (fix lands in one copy, sibling keeps the bug — one hit), plus #3-shaped
absence-as-total-failure. No new family found; every defect below is a
documented family recurring inside one subsystem.

---

## The headline defect (family #1, instance #5): decay never reached anything

`memory_epistemics.decay_confidence` exists, is tested as a pure function
(TestDecay passes), is CALLED by `_build_learnings`, its result is passed as
`effective_confidence` into `annotate_for_reinjection` — which then **overwrote
it** with `min(raw_stored_confidence, ceiling)`. Measured end-to-end before the
fix (real SQL, real section builder):

    learning recorded 100 days ago at stored 0.55 → prompt line "[eff 55% conf]"
    documented policy says ~5% (floor). Fresh and stale were also RANKED equal,
    so trimming could drop fresh learnings and keep a 200-day-stale one.

The docstring claimed "no ratchet survives into the prompt". A check that runs,
whose output is discarded before it can matter, is family #1 exactly — same
shape as W5/K1/C1/A6, new instance. The tests passed because they exercised
decay in isolation and storage round-trips, never the emitted prompt text
(family #7 masking).

**Fix (b02fd63):** `annotate_for_reinjection` honours caller-provided
effective_confidence (capped by class ceiling); callers passing none get the
old capped-raw behaviour unchanged (pinned — test_redteam_prov_memory_wiki
depends on it).

## Sibling hunt hit (family #2): the wiki's admission gate ignored decay too

P4's mechanism 2 claimed "the wiki's >= 0.5 admission gate cannot be reached by
an unverified guess alone". Numerically false when written: the INFERRED
ceiling is 0.55, ABOVE the 0.5 gate — any single self-reported guess >= 0.5 was
a compile source FOREVER, however stale, and fed article min-of-sources at full
strength. The read-time-decay rule landed in hermes_memory's prompt path and
never in knowledge_wiki's admission path.

**Fix (2c70aef):** `_get_uncompiled_sources` admits learnings on
`decay_confidence()` and stores the DECAYED value into the source dict, so an
unverified 0.55 guess falls below the gate after **~1.9 days** un-re-observed;
re-recording refreshes `learned_at` (re-observed learnings persist — that clock
semantics is deliberate and pinned by an existing red-team test). Direction of
error is conservative: decay only ever lowers admissibility and article
confidence. Also corrected the overstated mechanism-2 docstring to state what
actually holds — per the repo rule that a claim of protection must be true.

## Absence-as-total-failure (family #3 shape): one missing table blinded everything

`get_memory_context` built all nine sections inside one try. On any DB without
the sports tables — this checkout, any non-workstation machine, or the
domain-general future BUILD_MANDATE demands — `_build_bet_history` raised
`no such table: bankroll` and the WHOLE context degraded to identity-only plus
a DEGRADED banner, discarding learnings, messages and code state that were
perfectly available. Same root pattern as improve_cli's `callisto status`
crash: sports tables treated as load-bearing for general memory.

**Fix (12c5d7f):** per-section isolation; a failing section logs one warning
line and yields "". Workstation shape unchanged (pinned).

## Hygiene (c3a0777)

- `MESSAGES_FILE` computed at import, referenced nowhere (messages live in the
  `hermes_messages` TABLE) — removed.
- `_build_identity` told every Claude call "You are Claude Opus 4.6 — the
  PRIMARY reasoning engine": a hardcoded provider+model claim BUILD_MANDATE
  forbids ("Never hardcode a provider or a model") and role-based routing makes
  false. Now config-neutral.
- Wiki compile prompt opened with "You are a knowledge compiler for an
  autonomous **sports betting** research system" — agp/ is verified to contain
  zero sports vocabulary; the wiki compiler re-injected it into every compiled
  article. Neutralised.

---

## Before / after (all measured on this tree)

| measure | before | after |
|---|---|---|
| 100-day-old 0.55 learning, emitted confidence in prompt | 55% | ~floor (decay applies) |
| stale vs fresh under trim pressure | stale could win | fresh always outranks |
| unverified 0.55 guess admissible to wiki compile | forever | ~1.9 days un-re-observed |
| hermes-only DB context | identity + DEGRADED banner only | learnings + messages + code sections present, no banner |
| hardcoded model name in every prompt | yes | no |
| area tests | — | +68 (tests/test_build_memory_wiki_improve.py), failing-first where behaviour changed |
| full suite (this Mac, minus 2 xgboost-uncollectable files) | 11,625 passed / 53 failed | identical pass count; failure set byte-identical to clean base a6e4467 (diff-verified via worktree run) |
| money/sports/provenance suites (tier0 sizing, memory, p4, prov_memory_wiki, crossrun, seal_unprovable) | green | green (88 passed) |

## What I deliberately did NOT do

- **Did not ban INFERRED learnings from wiki compile.** The tier3 contract
  ("learnings still enter at >= 0.5", bounded by min-of-sources) is documented
  and tested intent; making the gate class-aware would also need migration-015
  columns that fixture DBs legitimately lack.
- **No production caller passes seals to record_learning** (all 9 call sites:
  self_repair ×5, autonomous ×3, work_queue ×1 pass key/value/confidence/source
  only). The seal-gated escalation path therefore has zero production users —
  noted for the owning areas, not fixed here.
- **CacheManager.get_memory_context duplicates this whole read path**
  (tools/cache_manager.py build_hot_cache) — orchestrator uses it, claude_code
  bridge uses HermesMemory. Two builders, one job (family #2 surface). Left
  alone: cache_manager is INFRA-tier with its own tests; merging them is a
  separate decision.
- **Topic extraction is still sports-keyed** (`_extract_topic`: mlb/nba/...);
  every financial session files under `{domain}_misc`. Real domain-generality
  gap, but redesigning topic extraction is capability work nobody asked for —
  recorded here so the next run in this area starts there deliberately.
- `record_learnings_batch` silently drops provenance kwargs (it never accepts
  source_class/seal_session/seal_hash). Dead in production today; adding params
  with zero callers would be a regression in disguise.

## Honest caveats

- Decay-at-admission means a wiki article's min-of-sources can drift DOWN over
  time as inputs age without re-observation. That is the intended direction
  (staleness weakens), but articles are not recomputed retroactively — only
  future compiles see weakened values.
- The ~1.9-day window assumes the writer claimed the full INFERRED ceiling;
  lower claims fall out sooner.
- The section-isolation warning lines go through logger.warning once per failed
  section per context build (TTL-cached afterwards, so at most one burst per
  90s per caller).
