# RED TEAM — provenance laundering findings

**The claim under attack:** "confidence is assigned by which code path fetched
the bytes, not by what the model says about itself."

**Verdict: the claim is FALSE as implemented.** Six of seven attack families
found confirmed, reproducible laundering paths; every finding below has a
failing test in `tests/test_redteam_prov_*.py`. The core ledger itself
(`ProvenanceLedger.assign_source_class` for a live, in-process session) held —
every defeat lives at a *boundary*: resume, storage, memory round-trips, host
normalisation, and the legacy-seal compatibility path.

Run them:

```
python -m pytest tests/test_redteam_prov_checkpoint.py \
                  tests/test_redteam_prov_memory_wiki.py \
                  tests/test_redteam_prov_independence_seal.py -q
```

---

## CONFIRMED DEFECTS

### R1. Checkpoint replay: missing digest = zero integrity check (CRITICAL)
`tools/pipeline/checkpoint.py::replay_ledger` line ~338:

```python
if digest and _sha(body) != digest:
```

A fetch record with **no `content_sha256`** skips the check entirely, then is
recorded `primary=True` (the default at line ~347). Tampered bytes read as
PRIMARY with confidence ceiling 1.0.
**Test:** `test_missing_digest_is_laundered_as_primary`

### R2. The integrity digest is unkeyed — attacker recomputes it (CRITICAL)
The "integrity check" is `sha256(body)` recorded in the same JSON file next to
the body. No secret. Anyone who can edit the checkpoint file (same machine,
repo access — the exact threat HMAC sealing was added for) rewrites body AND
digest; `provenance_is_intact()` passes; `seal_guard()` says SEAL over bytes
that never came from any tool.
**Test:** `test_attacker_recomputed_digest_defeats_integrity_check`
**Fix direction:** the stored digest must be keyed (reuse the seal HMAC) or the
body must be verified against a ledger entry made at original fetch time.

### R3. Empty body becomes PRIMARY observation of `""` (HIGH)
A fetch record with empty/no body replays into the ledger as a PRIMARY
observation of the empty string. Any later evidence whose content hashes to
empty inherits PRIMARY.
**Test:** `test_record_with_no_body_makes_empty_string_primary`

### R4. Cross-run laundering via `seal_guard(cp.list_all())` (CRITICAL)
`engine.py:604` calls `seal_guard(trace, cp.list_all(), self.ledger)`.
`list_all()` spans **every run in the store**. `provenance_is_intact()` then
*replays* those foreign fetch records into THIS run's fresh ledger. Bytes
fetched for claim X become observed PRIMARY observations while sealing claim Y
— evidence inheriting a class it never earned, exactly attack path 7.
**Test:** `test_seal_guard_replays_foreign_run_fetches`
**Fix direction:** filter checkpoints to `trace.run` before replay/guard.

