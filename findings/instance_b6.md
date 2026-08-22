# B6 build pass — EDGAR / financial models (branch build/edgar-financials)

## What landed

All under `tools/domains/finance/` + `tests/test_build_b6_*.py` — no files
outside the assigned ownership were touched. Pushed through 34c3b7c.

### S1 `edgar.py` — SEC structured fetcher
- ticker → CIK via official company_tickers.json; companyfacts,
  companyconcept, submissions endpoints.
- Rate-limited to ~4 req/s (SEC declares 10), declared User-Agent
  (`CALLISTO_SEC_USER_AGENT` env override), Retry-After honored on
  403/429, exponential backoff on 5xx/network.
- Every fetch recorded in `agp.provenance.ProvenanceLedger` as a PRIMARY
  observation with the exact body hash and URL — model numbers inherit
  primary provenance by construction.
- `annual_facts()` / `instant_facts()`: duration-span filtering (330–400d),
  latest-filing-wins dedupe per period → restated values win over originals.

### S2 `statements.py` — three-statement assembly
- Candidate-tag lists per line (revenue has 5 candidates etc.); winner is
  the tag covering the most recent anchor period — retired tags can't
  shadow current ones (found live: MSFT stopped using CostOfRevenue in
  2017; NVDA's old contract-revenue tag ends years back).
- Restatement flags when a period carries differing values across filings;
  fiscal labels from each fact's own dates (AAPL Sept FYE works).
- Missing concept → explicit gap entry, never zero. Non-USD facts carry
  their own unit, never relabelled.
- Derived lines (gross profit, simple FCF, working capital) marked
  derived=True with derivation strings; every reported line keeps
  tag + accession + form + filed date.
- `limitations` list states the XBRL-only boundary on every result.

### S3 `models.py` — live-formula templates (via tools/charts.py)
- **DCF**: assumption cells labelled XBRL-fact vs analyst-input vs
  REVIEW-placeholder; FCF projection, Gordon terminal value, EV→equity→
  per-share as LIVE Excel formulas; WACC×terminal-growth sensitivity as
  scenario overrides; sandbox reference computation sealed alongside so
  Excel chain and code agree number-for-number.
- **Proforma**: growth/margin/NWC assumptions drive I/S→B/S→C/F with a
  cumulative-cash plug; simplifications (no D&A schedule) stated in-sheet.
- **Comps**: multiples are formulas over raw peer inputs (blank-safe),
  medians included; prices flagged as analyst-supplied (EDGAR has none).

### S4 `plugin.py` — registration
`DomainPlugin(name="finance", domains={"FINANCIAL"}, keywords=…)` exposing
`edgar_get_statements` and `edgar_build_model`; registered exactly like
sports/compute; orchestrator untouched. Errors return structured
{ok: false, error} instead of raising into the model.

## Verified live during build
- AAPL FY2025 rev $416.16B / NI $112.01B; MSFT FY2023–26 full statements,
  zero gaps; NVDA FY2024–26 incl. capex tag switch; NVDA DCF end-to-end:
  workbook artifact + sandbox EV $3.74T at wacc=9%, g=2%.
- Cross-check: hand-computed DCF chain matches sandbox output to <$1.

## Tests (targeted suites only, per instructions)
- tests/test_build_b6_edgar_finance.py — 25 offline tests (no network).
- tests/test_build_b6_fixtures.py — 40 fixture-driven hard-parts tests
  (retired tags, restatements, sparse filers, Sept FYE, proforma tie-out,
  pinned DCF reference vector). Fixtures committed under
  tests/fixtures/edgar/ with generator.
- B2/B3 suites green alongside (131 combined).

## Honest limits (also emitted in every payload)
XBRL gives tagged statement lines ONLY. No footnotes, segment detail,
lease schedules, commitments, contingencies, or non-GAAP reconciliation —
those need document parsing of narrative sections and are out of scope
here. Market-dependent outputs need quotes EDGAR does not carry.
