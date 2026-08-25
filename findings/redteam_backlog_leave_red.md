# Group B / D2 argument — money-path repros that pin the WRONG invariant
# (or die at their own precondition), and the r3/r3b/r4/r4b family

Date: 2026-08-24. Branch: fix/redteam-backlog-sweep.
Precedent honoured: the M1/M1b standard set by review run 8 (de176bd,
findings/review_2026-08-23f.md §RT1). This file extends that argument to
the remaining red repros in tests/test_redteam_money_path.py and
tests/test_redteam_retrieval_relevance.py. Per the hard rule, none of
these assertions was weakened or edited; each is LEFT RED with this
argument.

## m1 / m1b — fixture arithmetic is wrong (precedent: RT1)

`test_m1_crossed_book_overround_negative_must_be_rejected`: prices 0.60 /
0.61 are complementary ASKS summing to 1.21 — overround **+0.21**, a
wide-but-valid book, not a crossed one. The assertion "overround < 0 must
be rejected" never fires because there is nothing negative to reject; the
test fails only on its own mis-stated premise (`error is not None`).
Review run 8 measured the identical defect in this suite and supplied
corrected repros (0.45/0.50 → overround −0.05). The production overround
sanity gate is now PORTED (tools/edge.py MIN_OVERROUND/MAX_OVERROUND +
fair_probability refusal), so the underlying defect M1 is FIXED against
genuinely crossed books; this specific fixture remains red for the wrong
reason and stays red until its author re-pins it.

`test_m1b_stale_snapshot_mix_manufactures_actionable_edge`: dies at its own
precondition (`assert a.devig_audit["overround"] < 0` → +0.21) before
reaching the invariant it exists to prove — exactly run 8's finding.

## m3 — the test requires the bug to exist ("expected the divergence to reproduce")

The first assertion demands a NONEMPTY list of assessments where edge<0
while kelly>0. The one-price fix (edge and Kelly both computed from the
same devigged probability) makes that population empty by construction.
A test whose first assert is `assert cases, "documents the bug exists"`
is a bug-preserving canary, not an invariant; after the fix it fails at
that line. The invariant it wants (`all(k == 0 ...)`) now holds vacuously
on every input. Left red on the precondition line only.

## m5 — reference quantity uses the RAW implied price

`a_edge_raw = p - q.implied_probability()` compares summary edge against
the RAW-implied edge. For the -110/-110 book the devigged fair is 0.5, so
at p = 0.500500001 the honest devigged edge IS +0.0005 while the raw
reference is −0.0233. Asserting `edge <= raw_edge` would re-instate the
phantom-vig comparison that defect M3 removed (family 2: one copy of
"the price" per module). The rounding direction itself is fixed:
summary() now quantises via truncation toward zero (_round_never_up) and
can no longer round any field upward. Left red on the raw-vs-devigged
reference mismatch.

## m6 — passes today; kept as documentation of the CLV gate

clv_points returns None for the crossed claim book under the ported
per-side audit check. (Recorded here because it was listed among the
original failures.)

## r3 — the assertion contradicts its own docstring

The test computes BOTH quantities inline: `traced` via independence_key
(=1, openalex+semanticscholar collapse) and `fallback` as the raw-name
count (=2), then asserts they are equal. No production change can satisfy
this short of DELETING the declared scholarly-aggregator family — the
opposite of R3's stated defect. The real R3 fix is in engine._answer_leaf's
no-trace fallback (ported: fallback now uses independence_key and adds zero
voices for sandbox runs); this differential harness should compare the OLD
fallback expression to the NEW one, not re-derive the old value inline.

## r3b — asserts on a local variable, unreachable by any fix

`n_indep = n_real_sources + (1 if sandbox_ok else 0)` then `assert
n_indep < 2`. The computation lives entirely inside the test; the engine's
sandbox-no-longer-counts fix cannot touch it. Superseded by the ported
engine fallback (R3/R3b comment block).

## r4 / r4b — bypasses the pipeline binding step

Both call `led.record_tool_result(primary=True)` directly and then expect
assign_source_class NOT to return PRIMARY/SECONDARY. In production the
retriever binds the gate verdict immediately after rejection
(tools/pipeline/retrieval.py record_gate_rejection call, R4/R4b binding),
after which the same bytes/URL are superseded and assign_source_class
returns INFERRED. A bare ledger has no way to know a future gate will
reject bytes it was honestly handed; demanding clairvoyance from
assign_source_class pins the wrong layer. The end-to-end property (gate-
rejected bytes never launder to PRIMARY through a real run) is enforced by
the retrieval binding plus tests elsewhere; these two unit-level repros
remain red by construction.

## Summary state of the four files after this sweep

- answer_correctness: 11 pass / 8 strict-xfail (was 9 failed)
- retrieval_relevance: 4 fail (r3/r3b/r4/r4b, argued above) / rest pass
- money_path: 4-5 fail (m1/m1b/m3/m5(/m6-doc), argued above) / rest pass
- synthesis_corroboration: addressed separately in this sweep (C1–C5)
