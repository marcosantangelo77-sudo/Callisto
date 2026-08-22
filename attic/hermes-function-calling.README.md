# hermes-function-calling/ — QUARANTINED 2026-08-22 (P4, build/memory-trust)

## Assessment (the honest call)

The submodule is a gitlink pinned at ea3c4723, four months stale, and its
working tree here is EMPTY (0 bytes, never checked out on this machine).
Live-import audit, re-derived:

- `inference.py` holds `_HERMES_PATH` (a sys.path insert) and
  `_get_hermes_tools()` (a lazy `from functions import get_openai_tools`).
  NEITHER symbol has any caller in the repo — grep across all .py files finds
  zero call sites. The path insert is dead weight; if the submodule were ever
  checked out it would shadow top-level module names (`functions`) on
  sys.path, which is actively hazardous.
- `upstream_review.py` references the directory by name only (fetch/review
  tooling, not an import), and itself notes UPSTREAM.md's import claims are
  false.
- `tools/hermes_validator.py` already vendors the ~120 lines worth keeping
  (jsonschema-backed validation + the XML/literal_eval extraction ladder),
  with the upstream's weaker hand-rolled type checker replaced.

Verdict: quarantine, not delete. Nothing live breaks; the pin stays in git
history and the submodule reference can be restored exactly.

## Restore note

Full tree:      `git checkout ea3c4723 -- hermes-function-calling`
Submodule init: `git submodule update --init hermes-function-calling`
(and remove this attic copy)

If restoring, also delete the dead `_HERMES_PATH` / `_get_hermes_tools()`
block in inference.py or keep them — they have no callers either way.
