# Battery D3 — transport noise masquerading as epistemic judgement

**Date:** 2026-08-24 · **Branch:** fix/adversary-parse-flakiness (gate worktree) · **Battery ref:** findings/question_battery.md

## The defect

Seven of 41 bad battery outcomes were not epistemic failures at all.
`hermes_cli` sometimes wraps its JSON response in prose (fences, leading
commentary, trailing explanation). Two compounding causes:

1. **Parser fragility.** `extract_json` (tools/pipeline/model.py) returned
   `None` as soon as the FIRST balanced `{...}` span failed `json.loads`.
   A prose sentence like "the verdict {see below}" aborted extraction even
   when a valid object followed. Separately, `Adversary.attack`
   (agp/adversary.py) had its OWN inline parse (`json.loads(content or
   "{}")`) that raised JSONDecodeError on prose wrapping — a second parsing
   rule, exactly the duplication pattern that has drifted every time in
   this repo.
2. **Undistinguished failure.** That JSONDecodeError was caught by the same
   blanket `except Exception` as a router crash and surfaced as a BLOCKING
   objection reading "adversary backend failed … refusing by default". The
   fail-closed outcome was CORRECT (red-team F6c: an unparseable critic
   must never read as approval) but the recorded REASON was a lie: a
   transport formatting quirk was indistinguishable from a genuine veto,
   so infrastructure noise got believed as epistemic judgement.

## Fixes

### 1. One parser, hardened (`tools/pipeline/model.py`)
`extract_json` now tries, most-specific first: whole-text parse → each
``` fenced block → EVERY balanced `{...}` candidate, skipping candidates
that don't parse instead of giving up on the first failure. Returns None
only when nothing in the text is a valid JSON object.

`Adversary._parse_verdict` now routes through `parse_model_json` — the one
shared parser — deleting the adversary's private `json.loads` path.

### 2. Two distinct facts, distinct code paths and reason strings
(`agp/adversary.py`)
- Router CRASH → BLOCKING `"adversary backend failed (...)"` — critic never spoke; unchanged behaviour.
- Response arrived but UNPARSEABLE after bounded retries → BLOCKING `"adversary transport failure: … UNPARSEABLE after N attempt(s), not a verdict — retryable infrastructure noise"` with the raw head quoted for diagnosis.
Both still fail closed. Only the label differs — deliberately, so a run
refused by transport noise can be identified and retried rather than read
as "the critic vetoed".

### 3. Bounded retry on parse failure ONLY
`Advision.PARSE_RETRIES = 2`: up to two extra `complete()` calls issued
only when parsing fails. A parsed verdict is final and is NEVER re-rolled
(test asserts call count stays at 1 for a real verdict).

## Tests

`tests/test_redteam_adversary_parse.py` — 10 cases:
prose/fenced/commentary-wrapped JSON parses; unparseable-first-brace no
longer poisons extraction; prose-wrapped verdict reaches the adversary as
a real objection; genuine BLOCKING objection still vetoes and is not
re-rolled; unparseable-after-retries fails CLOSED labelled
"adversary transport failure" with bounded call count; flaky-format
recovers on second attempt; crash path keeps its own distinct string.

Regression check: identical pre-existing failures before/after the change
(diff of FAILED lists over 7 affected test files: byte-identical). Full
suite compared against the branch's 25-failure baseline — no new failures.

## Side-check: Hermes proxy SSL_CERT_FILE fix

Verified landed and secure:
- `hermes_cli/proxy/server.py`: builds
  `ssl.create_default_context(cafile=$SSL_CERT_FILE)` when set, else plain
  `True`. Verification is ON in both branches — no insecure fallback.
- `hermes_cli/auth.py::_resolve_verify`: returns `False` (verify off) ONLY
  behind an explicit `insecure` opt-in; otherwise honours CA bundle env
  vars with an existence-checked fallback to default certs.

No `verify=False` / disabled-verification workaround anywhere in the fix.

## Invariants preserved

- No confidence score may be raised: apply_verdict untouched; both new
  failure paths return BLOCKING objections only.
- Fail-closed default retained in all three outcomes (crash, unparseable,
  veto).
