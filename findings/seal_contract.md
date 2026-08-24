# Seal contract audit — documentation and naming pass (task: act on seal_semantics)

Branch: `docs/seal-contract-honesty` (off master 754f473). Documentation and
naming only; no scoring behaviour touched. Follows
research/seal-semantics (d07081a), whose central finding was that PREREGISTRATION
IS NOT IN THE LIVE PIPELINE PATH while docs described it as a core guarantee,
and that the seal's contract needed to be renamed to what it actually certifies.

Authoritative contract now lives in **SEAL_CONTRACT.md** (repo root).

---

## 1. Claim-by-claim audit

| Location | Claim as found | Code supports? | Action |
|---|---|---|---|
| BUILD_MANDATE.md:88 | "seal is now HMAC-keyed; provenance ledger-assigned" | Yes (agp/__init__.py keyed HMAC; provenance.py hash-match assignment) — but listed without stating what the seal attests | Added boundary note pointing at SEAL_CONTRACT.md |
| BUILD_MANDATE.md:113 | "preregistered forward-testing" as generalisation path | Partially — preregistration exists but is unreachable from the pipeline | Covered by the boundary note + NEXT/HANDOFF corrections |
| READme.md:23 | Protocol "enforces disciplined, trustworthy analysis" | Process yes; "trustworthy" invites truth-inference | Kept, but added explicit "What a Seal Means" section with the honest sentence and the chatbot-comparison conclusion, unsoftened |
| READme.md:76 | `/session/{id}` "sealed AGP session" | Tamper-evidence only | api.py docstring now states process-integrity meaning |
| DEEP_RESEARCH.md:226-230 | "an answer with runnable, sealed math attached is checkable by anyone forever … converts 'earned against reality' into something mechanically verifiable even before outcomes resolve" | Over-promise. Artifact hashes make the *math reproducible*, not conclusions *true*; prose rides under the same seal with only process guarantees | Rewritten: re-runnable, tamper-evident; explicitly bounded; resolution still required for earned confidence |
| NEXT.md:233 | Mechanism #6 "Preregistration — commits to what would falsify before running" | NOT in engine path (grep prereg tools/pipeline/engine.py → nothing); reachable only via agp/claims.py:169 for long-lived Claims | Status correction appended: built but not wired; one-shot sealed answers carry no preregistration guarantee |
| MORNING_REPORT.md:131-134, :190 | "sealed criteria committed before evidence" listed as part of what makes Callisto "structurally incapable of overstating its own confidence" | Same gap | Correction appended at both mentions |
| HANDOFF.md:163 | build/preregistration "sealed falsifiers + long-lived claims" | Accurate about the branch's scope; risk of reading as wired | Note appended: built but NOT wired into engine live path |
| CLI `callisto ask` / `callisto show` / run record | SEALED printed bare; a reader can only assume it implies verified truth | — | Output now prints the real contract (process integrity + internal consistency, NOT verified truth; confidence = process score, not calibrated probability) on every SEALED display; run records carry a `seal_meaning` field so the record discloses its own semantics |
| orchestrator.py run_session docstring | "Returns the sealed session dict" | Ambiguous | Docstring states keyed-HMAC + process gates, does not certify truth |
| ARCHITECTURE_MAP.md / COVERAGE_MAP.md | No seal-guarantee claims found (grep seal/prereg: zero hits in ARCHITECTURE_MAP; COVERAGE_MAP is per-module numbers only) | n/a | No change needed |

## 2. Preregistration decision: REMOVE FROM DOCUMENTED GUARANTEES, mark unwired

Argued choice, not a dodge:

- Wiring it into `PipelineEngine.run()` is NOT cheap-and-safe under a docs
  mandate: criteria must be fixed before any fetch inside a synchronous run,
  which changes engine sequencing, run-record shape, and every seal test.
  Smuggling an engine-behaviour change into this pass would violate the task's
  own constraint (no behaviour change) and the repo rule "nothing automated
  may weaken a gate" in spirit — an untested wiring IS a weakened gate.
- Honesty about absence beats a dead promise: all user-facing descriptions now
  either omit preregistration or mark it explicitly BUILT BUT NOT WIRED.
- The subsystem itself is good and tested (tests/test_build_p2_preregistration.py);
  wiring it is filed as open work, with its real cost stated.

## 3. The honest sentence, everywhere it matters

> **"SEALED, PROBABLE, 0.55" means: this exact text, with this evidence, was
> produced through the declared retrieval-and-review process and has not been
> altered since. It does NOT mean anyone checked that the answer is true.**

Placed in SEAL_CONTRACT.md, README, and emitted by the CLI on both `ask` and
`show`. Run records self-describe via `seal_meaning`.

## 4. Chatbot comparison, unsoftened

README now carries verbatim-in-substance: on raw correctness Callisto is not
yet meaningfully better than chatbot deep research; both hand the same wrong
answer from the same wrong evidence, except Callisto stamps it PROBABLE inside
a cryptographically verifiable wrapper. What it genuinely adds today:
auditability, honesty about gaps, refuse-to-seal gates, immutability.

## 5. Verification

- Full suite re-run on this branch; failure count unchanged from baseline 25
  (docs-only diff: callisto.py print/record strings, docstrings, .md files).
- No file in ~/Documents/GitHub/Callisto touched.
