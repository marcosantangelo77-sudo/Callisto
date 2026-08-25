# Source registry integrity v2 — regressions for error-body provenance

Branch: fix/source-registry-integrity-v2 (production commits f7201d0, 79e33e5)
Date: 2026-08-25
Scope: tests only. No production file needed correction — every new
regression passes against f7201d0+79e33e5 as committed.

## What the regressions pin

New harness: tests/test_redteam_source_error_provenance.py (14 tests,
network-free under the NoSocket guard).

### Seam A — RestSource non-200 handling (f7201d0)
GET and POST 503 responses carrying raw JSON or HTML fixture bodies must:
- raise SourceError naming the status/URL, and
- leave the RAW wire bytes absent from ProvenanceLedger.has_observation /
  is_primary_bytes (asserted on the exact fixture strings, not a
  re-serialized dict), and
- leave the URL absent from observed_urls().

Covered: GET×{JSON,HTML}, POST×{JSON,HTML}.

### Seam B — IterativeRetriever scratch-recorder replay (79e33e5)
A raw, non-canonical BLS HTTP-200 REQUEST_NOT_PROCESSED envelope
(whitespace + key order deliberately unlike json.dumps(sort_keys=True))
routed through parallel fan-out must:
- surface as an honest source ERROR in trace.rounds ("quota" named),
  never as a gate rejection;
- have its RAW bytes AND its canonicalized form absent from the real
  ledger, and api.bls.gov absent from observed_urls();
- not poison a succeeding source's provenance (a good body on another
  source still lands PRIMARY and citation-verifiable).

### Valid envelopes unchanged
- BLS REQUEST_SUCCEEDED with empty series → classify_fetch_failure None.
- Normal nonempty HTTP-200 → PRIMARY bytes + cites_verified_url true.

## Commands / results

    python3 -m pytest tests/test_redteam_source_error_provenance.py \
        tests/test_build_r4_sources.py::TestRestSource -q
    → 14 passed in 0.25s

## Harness notes (for future editors)
- classify_fetch_failure keys on source_name == "bls"; fixture sources
  exercising Seam B must be literally named "bls".
- The engine's fixture_transport only serves status 200; Seam-B routes use
  a local (status, body)-tuple transport so future non-200 replay cases can
  be staged without touching production code.
