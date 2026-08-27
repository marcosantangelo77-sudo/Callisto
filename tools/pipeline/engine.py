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

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
from agp.ensemble import SELF_REVIEW_CEILING
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

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class FetchResult:
    """One successful source call, fully traceable."""
    source_name: str
    url: str
    content_sha256: str
    body: str
    parsed: Any
    question_id: str
    #: ISO-8601 UTC timestamp of when the bytes were actually fetched.
    #: MEASUREMENT ONLY — nothing scores, clamps, or adjusts confidence on
    #: this; it exists so a sealed conclusion can state how old its
    #: evidence was when the conclusion was drawn. Checkpoint-resumed
    #: runs restore the ORIGINAL fetch time (not the resume time): the
    #: age reported is the true age of the evidence, not the run's.
    fetched_at: str = ""


def evidence_age_summary(fetches, *, now: Optional[datetime] = None,
                         ) -> dict:
    """Age (seconds) of each fetch relative to *now* (default: seal time).

    Returns {"n": N, "oldest_s": ..., "newest_s": ..., "median_s": ...}
    with None values when no fetch recorded a timestamp (legacy payloads),
    so an unknown age is reported as unknown, never as zero.
    """
    ref = now or datetime.now(timezone.utc)
    ages: list[float] = []
    for f in fetches:
        ts = getattr(f, "fetched_at", "") or ""
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        ages.append(max(0.0, (ref - t).total_seconds()))
    if not ages:
        return {"n": len(list(fetches)), "oldest_s": None,
                "newest_s": None, "median_s": None}
    ages.sort()
    n = len(ages)
    mid = ages[n // 2] if n % 2 else (ages[n // 2 - 1] + ages[n // 2]) / 2
    return {"n": len(list(fetches)), "n_timestamped": n,
            "oldest_s": ages[-1], "newest_s": ages[0],
            "median_s": mid}


@dataclass
class LeafOutcome:
    question_id: str
    text: str
    answer: str = ""
    confidence: float = 0.0
    #: ESTIMATE vs CEILING (agp.estimate). The model's raw belief and the
    #: provenance entitlement, carried separately. DIAGNOSTIC ONLY: nothing
    #: that bets, scores, or seals may read these — the authoritative number
    #: is `confidence`, which equals min(estimate, ceiling) exactly as before.
    confidence_estimate: float = 0.0
    confidence_ceiling: float = 0.0
    tier: str = "UNVERIFIED"
    #: AFFIRMS / DENIES / UNDETERMINED, DECLARED by the model — never inferred
    #: from prose. The scorer used to keyword-scan the conclusion for six
    #: English phrases and default to YES, so the SIGN of every forecast was
    #: decided by incidental wording (see test_redteam_direction_from_prose).
    stance: str = "UNDETERMINED"
    source_classes: list[str] = field(default_factory=list)
    n_sources: int = 0
    requirement_reasons: list[str] = field(default_factory=list)
    #: The THREE-WAY verdict every conclusion carries (tools.gaps.NullKind):
    #:   honest_null       searched competently, nothing found
    #:   retrieval_failure we failed to FETCH — never read as "nothing there"
    #:   unprovable        evidence obtained but cannot meet our declared bar
    #:   "" (empty) for a leaf with a normal answered conclusion.
    #: Classification ONLY: this field must never move a confidence score.
    gap_kind: str = ""
    gap_explanation: str = ""
    #: DECLARED answer-bearing signal (task 212 / defect R3). The answering
    #: model states structurally whether the evidence ANSWERS the leaf's
    #: question — a fourth axis, orthogonal to provenance tier (where the
    #: bytes came from) and to gap_kind (why we lack usable evidence). A
    #: VERIFIED 0.90 fetch of the wrong period is answers_question=False.
    #: Declared, never inferred from conclusion prose. Absence of usable
    #: answer may only refuse or lower; nothing here raises a score.
    answers_question: bool = True
    sandbox_status: Optional[str] = None
    artifact_sha256s: list[str] = field(default_factory=list)
    #: Full refs (leaf-local) so a checkpoint-resumed run can restore what
    #: the seal must cover — hashes alone cannot rebuild an ArtifactRef.
    artifact_refs: list[ArtifactRef] = field(default_factory=list)


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
    #: Parent stance, taken from the leaf that set the parent's confidence.
    #: UNDETERMINED is a real, reachable value and must map to p=0.5.
    stance: str = "UNDETERMINED"
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
    #: Per-leaf three-way verdicts (see LeafOutcome.gap_kind), surfaced at
    #: the top level so a sealed result states WHICH kind of null each thin
    #: leaf is. Classification only — never read by scoring.
    gap_kinds: dict = field(default_factory=dict)   # qid -> kind value
    #: Evidence age at seal time (seconds): oldest, newest, median, and the
    #: number of timestamped fetches the summary covers. None values mean
    #: "no timestamped evidence" — unknown is reported as unknown. Pure
    #: measurement: nothing scores or adjusts confidence on this.
    evidence_age: dict = field(default_factory=dict)

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
            "gap_kinds": dict(self.gap_kinds),
            "evidence_age": dict(self.evidence_age),
            # A20: full sha256 ids — a truncated 12-hex id cannot be
            # resolved back to the stored object, so citing it vouches for
            # nothing a human (or verifier) can check.
            "artifacts": [r.sha256 for r in self.artifact_refs],
            "objections": [getattr(o, "text", str(o)) for o in self.objections],
        }


