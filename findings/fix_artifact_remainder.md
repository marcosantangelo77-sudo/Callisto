# FIX REPORT — artifact-store red-team remainder (A3, A4/A17, A9 + the rest)

Branch: `fix/artifact-remainder` (worktree `~/callisto-wt/money`)
Baseline when this pass started: **13 failing** in
`tests/test_redteam_artifacts_store.py`. After: **25/25 passing** (0 failed,
0 skipped, nothing weakened). Full suite: 22 unrelated failures remain — all
pre-existing or owned by a peer instance (see "What still fails and why").

Commit: `c3e263c` (autosave folded my working-tree edits together with a peer's
in-flight R4 provenance work into one commit; the artifact/charts/engine/agp
changes are mine, `agp/provenance.py`, `tools/pipeline/retrieval.py` and the two
new red-team test files are the peer instance's).

## Fix-by-fix

### A3 + A9 — `_index_add` is now strictly first-seen-wins (family #1 + #4)
The old code *commented* the invariant ("an artifact's origin does not change
because someone later re-put identical bytes") but enforced it for only two
fields and only when non-empty. `data_refs` and all of `meta` were overwritten,
and an empty `code_sha256` was claimable by any later put. That comment was a
label standing in for evidence (family #4) and the guard could not fail in the
cases that mattered (family #1: a check that cannot fail).

Fix shape: if the index already has an entry for the digest, the new put writes
NOTHING except going through the lock — the existing entry is kept verbatim.
Absence of provenance is no longer an invitation; empty stays empty (A9), and
`data_refs` / `meta` are immutable like the bytes they describe (A3).

### A4/A17 — export_ref: sanitize + contain + never overwrite (SECURITY)
Treated as arbitrary-file-write on a delivery surface, per instruction.
- `ref.name` is attacker/model-writable; export now strips every directory
  component (`../pwned` → `pwned`), allow-lists `[A-Za-z0-9._ -]`, rejects
  leading dots, and falls back to the digest prefix if nothing safe remains.
- The destination is resolve()-checked to be a direct child of dest_dir.
- Silent overwrite removed: an existing file at the delivery path causes a
  numbered disambiguation name instead of replacement, so a crafted artifact
  named like a real report cannot take its place.

### A11 — non-finite values can no longer reach SVG text
`chart_spec` now drops non-finite POINTS pairwise across x and every series
(preserving alignment) and records `[dropped N non-finite point(s)]` in notes;
a series that is entirely non-finite raises. `render_svg` independently refuses
non-finite input so hand-built specs cannot bypass chart_spec. nan/inf can
never appear as axis labels or polyline coordinates again.

### A12 — kind sniffing by structure, not substring
A PK zip is labelled xlsx only when it contains `[Content_Types].xml`; other
zips fall back to txt. `<svg` only counts when the document actually starts
with `<svg`, `<?xml`, or `<!DOCTYPE svg` — HTML carrying an embedded svg tag is
no longer misclassified.

### A13 — duplicate model cells: first-seen wins, contradiction surfaced
ModelLive previously let the LAST duplicate formula win while the Model listing
documented both — the audit sheet contradicted the computation. Now the first
formula owns the cell (same rule as artifact provenance) and duplicates are
recorded on the sheet (`_duplicate_model_cells`) so the conflict is visible
rather than silent.

### A18 — verify_artifacts reconciles claims to the store's index
Re-hashing bytes vouches only for bytes. verify now loads the put-time index
entry and reconciles the ref IN PLACE to what the store actually recorded
(kind, code_sha256, missing name), reporting every correction under
`reconciled[]`. A liar ref no longer gets ok=True *as declared*: what the seal
would cover is the stored truth, not the caller's spelling of it.
Additionally `ArtifactRef.__post_init__` enforces 64-hex on sha256 and
code_sha256 (from_dict inherits this), closing the junk-id hole at
construction time.

### B3 — provenance for unknown columns dropped LOUDLY
Previously `col_idx = ... else 1`: FRED attribution silently landed on column 1
whatever that was. Now the record is skipped with a loud warning naming the
column, sheet and valid columns — never misattached.

### A20 companion — full ids in engine summary_dict
`summary_dict()` cited artifacts as `sha256[:12]` — unresolvable, so citing
them verified nothing. Now full digests.

## Family classification (as requested)

- **A3 = family #4 primarily** (a label standing in for evidence): the
  docstring/comment asserted immutability of provenance while the code let the
  later writer rewrite the claims about immutable bytes. It also has a family
  #1 flavor: the partial field guard existed precisely so the check would pass
  without constraining anything.
- **A9 = family #1 primarily** (a check that cannot fail): the takeover path
  only existed because the guard treated ABSENCE of provenance as free space —
  feed the gate an empty input and it approved anything (family #3 by symptom).
  The mechanism is #1: the "first-seen wins" check was inert exactly where it
  mattered.

## Further instances hunted (grep sweep)

- `tools/model_registry.py:_model_path` — already validates strictly
  (allow-list + equality check); correct, no change needed.
- `tools/pipeline/checkpoint.py:_path` — `run`/`stage`/`key` are interpolated
  into paths with no validation. I probed `save("../escape_run", ...)` and it
  DOES escape root. However: every production caller passes `run_key()` output
  (a sha256 hex) and constant stage strings (`"retro_batch"` etc.), so it is
  not currently reachable with attacker text, and checkpoint.py is outside this
  surface's ownership. Logged here rather than fixed blind: **if any future
  caller interpolates user/model text into rk or stage, FileCheckpointer
  becomes the same traversal bug A4 was. Recommend validating rk/stage/key
  there.**
- `verify_artifacts` callers: previously zero (A6, family #1). Now called from
  `engine.verify_artifact_gate` before sealing — confirmed wired, so the
  reconciliation behavior added here is seal-relevant immediately.
- No other copyfile/join-with-name sites exist in tools/.

## What still fails and why (not mine, not weakened)

- `test_build_p1_pipeline.py::test_end_to_end_sealed_with_provenance_artifact_and_adversary`
  — broken by the PEER instance's R4 change in tools/pipeline/retrieval.py
  (`record_gate_rejection` superseding ledger entries). Isolated: reverting
  ONLY retrieval.py to 8123b86 makes it pass with ALL of my changes present;
  reverting only my files does not. Their new test file
  tests/test_redteam_retrieval_relevance.py asserts the opposite expectation —
  the two instances' assertions need reconciliation between peers.
- `test_tier7_deepresearch.py::TestNoCodeExecutionOrArtifacts::test_no_artifact_return_path_in_synthesis`
  — asserts `"artifact" not in agp/__init__.py source`. This is a
  characterization pin of a VERIFIED ABSENCE claim ("synthesis returns
  prose+scores"), written when agp had no artifact layer at all. My A20 fix
  necessarily introduces artifact refs into the session payload — that is the
  entire point of the fix the red-team file demands. The module's own header
  says: "If one of these fails because the code CHANGED, update both the test
  and the corresponding claim." So this is the documented, legitimate update
  path firing — but updating a test outside my red-team mandate is a decision
  for the tier7 owner; left failing deliberately with this argument recorded.
- Remaining ~20 failures are in peer-owned areas (confidence laundering /
  retrieval relevance / ml_classifier+ml_drift collection errors — the latter
  pre-existing xgboost environment errors, reproduced on clean 8123b86).
