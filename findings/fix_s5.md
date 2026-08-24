# FIX S5 — vacuous claims formed a full-credit corroborating group

Branch: `fix/s5-vacuous-claims` (worktree `loop`). Tests:
`tests/test_redteam_s5_vacuous_claims.py` — 10 tests, all green after fix;
the 3 repros failed before it.

## The defect

`claim_key("") == ()`. Every evidence item whose claim was empty,
whitespace-only, punctuation-only, or stopword-only shared that one key, so
`triangulate` collapsed them into a single group — and three junk items from
three independent hosts read as three INDEPENDENT VOICES.
`confidence_from_agreement` scored that group 1.0 on PRIMARY provenance:

    conf: (1.0, ['ceiling 1.00 from provenance-assigned class PRIMARY',
                 '3 independent source(s) agree -> 100% of ceiling'])

Same family as S1/S2/F5: a structural property (same key / same count)
standing in for actual agreement. Claim text comes from the model or the
extractor; nothing anywhere rejected claims with no content words.

## The decision: refuse to GROUP, do not reject at extraction

The two options have different blast radii:

- **Reject at extraction** silently drops evidence. A vacuous claim usually
  means the extractor failed on real content (a table, a chart caption, a
  non-prose body). Deleting the item destroys its values, provenance, and
  stance — the extraction table loses rows, and nobody can later see that
  extraction came up empty for three independent sources (itself a signal).
- **Refuse to group** keeps every item visible and merely denies it the one
  thing it never earned: corroboration credit. Nothing is dropped; nothing
  can raise a score.

Chose refusal-to-group, with score explicitly held at 0 for vacuous groups.
Silent evidence loss is the failure mode this codebase keeps rediscovering
(S7's null conflation, C3's vacuous provenance); a visible zero-item is
diagnosable, a missing item is not.

## The change (`tools/pipeline/synthesis.py`)

1. New `has_content_words(claim)`: true iff any word token survives after
   lowercasing minus `_CLAIM_STOPWORDS`. Deliberately a WEAKER bar than
   `claim_key`: `claim_key` drops tokens shorter than 3 chars because they
   make poor grouping keys ("c" vs "C++"), but "AI", "US", or a ticker
   symbol is still content. Only claims made of NOTHING fail here.
2. `triangulate` never groups vacuous items under the empty key. Each is
   returned as its own single-item group (deterministic order, sorted by
   independence key), so reports still show the items and their provenance.
3. `confidence_from_agreement` returns `(0.0, [reason])` for a group whose
   claim has no content words — agreement over nothing is not evidence,
   regardless of how many voices repeat the nothing.

## Boundary verified (the edge, not just empty string)

| input to claim_key / has_content_words | key | content? |
|---|---|---|
| `""`, `"   "`, `"... — !!!"` | `()` | no |
| `"the of and"`, `"IS was BE"` | `()` | no |
| `"GDP fell"` | `('fell','gdp')` | yes |

- Two independent sources on "GDP fell": still 0.85 (unchanged).
- One junk item + one real claim: the junk item does NOT join the real
  group; real claim scores single-voice 0.70 (was 0.85 via contamination).
- Two sources on "inflation eased": still 0.85 (guard against over-broad fix).

## Verification

- `tests/test_redteam_s5_vacuous_claims.py`: 10 passed (3 were failing pre-fix).
- Full suite (minus pre-existing joblib collection errors in
  test_ml_classifier/test_ml_drift): 21 failures before, the SAME 21 after —
  no new failures, none masked. No confidence score anywhere was raised;
  the only scores touched go DOWN, to exactly 0.

## Same-family sweep: other places "same key" stands in for agreement

Reported, not fixed (outside my ownership):

1. **engine.py:410-413** — leaf confidence's evidence-requirement gate counts
   `len(trace.independent_keys)`: distinct keys are treated as achieved
   diversity without asking whether the keyed sources AGREED on anything, or
   whether each key contributed an ADMITTED relevant item (keys are added in
   retrieval.py:447 whenever a fetch succeeds, even if relevance later gates
   everything out — need to confirm ordering; if a key can be added by an
   errored-but-retried round, diversity is credited without content).
2. **why.py:350-370** (`independence_from_fetches`) — counts distinct
   independence keys over raw fetches as "independent sources"; same shape:
   N distinct keys presented as N agreeing voices. Display layer today
   (IndependenceWhy), so blast radius is explanatory text, not scoring — but
   it feeds whatever reasons strings land in reports.
3. **synthesis.py detect_contradictions stance branch** — `set(sup) &
   set(ref))` emptiness check treats key-set disjointness as "no two-faced
   source", already flagged as S4b; the S5 lesson generalises: key sets
   answer "how many distinct units spoke", never "did they say anything".

None of these manufacture corroboration across a *vacuous* claim the way S5
did (they credit diversity without checking agreement), so they are siblings,
not duplicates.