# ── Transport staging for tests / offline use ─────────────────────────────

Transport = Callable[[str, dict], "tuple[int, str]"]


class _FetchRecorder:
    """Per-leaf capture of provenance writes from an off-loop retrieval.

    Parallel retrieval must not mutate the shared ledger from threads. Each
    leaf records its `record_tool_result` calls here; the caller replays
    them into the real ledger in leaf order, so the final ledger state is
    byte-identical to the serial run's (same keys, same per-key order,
    same first-wins url mapping). Any other attribute access fails loudly —
    retrieval should never need more than this one method.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.rejections: list[tuple] = []

    def record_tool_result(self, tool_name: str, content: str, *,
                           primary: bool = False, urls=None):
        self.calls.append((tool_name, content,
                           bool(primary), list(urls or ())))
        return None

    def record_gate_rejection(self, content: str,
                              urls=None) -> None:
        # R4/R4b (laundering-remainder): the gate's REJECT verdict must be
        # bound to the ledger too. Captured here and replayed in leaf order
        # by the engine so the real ledger sees rejections exactly where the
        # serial run wrote them.
        self.rejections.append((content, list(urls or ())))
        return None


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
                 checkpointer: Optional[ckpt.FileCheckpointer] = None,
                 crossrun_store=None):
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
        # Parallel-leaf machinery: compute-path serialization and artifact
        # refs collected during concurrent answers (extended onto
        # self.artifact_refs in leaf order at assembly).
        self._compute_lock = asyncio.Lock()
        self._pending_artifact_refs: list[ArtifactRef] = []
        #: CROSS-RUN MEMORY (tools.pipeline.crossrun). Optional; when set,
        #: each run ends by persisting a per-source outcome record and each
        #: run starts by loading the same QUESTION CLASS's records as an
        #: ORDER-ONLY hint over retrieval fan-out. See that module's gate
        #: rules: order and flags only — never confidence, never evidence.
        self.crossrun_store = crossrun_store
        self._crossrun_class = ""
        self._crossrun_view = None
        self._crossrun_traces: dict = {}

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

    def _fetch_leaf_sync(self, q: ResearchQuestion, question_type: str,
                         ledger) -> tuple[list[FetchResult], Any]:
        """Synchronous body of per-leaf retrieval (runs off the event loop).

        `ledger` is where THIS leaf's fetch records land. The parallel path
        passes a scratch ledger so threads never mutate shared state; the
        caller replays the records into the real ledger in leaf order.
        """
        from tools.pipeline.retrieval import IterativeRetriever

        reg = self._get_registry()
        qt = question_type or q.text
        # Query authoring is delegated to the W5 planner
        # (tools.sources.query_builder.build_plan): per-source, fully-formed
        # adapter calls covering 9 sources, honest gaps for the rest. This
        # replaced the 4-entry GENERIC_CALLS table whose mono-source fan-out
        # kept independence at 1 in the second live run.
        retriever = IterativeRetriever(
            registry=reg, ledger=ledger, transport=self.transport,
            source_order=(self._crossrun_view.order_specs
                          if self._crossrun_view is not None else None))
        trace = retriever.retrieve(
            q, qt, min_independent=q.evidence_requirements.min_independent_sources)
        return list(trace.admitted), trace

    async def _fetch_for_question(self, q: ResearchQuestion,
                                  question_type: str = "",
                                  ) -> tuple[list[FetchResult], Any]:
        """Iterative, gated, fanned-out retrieval for one leaf.

        Returns (fetches, RetrievalTrace). Every returned fetch passed the
        relevance gate; rejected items live on the trace with reasons. A
        trace with zero admitted items is an honest null, surfaced as such."""
        return await asyncio.to_thread(
            self._fetch_leaf_sync, q, question_type, self.ledger)


    # ── Stage 4+5: answer with optional sandbox compute + artifacts ───────

    async def _answer_leaf(self, q: ResearchQuestion,
                           fetches: list[FetchResult],
                           session: AGPSession,
                           trace: Any = None,
                           call_tag: str = "",
                           ) -> tuple[LeafOutcome, list[Evidence]]:
        """Answer one leaf. SIDE-EFFECT ISOLATED: returns the evidence items
        instead of appending them to the session, so concurrent leaf answers
        can be assembled in deterministic leaf order by the caller. (The
        legacy signature's session argument is retained for compatibility
        but is no longer mutated here.)"""
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
            out.source_classes.append(assigned.value)
            if _CLASS_RANK[assigned.value] > _CLASS_RANK[best_class.value]:
                best_class = assigned
        out.n_sources = len(evidence_items)

        # Model proposes an answer, possibly requesting computation first.
        resp = await self.model.complete(
            "Manager", answer_messages(q.text, [e.content for e in evidence_items]),
            _call_tag=call_tag or q.question_id)
        proposal = parse_model_json(resp) or {}

        compute = proposal.get("compute")
        if compute and isinstance(compute, dict) and compute.get("code"):
            # The compute path mutates shared stores (ledger, artifact
            # store). It is rare; serialize it so cross-leaf interleaving
            # cannot corrupt state, then continue.
            async with self._compute_lock:
                # keep_workspace=True so produced file BYTES reach the artifact
                # store — otherwise the child merely ATTESTS hashes and nothing
                # downstream can re-verify them (property 3: evidence you can
                # check). Workspace is destroyed right after sealing.
                sbx = await asyncio.to_thread(
                    run_python, str(compute["code"]),
                    inputs=compute.get("inputs") or {}, keep_workspace=True)
                out.sandbox_status = sbx.status
                if sbx.status == "ok":
                    refs = _store_sandbox(sbx, self.store)
                    _cleanup_workspace(sbx)
                    out.artifact_sha256s.extend(r.sha256 for r in refs)
                    out.artifact_refs.extend(refs)
                    self._pending_artifact_refs.extend(refs)
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
                    out.n_sources += 1
            # Re-ask for the final answer now that computation ran.
            resp = await self.model.complete(
                "Manager", answer_messages(
                    q.text, [e.content for e in evidence_items]),
                _call_tag=call_tag or q.question_id)
            proposal = parse_model_json(resp) or {}

        out.answer = str(proposal.get("answer", "")).strip()
        # DECLARED answer-bearing signal (task 212, defect R3). The model
        # states structurally whether the evidence ANSWERS the question —
        # distinct from provenance tier (where bytes came from) and from
        # gap_kind (why we have no usable evidence). Read AS DECLARED; the
        # conclusion prose is never parsed for meaning (forecast-sign
        # defect class). Absent field defaults to True so legacy models
        # are unchanged: this signal may only refuse or lower.
        out.answers_question = bool(proposal.get("answers_question", True))
        _st = str(proposal.get("stance", "")).strip().upper()
        # Unknown/absent stance is UNDETERMINED, never a lean. An unparseable
        # answer must not silently become a confident YES, which is exactly
        # what the old default-yes keyword scan did.
        out.stance = _st if _st in ("AFFIRMS", "DENIES") else "UNDETERMINED"
        proposed = float(proposal.get("proposed_confidence") or 0.0)

        # ESTIMATE vs CEILING (agp.estimate): the model's proposed_confidence
        # is its BELIEF; provenance and the requirement gate are ENTITLEMENT.
        # Carrying both keeps the belief visible for calibration while the
        # sealed number stays exactly what min() produced before — sealable()
        # IS the old collapsed value, by construction.
        from agp.estimate import EstimateCeiling
        ec = EstimateCeiling(
            estimate=min(1.0, max(0.0, proposed)),
            ceiling=MAX_CONFIDENCE_BY_SOURCE.get(best_class.value, 0.55))

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
            # A ceiling mechanism, applied through the type: only the ceiling
            # falls; the estimate rides through untouched.
            ec = ec.with_ceiling(min(ec.ceiling, 0.54))

        out.confidence_estimate = ec.estimate          # diagnostic, not acted on
        out.confidence_ceiling = ec.ceiling
        # min(estimate, ceiling) is exactly what sealable() computes; the
        # historical engine rounding (round-half) is preserved verbatim so the
        # sealed/stored/reported number cannot move. sealable()'s floor_conf
        # quantisation differs only on proposals that are not already 2dp —
        # see tests/test_estimate_wiring.py for the pinned equivalence.
        out.confidence = round(
            max(0.0, min(ec.estimate, ec.ceiling)), 2)
        out.tier = ConfidenceTier.from_score(out.confidence).value
        # Three-way gap classification (tools.gaps.NullKind). CLASSIFICATION
        # ONLY — this reads nothing that scores and moves no number.
        #   no fetches at all -> honest_null or retrieval_failure per THE
        #     single membership rule; an answer written on top of a
        #     retrieval failure still carries the retrieval_failure verdict,
        #     so "we could not look" can never read as "there is nothing".
        #   evidence obtained but requirements unmet -> unprovable: a
        #     deliberate decision about OUR OWN bar, not a fetch fault.
        from tools.gaps import NullKind, classify_null_kind
        if not fetches:
            kind, expl = classify_null_kind(trace)
            out.gap_kind, out.gap_explanation = kind, expl
        elif reasons:
            # Fetches were admitted and requirements are unmet -> unprovable
            # regardless of whether the model emitted prose; an empty answer
            # on partial evidence is still an unprovable verdict, never a
            # silent fall-through that dies as '(no gap verdict)'.
            out.gap_kind = NullKind.UNPROVABLE.value
            out.gap_explanation = (
                "evidence was obtained but does not meet this question's "
                "declared standard: " + "; ".join(reasons))
        return out, evidence_items

    # ── The whole chain ───────────────────────────────────────────────────

    async def run(self, question: str, *, domain: Domain = Domain.GENERAL,
                  today: Optional[date] = None) -> PipelineResult:
        """One pipeline run with cross-run memory wrapped around it.

        Start of run (when a crossrun_store is injected): load the records
        for this QUESTION CLASS and expose them as an ORDER-ONLY hint to
        retrieval. End of run: persist one structured record of what this
        run's sources actually did — admitted vs rejected vs errored, the
        per-leaf gap kinds, final stance and tier. Facts, not prose; never
        evidence, never confidence (see tools.pipeline.crossrun gate rules).
        """
        self._crossrun_class = ""
        self._crossrun_view = None
        self._crossrun_traces = {}
        if self.crossrun_store is not None:
            try:
                from tools.pipeline.crossrun import question_class_for, \
                    planning_view
                self._crossrun_class = question_class_for(question)
                self._crossrun_view = planning_view(
                    self.crossrun_store, self._crossrun_class)
            except Exception as e:  # noqa: BLE001 — memory load must not
                logger.warning("cross-run memory load failed: %s", e)
        result = await self._run_inner(question, domain=domain, today=today)
        if self.crossrun_store is not None:
            try:
                from tools.pipeline.crossrun import record_run
                self.crossrun_store.append(
                    record_run(result, self._crossrun_traces,
                               self._crossrun_class or "default", question))
                note = (self._crossrun_view.briefing()
                        if self._crossrun_view is not None else "")
                if note:
                    result.notes.append(note)
            except Exception as e:  # noqa: BLE001 — memory write must not
                logger.warning("cross-run memory write failed: %s", e)
        return result

    async def _run_inner(self, question: str, *,
                         domain: Domain = Domain.GENERAL,
                         today: Optional[date] = None) -> PipelineResult:
        from tools.pipeline.run_inner import run_inner
        return await run_inner(self, question, domain=domain, today=today)


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
                       question_id=rec["question_id"],
                       # Restore the ORIGINAL fetch time: a resumed run's
                       # reported evidence age must reflect when the bytes
                       # were actually fetched, not when they were replayed
                       # from the checkpoint.
                       fetched_at=rec.get("fetched_at", ""))


def _leaf_from_payload(d: dict) -> LeafOutcome:
    """Rebuild a LeafOutcome from its checkpointed asdict form."""
    d = dict(d)
    d["source_classes"] = _as_list_or_empty(d.get("source_classes"))
    d["requirement_reasons"] = _as_list_or_empty(d.get("requirement_reasons"))
    # Malformed (non-list) checkpoint fields must not raise during
    # hydration. A truly ABSENT field is the legacy form and falls back to
    # the dataclass default (empty list); a PRESENT non-list value
    # (None, int, dict, ...) is preserved verbatim so ordered assembly can
    # distinguish "legacy checkpoint without this field" from "checkpoint
    # with a malformed field" and fail closed on the latter.
    for k in ("artifact_sha256s", "artifact_refs"):
        if k in d and not isinstance(d[k], list):
            pass  # preserve the malformed value for strict assembly checks
    # Full refs may be absent on legacy checkpoints; the ordered-assembly
    # SHA cross-check then fails closed rather than sealing artifactlessly.
    return LeafOutcome(**d)


def _as_list_or_empty(v) -> list:
    return list(v) if isinstance(v, list) else []


def _make_adapter(registry, name: str, source: RestSource):
    entry = registry.get(name)
    if entry is None:
        raise StopIteration(name)
    return entry.make_adapter(source)


def verify_artifact_gate(store: ArtifactStore, refs) -> Optional[str]:
    """RED TEAM A6 — the seal gate over artifacts. Returns None when every
    cited artifact's bytes are in the store and match its hash; otherwise a
    refusal reason. This is the production caller of
    ArtifactStore.verify_artifacts, which previously had zero callers."""
    refs = list(refs or [])
    if not refs:
        return None
    report = store.verify_artifacts(refs)
    if report.get("ok"):
        return None
    bad = report.get("missing", []) + report.get("corrupt", [])
    example = (bad[0] or "?")[:16]
    return ("artifact verification failed before seal: "
            f"{len(bad)} missing/corrupt (e.g. {example}…)")


def _store_sandbox(sbx, store: ArtifactStore) -> list[ArtifactRef]:
    """Persist sandbox stdout + files. The engine runs run_python with
    keep_workspace=True, so produced file bytes are read and re-hashed by
    the store itself — the artifact chain is verifiable, not merely
    attested by the child."""
    from tools.artifacts import store_sandbox_outputs
    workspace = getattr(sbx, "workspace", None)
    return store_sandbox_outputs(sbx, store,
                                 workspace=Path(workspace) if workspace else None)


def _cleanup_workspace(sbx) -> None:
    """Destroy the preserved scratch dir once its bytes are sealed."""
    import shutil

    ws = getattr(sbx, "workspace", None)
    if ws:
        shutil.rmtree(ws, ignore_errors=True)
        sbx.workspace = None
