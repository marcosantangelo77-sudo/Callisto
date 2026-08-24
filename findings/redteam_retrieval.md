# RED TEAM — Retrieval & Relevance (findings)

Scope: `tools/pipeline/retrieval.py`, `engine.py`, `synthesis.py`,
`sources/registry.py`, `sources/query_builder.py`. Tests:
`tests/test_redteam_retr_gate_bypass.py`,
`tests/test_redteam_retr_independence.py`,
`tests/test_redteam_retr_selection_nulls.py`
(all currently PASS — each is a deterministic reproduction of a confirmed
defect; when a defect is fixed the corresponding test FAILS, which is the
canary convention this repo already uses for red-team suites).

---

## CONFIRMED DEFECTS

### D1 — Resume replays stored fetches WITHOUT re-applying the relevance gate (H1; sibling of the known defect — this IS the family head, reproduced and pinned)

`engine.ResearchPipeline.run`, resume path:

```python
fetches = [_fetch_from_payload(r) for r in f_oc.payload["fetches"]]
```

Between `_fetch_from_payload` and ingestion in `_answer_leaf` there is no
call to `RelevanceGate.judge`. The only integrity check on replay is
`replay_ledger()`'s `sha256(body) == content_sha256` — that verifies STORAGE,
not GATE PROVENANCE. A poisoned, stale, or legacy checkpoint payload injects
evidence the live gate rejected. Worse: replayed bytes are recorded
`primary=True`, so `assign_source_class` rates them at the PRIMARY ceiling.

Test: `test_resume_replays_fetches_without_reapplying_gate`,
`test_checkpoint_payload_carries_no_admission_proof`.

**Root cause / fix direction:** the fetch_leaf payload must carry the gate's
verdict per admitted item (coverage score + question hash), and the resume
path must verify each restored item against it — or simply re-run the gate
on restore. Admission must be falsifiable across the boundary.

### D2 — Legacy checkpoints silently switch independence counting to the weak rule

`_trace_from_payload` degrades missing fields to empty (correctly refusing
"everything was admitted"), but a pre-wave-4 payload has NO
`independent_keys`. Engine then falls into its fallback:

```python
n_indep = len({f.source_name for f in fetches}) + ...
```

i.e. distinct ADAPTER NAMES, not families/hosts — the exact rule I2 removed.
An old resumed run can claim 2 independent sources from two names inside one
family. Test: `test_legacy_checkpoint_with_no_rejections_field_restores_clean_trace`.

### D3 — HTTP-200 error bodies pass the gate whenever they echo the query (H1)

The gate sees parsed JSON only. Nothing distinguishes an error document from
a substantive one, and API error messages almost always echo the query terms
("no results found for 'federal funds rate rise'"). Confirmed admitted at ≥25%
coverage for three common error shapes (invalid key, quota exceeded, service
unavailable). An outage dressed as evidence.
Tests: `test_error_body_with_200_passes_status_check_and_may_pass_gate`,
`test_error_shapes_are_never_detected_as_errors`.

Fix direction: adapters should surface structured error fields
(`error`, `status: "error"`, `meta.error`) BEFORE the gate, mapping to an
explicit retrieval-failure record rather than candidate evidence.

### D4 — Keyword stuffing passes the gate; dismissal scores like measurement (H4)

Coverage counts token PRESENCE both prefix-wise, not evidential support.
A cross-reference/citation index naming all topic tokens admits; a paper
whose abstract names the topic to REBUT it scores within 0.35 of the paper
that measures it. The gate cannot rank, only admit — so downstream nothing
distinguishes support from mention.
Tests: `test_keyword_stuffing_beats_the_gate`,
`test_abstract_naming_the_topic_to_dismiss_it_scores_equal_to_a_study`.

### D5 — One document mirrored across hosts satisfies min_independent_sources (H3)

Demonstrated end-to-end through `IterativeRetriever`: one byte-identical
body served from `host-a.example` and `host-b.example` produced TWO admitted
FetchResults with the SAME content hash and independent keys
`{host-b.example, scholarly-aggregator}` — requirement of 2 met by one
document. There is NO content-hash dedup anywhere in retrieval/engine/
synthesis (`test_identical_content_hashes_are_never_deduped_anywhere`).
Note also the key mix: openalex collapsed by family name while gdelt fell
back to raw host — two different rules coexist in one set.

Fix direction: dedupe admitted items on content hash before counting
independence; treat identical bytes as ONE voice regardless of source.

### D6 — Independence is keyed by DNS for 17 of 19 sources (H2)

`independence_key` = declared-family name if member else **hostname**.
Consequences, all confirmed:

* Mirror/CDN/proxy host ⇒ new independent voice
  (`test_mirror_or_cdn_host_mints_fake_independence`).
* One publisher, two real API hosts (World Bank api vs datasets) ⇒ two
  voices (`test_naming_drift_leaks_for_anything_outside_the_two_families`).
* Reseller republishing OpenAlex under its own name+host ⇒ fully
  independent voice (`test_family_membership_ignores_base_url_entirely`).
* Synthesis compounds it: `EvidenceItem.from_fetch` defaults base_url to
  `https://{source_name}` — adapter NAME becomes the independence unit;
  two adapter names over one corpus count as two voices even for
  byte-identical content
  (`test_two_urls_same_host_same_doc_two_voices_in_synthesis`,
  `test_claim_group_confidence_rises_with_minted_voices`).

