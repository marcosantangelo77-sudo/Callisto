# RED TEAM — the source registry & query-builder seam: sources that lie,
# checkpoints that lie

**Surface:** `tools/sources/base.py` (RestSource provenance recording) x
`tools/pipeline/retrieval.py` (`_fetch_one`, the parallel fan-out) x
`tools/pipeline/engine.py` (the `_FetchRecorder` replay) x
`tools/pipeline/checkpoint.py` (`seal_guard` / `_trace_from_payload`) —
i.e. what happens when a source returns 200/502 with an ERROR body, and
when a checkpoint's independence claim is inflated.

**Why this surface:** unattacked ground per the rotation list ("the source
registry and query builders — what happens when a source lies, or returns
200 with zero results"). The last three passes clustered on
resume/checkpoint variants and confidence arithmetic; this seam — where a
remote server's bytes first acquire trust — had only its happy path pinned.
It is also where FAMILY #1 ("a check whose input can be missing") predicts
more blood: RestSource records provenance BEFORE any consumer looks at the
status code.

**Method:** corrupt-one-field replay (a recorded run with one field —
`independent_keys` — corrupted) plus adversarial constructions against the
modules' own docstring contracts. Not previously used here; distinct from
the last pass's property sweep and the earlier differentials.

Deliverable: `tests/test_redteam_source_lies.py` — **3 fail on master**
(SL-1a/b/c, SL-2a, SL-2b), 4 honest pins pass. Run:

    python3 -m pytest tests/test_redteam_source_lies.py -q

---

## HEADLINE FINDING — SL-2 (HIGH): the seal guard verifies BYTES but not the
## COUNT the requirement gate consumed; on the default deployment a resumed
## run seals over an independence claim nothing recomputed

The resume path restores `independent_keys` from checkpoint JSON verbatim
(`engine._trace_from_payload`, engine.py:1023). The answer stage then reads
`len(trace.independent_keys)` to satisfy `min_independent_sources`
(engine.py:508-512). NOTHING cross-checks those keys against the fetch
records restored beside them — or even bounds them by their number:

    payload["fetches"]        -> 1 record (openalex)
    payload["independent_keys"] -> ["api.openalex.org",
                                    "api.semanticscholar.org"]   # the lie

`_answer_leaf` computes n_indep = 2 → requirement gate MET on one real
fetch. `replay_ledger` verifies only body digests; `provenance_is_intact`
only checks bodies are in the ledger; **`seal_guard` returns SEAL**
(test_sl2_seal_guard_seals_over_the_lie fails). The identical FRESH run
could never produce this state — keys come from actual specs there — so
this violates the resume invariant "a resumed run cannot beat a live one"
(the C2/D3 family, one layer deeper).

Deployment context matters: `_harness_key()` defaults to None (documented
default), so the HMAC layer never runs and nothing authenticates the
payload either. Under a keyed regime the tampered payload is caught
(SL-3 pin proves it) — but the D1 gap noted in checkpoint.py:440-447 means
even keyed deployments never call `verify_signature()` on the load/replay
path itself. The content-based floor (`_is_fetch_stage`) checks that fetch
records EXIST, never that they SUPPORT the trace claims beside them.

This is FAMILY #3 inverted: absence-as-success was fixed for `fetches`
(C3); now PRESENCE-of-a-lie-as-success remains for the field beside it.
And FAMILY #5: a stored count standing in for actual independence.

## CONFIRMED BREAKS

### SL-1 (HIGH): a failed fetch's ERROR BODY mints PRIMARY provenance and
### verifies citations

RestSource.`_record()` writes every body as `primary=True` BEFORE any
consumer sees the status (base.py:299-318). The retriever's status!=200
check raises AFTERWARD (retrieval.py:622-626), but the scratch recorder
already holds the error bytes as PRIMARY observations — and the engine's
leaf-order replay loop (engine.py:737-738) replays EVERY scratch call into
the real ledger UNCONDITIONALLY. Nothing ever supersedes a failed fetch:
`record_gate_rejection` fires only on the gate-rejected (200-but-
irrelevant) path. Consequences, all reproduced in test_sl1:

  - SL-1a: an HTTP 502 API-error JSON sits in the real ledger as PRIMARY.
    Any model output echoing those bytes assigns PRIMARY (ceiling 1.0).
  - SL-1b: the URL of a FAILED fetch registers in `_urls`, so citation
    grounding treats it as genuinely fetched.
  - SL-1c: model prose merely citing a failed URL earns SECONDARY (0.75
    ceiling) instead of INFERRED (0.55).

Corollary (SL-1d, asserted in the test): `RestSource.get_json()` NEVER
consults `status` at all — a 502-with-JSON-body returns cleanly with a
well-formed FetchRecord. Only the retriever checks; every other caller of
get_json across tools/sources/*.py inherits the hole. Note the branch I
found this on already carries failing R4/R4b tests asserting the sibling
defect (gate-rejected bytes minting PRIMARY) — same replay seam, one fix
that half-landed: rejections are superseded when the ordered replay runs,
failures never are. FAMILY #2 shape exactly.

### Pre-existing failures observed (not mine, corroborating)

`tests/test_redteam_retrieval_relevance.py` currently fails 6 cases on
this worktree (R2 junk-prefix coverage, R3 fake voices from the sandbox
fallback, R4/R4b laundering). I edited nothing outside my own file; these
are regressions already on `redteam/answer-correctness` at 96e09c9. Their
presence strengthens the family case: the ledger-trust boundary is leaking
from at least three directions at once.

## HONEST NEGATIVES

- **Host-collision independence:** I swept all 21 registered adapters for
  two distinct sources sharing an independence_key host — none collide
  beyond the declared scholarly-aggregator family. `independence_key`'s
  empty-input fallbacks return "" / "not a url" consistently, and why.py's
  re-derived counting AGREES with retrieval's on differential probes
  (including full-URL vs spec-base_url inputs). No third copy of the
  membership rule has drifted.
- **round() vs floor_conf in `_answer_leaf`:** a 10k×6 sweep found round()
  CAN raise above floor_conf (16k boundary cases like 0.005→0.01), but not
  on any value reachable through the leaf's clamps (est≤ceil both quantised
  inputs); flagged as latent, not asserted.
- **classify_null_kind:** fed empty traces, error-only rounds, mixed
  partial coverage — correctly refuses to read retrieval failure as honest
  null in every case probed.

## RECOMMENDED FIX SHAPE (for the owner; not implemented)

1. SL-1: make `RestSource.get_json/post_json` raise (or mark the record
   non-primary) on status >= 400 before `_record`; OR have the engine's
   replay skip scratch calls whose FetchRecord.status != 200. One-line
   direction rule: a non-2xx response may only ever LOWER trust.
2. SL-2: in `_trace_from_payload` (or better, inside `provenance_is_intact`
   so the guard covers it), recompute `len(independent_keys)` upper bound =
   number of admitted fetch records, and reject/seal-refuse when the stored
   set exceeds it. Keep ONE predicate shared by engine and guard (the file
   already states this discipline for `_is_fetch_stage`).

## FAMILY ACCOUNTING

Both findings are instances of EXISTING families wearing new clothes —
which is the point of PATTERNS.md: SL-1 is Family #1/#3 (a check whose
input — the status code — is missing at the moment trust is assigned;
absence of failure treated as success), SL-2 is Family #5 + the resume
differential (a stored count standing in for actual independence; resumed
beats fresh). No NEW family proposed; but note the recurring LOCATION:
every defect found tonight lives at the exact line where a WRITE is
recorded before its VERDICT exists — base.py:_record, engine replay,
_trace_from_payload. "Provenance precedes judgment" is load-bearing and
unwritten.
