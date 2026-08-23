"""The engine — one question in, a sealed (or refused) conclusion out.

This is the P1 wiring job: every stage below was built separately and none
of them were connected. Each stage is deliberately thin — it adapts an
existing, tested component; the logic lives where it always did.

Stage map (component -> use here):
  decompose   agp.research_program.ResearchProgram + PipelineModel
  select      tools.sources.registry.SourceRegistry.select(question_type)
  fetch       registry.instantiate(name) over RestSource with an injectable
              transport; every body is recorded via
              ProvenanceLedger.record_tool_result(primary=True) by base.py
  compute     tools.sandbox.run_python when the model asks for arithmetic
  artifacts   tools.charts.store_chart / tools.artifacts.store_sandbox_outputs
  confidence  provenance-assigned source class -> agp.thresholds clamp,
              then the leaf's EvidenceRequirement gate, then the inheritance
              rule (tools.research_program.clamp_parent_confidence)
  adversary   agp.adversary.Adversary.attack + apply_verdict
  seal        agp.AGPSession.seal() — refuses on empty evidence or veto

ASYMMETRY: every confidence adjustment in this file is min(...) or minus.
The pipeline can only ever lower what the model proposes.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from agp import (
    AGPSession,
    ConfidenceTier,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)
from agp.provenance import ProvenanceLedger
from agp.research_program import (
    EvidenceRequirement,
    Horizon,
    QuestionKind,
    ResearchProgram,
    ResearchQuestion,
    SourceClassRank,
)
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE, DB_CONFIDENCE_FLOOR
from agp import ConfidenceTier
from tools.artifacts import ArtifactStore, ArtifactRef
from tools.pipeline import checkpoint as ckpt
from tools.pipeline.model import (
    PipelineModel,
    answer_messages,
    decompose_messages,
    parse_model_json,
)
from tools.research_program import ResolutionRecord, clamp_parent_confidence
from tools.sandbox import run_python
from tools.sources.base import RestSource, SourceError, SourceSpec

logger = logging.getLogger("callisto.pipeline")

# Map agp SourceClass values to an ordering (higher = stronger evidence).
_CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ── Result types ──────────────────────────────────────────────────────────

@dataclass
class FetchResult:
    """One successful source call, fully traceable."""
    source_name: str
    url: str
    content_sha256: str
    body: str
    parsed: Any
    question_id: str


@dataclass
class LeafOutcome:
    question_id: str
    text: str
    answer: str = ""
    confidence: float = 0.0
    tier: str = "UNVERIFIED"
    source_classes: list[str] = field(default_factory=list)
    n_sources: int = 0
    requirement_reasons: list[str] = field(default_factory=list)
    sandbox_status: Optional[str] = None
    artifact_sha256s: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    root_query: str
    sealed: bool
    refusal_reason: str = ""
    session: Optional[AGPSession] = None
    program: Optional[ResearchProgram] = None
    leaves: list[LeafOutcome] = field(default_factory=list)
    conclusion: str = ""
    confidence_score: float = 0.0
    confidence_tier: str = "UNVERIFIED"
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    objections: list = field(default_factory=list)
    fetches: list[FetchResult] = field(default_factory=list)
    #: recoverable problems handled during the run (e.g. a repaired
    #: decomposition). Surfaced so a run that needed help does not look
    #: identical to one that did not.
    notes: list[str] = field(default_factory=list)
    #: present only when a checkpointer was injected — records which stages
    #: were resumed vs fresh, so stale evidence is visible to the caller.
    trace: Optional[ckpt.RunTrace] = None

    def summary_dict(self) -> dict:
        return {
            "root_query": self.root_query,
            "sealed": self.sealed,
            "refusal_reason": self.refusal_reason,
            "conclusion": self.conclusion,
            "confidence": self.confidence_score,
            "tier": self.confidence_tier,
            "n_leaves": len(self.leaves),
            "n_fetches": len(self.fetches),
            "artifacts": [r.sha256[:12] for r in self.artifact_refs],
            "objections": [getattr(o, "text", str(o)) for o in self.objections],
        }


# ── Transport staging for tests / offline use ─────────────────────────────

Transport = Callable[[str, dict], "tuple[int, str]"]


def fixture_transport(routes: dict[str, str]) -> Transport:
    """Serve canned bodies by URL substring. Replaces the HTTP layer entirely
    (tools/sources/base), so no socket is ever opened — safe under the
    no-socket test guard."""
    calls: list[str] = []

    def transport(url: str, headers: dict) -> tuple[int, str]:
        calls.append(url)
        for pattern, body in routes.items():
            if pattern in url:
                return 200, body
        return 404, '{"error": "no fixture route"}'

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


class NoRoute(Exception):
    """No generic adapter call exists for this source; skipped, not failed."""


# ── The pipeline ──────────────────────────────────────────────────────────

class ResearchPipeline:
    """Question -> sealed, artifact-backed, calibrated conclusion.

    Injected dependencies (this is what keeps tests network-free and
    model-free — see BUILD brief and retrodiction harness Researcher seam):
      model      PipelineModel — production: RouterModel(ProviderRouter);
                 tests: ScriptedModel.
      adversary_router  backend for agp.adversary.Adversary (its own seam).
      transport  REST transport for all source fetches (tests: fixtures).
      store      ArtifactStore (tests pass a temp-dir store).
      ledger     ProvenanceLedger.
      registry   SourceRegistry (defaults to the real one).
      descendant_resolutions  optional ResolutionRecords feeding the
                 inheritance rule for the parent conclusion's ceiling.
    """

    def __init__(self, *, model: PipelineModel, adversary_router=None,
                 transport: Optional[Transport] = None,
                 store: Optional[ArtifactStore] = None,
                 ledger: Optional[ProvenanceLedger] = None,
                 registry=None,
                 descendant_resolutions: Optional[list] = None,
                 checkpointer: Optional[ckpt.FileCheckpointer] = None):
        self.model = model
        self.transport = transport
        self.store = store or ArtifactStore()
        self.ledger = ledger or ProvenanceLedger()
        self.registry = registry
        self.descendant_resolutions = list(descendant_resolutions or [])
        self._adversary_router = adversary_router
        self._adversary = None
        #: True when the adversary runs on the SAME model as the author
        #: (no adversary_router injected). Self-review is visible and capped.
        self._adversary_is_self_review = adversary_router is None
        # None = no checkpointing; behaviour identical to the pre-W3 run.
        self.checkpointer = checkpointer

    # -- lazy components ---------------------------------------------------

    def _get_registry(self):
        if self.registry is None:
            from tools.sources.registry import get_source_registry
            self.registry = get_source_registry()
        return self.registry

    @property
    def adversary(self):
        if self._adversary is None:
            from agp.adversary import Adversary, AdversaryLedger
            import tempfile
            tmp = tempfile.mkdtemp(prefix="callisto_adv_")
            # CONSTRUCTION ERGONOMICS (JOB 3): a live run used to die at
            # stage 6 — ~100 seconds in — because no adversary router was
            # wired. Defaulting to the SAME model as the author is safe by
            # construction: agp.ensemble marks same-model review as
            # self_review and caps it at SELF_REVIEW_CEILING, so this can
            # only ever subtract confidence, never inflate independence.
            router = self._adversary_router or self.model
            self._adversary = Adversary(
                router,
                ledger=AdversaryLedger(path=f"{tmp}/dissent.jsonl"))
            self._adversary_is_self_review = (
                self._adversary_router is None)
        return self._adversary

    # ── Stage 1: decompose ────────────────────────────────────────────────

    async def _decompose(self, query: str, today: date,
                         _repair: str = "") -> ResearchProgram:
        msgs = decompose_messages(query)
        if _repair:
            # Repair turn: hand the model its own validation errors rather than
            # failing the run. A model that omits a horizon has made a
            # recoverable mistake; killing the pipeline over it wastes the
            # whole decomposition.
            msgs = msgs + [{"role": "user", "content":
                            "Your previous decomposition was REJECTED:\n"
                            f"{_repair}\n"
                            "Return corrected JSON in the same shape. Fix only "
                            "the rejected items."}]
        resp = await self.model.complete("Architect", msgs)
        parsed = parse_model_json(resp) or {}
        program = ResearchProgram(root_query=query)
        self._question_types = {}
        for spec in (parsed.get("sub_questions") or [])[:5]:
            try:
                kind = QuestionKind(str(spec.get("kind", "descriptive")).lower())
            except ValueError:
                kind = QuestionKind.DESCRIPTIVE
            req = EvidenceRequirement(
                min_source_class=(SourceClassRank.PRIMARY
                                  if int(spec.get("min_source_tier") or 2) <= 1
                                  else SourceClassRank.SECONDARY),
                min_independent_sources=max(
                    1, int(spec.get("min_independent_sources") or 2)),
                quant_required=bool(spec.get("quant_required")),
            )
            horizon = None
            days = spec.get("horizon_days")
            if kind == QuestionKind.PREDICTIVE and days:
                horizon = Horizon(
                    claim_date=today,
                    resolve_date=date.fromordinal(
                        today.toordinal() + int(days)))
            program.questions.append(ResearchQuestion(
                text=str(spec.get("text", ""))[:500],
                kind=kind,
                priority=float(spec.get("priority") or 0.5),
                evidence_requirements=req,
                horizon=horizon,
            ))
            self._question_types[program.questions[-1].question_id] = \
                str(spec.get("question_type") or "")
        errs = program.validate()
        if errs:
            raise ValueError(f"decomposition invalid: {errs}")
        return program

    # ── Stage 2+3: select sources and fetch per leaf ──────────────────────

    async def _fetch_for_question(self, q: ResearchQuestion,
                                  question_type: str = "",
                                  ) -> tuple[list[FetchResult], Any]:
        """Iterative, gated, fanned-out retrieval for one leaf.

        Returns (fetches, RetrievalTrace). Every returned fetch passed the
        relevance gate; rejected items live on the trace with reasons. A
        trace with zero admitted items is an honest null, surfaced as such."""
        from tools.pipeline.retrieval import IterativeRetriever

        reg = self._get_registry()
        qt = question_type or q.text
        # Query authoring is delegated to the W5 planner
        # (tools.sources.query_builder.build_plan): per-source, fully-formed
        # adapter calls covering 9 sources, honest gaps for the rest. This
        # replaced the 4-entry GENERIC_CALLS table whose mono-source fan-out
        # kept independence at 1 in the second live run.
        retriever = IterativeRetriever(
            registry=reg, ledger=self.ledger, transport=self.transport)
        trace = retriever.retrieve(
            q, qt, min_independent=q.evidence_requirements.min_independent_sources)
        return list(trace.admitted), trace


    # ── Stage 4+5: answer with optional sandbox compute + artifacts ───────

    async def _answer_leaf(self, q: ResearchQuestion,
                           fetches: list[FetchResult],
                           session: AGPSession,
                           trace: Any = None,
                           ) -> LeafOutcome:
        out = LeafOutcome(question_id=q.question_id, text=q.text)
        evidence_items: list[Evidence] = []
        best_class = SourceClass.INFERRED
        for f in fetches:
            ev = Evidence(
                content=f.body[:4000], source_class=SourceClass.INFERRED,
                confidence_score=0.30, domain=session.domain or Domain.GENERAL,
                origin_agent="pipeline", source_name=f.source_name)
            assigned = self.ledger.assign_source_class(ev)
            ev.source_class = assigned
            ceiling = MAX_CONFIDENCE_BY_SOURCE.get(assigned.value, 0.55)
            ev.confidence_score = round(min(0.45, ceiling), 2) \
                if assigned != SourceClass.PRIMARY else round(ceiling, 2)
            evidence_items.append(ev)
            session.add_evidence(ev)
            out.source_classes.append(assigned.value)
            if _CLASS_RANK[assigned.value] > _CLASS_RANK[best_class.value]:
                best_class = assigned
        out.n_sources = len(evidence_items)

        # Model proposes an answer, possibly requesting computation first.
        resp = await self.model.complete(
            "Manager", answer_messages(q.text, [e.content for e in evidence_items]))
        proposal = parse_model_json(resp) or {}

        compute = proposal.get("compute")
        if compute and isinstance(compute, dict) and compute.get("code"):
            sbx = run_python(str(compute["code"]),
                             inputs=compute.get("inputs") or {},
                             keep_workspace=True)
            out.sandbox_status = sbx.status
            if sbx.status == "ok":
                refs = _store_sandbox(sbx, self.store)
                out.artifact_sha256s.extend(r.sha256 for r in refs)
                self.artifact_refs.extend(refs)
                # The computation itself is real executed output → recorded
                # in the ledger; provenance assigns its class.
                comp_body = f"sandbox code:\n{sbx.code}\nstdout:\n{sbx.stdout}"
                self.ledger.record_tool_result("run_python", comp_body,
                                               primary=False)
                comp_ev = Evidence(
                    content=comp_body[:4000],
                    source_class=SourceClass.INFERRED,
                    confidence_score=0.30,
                    domain=session.domain or Domain.GENERAL,
                    origin_agent="sandbox")
                comp_ev.source_class = self.ledger.assign_source_class(comp_ev)
                comp_ev.confidence_score = round(min(
                    0.45, MAX_CONFIDENCE_BY_SOURCE.get(
                        comp_ev.source_class.value, 0.55)), 2)
                evidence_items.append(comp_ev)
                session.add_evidence(comp_ev)
                out.n_sources += 1
            # Re-ask for the final answer now that computation ran.
            resp = await self.model.complete(
                "Manager", answer_messages(
                    q.text, [e.content for e in evidence_items]))
            proposal = parse_model_json(resp) or {}

        out.answer = str(proposal.get("answer", "")).strip()
        proposed = float(proposal.get("proposed_confidence") or 0.0)

        # Clamp to the provenance-assigned BEST class ceiling (only down).
        clamped = min(proposed,
                      MAX_CONFIDENCE_BY_SOURCE.get(best_class.value, 0.55))

        # Evidence-requirement gate (agp.research_program): unmet requirements
        # cap the leaf at SPECULATIVE floor band. Independent-source counting
        # now reflects ACTUAL source diversity (distinct hosts / declared
        # overlap families), not the number of API calls that returned.
        achieved = SourceClassRank(best_class.value)
        if trace is not None and trace.independent_keys:
            n_indep = len(trace.independent_keys)
        else:
            n_indep = len({f.source_name for f in fetches}) + (
                1 if out.sandbox_status == "ok" else 0)
        reasons = q.evidence_requirements.unmet_reasons(
            achieved, n_indep,
            produced_quant=out.sandbox_status == "ok" or
            bool(out.answer and re.search(r"\d", out.answer)))

        out.requirement_reasons = reasons
        if reasons:
            clamped = min(clamped, 0.54)

        out.confidence = round(max(0.0, clamped), 2)
        out.tier = ConfidenceTier.from_score(out.confidence).value
        return out

    # ── The whole chain ───────────────────────────────────────────────────

    async def run(self, question: str, *, domain: Domain = Domain.GENERAL,
                  today: Optional[date] = None) -> PipelineResult:
        today = today or date.today()
        self.artifact_refs = []
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
        session = AGPSession(question)
        session.scope = question
        session.domain = domain
        session.advance_to(SessionStep.ASSIGN_DOMAIN)
        session.advance_to(SessionStep.SOURCE_ENUMERATION)
        session.sources = [s["name"] for s in self._get_registry().specs()]

        for q in program.leaves:
            async def _fetch_payload(q=q) -> dict:
                fetches_q, trace_q = await self._fetch_for_question(
                    q, self._question_types.get(q.question_id) or "")
                # Store the relevance gate's VERDICTS, not just its admits.
                # A resume that replays only stored fetches silently skips
                # the gate — evidence the live run rejected would enter the
                # resumed run, and zero reported rejections would make the
                # resumed run look cleaner than it was. Restoring the whole
                # trace (rejects included) keeps rejection itself auditable.
                return {"fetches": [dataclasses.asdict(f)
                                    for f in fetches_q],
                        "rejections": [dataclasses.asdict(r)
                                       for r in trace_q.rejected],
                        "independent_keys": sorted(trace_q.independent_keys),
                        "queries": list(trace_q.queries),
                        "stop_reason": trace_q.stop_reason}
            if cp is not None:
                f_oc = await ckpt.run_stage(
                    cp, trace, "fetch_leaf", {"qid": q.question_id},
                    _fetch_payload,
                    claim_ids=[session.session_id])
                # Restore the fetched bytes into this run's ledger so
                # source-class assignment works identically on a resume.
                ck = cp.load_by_key(
                    trace.run,
                    ckpt.step_key(trace.run, "fetch_leaf",
                                  ckpt.hash_inputs({"qid": q.question_id})))
                if ck is not None:
                    ckpt.replay_ledger(self.ledger, [ck])
                fetches = [_fetch_from_payload(r)
                           for r in f_oc.payload["fetches"]]
                # Restore the FULL retrieval trace — admitted AND rejected —
                # whether this stage was fresh or served from the checkpoint.
                # The gate has already been applied to produce this payload;
                # restoring it verbatim is how a resumed run scores exactly
                # what the equivalent live run scored.
                trace_q = _trace_from_payload(q.question_id, f_oc.payload)
                rejected = trace_q.rejected
            else:
                fetches, trace_q = await self._fetch_for_question(
                    q, self._question_types.get(q.question_id) or "")
                rejected = trace_q.rejected
            result.fetches.extend(fetches)
            if rejected:
                result.notes.append(
                    f"leaf '{q.text[:60]}': {len(rejected)} fetch(s) "
                    "rejected at ingestion: " + "; ".join(
                        f"[{r.source_name}] {r.reason}" for r in rejected))

            async def _answer() -> dict:
                before = len(session.evidence)
                outcome = await self._answer_leaf(q, fetches, session,
                                                  trace=trace_q)
                # Persist what this leaf contributed to the session so a
                # resume can rehydrate session.evidence without re-running
                # the model — otherwise AGP would rightly refuse to seal a
                # zero-evidence session.
                return {"leaf": dataclasses.asdict(outcome),
                        "evidence": [dataclasses.asdict(e)
                                     for e in session.evidence[before:]]}
            if cp is not None:
                a_oc = await ckpt.run_stage(
                    cp, trace, "answer_leaf", {"qid": q.question_id}, _answer)
                outcome = _leaf_from_payload(a_oc.payload["leaf"])
                for e_rec in a_oc.payload.get("evidence") or []:
                    ev = Evidence(
                        content=e_rec["content"],
                        source_class=SourceClass(e_rec["source_class"]),
                        confidence_score=e_rec["confidence_score"],
                        domain=domain,
                        origin_agent=e_rec["origin_agent"],
                        source_name=e_rec["source_name"])
                    session.add_evidence(ev)
            else:
                outcome = await self._answer_leaf(q, fetches, session,
                                                  trace=trace_q)
            result.leaves.append(outcome)


        session.advance_to(SessionStep.PRIMARY_COLLECTION)
        session.advance_to(SessionStep.CONTRADICTION_CHECK)
        session.advance_to(SessionStep.SYNTHESIS)

        if not any(l.answer for l in result.leaves):
            result.refusal_reason = "every leaf came back unanswered"
            return result

        # 6. Assemble parent conclusion; confidence derived from provenance.
        answered = [l for l in result.leaves if l.answer]
        conclusion = f"{question}\n\n" + "\n".join(
            f"- [{l.tier} {l.confidence:.2f}] {l.text}: {l.answer}"
            for l in answered)
        best_leaf = max(answered, key=lambda l: l.confidence)
        proposed = best_leaf.confidence

        # Inheritance rule: zero/weak resolved descendants cap at SPECULATIVE.
        clamped, tier = clamp_parent_confidence(
            proposed, self.descendant_resolutions)

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
        try:
            seal_hash = session.seal()
        except Exception as e:  # noqa: BLE001 — AGPSealRefused et al.
            result.refusal_reason = f"seal refused: {e}"
            return result

        result.sealed = True
        result.session = session
        result.conclusion = conclusion
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


# ── helpers ───────────────────────────────────────────────────────────────

def _trace_from_payload(question_id: str, payload: dict):
    """Rebuild a RetrievalTrace from a checkpointed fetch_leaf payload.

    Restores the relevance gate's full verdict set — admitted fetches,
    rejected items with reasons, and the independence keys the live run
    computed — so a resumed run scores on exactly the evidence (and only
    the evidence) the live run admitted. Missing legacy fields degrade to
    empty, never to 'everything was admitted'.
    """
    from tools.pipeline.retrieval import RejectedItem, RetrievalTrace

    trace = RetrievalTrace(question_id=question_id)
    for r in payload.get("rejections") or []:
        trace.rejected.append(RejectedItem(
            source_name=r.get("source_name", ""),
            url=r.get("url", ""),
            reason=r.get("reason", ""),
            relevance_score=float(r.get("relevance_score") or 0.0),
            content_sha256=r.get("content_sha256", "")))
    trace.independent_keys = set(payload.get("independent_keys") or [])
    trace.queries = list(payload.get("queries") or [])
    trace.stop_reason = payload.get("stop_reason", "")
    return trace


def _fetch_from_payload(rec: dict) -> FetchResult:
    """Rebuild a FetchResult from its checkpointed asdict form."""
    rec = dict(rec)
    parsed = rec.get("parsed")
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (ValueError, TypeError):
            pass
    return FetchResult(source_name=rec["source_name"], url=rec["url"],
                       content_sha256=rec["content_sha256"],
                       body=rec["body"], parsed=parsed,
                       question_id=rec["question_id"])


def _leaf_from_payload(d: dict) -> LeafOutcome:
    """Rebuild a LeafOutcome from its checkpointed asdict form."""
    d = dict(d)
    d["source_classes"] = list(d.get("source_classes") or [])
    d["requirement_reasons"] = list(d.get("requirement_reasons") or [])
    d["artifact_sha256s"] = list(d.get("artifact_sha256s") or [])
    return LeafOutcome(**d)


def _make_adapter(registry, name: str, source: RestSource):
    entry = registry.get(name)
    if entry is None:
        raise StopIteration(name)
    return entry.make_adapter(source)


def _store_sandbox(sbx, store: ArtifactStore) -> list[ArtifactRef]:
    """Persist sandbox stdout + files as REAL bytes.

    run_python is called with keep_workspace=True, so the workspace survives
    and store_sandbox_outputs re-hashes every produced file into the store.
    The workspace is destroyed here, after sealing. Before this fix the
    deleted workspace forced child-attested-only refs (hashes pointing at
    bytes that no longer existed), so verify_artifacts failed ok=False on
    every sealed claim citing sandbox output.
    """
    from pathlib import Path

    from tools.artifacts import store_sandbox_outputs

    ws = getattr(sbx, "workspace", None)
    try:
        return store_sandbox_outputs(
            sbx, store,
            workspace=Path(ws) if ws else None)
    finally:
        if ws:
            shutil.rmtree(ws, ignore_errors=True)
