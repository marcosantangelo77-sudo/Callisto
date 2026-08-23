# Fix: A14 (concurrent puts) and A2 (gc destroys evidence)

Branch `fix/a14-a2-store-integrity`, commit d8f0b17. File touched:
`tools/artifacts.py` only.

## Root cause shared by both defects

The index (`index.json`) was treated as authoritative about what exists,
when the objects on disk are the actual evidence and the index is derived
state. That inverted trust shows up twice:

- A14: every `put()` did an unlocked read-modify-write of index.json, so
  concurrent writers raced; worse, the temp file was a FIXED name
  (`index.tmp`), so two threads could hold the same tmp path — one wins the
  write, the other dies with FileNotFoundError, and last-writer-wins drops
  every entry written since it read.
- A2: `_load_index` explicitly tolerates corruption ("objects survive") by
  returning `{}` — but `gc()` then read that `{}` as ground truth about
  reachability and deleted every object not in it. One gc call after bit rot
  wiped the whole store.

## A14 fix

- All index mutations now go through `_IndexLock`, keyed per store root:
  - in-process: an RLock with owner/depth tracking so nested use
    (`_index_add` → `_write_index`) does not self-deadlock;
  - cross-process: an exclusive flock on `<root>/.index.lock` taken only at
    the outermost acquisition. This repo runs many agents concurrently by
    design, so thread-only locking would not have been enough.
- The write is atomic via `tempfile.mkstemp(dir=root)` — a UNIQUE temp name
  per call — then `os.replace`. No shared tmp path, no torn reads.

Proof:
- 3 threads x 25 puts test passes 30/30 consecutive runs (not flaky-passing).
- 3 independent PROCESSES x 40 puts against one root: 120/120 entries.
- Healthy-index gc still removes nothing; existing pins pass.

Deadlock note for reviewers: the first cut used plain Locks and deadlocked on
the nesting; flock in particular is per open-file-description and will
deadlock against yourself with a second fd on the same file. Hence the
RLock + owner/depth + "flock only at outermost acquisition" design.

## A2 fix

Chose REFUSE over silent rebuild-by-default, with rebuild available:

- On a corrupt/unreadable index, `gc()` emits an `ArtifactIndexCorrupt`
  warning saying exactly what happened and what to do, and deletes NOTHING.
  Failure direction: keep garbage rather than destroy evidence. An empty
  dict proves nothing about which objects are orphans, so deleting on its
  authority was never sound.
- `gc(allow_rebuild=True)` is the recovery path: rebuild the index FROM THE
  OBJECTS (re-hash each, verify digest == filename) and proceed. Objects are
  evidence; the index can always be regenerated from them.

Why not rebuild by default? gc() is destructive; auto-rebuilding inside it
would hide corruption instead of surfacing it, and silently launder
provenance (code_sha256/name/data_refs cannot be recovered from bytes). A
corrupt index is an operator-visible event; refusing makes it one.

## Hardening picked up along the way

- `rebuild_index` now marks rebuilt entries `meta.reconstructed: True` and
  skips any object whose re-hash mismatches its filename (never index bytes
  that lie about their identity). This also un-breaks the A16 red-team test
  that was failing in this file.
- `_load_index` backs up corrupt indexes only once (no clobber of the first
  `.corrupt` evidence copy by later loads).

## Results

tests/test_redteam_artifacts_store.py: 17 failed → 14 failed.
Fixed here: A2 (test_gc_after_corrupt_index_deletes_objects),
A14 (test_concurrent_puts_do_not_lose_entries_or_crash), and A16
(test_rebuild_index_erases_code_and_name_provenance). Remaining 14 failures
are the separate defects A3/A4/A5/A6/A9/A11–A13/A18/A20/B1/B3 — untouched,
as scoped. tests/test_build_b2_artifacts.py (13 tests) still green.

No confidence scores were raised. ~/Documents/GitHub/Callisto untouched.