### R5. Memory: legacy unkeyed seals mint provenance classes without a key (CRITICAL)
`tools/memory_epistemics.py::verify_learning_seal` accepts the LEGACY UNKEYED
sha256 digest as valid seal verification. End-to-end chain demonstrated:
take any dict, hash it with plain sha256, pass it as `seal_session`/
`seal_hash` to `admit_learning` → an INFERRED guess is admitted as PRIMARY at
stored confidence 0.99. No `CALLISTO_SEAL_KEY` required. That learning is then
re-injected into every future prompt annotated `[provenance PRIMARY]`.
Decay does not help: each re-observation resets `learned_at`, so a periodically
re-written learning never decays below admission thresholds.
**Tests:** `test_seal_from_legacy_unkeyed_era_verifies`,
`test_end_to_end_class_escalation_via_forged_legacy_seal`
**Fix direction:** when a claimed class is above INFERRED, require the KEYED
digest only — the legacy fallback should apply exclusively to rows that claim
no class (the docstring even says this; the code doesn't do it).

### R6. Independence keys split one publisher on port / www variants (HIGH)
`tools/pipeline/retrieval.py::independence_key` uses the raw `host[:port]`
string after stripping scheme/path. Confirmed splits:
- `econ.reuters.com` vs `econ.reuters.com:8443`
- `reuters.com` vs `www.reuters.com`

Two dependent fetches of one publisher count as two independent voices;
through `confidence_from_agreement` that lifts the score from 0.70×ceiling to
0.85×ceiling with zero new information. Mirrors of the same document on two
hosts inflate identically, and nothing resolves redirects before keying.
**Tests:** `test_port_strip_never_collapses_hosts`,
`test_www_prefix_never_collapses`, `test_redirect_target_never_checked`
(`test_two_mirrors_inflate_confidence` documents that same-adapter mirrors
collapse correctly — the split requires distinct hosts/adapter names.)
**Fix direction:** strip port and leading `www.`; normalise before comparison;
resolve redirect chains to final URL where transport allows.

### R7. Wiki articles inherit 0.5 from nothing (MEDIUM)
`_article_confidence([])` returns **0.5** — an article compiled from zero
sources scores the same as a moderately corroborated one. Per-source default is
also 0.5 (`s.get("confidence", 0.5)`), so a writer omitting the field mints a
half-confidence floor. And article confidence has **no source-class term at
all**: two INFERRED 0.55 items are indistinguishable from two PRIMARY 1.0 items
to the compiler.
**Test:** `test_empty_sources_default_confidence_is_half`,
`test_no_source_class_anywhere_in_wiki_confidence`
**Fix direction:** empty/unknown sources → floor (0.30), not 0.5; carry class
into article metadata and cap by weakest class.

### R8. Artifact store: byte-swap is caught, label-swap is not (MEDIUM)
Good news first: swapping object bytes while keeping a reference fails
`verify_artifacts` (content addressing holds — could not break it). But the
INDEX is writable without any key and `verify_artifacts` checks bytes only:
an attacker can relabel an artifact's `name`/`meta` to e.g.
`{"class": "PRIMARY", "source": "SEC EDGAR"}` and the forged label survives
verification indefinitely. Anything that trusts index metadata inherits it.
**Test:** `test_index_only_attack_kind_confusion`

---

## WHAT I COULD NOT BREAK (genuine failures to falsify)

1. **Seal replay onto altered content** — `verify_seal` recomputes over the full
   canonical payload; changing any field breaks the hash. Held.
   (`test_seal_replay_onto_different_content` passes.)
2. **Content-addressed artifact swap** — the id IS the bytes; no collision path
   found through the public API. Held.
3. **Live-session ledger demotion** — within one process with no checkpoints,
   `assign_source_class` correctly caps INFERRED regardless of declared labels;
   the model's own JSON buys nothing. Held. Every real defect was a boundary
   crossing, not the core rule.
4. **Relevance gate on fresh fetches** — the gate runs at ingestion on live
   paths; only the resume path (known defect) skips it. No additional skip found.
5. **Adversary raising confidence** — synthesis adjustments are all min() or
   capped adds within the provenance ceiling; random probing found no path above
   `MAX_CONFIDENCE_BY_SOURCE[best_class]`.

## PRIORITY ORDER

R2/R5 together mean the entire keyed-HMAC upgrade is bypassable today via the
legacy-compat fallbacks: fix both fallbacks first (small diffs, fail closed),
then R4 (filter `seal_guard` input to the current run), then R6 (host
normalisation), then R7/R8.

## SIBLING PATTERN (why these keep recurring)

Every confirmed defect is the same shape: **a verification that degrades to a
pass under its degenerate input**. Missing digest → skip check; missing key →
accept unkeyed hash; foreign-run checkpoints → replay anyway; empty sources →
0.5; unknown host form → treat as distinct. The fix class is uniform: every
"if X then check" must have an explicit else-fail branch.
