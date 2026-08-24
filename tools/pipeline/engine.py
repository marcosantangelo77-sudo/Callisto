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
    #: Set when a verified sandbox computation contradicts the declared
    #: answer/stance: the leaf refuses (answer emptied, stance UNDETERMINED,
    #: estimate zeroed) rather than sealing its own arithmetic's negation.
    #: (Redteam C5; restored after autosave snapshot 4a59aa1 silently
    #: reverted it — see findings/redteam_backlog_sweep.md group A.)
    reconciliation_failure: Optional[str] = None
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
            # RED TEAM C4: sources that ANSWERED (contributed evidence), not
            # merely asked. A consumer reading n_fetches alone cannot tell a
            # triangulated answer from one resting on a single source.
            "n_sources_answered": len({f.source_name for f in self.fetches}),
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

        sbx = None  # sandbox result, if a computation was requested
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
        _st = str(proposal.get("stance", "")).strip().upper()
        # Unknown/absent stance is UNDETERMINED, never a lean. An unparseable
        # answer must not silently become a confident YES, which is exactly
        # what the old default-yes keyword scan did.
        out.stance = _st if _st in ("AFFIRMS", "DENIES") else "UNDETERMINED"

        # ── Compute-output↔stance reconciliation (redteam C5) ──────────────
        # A sandbox run whose stdout contains exactly one bare boolean is a
        # VERIFIED comparison. It is binding on the leaf's direction: prose
        # that asserts its negation must not seal. Refusal — not correction,
        # not a lowered-but-sealed score. A reconciliation failure may only
        # LOWER confidence or refuse; it never raises either side.
        computed_bool = None
        if sbx is not None and getattr(sbx, "status", None) == "ok":
            computed_bool = _sole_bare_boolean(
                str(getattr(sbx, "stdout", "") or ""))
        reconciliation_failure = None
        if computed_bool is not None and out.stance != "UNDETERMINED":
            required = "AFFIRMS" if computed_bool else "DENIES"
            if out.stance != required:
                reconciliation_failure = (
                    f"sandbox printed {computed_bool} ({required}) but the "
                    f"answer asserted {out.stance}")
                out.answer = ""
                out.stance = "UNDETERMINED"

        proposed = float(proposal.get("proposed_confidence") or 0.0)
        if reconciliation_failure:
            # Refusal path: nothing may be sealed on a contradicted
            # computation, and no number may survive the contradiction.
            out.reconciliation_failure = reconciliation_failure
            out.gap_explanation = (
                "refused: the executed computation contradicts the proposed "
                "conclusion — " + reconciliation_failure)
            proposed = 0.0

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
            # No-trace fallback (resumed/legacy payload): count independence
            # units with THE SAME rule the live retriever used, not raw
            # source-name distinctness — openalex + semanticscholar are one
            # voice, and two spellings of one name must not read as two
            # sources (red team R3). Computation is not corroboration: a
            # sandbox success adds ZERO independent voices (R3b) — it can
            # satisfy produced_quant above, never min_independent_sources.
            from tools.pipeline.retrieval import independence_key
            n_indep = len({
                independence_key(f.source_name,
                                 getattr(f, "base_url", "")
                                 or f"https://{f.source_name}")
                for f in fetches})
        reasons = q.evidence_requirements.unmet_reasons(
            achieved, n_indep,
            produced_quant=_produced_quantitative(out.answer, sbx))

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
        elif reasons and out.answer:
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
                    # RED TEAM C4: round details feed the asked-vs-answered
                    # notes; a resume that drops them cannot reproduce the
                    # fresh run's observable output.
                    "rounds": list(trace_q.rounds),
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
            # RED TEAM C4 (answer correctness): a sealed answer must be able
            # to say WHICH sources were asked and did NOT contribute. Before
            # this note, a single-source answer looked identical to a
            # triangulated one: session.sources listed all 21 registry specs,
            # and source errors produced no trace anywhere in the result.
            # Additive information only — reads nothing that scores.
            asked_failed = []
            for rd in trace_q.rounds:
                for s_detail in (rd.get("sources") or []):
                    name = s_detail.get("name", "")
                    if not s_detail.get("admitted"):
                        asked_failed.append(
                            f"{name} ({'error: ' + s_detail['error'][:80]}"
                            if s_detail.get("error")
                            else f"{name} ({s_detail.get('skipped') or s_detail.get('rejected', 'not admitted')})")
            if asked_failed:
                answered_names = sorted({f.source_name for f in fetches_i})
                result.notes.append(
                    f"leaf '{q.text[:60]}': sources asked but NOT "
                    f"contributing evidence: {', '.join(asked_failed)}; "
                    f"answer rests on {answered_names or 'NO sources'}")
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
        self.artifact_refs.extend(self._pending_artifact_refs)


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
        provable = [l for l in result.leaves if l.answer and not l.gap_kind]
        if not provable:
            from collections import Counter
            counts = Counter(l.gap_kind or "(no gap verdict)" for l in result.leaves)
            breakdown = ", ".join(
                f"{kind} x{n}" for kind, n in sorted(counts.items()))
            result.refusal_reason = (
                f"no provable leaf: every leaf is gap-classified "
                f"({breakdown}) — nothing was established, so there is "
                f"nothing to seal")
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
    # RED TEAM C4: round details restore verbatim so the resumed run emits
    # the same asked-vs-answered notes the fresh run did. Legacy payloads
    # without them degrade to empty (no notes), never to fabricated detail.
    for rd in payload.get("rounds") or []:
        if isinstance(rd, dict):
            trace.rounds.append(rd)
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
    d["source_classes"] = list(d.get("source_classes") or [])
    d["requirement_reasons"] = list(d.get("requirement_reasons") or [])
    d["artifact_sha256s"] = list(d.get("artifact_sha256s") or [])
    return LeafOutcome(**d)


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


