# Instance 4 findings — the epistemics (agp/, knowledge_wiki.py, hermes_memory.py, seal + calibration)

Session opened 2026-08-22 on branch audit/tier3-epistemics.
Method: START_HERE brief; AUDIT_MANDATE §2; ROADMAP treated as unverified prior, re-derived.
Peer findings read: origin/audit/tier2-gate:findings/instance3.md (in full).
In-scope change landed: **keyed HMAC seal in agp/__init__.py** (commit eb6151b) with
characterization tests tests/test_tier3_epi_seal.py + tests/test_tier3_epi_trust.py
(36 tests green incl. the pre-existing test_agp_seal.py suite — backward compat proven, not asserted).

---

## [VERIFIED] agp/__init__.py:354 (pre-change) — the seal WAS an unkeyed SHA-256, forgeable by anyone with DB write. ROADMAP C3 confirmed. FIXED in-scope.
Blast radius: SILENT → now requires the key
Evidence: pre-patch `seal()` computed `hashlib.sha256(canonical_payload)` and
`verify_seal()` recomputed the identical public digest — verification proved
only "the bytes were not edited by someone too lazy to re-hash". `memory.py:226`
stores sessions via INSERT OR REPLACE, so a DB-write attacker could swap a whole
row and recompute a self-consistent seal. Fix: `CALLISTO_SEAL_KEY` (hex) →
HMAC-SHA256; legacy unkeyed rows still verify; rotation via `CALLISTO_SEAL_KEY_OLD`;
constant-time compares. Tests pin: keyed seal ≠ public hash; wrong-key forgery fails;
legacy rows survive key rollout.
Falsifier: forge a seal that passes verify_seal with CALLISTO_SEAL_KEY set, without knowing the key.
For: DONE (this branch). Remaining: operator must generate and deploy the key; memory.py INSERT OR REPLACE is OUTSIDE my ownership — flag for the owner (row-swap still possible; it just no longer verifies).

## [VERIFIED] orchestrator.py:725-731,1106,1171,1768,1799 — VERIFIED tier is unreachable by construction. ROADMAP C2 confirmed.
Blast radius: SILENT (manufactures a tier taxonomy that flatters without constraining)
Evidence: `_best_source_class` ranks whatever evidence carries; the only
ceiling above 0.75 is PRIMARY (1.0); but NO code path constructs session
Evidence with SourceClass.PRIMARY — the collection prompts offer the model
only `"SECONDARY"`/`"INFERRED"` (orchestrator.py:1106, :1171), and the Claude
enhancement pass assigns `SourceClass.SECONDARY if cited else INFERRED`
(:1770, :1799). Pinned by test: `source_class=SourceClass.PRIMARY` appears
nowhere in orchestrator.py. Meanwhile `tools/edge_confidence.py:154` DOES mint
"PRIMARY" — for any edge where Pinnacle appears in a book list — so the one
PRIMARY producer is the betting path, not the research protocol. The 5-tier
ladder is a 3.5-tier ladder.
Falsifier: any sealed sessions row with confidence_tier='VERIFIED' (workstation: `SELECT COUNT(*) FROM sessions WHERE confidence_tier='VERIFIED'` — expect 0).
For: unowned (orchestrator is outside my edit scope; the fix is a design decision, see EARNED section)

## [VERIFIED] orchestrator.py:736-742 (`_response_cites_urls`) — the citation check is substring matching; any URL, fabricated included, buys +0.20 ceiling. ROADMAP C1 confirmed.
Blast radius: SILENT (self-reported inputs gating self-reported confidence — the exact anti-moat)
Evidence: `("http://" in lowered) or ("https://" in lowered)`. A response that
prints one invented URL upgrades INFERRED (0.55 ceiling) → SECONDARY (0.75).
Worse: orchestrator.py:1797-1801 — the non-JSON fallback path assigns the
response the FULL ceiling outright (`confidence_score=ceiling`) when cited, so
an unparseable response containing "http://" gets exactly 0.75 with zero
content checks. Pinned by tests (TestCitationCheckVacuity).
Falsifier: show any URL validation (fetch, HEAD request, dedup against search results actually returned) anywhere in the citation path.
For: unowned (orchestrator). Design answer below.

## [VERIFIED] knowledge_wiki.py:242-257 + hermes_memory.py:268-292 — the wiki/hermes trust escalator is real, and it lives at INGESTION. ROADMAP C3 confirmed, with a correction.
Blast radius: SILENT (yesterday's unverified self-report becomes today's prompt-context "prior knowledge")
Evidence chain, each link pinned by test:
1. hermes `record_learning` upserts `confidence=MAX(confidence, excluded.confidence)`
   (hermes_memory.py:273, :289) — a learning's confidence can never fall, so one
   optimistic write permanently contaminates the key.
