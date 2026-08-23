# KNOWN FAILURE FAMILIES — read this before attacking anything

Every family below was found MORE THAN ONCE in this codebase, in different
modules, by different agents who did not know the others existed. They are not
anecdotes; they are the shapes this system fails in. Hunt the FAMILY, not the
file — if you find one instance, there is almost always another.

---

## 1. A verification layer that never actually runs (FOUR instances)

The most expensive family. A check exists, looks authoritative, and is inert.

- **W5** `CutoffEnforcer`: signature check ran only `if self._signing_key`, and
  the default constructor passed none. Worse, `sign_key` was never supplied by
  ANY production caller — nothing signed, nothing verified. Dead code guarding
  dead code.
- **K1** `_implied_outcome`: "realised outcome" reconstructed from the recorded
  brier, falling back to `sign(p)` — the prediction's own direction. Agreed
  with the prediction 2000/2000. The calibration table scored the model against
  itself.
- **C1** `replay_ledger`: `if digest and _sha(body) != digest` — an ABSENT
  digest skipped verification entirely and the bytes were still minted PRIMARY.
- **A6** `verify_artifacts`: correctly reports child-attested refs as missing,
  and has ZERO production callers. Sealed conclusions cite artifacts nobody
  stored.

**How to hunt it:** for every check, ask "what calls this, and what happens
when its input is missing?" Grep for the verifier's name; if nothing outside
its own tests calls it, that is the bug. A check that cannot fail is not a
check.

## 2. A fix lands in one copy while another keeps the bug (THREE+ instances)

- The independence membership rule landed **three separate times**:
  `retrieval.in_family`, then `why.py` reimplemented it without normalisation,
  then `sources/base.py:339` was still raw `in members`.
- `floor_conf` had to be applied at **six** sites; the first pass fixed one.
- The forecast-sign rule existed in `retro._leans_yes` AND was mirrored in
  `calibration/instrument.py`.

**How to hunt it:** after finding any rule, grep the whole tree for OTHER
implementations of the same idea — by behaviour, not by function name. The
copies never share a name; that is why they are missed.

## 3. Absence treated as success

- empty/missing `content_sha256` → integrity pass (C1)
- checkpoint with no `fetches` → "provenance intact" (C3)
- empty adversary panel → approval rather than veto (F6c)
- source returns HTTP 200 with zero results → healthy (the FDIC and
  ClinicalTrials live-API defects, 11 of them, all passing fixtures)
- no `answer_binary` → outcome imputed (K1)

**How to hunt it:** feed every gate an EMPTY input. Absence must fail closed.

## 4. A label standing in for evidence

- **D2** stage name: `"fetch" in stage` on an editable JSON string. Rename to
  "decompose" and the structural check goes blind while the bytes still mint
  PRIMARY.
- **F6b** model identity was *spelling* — two spellings of one model read as
  two independent reviewers.
- **S5** `claim_key("") == ()` — three junk claims became three "independent
  voices" at confidence 1.0.

**How to hunt it:** find every place a STRING decides a trust outcome, then ask
who can write that string.

## 5. A structural property standing in for actual agreement

Same key ≠ corroboration. Same count ≠ independence. Volume ≠ breadth.

- **K2** the routing store: one question recorded 100× yields n=100,
  basis="measured". A cherry-picked model answering 10 easy questions beat an
  honest one answering 30 in **500/500** decisions.
- **S1/S5/F5** corroboration groups scoring on membership rather than content.

## 6. Rounding, quantisation, and the direction of error

`round(0.269183, 2) == 0.27` is an automated actor RAISING a confidence score —
the one thing this architecture exists to prevent. A property sweep of 8,956
cases found **1,385 violations**. Always ask which direction an error moves the
number, and prefer the direction that loses information over the one that
manufactures it.

## 7. Tests that pass for the wrong reason

- A test asserting rejection passes because a DIFFERENT gate rejected first,
  never reaching its own subject (10 cutoff tests were about to do this).
- A test whose assertion loop runs over an empty list.
- Hand-picked inputs that never touch the failing boundary — the 8,956-case
  sweep found what every hand-written case missed.

**How to hunt it:** break the production code deliberately and confirm the test
FAILS. If it still passes, it tests nothing.

## 8. The doom loop: a maintenance routine that weakens its own gate

`self_repair` responded to "nothing is passing the quality bar" by LOWERING the
bar. Šidák over lifetime N produced 3,192 rejections and zero promotions.

**Rule:** nothing automated may weaken a gate. A gate may be re-scoped on an
argued basis; it may never be lowered because it is inconvenient.

---

## Method matters more than surface

Ranked by what actually found defects here:

1. **Property-based sweeps** over a parameter space — the single most
   productive method used (1,385 real violations in one run).
2. **Differential testing** — run A vs run B, or resumed vs fresh. Found the
   cross-run laundering (C2) and the split-world guard (D3).
3. **Seam analysis** — a sound component handing a value to a neighbour one
   stage too early or one trust level too high. Found W1–W9.
4. **Corrupt-one-field replay** — take a real recorded run, change one byte.
5. **Mutation testing** — change the code, see which tests fail to notice.
   NOT YET TRIED here.
