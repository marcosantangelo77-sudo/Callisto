# CLI Front Door — run persistence (2026-08-23)

Area: the CLI and how a human actually uses this thing. Not previously
covered — retrieval (wave 4/P1), synthesis (i3), checkpointing (w3),
provider routing (oxalpha), claims migration (p2) were all done.

## The defect

`callisto.py ask` ran the full pipeline and printed a verdict, but
discarded `result.conclusion`, `result.artifact_refs`, and every fetch
URL/content hash. A 4-minute live run produced checkable evidence that
died when the terminal scrolled. No way to see a past run again. This
violated BUILD_MANDATE property 3 ("evidence a human can check") at the
last step of the chain.

## What landed (b1129e5)

- `ask` persists every result as an atomic JSON record under
  `$CALLISTO_STATE_DIR/runs/` (`CALLISTO_RUNS_DIR` overrides): question,
  sealed/refused + reason, full conclusion text, per-leaf outcomes,
  artifact refs (full dicts), fetches with source/url/content_sha256,
  objections, notes. Path printed after the verdict; each artifact hash
  printed too.
- `callisto runs [--limit N]` — newest-first list: id, SEALED/REFUSED,
  tier/score, question.
- `callisto show <id|prefix>` — reprints the conclusion; **re-hashes
  every referenced artifact against the ArtifactStore** and prints
  ok / CORRUPT / missing per one; dedups fetch provenance. Ambiguous
  prefix is rejected; unknown id exits 1.
- Records live off OneDrive via `tools.state_paths.state_dir()`.

Tests: 6 new in tests/test_cli_front_door.py (19 CLI total). Full suite:
2075 passed; 18 failures verified pre-existing on this Mac (xgboost/
libomp dlopen + fixture tests) by stashing the diff and re-running.

## What was deliberately NOT added

- No `--json` output flag, no run deletion/GC, no export. Nobody asked;
  records are plain JSON files already.
- Sports untouched; no lifecycle/gate code touched.

## Remaining observations for a future run on this area

- `status` still reads only the sports lifecycle DB; it says nothing
  about saved AGP runs or claims. A "runs" section there would unify
  the two surfaces.
- Run ids embed a Python `hash()` slice — stable within a process run
  but not across PYTHONHASHSEED changes; collisions fall through to
  same-second overwrite risk (low, but a uuid4 suffix would be cleaner).
- `doctor` does not check artifact-store writability or disk state.
