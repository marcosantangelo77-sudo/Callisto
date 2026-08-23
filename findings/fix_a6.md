# FIX A6 — phantom artifacts & dead verification

**Branch:** `fix/a6-phantom-artifacts` · **Baseline:** 51 failures on master
(34 pre-existing + 17 artifact repros) · **After:** 48 failures
(**−3**, no regressions). Breakdown of the artifact-surface files:

- `tests/test_redteam_artifacts_store.py`: 17 failed on master → 15 now.
  The two A6-family tests pass: the child-attested test was rewritten to
  pin the fix (no byteless refs; claim record stored, non-citable;
  verify_artifacts ok), and its failed-run sibling passes unchanged.
- `tests/test_build_b2_models.py::TestSandboxRegistrySeam::test_full_chain`
  — previously failing because it asserted the OLD phantom-minting
  behaviour; updated to assert the new invariant and to actually pass the
  tmp store into store_sandbox_outputs (it never did — refs went to the
  repo default store while assertions checked the tmp one). Now passes
  (file: 17/17).
- `tests/test_a6_seal_gate.py` — NEW, 6 tests, all passing: gate pass /
  refuse-missing / refuse-corrupt / no-refs, gate wired before seal() in
  engine source, end-to-end phantom-ref refusal at the pipeline level.

## Decision 1 — bytes in the store, or the ref is not cited

**Chosen: bytes or nothing. A ref may not exist without stored bytes.**

A child-attested hash is a CLAIM about bytes nobody observed, and a claim
cannot back a citation in a sealed conclusion. Two options were considered:

1. *Keep minting refs, force every consumer to filter on
   `attested_by_child_only`.* Rejected: N consumers each must remember the
   rule forever — exactly the failure shape of this finding (a property held
   by convention, violated by any new caller).
2. *Stop minting citable refs from child-reported hashes* (CHOSEN). The
   claim itself becomes a stored, hash-verifiable JSON record
   (`sandbox_attestation_claim`, `citable_as_evidence: false`) documenting
   what the child reported. Nothing is lost: the claim is preserved and
   auditable — it just can no longer masquerade as evidence. The engine's
   normal path (`keep_workspace=True`) already stores real bytes, so the
   attested path is now only reachable outside the pipeline.

`store_sandbox_outputs` now self-checks the invariant before returning:
every returned ref must satisfy `store.exists(ref.sha256)`.

## Decision 2 — wire verify_artifacts, don't delete it

The function's contract is correct and cheap; what was wrong was that no
production path called it — the system LOOKED checked while nothing was
(fourth instance of the pattern: W5, K1, C1, A6). New
`engine.verify_artifact_gate(store, refs)` runs immediately before
`session.seal()`; any missing/corrupt cited artifact refuses the seal with
an explicit reason, same fail-closed shape as the checkpoint seal_guard.

## Test accounting

- `test_child_attested_ref_has_no_bytes_in_store` — rewritten to pin the fix
  (no byteless refs; claim record stored non-citable; verify passes). PASS.

## JOB 3 — sweep: other attestation paths never verified

Grep across tools/, agp/, callisto.py, memory.py for verify_* definitions
and their call sites. Listed even where unfixed:

1. **Preregistration.verify_seal has ZERO callers** (agp/preregistration.py:181).
   `Preregistration.from_dict` sets `_sealed = bool(seal_hash)` — presence of
   ANY hash STRING counts as sealed. `Claim.resolve()` scores against
   "sealed criteria" without ever recomputing the seal, and
   `ClaimStore.load()` verifies only the journal's prev-hash chain, not the
   embedded preregistration seal. A tampered claim state carrying an
   arbitrary 64-hex string scores CONFIRMED against criteria that were never
   sealed. SAME PATTERN AS A6, fifth instance.
2. **ModelRegistry accepts unverifiable provenance end-to-end**
   (tools/model_registry.py): `register(code_sha256=…)` /
   `add_prediction(artifact_refs=[…])` take caller-supplied strings;
   nothing checks those artifacts exist in the store, and `track_record()`
   computes Brier scores/hit rates over them. Phantom refs flow into the
   model's measured performance — A6's shape applied to track records.
3. **install_adversary/make_seal_veto are never installed in production**
   (agp/adversary.py:499,566; zero callers outside tests). AGPSession.seal()'s
   documented fail-closed reviewer veto hook runs with `seal_veto=None`;
   the engine calls `adversary.attack()` directly beforehand instead, so the
   in-seal fail-closed wrapper (reviewer crash ⇒ refuse) is bypassed on the
   real seal path.
4. **Claim journal chain is unkeyed** (agp/claims.py save/load): prev-hash
   chaining over plain SHA-256 — anyone with file write access can rewrite
   the entire history and recompute the chain. Also: `load()` computes
   `expected = sha256(ln)` and never uses it (dead variable next to the
   live check).
5. **PipelineResult.summary_dict cites 12-hex truncated ids**
   (tools/pipeline/engine.py:151) — unresolvable against the store, so the
   human-facing summary cannot be verified even now that the gate exists
   (subset of A20; unfixed here).
6. Verified-clean during sweep (have real production callers):
   `AGPSession.verify_seal` (memory.py:444, knowledge_wiki.py:280,
   scripts/_seal_audit.py), `Checkpoint.verify_signature`
   (partition_admissibility), `EvidenceRecord.verify_proof` +
   signature gate (retrodiction/cutoff.py), `verify_seal_method`
   (memory_epistemics.admit_learning). `verify_learning_seal`
   (memory_epistemics.py:162) has zero callers but is redundant with
   admit_learning's own use of verify_seal_method — candidate for deletion.

## No confidence score was raised

All changes are refusals, records-of-claims, and gates. The clamp/min-only
asymmetry in engine.py is untouched.
