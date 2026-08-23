# RED TEAM FINDINGS — checkpointing & resume (differential pass)

**Surface:** checkpointing and resume (`tools/pipeline/checkpoint.py` + its
wiring in `tools/pipeline/engine.py`). **Method: B — differential.**

Why this surface and this method: the module's own contract says resumption
must never launder evidence whose provenance was lost, and must refuse rather
than seal the unverifiable. The prior c1–c4 passes each patched one *named*
hole; nobody ran the general differential claim — a live run and the resumed
run replaying its checkpoints must earn identical provenance from identical
bytes, whatever an on-disk corruption or attacker changes. Resume is days-old
wave-4 code guarding exactly the provenance boundary F4-style laundering
attacks target. Differential attack is the native style for "two paths that
must agree": here, *the world the guard inspects* vs *the world the seal
covers*.

Tests: `tests/test_redteam_resume_differential.py`
Run: `python3 -m pytest tests/test_redteam_resume_differential.py -q`
Current result: 3 findings fail exactly as described; 6 honest negatives
pass and are pinned.

---

## CONFIRMED BREAKS

### D1 — The checkpoint HMAC is decorative (CRITICAL)
`checkpoint.py:146-156` defines `signed()`/`verify_signature()`;
`save()` signs when `_harness_key()` is set. But **no code in the entire
load/replay/seal path ever calls `verify_signature`**: `load_by_key`,
`replay_ledger`, `provenance_is_intact`, and `seal_guard` all treat the sig
as inert metadata. Two consequences:

- Unkeyed deployment (the default — nothing in the repo sets
  `CALLISTO_CUTOFF_KEY`/`CALLISTO_SEAL_KEY` outside tests): checkpoints are
  saved with `sig=""`. There is no tamper-evidence at all.
- Keyed deployment: editing body AND recomputing `content_sha256` in the JSON
  file leaves a stale-but-unchecked signature. `replay_ledger` reports zero
  integrity failures, mints the fabricated bytes PRIMARY, and `seal_guard`
  returns SEAL. Demonstrated in `test_d1b`.

This is the C4 bug class ("produced_at was signed but trusted_age was the
only consumer") on the *integrity* axis instead of the age axis. Same root
cause: a signature added without threading its verification into consumers.

Blast radius: SILENT. Falsifier: set a key, corrupt one checkpoint file,
run a resume to seal — today it seals.
For: instance 1 (loop) or whoever owns tools/pipeline/checkpoint.py.
Fix: verify the signature at load time (a bad/unverifiable sig = cache miss,
not a crash), and refuse unsigned fetch-bearing checkpoints under a keyed
regime — mirroring `trusted_age_seconds`'s "untrusted = maximally old".

### D2 — Stage-name string matching is attacker-controlled state (HIGH)
`_is_fetch_stage()` (line 415) keys the C3 mandatory-structure rule on
`"fetch" in stage`. But `stage` is a plain string field in an editable JSON
file, and it also determines the filename glob. Renaming the stage in-file
to `"decompose"` hides real fetch records from every structural check while
`replay_ledger` (which never looks at the stage name) still mints their
bytes PRIMARY — and the guard seals (`test_d2`). The C3 fix checks payload
structure only for stages whose *label* admits to fetching.

Blast radius: SILENT. Fix: make the mandatory-fetches rule content-based —
any checkpoint whose payload contains a non-empty `fetches` list is treated
as a fetch checkpoint regardless of name — and/or authenticate the whole
record (D1), which makes renames detectable.

### D3 — seal_guard and the ledger inspect different worlds (CRITICAL)
engine.py:493-498 replays each leaf's checkpoint into `self.ledger` via
`load_by_key` with no run filter, no signature check, and no integrity
check. seal_guard then scopes `cp.list_all()` down to `ck.run == trace.run`
(the C2 fix) before judging anything. Relabeling `ck.run` in the file makes
the guard blind to a record whose bytes the ledger has already absorbed:
guard reasons over a checkpoint set that excludes evidence the seal will
cover (`test_d3`). Even without relabeling, the guard's verdict is computed
from `list_all()` while ledger state comes from per-leaf replays — two
codepaths assembling "what evidence exists" independently, which is the
exact duplicated-logic drift pattern (method F) that produced c1–c4.

Blast radius: SILENT. Fix: engine should replay ONLY checkpoints that
pass the same predicate seal_guard uses (run match + verified sig +
intact digest) — ideally by having seal_guard return the vetted set and
the engine consume it, so there is ONE definition of admissible.

---

## HONEST NEGATIVES — attacks that did NOT land (pinned as regression tests)

- Corrupt JSON file → cache miss, no crash, no partial state.
- Replay dedup: double-replay yields one observation; no duplicate PRIMARY.
- Digest-mismatch refusal (C1 fix): still holds where the record is looked at.
- Cross-run scope filter (C2 fix) does stop guard-level absorption of other
  runs' checkpoints — the break is that the ledger path doesn't share it (D3).
- Independence counting on resume: keys restored verbatim from payload, so a
  resumed run cannot claim MORE independence than the live run had (I tried;
  `independent_keys` round-trips honestly).
- Cache-hit staleness honesty: original `produced_at` genuinely carried
  forward through hits; `trusted_age_seconds` treats unauthenticated age as
  infinite. Age forgery remains closed post-C4.
- `hash_inputs(default=str)` collision on objects with identical str(): real
  quirk, but all engine call sites hash only strings/ints — not reachable.

## WHAT TO FIX (one rule)

**A checkpoint's trustworthiness must be established once, at load, by one
function** — signature first (unverifiable ⇒ miss), then run-scope, then
content integrity — and both the ledger-replay path and seal_guard must
consume that single verdict. Today three codepaths each re-derive partial
versions of it, and every previous red-team hole (C1–C4) lived in exactly
the gap between two of them.
