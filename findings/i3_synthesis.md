# I3 — Cross-Source Synthesis (WAVE 5)

**Shipped:** `tools/pipeline/synthesis.py` (~490 lines) +
`tests/test_build_i3_synthesis.py` (25 tests, property-based for the
confidence invariants). Fixtures only; no socket; model-free and
deterministic — the model reasons ON the report this module produces,
it never establishes what the evidence structure is.

## What changed epistemically

Before: engine hands the synthesizer a flat list of `e.content` strings
and asks for an answer — concatenation. After: the evidence's AGREEMENT
STRUCTURE is a first-class, serialisable object:

1. **Triangulation** — `triangulate()` groups evidence by CLAIM
   (`claim_key`, normalised tokens), not by document. Within a group,
   voices are counted with `retrieval.independence_key` reused verbatim
   (re-exported; there is exactly one notion of independence in the
   codebase). Ten documents from one publisher = one voice.
2. **Contradiction as output** — `detect_contradictions()` returns
   first-class `Contradiction` objects: kind (`numeric`/`stance`),
   each side with its sources, independence keys and per-cell provenance,
   severity, and `what_would_settle_it`. Same-publisher values collapse
   to one voice before comparison; tolerance is relative (default 10%).
3. **Confidence from structure** — `confidence_from_agreement()` starts
   at the provenance source-class ceiling (agp.thresholds) and grants
   70% of ceiling for one independent voice +15% per extra DISTINCT
   voice — never above the ceiling, never above what inheritance allows
   (engine applies `clamp_parent_confidence` after, unchanged). Any live
   contradiction caps the group at SPECULATIVE (0.54), matching the
   existing requirement-floor band.
4. **Structured extraction** — `extraction_table()`: one row per stated
   value, unit-normalised (`extract_values`: %, bps, bn/mn/trillion),
   provenance per cell (source, independence key, sha256, URL).
   `SynthesisReport.to_dict()` ships the whole thing JSON-serialisable.
5. **Honest nulls** — `classify_null(trace)` reads the retrieval trace:
   reachable sources whose results were rejected at the relevance gate
   → `literature_null`; source errors / no-route / nothing attempted →
   `retrieval_failure` with an explicit "do not read this as 'the
   literature does not address this'" explanation.

Domain-general throughout: no domain vocabulary; scholarly / market /
materials inputs produce byte-identical structures (tested).

## Property-based invariants (hypothesis, ~1000 examples total)

- score never exceeds `MAX_CONFIDENCE_BY_SOURCE[best class]`
- N items from ONE independence unit score exactly like 1 item
- score non-decreasing in distinct independent voices
- contradiction strictly lowers or caps at 0.54; reasons name SPECULATIVE

## EXACT ENGINE ADOPTION DIFF (for the merge pass)

Engine needs three touches, all additive; nothing existing changes
behaviour when the feature flag is off.

```diff
--- a/tools/pipeline/engine.py
+++ b/tools/pipeline/engine.py
@@ imports (top of file)
+from tools.pipeline.synthesis import (
+    EvidenceItem, synthesize as synthesize_report,
+)

@@ ResearchPipeline.__init__ signature
     def __init__(self, *, model: PipelineModel, adversary_router=None,
                  ...
+                 synthesis_enabled: bool = False):
         ...
+        self.synthesis_enabled = synthesis_enabled

@@ LeafOutcome dataclass — add fields (defaults keep old callers valid)
@@ (or attach on PipelineResult only; minimal version below attaches
@@  nothing per-leaf)
```

```diff
@@ run(), after the leaf loop, BEFORE stage 6 conclusion assembly
+        if self.synthesis_enabled:
+            from tools.pipeline.retrieval import RetrievalTrace
+            # Rebuild EvidenceItems from admitted fetches. Claim text comes
+            # from the Manager answer per leaf (one claim per leaf today);
+            # base_url from the fetch record so independence_key is real.
+            items = []
+            null_traces = {}
+            for q, l in zip(program.leaves, result.leaves):
+                leaf_fetches = [f for f in result.fetches
+                                if f.question_id == q.question_id]
+                if l.answer and leaf_fetches:
+                    for f in leaf_fetches:
+                        ev = next((e for e in session.evidence
+                                   if e.source_name == f.source_name
+                                   and e.content.startswith(f.body[:100])),
+                                  None)
+                        items.append(EvidenceItem.from_fetch(
+                            f, claim=l.answer.strip(),
+                            source_class=(ev.source_class.value
+                                          if ev else "INFERRED"),
+                            base_url=_base_url_for(self._get_registry(),
+                                                   f.source_name)))
+                else:
+                    null_traces[q.question_id] = getattr(
+                        l, "_trace", None) or RetrievalTrace(
+                            question_id=q.question_id)
+            rep = synthesize_report(question, items,
+                                    null_traces=null_traces)
+            result.notes.extend(rep.notes)
+            result.contradictions = rep.contradictions
+            # Structure can only LOWER the parent proposal (asymmetry kept).
+            proposed = min(proposed, rep.confidence) \
+                if rep.groups else proposed
```

```diff
@@ helper at bottom of engine.py
+def _base_url_for(registry, source_name: str) -> str:
+    entry = registry.get(source_name)
+    return getattr(getattr(entry, "spec", None), "base_url", "") \
+        or f"https://{source_name}"
```

Two small enablers the merge pass should also apply (both inside
I3-owned territory if preferred):

- `_fetch_for_question` should stash `trace` on the outcome (e.g.
  `outcome._trace = trace`) so honest-null classification has the real
  retrieval trace instead of an empty fallback.
- `PipelineResult` gains optional fields
  `contradictions: list = field(default_factory=list)` and
  `synthesis: Optional[dict] = None`; `summary_dict()` includes
  `"contradictions": [c.to_dict() for c in self.contradictions]`.
  Both default-empty → byte-identical output until enabled.

Flag stays OFF by default: behaviour identical to d33d61f until
`synthesis_enabled=True`.

## Test results

- `tests/test_build_i3_synthesis.py`: 25 passed.
- Full suite (excluding 4 pre-existing collection errors from missing
  deps — fastapi etc., not touched by this work): 1884 passed /
  17 failed, IDENTICAL failure set before and after this change
  (claude_findings, prop_scanner, ... — pre-existing on the branch).
