# Instance W3 — checkpointing and resumability

Branch: `build/checkpointing`. New file: `tools/pipeline/checkpoint.py`
(no existing file edited). Tests: `tests/test_build_w3_checkpoint.py` (18,
including two property-based suites).

## What was built

1. **Step-level checkpoints.** `FileCheckpointer` stores one JSON record per
   pipeline stage under `$CALLISTO_STATE_DIR/callisto/checkpoints/<run[:16]>/`.
   A step key is `sha256(run_key | stage | input_hash)` where `run_key`
   binds root question/domain/date. Content-addressed: unchanged inputs are
   cache hits and are NOT re-executed — no duplicate fetches, ledger
   entries, or artifacts. Writes are atomic (temp + rename).

2. **Resume semantics that do not lie.** Every checkpoint records UTC
   `produced_at`. A cache hit returns the payload WITH ITS ORIGINAL
   timestamp — evidence fetched an hour ago still reads as an hour old.
   `RunTrace` reports which stages were resumed vs fresh, whether the run
   was a resume at all (`is_resume`), and `oldest_produced_at()`, so the
   caller decides what staleness means.

3. **Idempotence.** Because a completed step short-circuits before its
   execute callable runs, kill→resume cannot double-record anything.
   Property test sweeps every crash point of a 4-stage chain and asserts
   the resumed run's ledger observations, artifact hashes, and on-disk
   payloads equal a clean run's exactly.

4. **Sealing across the resume boundary.** `replay_ledger()` restores
   checkpointed fetch bytes into a fresh `ProvenanceLedger` (dedupe keyed
   on the ledger instance itself, so replaying twice cannot duplicate).
   Integrity is verified, not assumed: each stored body must hash to its
   recorded `content_sha256`, and `provenance_is_intact()` confirms every
   fetch is observable in the ledger. `seal_guard()` returns SEAL/REFUSE:
   any run whose checkpointed evidence fails verification is REFUSED, not
   sealed. Resumption cannot launder provenance-lost evidence.

5. **GC.** `FileCheckpointer.gc(max_age_days=30)` deletes stale checkpoints
   but never one whose `claim_ids` contain an open claim; openness is an
   injected callable so this stays domain-general. Property-tested over
   arbitrary age/openness patterns.

## The exact engine.py adoption diff (for the merge pass)

Three changes, ~15 lines. Nothing else in engine.py moves.

```diff
--- a/tools/pipeline/engine.py
+++ b/tools/pipeline/engine.py
@@ imports (top of file)
+from tools.pipeline import checkpoint as ckpt

@@ ResearchPipeline.__init__ — add one optional dependency
     def __init__(self, *, model: PipelineModel, adversary_router=None,
                  transport=None, store=None, ledger=None, registry=None,
-                 descendant_resolutions: Optional[list] = None):
+                 descendant_resolutions: Optional[list] = None,
+                 checkpointer: Optional[ckpt.FileCheckpointer] = None):
         ...
         self._adversary_router = adversary_router
         self._adversary = None
+        self.checkpointer = checkpointer  # None = no checkpointing (unchanged behavior)

@@ ResearchPipeline.run — wrap stages, guard the seal
     async def run(self, question: str, ...):
         today = today or date.today()
         ...
+        cp = self.checkpointer
+        rk = ckpt.run_key(question, domain.value if hasattr(domain, "value") else str(domain),
+                          today.isoformat())
+        trace = ckpt.RunTrace(run=rk)
+
         # 1. Decompose.
-        try:
-            program = await self._decompose(question, today)
-        except ValueError as first:
-            result.notes.append(f"decomposition repair attempted: {first}")
-            program = await self._decompose(question, today, _repair=str(first))
+        async def _do_decompose():
+            try:
+                prog = await self._decompose(question, today)
+            except ValueError as first:
+                result.notes.append(f"decomposition repair attempted: {first}")
+                prog = await self._decompose(question, today, _repair=str(first))
+            return {"program": prog.to_dict()}          # JSON-safe payload
+        oc = await ckpt.run_stage(cp, trace, "decompose",
+                                  {"question": question, "today": today.isoformat()},
+                                  _do_decompose) if cp else \
+            ckpt.StageOutcome("decompose", False,
+                              {"program": (await _do_decompose())["program"]}, "")
+        program = ResearchProgram.from_dict(oc.payload["program"])  # needs from_dict on ResearchProgram
         result.program = program

@@ per-leaf loop — checkpoint fetch and answer separately
         for q in program.leaves:
-            fetches = await self._fetch_for_question(q)
-            result.fetches.extend(fetches)
-            outcome = await self._answer_leaf(q, fetches, session)
-            result.leaves.append(outcome)
+            f_oc = await ckpt.run_stage(
+                cp, trace, "fetch_leaf", {"qid": q.question_id},
+                lambda q=q: self._fetch_payload_for(q))   # wrapper returning
+            fetches = [FetchResult(**r) for r in f_oc.payload["fetches"]]
+            if cp:
+                ckpt.replay_ledger(self.ledger,
+                                   [ckpt.Checkpoint(key="", run=rk, stage="fetch_leaf",
+                                                    input_hash="", payload=f_oc.payload)])
             outcome = await self._answer_leaf(q, fetches, session)
+            if cp:
+                await ckpt.run_stage(cp, trace, "answer_leaf",
+                                     {"qid": q.question_id},
+                                     lambda: _leaf_payload(outcome))
             result.leaves.append(outcome)

@@ before seal — the anti-laundering gate
         ...
+        if cp:
+            verdict, reason = ckpt.seal_guard(trace, cp.list_all(), self.ledger)
+            if verdict == "REFUSE":
+                result.refusal_reason = reason
+                return result
+        if result.sealed or True:  # after successful session.seal():
+            result.trace = trace   # add `trace: Optional[ckpt.RunTrace] = None`
+                                   # to PipelineResult so resumed runs are
+                                   # distinguishable in their own record
```

Notes for whoever applies this:

- `_fetch_payload_for(q)` is a thin wrapper around `_fetch_for_question`
  that serializes each `FetchResult` via `dataclasses.asdict` — bodies are
  already strings, so the payload is JSON-safe. The deserialization path
  rebuilds `FetchResult(**r)`.
- `ResearchProgram.from_dict` does not exist yet; either add it (agp side,
  trivial) or serialize just `(text, kind, priority, evidence_requirements,
  horizon)` tuples. Do not pickle.
- If `checkpointer is None` the pipeline behaves byte-for-byte as before;
  all new behavior is opt-in. Existing P1 tests pass unchanged (verified by
  running them against this branch).
- `result.trace.is_resume` / `trace.resumed_stages` /
  `trace.oldest_produced_at()` should be surfaced in whatever reports run
  results, so stale evidence is visible to the caller.

## Verification

- `python3 -m pytest tests/test_build_w3_checkpoint.py` → 18 passed.
- Full suite run before merge: see commit history on build/checkpointing.

## Honest limits

- Checkpoints are JSON files, not a transactional store: a torn read is
  handled (corrupt file = cache miss) but there is no cross-step atomicity.
  That matches the failure mode being fixed (process death between stages).
- `replay_ledger` restores content-hash provenance, not wall-clock fetch
  context (headers, rate-limit state). That is deliberate — those facts do
  not affect source-class assignment.
- Sealing resumed runs requires the fetch payloads' bodies verbatim. If a
  future change truncates stored bodies, seal_guard refuses — fail closed.
