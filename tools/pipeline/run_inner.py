"""Ask-path inner pipeline run extracted from ResearchPipeline._run_inner.

``callisto ask`` drives ResearchPipeline.run -> _run_inner. The facade keeps
a thin wrapper plus the cross-run wrap in ``run()``. Helpers
(_fetch_from_payload, _trace_from_payload, _leaf_from_payload,
_FetchRecorder, verify_artifact_gate) stay on the engine module.

Do not import tools.autonomous. Do not arm live betting. Do not add live
to paper-signal. Completions stay HTTP; Hermes is the agent runtime.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import date
from typing import Any, Optional

from agp import (
    AGPSession,
    ConfidenceTier,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)
from agp.ensemble import SELF_REVIEW_CEILING
from agp.research_program import ResearchProgram, ResearchQuestion
from agp.thresholds import DB_CONFIDENCE_FLOOR
from tools.artifacts import ArtifactRef
from tools.pipeline import checkpoint as ckpt
from tools.research_program import clamp_parent_confidence

from tools.pipeline.engine import (
    FetchResult,
    PipelineResult,
    _FetchRecorder,
    _fetch_from_payload,
    _leaf_from_payload,
    _trace_from_payload,
    evidence_age_summary,
    verify_artifact_gate,
)

logger = logging.getLogger("callisto.pipeline")


async def run_inner(pipeline, question: str, *,
                    domain: Domain = Domain.GENERAL,
                    today: Optional[date] = None) -> PipelineResult:
    """Ask-path inner run. ``pipeline`` is the ``ResearchPipeline`` instance.

    Nested closures keep using ``self`` (decompose, retrieve, answer).
    The facade ``ResearchPipeline._run_inner`` is a thin wrapper; ``run()``
    stays on the facade so cross-run memory wrap is unchanged.
    """
    self = pipeline
    today = today or date.today()
    self.artifact_refs = []
    self._pending_artifact_refs = []
    result = PipelineResult(root_query=question, sealed=False)
    cp = self.checkpointer
    trace: Optional[ckpt.RunTrace] = None
    if cp is not None:
        trace = ckpt.RunTrace(run=ckpt.run_key(
            question,
            domain.value if hasattr(domain, "value") else str(domain),
            today.isoformat()))

    # 1. Decompose (checkpointed when a checkpointer is injected).
    async def _do_decompose() -> ResearchProgram:
        try:
            prog = await self._decompose(question, today)
        except ValueError as first:
            # One repair turn. The validator's message names exactly what
            # was wrong, so hand it back rather than losing the whole run
            # to a recoverable model mistake. A second failure raises.
            result.notes.append(
                f"decomposition repair attempted: {first}")
            prog = await self._decompose(question, today,
                                         _repair=str(first))
        return prog

    if cp is not None:
        async def _decompose_payload() -> dict:
            prog = await _do_decompose()
            return {"program": prog.to_dict(),
                    "question_types": self._question_types}
        oc = await ckpt.run_stage(cp, trace, "decompose",
                                  {"question": question,
                                   "today": today.isoformat()},
                                  _decompose_payload)
        program = ResearchProgram.from_dict(oc.payload["program"])
        self._question_types = {
            qid: qt for qid, qt in
            (oc.payload.get("question_types") or {}).items()}
    else:
        program = await _do_decompose()
    result.program = program

    # 2..5. Per leaf: select sources, fetch, compute, answer.
    #
    # PARALLEL-LEAF RESTRUCTURE (speed run 2026-08-23). The leaves of a
    # decomposition are independent: leaf k's retrieval and answer depend
    # on leaf k only. The old loop paid Σ(leaf work) serially — sync
    # fetches that blocked the event loop plus an awaited model call per
    # leaf. This costs a month on questions that need an answer this
    # week. The restructure keeps byte-identical outputs:
    #   Phase A — all retrievals concurrently off-loop; each writes a
    #             scratch recorder replayed into the ledger in leaf order.
    #   Phase B — all answers concurrently on-loop; evidence is returned
    #             per leaf and appended to the session in leaf order, so
    #             the conclusion and adversary see exactly what the serial
    #             run saw.
    session = AGPSession(question)
    session.scope = question
    session.domain = domain
    session.advance_to(SessionStep.ASSIGN_DOMAIN)
    session.advance_to(SessionStep.SOURCE_ENUMERATION)
    session.sources = [s["name"] for s in self._get_registry().specs()]

    leaves = list(program.leaves)
    n_leaves = len(leaves)

    def _fetch_payload_dict(fetches_q: list[FetchResult],
                            trace_q: Any) -> dict:
        # Store the relevance gate's VERDICTS, not just its admits.
        # A resume that replays only stored fetches silently skips
        # the gate — evidence the live run rejected would enter the
        # resumed run, and zero reported rejections would make the
        # resumed run look cleaner than it was. Restoring the whole
        # trace (rejects included) keeps rejection itself auditable.
        return {"fetches": [dataclasses.asdict(f) for f in fetches_q],
                "rejections": [dataclasses.asdict(r)
                               for r in trace_q.rejected],
                "independent_keys": sorted(trace_q.independent_keys),
                "queries": list(trace_q.queries),
                "stop_reason": trace_q.stop_reason}

    def _fetch_inputs(q: ResearchQuestion) -> dict:
        return {"qid": q.question_id}

    # ── Phase A: retrieval for every leaf ──────────────────────────────
    # Checkpoint hits are detected up front; only misses retrieve.
    fetch_hits: dict[int, ckpt.Checkpoint] = {}
    if cp is not None:
        for i, q in enumerate(leaves):
            hit = cp.load(trace.run, "fetch_leaf",
                          ckpt.hash_inputs(_fetch_inputs(q)))
            if hit is not None:
                fetch_hits[i] = hit

    fresh = [i for i in range(n_leaves) if i not in fetch_hits]

    # R1 fix (speed run 2026-08-23-164040): each completed retrieval is
    # checkpointed INSIDE the worker, immediately — a sibling leaf's
    # failure must not forfeit paid fetch work. Saves happen off the
    # shared trace (appended in leaf order during assembly below) so the
    # trace stays deterministic; cp.save itself is atomic (tempfile +
    # os.replace), so concurrent saves are safe. Serial contract
    # preserved: the serial engine saved each leaf's checkpoint right
    # after that leaf finished; this does exactly that, concurrently.
    _fetch_saved: dict[int, ckpt.StageOutcome] = {}
    async def _retrieve(i: int):
        q = leaves[i]
        rec = _FetchRecorder()
        fetches_i, trace_i = await asyncio.to_thread(
            self._fetch_leaf_sync, q,
            self._question_types.get(q.question_id) or "", rec)
        if cp is not None:
            saved = cp.save(
                trace.run, "fetch_leaf",
                ckpt.hash_inputs(_fetch_inputs(q)),
                _fetch_payload_dict(fetches_i, trace_i),
                claim_ids=[session.session_id])
            _fetch_saved[i] = ckpt.StageOutcome(
                stage="fetch_leaf", resumed=False,
                payload=_fetch_payload_dict(fetches_i, trace_i),
                produced_at=saved.produced_at)
        return i, fetches_i, trace_i, rec

    retrieved: dict[int, tuple] = {}
    if fresh:
        outcomes = await asyncio.gather(
            *[_retrieve(i) for i in fresh], return_exceptions=True)
        # Fail in LEAF ORDER — identical error selection to the serial
        # loop (first failing leaf decides the exception). Completed
        # leaves' checkpoints are already on disk (saved in _retrieve).
        for i, oc in zip(fresh, outcomes):
            if isinstance(oc, BaseException):
                raise oc
        for oc in outcomes:
            retrieved[oc[0]] = oc[1:]
        # Replay each leaf's provenance records into the real ledger in
        # leaf order → same keys, same per-key order, same first-wins
        # url mapping as the serial run produced.
        for i in fresh:
            for tool, body, primary, urls in retrieved[i][2].calls:
                self.ledger.record_tool_result(tool, body, primary=primary,
                                               urls=urls or None)
            # R4/R4b replay: gate rejections land on the real ledger in
            # leaf order, right after the leaf's own fetch records — the
            # position the serial loop wrote them.
            for content, urls in retrieved[i][2].rejections:
                self.ledger.record_gate_rejection(content, urls)

    fetches_by_leaf: list[list[FetchResult]] = []
    traces_by_leaf: list[Any] = []
    for i, q in enumerate(leaves):
        if i in fetch_hits:
            ck = fetch_hits[i]
            # Restore the fetched bytes into this run's ledger so
            # source-class assignment works identically on a resume.
            # The replay consumes the SAME admissible set seal_guard
            # will judge (run-scope + verified signature, one shared
            # predicate) — a record the guard cannot see must never
            # enter this ledger either (red-team D3). A signature that
            # fails is not replayed AT ALL.
            ck = cp.load_by_key(
                trace.run,
                ckpt.step_key(trace.run, "fetch_leaf",
                              ckpt.hash_inputs({"qid": q.question_id})))
            if ck is not None:
                admissible = ckpt.admissible_checkpoints(trace.run, [ck])
                if admissible:
                    ckpt.replay_ledger(self.ledger, admissible)
            trace.stages.append(ckpt.StageOutcome(
                stage="fetch_leaf", resumed=True, payload=ck.payload,
                produced_at=ck.produced_at))
            fetches_i = [_fetch_from_payload(r)
                         for r in ck.payload["fetches"]]
            # Restore the FULL retrieval trace — admitted AND rejected —
            # whether this stage was fresh or served from the checkpoint.
            # The gate has already been applied to produce this payload;
            # restoring it verbatim is how a resumed run scores exactly
            # what the equivalent live run scored.
            trace_q = _trace_from_payload(q.question_id, ck.payload)
        else:
            fetches_i, trace_q, _rec = retrieved[i]
            if i in _fetch_saved:
                # Already persisted inside _retrieve (R1 fix); just
                # record the outcome on the trace in leaf order.
                trace.stages.append(_fetch_saved[i])
        # Cross-run memory: keep what this leaf's retrieval actually
        # did (admitted/rejected/errored/skipped per source). Counts
        # only — the record builder never sees bodies or verdicts'
        # contents beyond tools.gaps' own classification. Recorded for
        # BOTH branches above (fresh parallel and checkpoint restore),
        # so a resumed run's records match a live run's.
        self._crossrun_traces[q.question_id] = trace_q
        fetches_by_leaf.append(fetches_i)
        traces_by_leaf.append(trace_q)
        result.fetches.extend(fetches_i)
        rejected = trace_q.rejected
        if rejected:
            result.notes.append(
                f"leaf '{q.text[:60]}': {len(rejected)} fetch(s) "
                "rejected at ingestion: " + "; ".join(
                    f"[{r.source_name}] {r.reason}" for r in rejected))

    # ── Phase B: answers for every leaf ────────────────────────────────
    # Answer-stage checkpoint hits skip the model exactly as before;
    # misses run concurrently and are assembled in leaf order below.
    answer_hits: dict[int, ckpt.Checkpoint] = {}
    if cp is not None:
        for i, q in enumerate(leaves):
            hit = cp.load(trace.run, "answer_leaf",
                          ckpt.hash_inputs({"qid": q.question_id}))
            if hit is not None:
                answer_hits[i] = hit

    async def _answer_fresh(i: int) -> dict:
        outcome_i, ev_items = await self._answer_leaf(
            leaves[i], fetches_by_leaf[i], session,
            trace=traces_by_leaf[i], call_tag=f"leaf{i}")
        return {"i": i,
                "leaf": dataclasses.asdict(outcome_i),
                "evidence": [dataclasses.asdict(e) for e in ev_items]}

    to_run = [i for i in range(n_leaves) if i not in answer_hits]
    payloads: dict[int, dict] = {}
    resumed_answer: dict[int, dict] = {}
    # R1 fix, answer stage: each completed answer is checkpointed INSIDE
    # the worker immediately (same reasoning as the fetch stage above).
    _answer_saved: dict[int, ckpt.StageOutcome] = {}
    async def _answer_fresh(i: int) -> dict:
        outcome_i, ev_items = await self._answer_leaf(
            leaves[i], fetches_by_leaf[i], session,
            trace=traces_by_leaf[i], call_tag=f"leaf{i}")
        payload = {"i": i,
                "leaf": dataclasses.asdict(outcome_i),
                "evidence": [dataclasses.asdict(e) for e in ev_items]}
        if cp is not None:
            saved = cp.save(
                trace.run, "answer_leaf",
                ckpt.hash_inputs({"qid": leaves[i].question_id}),
                payload, claim_ids=[session.session_id])
            _answer_saved[i] = ckpt.StageOutcome(
                stage="answer_leaf", resumed=False, payload=payload,
                produced_at=saved.produced_at)
        return payload

    if to_run:
        ans_outcomes = await asyncio.gather(
            *[_answer_fresh(i) for i in to_run], return_exceptions=True)
        for i, oc in zip(to_run, ans_outcomes):
            if isinstance(oc, BaseException):
                raise oc
        for i, oc in zip(to_run, ans_outcomes):
            payloads[i] = oc
    for i in range(n_leaves):
        if i in answer_hits:
            ck = answer_hits[i]
            resumed_answer[i] = ck.payload
            trace.stages.append(ckpt.StageOutcome(
                stage="answer_leaf", resumed=True, payload=ck.payload,
                produced_at=ck.produced_at))

    # ── Ordered assembly: identical observable state to the serial run ──
    # Deterministic ordered artifact-ref assembly (BOTH fresh and
    # resumed): full refs ride leaf-local through the answer checkpoint
    # payload and are validated against LeafOutcome.artifact_sha256s in
    # leaf order. Order and duplicates preserved; never derived from a
    # bare hash, never de-duplicated. A hash-only (legacy/malformed)
    # checkpoint cannot rebuild an ArtifactRef, so a leaf that declares
    # hashes without full refs FAILS CLOSED below: the run returns
    # unsealed with an honest reason instead of sealing artifactlessly.
    refs_assembled: list[ArtifactRef] = []
    refs_failed: Optional[str] = None

    def _assemble_leaf_refs(qid: str, outcome) -> None:
        nonlocal refs_failed
        if refs_failed is not None:
            return
        leaf_refs = []
        if not isinstance(outcome.artifact_refs, list):
            refs_failed = (
                f"artifact refs for leaf '{qid}' are malformed "
                f"(not a list: {outcome.artifact_refs!r}); "
                "refusing artifactless seal")
            return
        if not isinstance(outcome.artifact_sha256s, list):
            refs_failed = (
                f"artifact sha256s for leaf '{qid}' are malformed "
                f"(not a list: {outcome.artifact_sha256s!r}); "
                "refusing artifactless seal")
            return
        try:
            for rd in outcome.artifact_refs:
                # Accept only real ArtifactRef instances (fresh paths) or
                # dict payloads that rebuild cleanly via from_dict()
                # (resumed paths). Anything else — junk strings, malformed
                # dicts, bare hashes — fails closed here instead of
                # crashing the run or sealing artifactlessly.
                if isinstance(rd, ArtifactRef):
                    leaf_refs.append(rd)
                elif isinstance(rd, dict):
                    leaf_refs.append(ArtifactRef.from_dict(rd))
                else:
                    raise ValueError(
                        f"entry is neither an ArtifactRef nor a dict: "
                        f"{rd!r}")
        except (KeyError, TypeError, ValueError) as e:
            refs_failed = (
                f"artifact refs for leaf '{qid}' cannot be rebuilt from "
                f"checkpoint ({e}); refusing artifactless seal")
            return
        # Hydrate the public LeafOutcome too: resumed dict refs must be
        # visible as ArtifactRef instances everywhere, matching fresh runs.
        outcome.artifact_refs = list(leaf_refs)
        sha_seq = [r.sha256 for r in leaf_refs]
        if sha_seq != list(outcome.artifact_sha256s or []):
            refs_failed = (
                f"artifact refs for leaf '{qid}' do not match its "
                "declared artifact_sha256s; refusing artifactless seal")
            return
        refs_assembled.extend(leaf_refs)

    for i, q in enumerate(leaves):
        if i in resumed_answer:
            payload = resumed_answer[i]
            outcome = _leaf_from_payload(payload["leaf"])
            for e_rec in payload.get("evidence") or []:
                ev = Evidence(
                    content=e_rec["content"],
                    source_class=SourceClass(e_rec["source_class"]),
                    confidence_score=e_rec["confidence_score"],
                    domain=domain,
                    origin_agent=e_rec["origin_agent"],
                    source_name=e_rec["source_name"])
                session.add_evidence(ev)
        else:
            payload = payloads[i]
            outcome = _leaf_from_payload(payload["leaf"])
            for e_rec in payload["evidence"]:
                ev = Evidence(
                    content=e_rec["content"],
                    source_class=SourceClass(e_rec["source_class"]),
                    confidence_score=e_rec["confidence_score"],
                    domain=domain,
                    origin_agent=e_rec["origin_agent"],
                    source_name=e_rec["source_name"])
                session.add_evidence(ev)
            if i in _answer_saved:
                # Already persisted inside _answer_fresh (R1 fix); just
                # record the outcome on the trace in leaf order.
                trace.stages.append(_answer_saved[i])
        result.leaves.append(outcome)
        _assemble_leaf_refs(q.question_id, outcome)

    if refs_failed is not None:
        result.refusal_reason = refs_failed
        logger.warning("run %s: %s", question, refs_failed)
        return result

    self.artifact_refs.extend(refs_assembled)

    # The seal must cover exactly what the run cites: attach the
    # assembled refs to the session so to_dict()["artifact_refs"]
    # matches PipelineResult.artifact_refs and the keyed seal covers
    # them — for fresh AND resumed runs. Without this a resumed run
    # could seal with empty session refs while its leaves cite hashes.
    if self.artifact_refs:
        session.add_artifacts(self.artifact_refs)


    session.advance_to(SessionStep.PRIMARY_COLLECTION)
    session.advance_to(SessionStep.CONTRADICTION_CHECK)
    session.advance_to(SessionStep.SYNTHESIS)

    if not any(l.answer for l in result.leaves):
        # Name the STRUCTURED gap kinds when they exist: "unanswered"
        # alone hides whether we could not look, found nothing, or
        # could not meet our own bar.
        from collections import Counter
        counts = Counter(l.gap_kind or "(no gap verdict)" for l in result.leaves)
        breakdown = ", ".join(
            f"{kind} x{n}" for kind, n in sorted(counts.items()))
        result.refusal_reason = (
            f"every leaf came back unanswered ({breakdown})")
        return result

    # 6. Assemble parent conclusion; confidence derived from provenance.
    # Every thin leaf's verdict is IN the conclusion text — a user
    # reading the sealed output can tell an honest null from a
    # retrieval failure from an unprovable claim without opening a
    # debug field.
    answered = [l for l in result.leaves if l.answer]
    conclusion = f"{question}\n\n" + "\n".join(
        f"- [{l.tier} {l.confidence:.2f}] {l.text}: {l.answer}"
        + (f"  [GAP: {l.gap_kind}]" if l.gap_kind else "")
        for l in answered)
    for l in result.leaves:
        if l.gap_kind:
            result.gap_kinds[l.question_id] = l.gap_kind

    # ── SEAL CONTRACT: a seal is a claim that something was PROVEN. ────
    # A leaf carrying a gap_kind verdict (unprovable / honest_null /
    # retrieval_failure) proves NOTHING, regardless of whether a model
    # wrote words into its answer field. The parent may therefore stand
    # only on PROVABLE leaves: answered AND gap-free. This reads the
    # STRUCTURED gap_kind set during answering — never the conclusion
    # prose (parsing prose for meaning is the forecast-sign defect
    # class). Two consequences, deliberately:
    #
    #   ALL leaves gapped -> REFUSE, naming the gap kinds, so the caller
    #     learns WHICH kind of nothing it got instead of receiving a
    #     sealed non-answer indistinguishable from a sealed answer.
    #   MIXED (some provable, some gapped) -> SEAL, but standing only on
    #     the provable leaves and with the parent ceiling CAPPED AT
    #     SPECULATIVE: a parent with unanswered siblings is a weaker
    #     claim than one standing on five proven legs, and the cap says
    #     so numerically. Only ever refuses or LOWERS — never raises.
    #
    # ONE predicate (task 212 / defect R3): provable = the leaf carries
    # an answer AND declares answers_question AND is gap-free. The
    # declared answer-bearing signal lives on the SAME predicate rather
    # than in a competing seal rule — duplicated rules have drifted
    # every time in this repo. A VERIFIED 0.90 leaf that determines
    # nothing fails this predicate exactly as an unprovable one does:
    # provenance measures where bytes came from, not whether they
    # answer anything.
    provable = [l for l in result.leaves
                if l.answer and not l.gap_kind and l.answers_question]
    if not provable:
        from collections import Counter

        def _why_not_provable(l) -> str:
            if not l.answer:
                return "(no answer written)"
            if l.gap_kind:
                return l.gap_kind
            if not l.answers_question:
                return "non-answering"
            return "(no gap verdict)"

        counts = Counter(_why_not_provable(l) for l in result.leaves)
        breakdown = ", ".join(
            f"{kind} x{n}" for kind, n in sorted(counts.items()))
        result.refusal_reason = (
            f"no provable leaf: nothing was established ({breakdown}) — "
            f"there is nothing to seal")
        return result

    best_leaf = max(provable, key=lambda l: l.confidence)
    proposed = best_leaf.confidence
    # The parent's DIRECTION comes from the same leaf as its magnitude.
    parent_stance = best_leaf.stance

    # Inheritance rule: zero/weak resolved descendants cap at SPECULATIVE.
    clamped, tier = clamp_parent_confidence(
        proposed, self.descendant_resolutions)

    # Mixed decomposition: partial proof caps the parent at SPECULATIVE.
    # Applied AFTER the inheritance clamp so it can only subtract.
    if len(provable) < len(result.leaves):
        clamped = min(clamped, SELF_REVIEW_CEILING)
        tier = ConfidenceTier.from_score(max(0.0, clamped)).value
        result.notes.append(
            f"parent sealed on {len(provable)} of {len(result.leaves)} "
            f"leaves (rest gap-classified: "
            f"{sorted(result.gap_kinds.values())}); ceiling capped")

    # 7. Adversary. When no dedicated router was injected, the author's
    # own model attacks — recorded honestly as self-review in the notes.
    if self._adversary_is_self_review:
        result.notes.append(
            "adversary running in self-review mode: no separate "
            "adversary_router was wired; same-model review is capped "
            "(SELF_REVIEW_CEILING) and counts as zero independent "
            "reviewers")
    objections = await self.adversary.attack(
        claim_id=session.session_id, conclusion=conclusion,
        evidence_items=[e.content for e in session.evidence])
    result.objections = objections
    from agp.adversary import Adversary
    clamped, _ = Adversary.apply_verdict(clamped, objections)

    session.summary = SessionSummary(
        scope=question, domain=domain, conclusion=conclusion,
        confidence_score=max(0.0, clamped),
        evidence_count=len(session.evidence),
        contradiction_count=len(session.contradictions))
    session.advance_to(SessionStep.SESSION_CLOSE)

    # 8. Seal or refuse. Only a BLOCKING objection vetoes; MAJOR/MINOR
    # objections have already lowered the score via apply_verdict. A
    # penalty dropping below the DB floor means "refuse", not "store
    # something below the floor".
    result.session = session
    blocking = next((ob for ob in objections if ob.is_blocking), None)
    if blocking is not None:
        result.refusal_reason = f"adversary veto: {blocking.text}"
        self.adversary.ledger.record_sustained(
            session.session_id, blocking.text)
        return result
    if session.summary.confidence_score < DB_CONFIDENCE_FLOOR:
        result.refusal_reason = (
            f"confidence {session.summary.confidence_score} below DB "
            f"floor {DB_CONFIDENCE_FLOOR} after adversary penalties")
        for ob in objections:
            self.adversary.ledger.record_sustained(
                session.session_id, ob.text)
        return result
    if cp is not None:
        verdict, reason = ckpt.seal_guard(trace, cp.list_all(),
                                          self.ledger)
        if verdict == "REFUSE":
            result.refusal_reason = reason
            result.trace = trace
            return result
    # RED TEAM A6: verify_artifacts had zero production callers — the
    # system LOOKED checked while nothing was. Wire it at the one point
    # where a conclusion becomes sealed: every artifact the leaves cite
    # must have stored bytes matching its hash, or the seal is refused.
    # (Dead verification is worse than none; this makes the check real.)
    refusal = verify_artifact_gate(self.store, self.artifact_refs)
    if refusal is not None:
        result.refusal_reason = refusal
        logger.warning("seal refused for %s: %s",
                       session.session_id, refusal)
        return result
    try:
        seal_hash = session.seal()
    except Exception as e:  # noqa: BLE001 — AGPSealRefused et al.
        result.refusal_reason = f"seal refused: {e}"
        return result

    # Evidence age at seal time — MEASUREMENT ONLY. Recorded after the
    # seal succeeds; nothing here moves confidence in either direction.
    result.evidence_age = evidence_age_summary(result.fetches)

    result.sealed = True
    # State the age of the evidence IN the sealed conclusion text, so a
    # reader never has to open a debug field to learn how stale the
    # basis was. Unknown ages (legacy fetches) are stated as unknown.
    ea = result.evidence_age
    if ea.get("oldest_s") is None:
        conclusion += ("\n\n[evidence age: unknown — no timestamped "
                       "fetches]")
    else:
        conclusion += (
            f"\n\n[evidence age at seal: oldest {ea['oldest_s']:.0f}s, "
            f"newest {ea['newest_s']:.0f}s, median {ea['median_s']:.0f}s "
            f"across {ea['n']} timestamped fetches]")
    result.conclusion = conclusion
    result.session = session
    result.stance = parent_stance
    result.confidence_score = session.summary.confidence_score
    result.confidence_tier = tier
    result.artifact_refs = list(self.artifact_refs)
    result.trace = trace
    for ob in objections:
        self.adversary.ledger.record_overrule(
            session.session_id, ob.text,
            "sealed after penalty applied; objection preserved per "
            "dissent-logging policy")
    return result