2. wiki `_get_uncompiled_sources` admits hermes learnings at `confidence >= 0.5`
   (knowledge_wiki.py:244) with NO seal check, NO source check — the string
   "verify_seal"/"seal_hash" appears nowhere in knowledge_wiki.py (asserted in test).
3. `_create_article` sets article confidence = mean(source confidences)
   (:375) — two 0.75-ceiling SECONDARY items average to CORROBORATED. Averaging
   identical uncorroborated sources manufactures corroboration.
4. `autonomous.py:148-150` injects these articles back into prompts as
   PRIOR KNOWLEDGE. Cycle closed: self-report → MAX-ratchet → wiki → prompt → next self-report.
CORRECTION to the escalator narrative: the `_update_article` weighted merge is
NOT a one-way ratchet — my first hypothesis ("article tier cannot fall") was
FALSIFIED by arithmetic: 20 consecutive all-garbage compiles drag a 0.78 article
to ~0.37. The decay is glacial (5 garbage rounds: 0.78 → 0.686, still PROBABLE)
but nonzero. The escalator is at ingestion (steps 1-3), not the merge.
Falsifier: workstation query — any wiki article whose source_sessions contains a session whose seal fails verify_seal, or whose source_entries include a self_repair learning (ROADMAP §3.1's fake-learnings query joined through wiki_articles.source_entries).
For: me (wiki + hermes are in scope) — repair proposed below, not landed (needs the seal-key deployed first to be meaningful)

## [VERIFIED] orchestrator.py:982-1002 + scripts/sentinel.py:71-72 — the Sentinel vetoes nothing and protects a nonexistent file. ROADMAP C4 confirmed.
Blast radius: SILENT (governance theater)
Evidence: the Sentinel's ONLY call site in the orchestrator is
`_step_assign_domain` — a 32-token constrained-decode classifier mapping the
query string to one of five domains. It never sees evidence, never sees
conclusions, and its return value cannot block anything. And
`scripts/sentinel.py:72` lists `"agp.py"` in PROTECTED_FILES; the module is
`agp/__init__.py` (no `agp.py` exists — verified with ls), so the Sentinel's
auto-repair pass would patch AGP core while believing it protected. Both facts
re-derived independently of ROADMAP.
Falsifier: a git log of agp/ modified by scripts/sentinel.py's fixer; or any Sentinel code path returning a veto.
For: unowned (scripts/, orchestrator)

## [VERIFIED] agp/ is 453 lines carrying the protocol: the ratio is UNDER-SPECIFICATION, not elegance — but the 453 lines themselves are clean.
Blast radius: n/a (verdict)
Evidence: what the 453 lines actually enforce: sequential step advancement,
UNVERIFIED-evidence filtering with a majority-noise seal refusal, garbage-seal
refusal, and (now) keyed seals. What they do NOT enforce — because the
enforcement lives in orchestrator.py (2,848 lines, outside the protocol):
where source_class comes from (a prompt asking an LLM to label its own
evidence), whether citations are real (substring check), whether the session
did the 7 steps or performed step-theater (progress_events counts calls, not
substance). The protocol core is honest; the protocol is enforced at its
untrusted boundary by the model being governed. Q1 doc/code agreement inside
agp/ itself is good — the drift is all at the boundary.
Falsifier: a sealed session whose evidence was never fetched from anywhere (constructible today — the collection prompt can be satisfied from training data and still seals at SECONDARY if a URL string appears).
For: me (design answer below)

## Clean-bill items (explicitly permitted conclusions)
- **agp/__init__.py core mechanics are correct and should not change further**:
  step sequencing, filtered-evidence accounting, seal-refusal gates all behave
  as documented; the pre-existing test_agp_seal.py suite is genuine
  behavior-pinning (tamper, canonicalization, refusal) — the best test file I
  read in this tier.
- **agp/thresholds.py** is a true single source of truth for the values it
  holds; its one sin is documented honestly in its own docstring ("must also
  update DB CHECK"). tools/edge_confidence.py:26 re-hardcoding the ceilings
  ("must match orchestrator.py") remains a real duplication defect — outside my
  edit scope, flag for owner.

---

# THE CENTRAL QUESTION — what would make a tier EARNED AGAINST REALITY

The system's tiers today are functions of self-reports: source_class is
model-labeled, citations are substring checks, learnings ratchet their own
confidence, the wiki launders the result into priors. The repair is not better
clamping — it is changing what the inputs are allowed to be. Four mechanisms,
in order of leverage:

**1. Source class must be assigned by PROVENANCE, not by the model.**
The orchestrator already has the ground truth it needs: it is the component
that executed the tool calls. Evidence assembled from `web_search` results that
actually came back from the API is SECONDARY by construction; text the model
produced without a tool call is INFERRED; a fetched primary document is
PRIMARY. The label should be a side effect of the code path that produced the
content, never a field in the model's JSON response. This one change kills the
citation-substring check (no longer needed — cited-ness is structural) and
makes PRIMARY assignable to research sessions, un-freezing the VERIFIED tier
for real. Cost: ~50 lines in orchestrator.py. Nothing else in the protocol
changes.

**2. Calibration must be scored against settled outcomes — the loop that
exists in the schema but is never closed.**
Sports is the one domain where ground truth arrives in hours; that is the whole
point of the proving ground. Every sealed session whose conclusion is a
testable claim about a resolvable event should get a `resolution` row when the
event settles, and the tier should be a function of the Brier score of the
agent's OWN past calibration: an agent whose 0.75-confidence claims come true
~75% of the time has EARNED CORROBORATED; one whose 0.75s come true 55% of the
time gets clamped to 0.55 regardless of what it reports. This is
`_clamp_confidence` with the ceiling parameterized by measured reliability
instead of asserted provenance. The infrastructure half-exists (Brier is
computed in the gate; hermes learnings carry confidence); what is missing is
the join from sessions → resolved outcomes → per-confidence-band hit rates →
next session's ceiling. That join is maybe 150 lines and is the single highest-
leverage addition in this tier. It converts the moat from "we hash our
opinions" to "our confidence numbers are empirically audited against reality" —
which genuinely nothing importable does.

**3. The wiki must ingest only verified bytes and must never raise a tier.**
Route `_get_uncompiled_sources` through `AGPSession.verify_seal` (reject rows
that fail or predate sealing), tag every learning with its provenance class,
and cap article confidence at the MINIMUM of its sources' ceilings rather than
the mean (averaging manufactures corroboration; minimum preserves it).
Articles are retrieval aids, not evidence — their confidence should never
exceed the weakest thing that fed them.

**4. Give the Sentinel a real job or rename it.**
It already runs a local model at zero marginal cost. Feed it the conclusion +
the collected evidence (not just the query) and let its veto be the one thing
that can refuse a seal: "conclusion asserts X, evidence contains no X" →
AGPSealRefused. That is a real adversarial check, it is cheap, and it is the
only component in the system that does not grade its own homework (it is not
the model that wrote the conclusion).

With 1+2, the answer to "why can't this be downloaded?" stops being
"because the code is 453 tidy lines" and becomes "because the confidence
numbers are backed by a measured track record that only exists if you've been
running the loop" — which is a data moat, not a code moat. That is the version
of this system that is hard to replicate.

# Q6 — how I'd build it today
Unchanged in shape from what exists: session lifecycle + provenance-assigned
evidence + keyed seals + outcome-scored calibration. The 453-line core is the
right size; the missing 200 lines are provenance plumbing (orchestrator) and
the calibration join. I would NOT reach for an in-toto/DSSE envelope until the
inputs are real — an attestation envelope over self-reported bytes is a
notarized opinion.

# Q7 — retirement conditions
agp/: retired only if the system stops making stored, tiered claims. The wiki:
retire the compile loop if the calibration join (mechanism 2) shows article
priors do not improve session outcomes — it is maintenance cost with unproven
return. hermes MAX-ratchet: replace with decay regardless; nothing justifies
monotonic confidence.

# Workstation queries this tier owes the next pass
```sql
-- Is VERIFIED truly empty? (C2 falsifier)
SELECT confidence_tier, COUNT(*) FROM sessions GROUP BY 1;
-- Escalator contamination: wiki articles fed by self_repair learnings
SELECT w.topic, w.confidence, w.source_entries FROM wiki_articles w
WHERE EXISTS (SELECT 1 FROM hermes_learnings h
              WHERE w.source_entries LIKE '%' || h.key || '%'
                AND h.source='self_repair');
-- Seal integrity of the stored corpus (after deploying CALLISTO_SEAL_KEY, legacy rows verify unkeyed)
SELECT COUNT(*) FROM sessions WHERE seal_hash IS NULL;
```

— Instance 4, branch audit/tier3-epistemics. Commits: eb6151b (HMAC seal + tests).
