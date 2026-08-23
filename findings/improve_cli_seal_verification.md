# CLI FRONT DOOR — the run record carries its own proof (2026-08-23)

**Area chosen: the CLI front door's run persistence — the last mile of
BUILD_MANDATE property 3 ("evidence a human can check").**

Why this one: every area in the mandate list now carries an improve_*.md or a
peer's uncommitted work (engine.py, sources/*, schema/core were mid-edit this
run — off-limits under exclusive file ownership; three red-team canaries on
this branch reproduce only against build/dd-decomposition-diversity, which is
a merge problem, not an improvement). The prior CLI persistence pass
(improve_cli_run_persistence.md) closed "the evidence dies when the terminal
scrolls" but left the chain broken at its final link — and that gap is on a
file no peer touches.

## What was wrong — measured

`_result_record()` persisted the verdict but discarded `result.session` —
the one object `AGPSession.seal()` actually covers. Consequences, reproduced:

1. **Post-seal editing was free.** Edit `"conclusion"` in a saved run JSON →
   `callisto show` printed the tampered text under `SEALED : VERIFIED 0.90`,
   exit 0, no complaint. Artifacts were re-hashed at show time; **the
   conclusion itself was trusted blindly.** A checkable-evidence record that
   silently accepts edits launders tampering with the system's own seal
   imprint.
2. **No session meant no way to re-check anything.** The record carried leaf
   summaries but not the evidence list, source classes, objections or seal
   hash — the bytes `AGPSession.verify_seal` needs. The HMAC keying work
   (instance4/R5) had no read path from the CLI.

Also checked and found sound: `_verify_artifact` re-hashing (ok/missing/
CORRUPT), runs-dir env handling, atomic persist, legacy graceful paths.

## What changed (1 commit: 91af118)

- `_result_record` persists the full sealed AGP session dict (`session`
  field, ~1 KB for a typical run) whenever `result.sealed`; degrades to
  null rather than crashing `ask`.
- New `_verify_session(rec)`: recomputes the seal via `AGPSession.verify_seal`
  AND compares the printed conclusion against the sealed summary's conclusion
  (catching edits to either the session or the wrapper). Returns
  verified / TAMPERED / unsealed — never raises.
- `callisto show` prints a `seal :` line for sealed runs. Tampered records
  still print (honest report, not censorship) but say "treat everything below
  as untrusted". Legacy records without a session report "unverifiable".

## Before/after

| measure | before | after |
|---|---|---|
| edit conclusion in saved JSON | SEALED : VERIFIED 0.90, exit 0 | `seal : TAMPERED — treat everything below as untrusted` |
| edit stored session evidence | undetectable | seal fails → TAMPERED |
| re-checkable proof in record | artifact hashes only | artifact hashes + full sealed session |
| CLI suite | 19 | 23 (+4, real HMAC seal, not stubs) |

## Honest caveats

- The seal is only as strong as its regime: with no `CALLISTO_SEAL_KEY`, an
  unkeyed digest verifies integrity but not who sealed (documented in
  memory_epistemics; unchanged here). Setting the key upgrades this to a
  forgery-resistant check for free.
- `runs list` does not verify seals (show does); acceptable — show is the
  audit command.
- Pre-existing failure noted while verifying: i1_integration
  multi-source test fails on this branch WITH my files stashed too — caused
  by a peer's uncommitted engine.py/retrieval.py work, not by this diff
  (verified by stash-push/stash-pop round-trip).