def _sole_bare_boolean(stdout: str) -> Optional[bool]:
    """Return the boolean iff stdout consists of EXACTLY one bare True/False
    line and nothing else. Anything richer — multiple lines, extra prose,
    numbers — is NOT treated as a verified comparison verdict; the
    reconciliation check stays silent rather than guess intent."""
    stripped = stdout.strip()
    if stripped == "True":
        return True
    if stripped == "False":
        return False
    return None


_YEAR_RE = re.compile(r"(?:^|[^\w.])(19|20)\d{2}(?:[^\w.]|$)")


def _prose_carries_quantity(answer: str) -> bool:
    """True iff the answer prose contains a number that is not purely a
    year token. A year (19xx/20xx) is a date reference, not quantitative
    evidence: 'in 2023 the rate was high' asserts no quantity. Redteam C5
    companion canary, promoted to a real gate — a digit alone no longer
    satisfies quant_required. Conservative: any non-year number counts
    (units/polarity are NOT interpreted here; this only decides whether
    the requirement is met at all)."""
    if not answer:
        return False
    cleaned = _YEAR_RE.sub(" ", answer)
    return bool(re.search(r"\d", cleaned))


def _produced_quantitative(answer: str, sbx) -> bool:
    """Quantitative-support test for the requirement gate.

    A successful sandbox run whose structured `result` is a NUMBER is real
    quantitative production (a boolean verdict is a comparison, not a
    quantity). Prose falls back to _prose_carries_quantity. The old rule —
    ANY successful sandbox run or ANY digit — counted a bare 'ok' status
    and year tokens as quantitative evidence."""
    if sbx is not None and getattr(sbx, "status", None) == "ok":
        rv = getattr(sbx, "return_value", None)
        if isinstance(rv, bool):
            pass  # comparison verdict, not a quantity
        elif isinstance(rv, (int, float)):
            return True
    return _prose_carries_quantity(answer)


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
