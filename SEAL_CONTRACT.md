# What a Callisto Seal Means — the actual contract

This is the authoritative statement of what `SEALED` certifies. Every other
document and every piece of user-facing output defers to this file. If any
document implies more than this, that document is wrong (see
findings/seal_contract.md for the audit that established this).

## The one honest sentence

> **"SEALED, PROBABLE, 0.55" means: this exact text, with this evidence, was
> produced through the declared retrieval-and-review process and has not been
> altered since. It does NOT mean anyone checked that the answer is true.**

Read that again. A seal is a statement about *process*, not about *truth*.

## What SEALED does certify

1. **Process integrity.** Every byte of cited evidence was fetched by real
   tool calls this session and passed a topical-relevance gate
   (tools/pipeline/retrieval.py). Rejected fetches are recorded and cannot
   mint PRIMARY evidence later (agp/provenance.py).
2. **Provenance-assigned source classes.** PRIMARY/SECONDARY labels come from
   content-hash match against the append-only ledger — which code path
   produced the bytes — never from the model's self-description.
3. **No automated inflation of confidence.** Every step on the chain can
   lower a score; none can raise it (agp/thresholds.py floor_conf,
   clamp_parent_confidence, Adversary.apply_verdict).
4. **One adversarial review pass found no blocking internal contradiction**
   between the conclusion and the evidence text (agp/adversary.py).
5. **Non-vacuous payload.** Refuse-to-seal gates reject empty conclusions,
   zero-evidence answers, filtered>kept fetch sets, artifact-hash mismatches,
   and failed checkpoint lineage.
6. **Tamper evidence.** After sealing, any alteration of the record breaks a
   keyed HMAC (agp/__init__.py verify_seal).

## What SEALED does NOT certify

- **That the conclusion is true, or probably true.** The adversary reads the
  same evidence the author did. If the evidence itself is wrong (wrong units,
  wrong series, misread table), the process runs cleanly to a sealed wrong
  answer.
- **That the confidence number is a calibrated probability.** PROBABLE is a
  tier derived from a clamping chain over the model's own estimate and the
  best evidence channel. Nothing yet shows these numbers are calibrated
  against resolved outcomes.
- **That the answered sub-question was the question asked.** The parent
  inherits stance and confidence from its highest-confidence leaf; whether
  that leaf's question entails the root question is not checked on master
  today (the DECISIONAL-leaf stance fix exists on review/deep-audit-0824,
  unmerged at time of writing).
- **That success criteria were preregistered.** See below.

## Preregistration status: BUILT BUT NOT WIRED

`agp/preregistration.py` implements sealed, immutable commit-before-evidence
criteria with marker-based scoring, amendment chains, and disclosure. It is
real and tested (tests/test_build_p2_preregistration.py).

It is **not part of the pipeline seal path.** `PipelineEngine.run()`
(tools/pipeline/engine.py) never creates or scores against a Preregistration.
It is reachable only through long-lived `Claim` objects
(agp/claims.py:169 seal_preregistration) — a subsystem the one-shot
question-to-seal path does not invoke.

Therefore: **"criteria were preregistered" is NOT among a pipeline seal's
guarantees**, and no document may list it as one. Wiring preregistration into
the live engine path is open work (it requires criteria to exist before any
fetch inside a synchronous run — an engine-behaviour change, deliberately not
smuggled in under a docs pass).

## Honest standing relative to chatbot deep research

On the dimension that matters — getting the answer right — Callisto is **not
yet meaningfully better** than chatbot-style deep research. Given the same
wrong evidence, both produce the same wrong answer; Callisto would additionally
stamp it PROBABLE inside a cryptographically verifiable wrapper, which lends
institutional authority to an unchecked guess.

Where Callisto IS genuinely better today: auditability (you can diff exactly
what a run fetched and what was rejected and why), honesty about gaps
(UNPROVABLE / honest-null classification), refuse-to-seal gates, and an
immutable tamper-evident record. These matter for forensics and for building
on results. They are process accountability, not correctness.