And confidence follows: `confidence_from_agreement` grants
0.7 + 0.15×(extra voices) of the ceiling, so minted voices mint confidence.

### D7 — Redirects launder provenance (H2)

`RestSource._http_transport` never captures the final URL (`resp.geturl`).
urllib follows redirects internally; ledger and FetchResult attribute the
bytes to the REQUESTED url. A compromised mirror can serve bytes provenance
credits to the canonical host.
Test: `test_redirect_rewrites_url_and_nobody_checks`.

### D8 — Evidence content is body[:4000]; provenance breaks at exactly that boundary (H3 sibling)

`_answer_leaf` stores `Evidence(content=f.body[:4000])` but the ledger
recorded the FULL body. Verified consequences:
(1) `assign_source_class` misses primary observation for every document
longer than 4000 chars — silent downgrade to INFERRED;
(2) two documents sharing their first 4000 bytes collapse to identical
`Evidence.content` — one document's provenance covers another.
Test: `test_truncated_evidence_content_breaks_provenance_and_collapses_docs`.

---

## CONFIRMED DEFECTS — selection & nulls

### D9 — Diagnostic-term floor selects sources that cannot answer (H5)

One diagnostic word grants ANY source a 0.50 score floor, above
min_score=0.34. A long question containing "trial" drags clinicaltrials
in regardless of everything else.
Tests: `test_diagnostic_floor_selects_a_source_that_cannot_answer`;
also `test_prefix_matching_mints_spurious_selections` ('war' fully covered
by 'warehouse' — prefix matching has no minimum stem length).

### D10 — Query translation dilutes the subject below 25% (H5)

`translate_question_type` unions ALL tokens of winning adapters' answer
clauses into the selection text. With two winners the subject words are a
minority of the next round's query — sources get selected for
self-describing well, not answering.
Test: `test_translated_query_dilutes_the_real_question`.
Related: `core_query` strips relation/instrument words ('affected',
'data', 'measures'), so any document mentioning the bare nouns passes
relevance even if it never addresses the question's actual claim
(`test_core_query_strips_the_answer_out_of_the_question`).

### D11 — Zero-fetch leaf classifies as an HONEST LITERATURE NULL (H6 — the headline)

A leaf where EVERY source was skipped at planning (ambiguity, honest gaps)
records a round whose sources are all `{"skipped": reason}` — planner skips
are arbitrary prose, and `classify_null` only recognizes the exact legacy
string `"no generic route"`. Result: errors empty, rejections empty,
rounds non-empty → falls through BOTH branches to the final
NULL_LITERATURE return. **"We failed to look" renders as "the literature
is silent."** This is precisely the conflation synthesis.py's docstring
says must never happen.
Test: `test_skip_only_leaf_is_mislabeled_literature_null`.
Related: `test_skipped_planner_gap_is_not_counted_as_no_route` — partial
fan-out reads as full survey because skipped sources appear nowhere in the
verdict.

### D12 — classify_null's documented "mixed" branch is dead code (H6)

The final branch promises "honest null but disclose errors". Unreachable:
that input has non-empty errors, which the SECOND branch intercepts first.
Either behavior is wrong or the branch lies; today any single source error
forces NULL_RETRIEVAL even when other sources genuinely answered
"nothing relevant" (safe direction, but the code does not do what it says).
Tests: `test_classify_null_mixed_branch_is_dead_code`,
`test_one_error_source_plus_one_rejection_is_retrieval_failure`.

### D13 — Empty-envelope 200s (degraded-API signature) read as literature nulls (H6)

`{'results': []}` is correctly rejected by the gate per-item — but a leaf
of only such rejections classifies NULL_LITERATURE ("sources returned only
irrelevant material"). Several APIs serve empty 200s under load; that
signature is indistinguishable from a genuine literature gap.
Test: `test_single_source_architecture_always_yields_honest_looking_nulls`
(the assertion block after the analysis) and
`test_gate_cannot_distinguish_empty_envelope_from_outage`.

---

## ATTACKS THAT FAILED (the wall held)

* Family members are immune to host drift: openalex served from any host
  still collapses to `scholarly-aggregator`. The I2 normalisation works.
* Any-error-forces-retrieval-failure: classify_null errs toward honesty on
  real source errors (D12's flip side).
* Empty result lists DO reach the gate and are rejected (coverage 0).
* `floor_conf` quantises downward everywhere in synthesis — no rounding
  creep found in this family.
* The diagnostic floor correctly cannot be bypassed by caller strictness
  (it floors, never overrides min_score) — but see D9 for what it DOES do.

## PRIORITISED FIX ORDER

1. D1/D2 — checkpoint admission proof + re-gate on restore (confidence
   inflation via resume is the confirmed family defect).
2. D11 — planner-skip awareness in classify_null (silent lying about
   whether we looked).
3. D5 — content-hash dedup before independence counting (one-line-ish,
   large calibration payoff).
4. D8 — store full body or hash-of-stored-content consistently.
5. D3 — structured error detection before the gate.
6. D6/D7 — publisher identity table + capture final URL.
7. D9/D10 — selection scoring (embeddings were always the real answer).
