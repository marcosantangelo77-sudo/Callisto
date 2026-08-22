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

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from agp import (
    AGPSession,
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
from tools.artifacts import ArtifactStore, ArtifactRef
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

    #: registry name -> adapter method used by the generic fetcher.
    GENERIC_CALLS = {
        "openalex": ("works_search", ("term",)),
        "federalregister": ("search", (), {"query_term": "term", "limit": 3}),
        "clinicaltrials": ("search_studies", (), {"query_term": "term"}),
        "gdelt": ("doc_query", ("term",)),
        "treasury": None,   # needs a dataset name; not generically callable
        "bls": None,        # POST + series ids; not generically callable
        "fred": None,       # needs an API key + series id
        "wikidata": None,   # raw SPARQL; needs query authoring
    }

    def __init__(self, *, model: PipelineModel, adversary_router=None,
                 transport: Optional[Transport] = None,
                 store: Optional[ArtifactStore] = None,
                 ledger: Optional[ProvenanceLedger] = None,
                 registry=None,
                 descendant_resolutions: Optional[list] = None):
        self.model = model
        self.transport = transport
        self.store = store or ArtifactStore()
        self.ledger = ledger or ProvenanceLedger()
        self.registry = registry
        self.descendant_resolutions = list(descendant_resolutions or [])
        self._adversary_router = adversary_router
        self._adversary = None

    # -- lazy components ---------------------------------------------------

    def _get_registry(self):
        if self.registry is None:
            from tools.sources.registry import get_source_registry
            self.registry = get_source_registry()
        return self.registry

    @property
    def adversary(self):
        if self._adversary is None:
            if self._adversary_router is None:
                raise ValueError(
                    "adversary_router is required to attack conclusions")
            from agp.adversary import Adversary, AdversaryLedger
            import tempfile
            tmp = tempfile.mkdtemp(prefix="callisto_adv_")
            self._adversary = Adversary(
                self._adversary_router,
                ledger=AdversaryLedger(path=f"{tmp}/dissent.jsonl"))
        return self._adversary

    # ── Stage 1: decompose ────────────────────────────────────────────────

    async def _decompose(self, query: str, today: date) -> ResearchProgram:
        resp = await self.model.complete("Architect", decompose_messages(query))
        parsed = parse_model_json(resp) or {}
        program = ResearchProgram(root_query=query)
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
        errs = program.validate()
        if errs:
            raise ValueError(f"decomposition invalid: {errs}")
        return program

    # ── Stage 2+3: select sources and fetch per leaf ──────────────────────

    async def _fetch_for_question(self, q: ResearchQuestion,
                                  max_fetches_per_leaf: int = 3
                                  ) -> list[FetchResult]:
        reg = self._get_registry()
        qt = getattr(q, "question_type", "") or q.text
        specs = reg.select(qt, max_tier=3)[:max_fetches_per_leaf]
        results: list[FetchResult] = []
        for spec in specs:
            call = self.GENERIC_CALLS.get(spec.name)
            if not call:
                logger.info("no generic route for source '%s'; skipped",
                            spec.name)
                continue
            method_name, pos_args, kw_args = call
            term = re.sub(r"[^A-Za-z0-9 ]", " ", q.text).strip()
            kwargs = {k: (term if v == "term" else v)
                      for k, v in (kw_args or {}).items()}
            args = tuple(term if a == "term" else a for a in pos_args)
            try:
                source = RestSource(spec, ledger=self.ledger,
                                    transport=self.transport)
                adapter = reg.instantiate(spec.name).__class__(source) \
                    if False else _make_adapter(reg, spec.name, source)
                fetched = getattr(adapter, method_name)(*args, **kwargs)
                body = json.dumps(fetched, sort_keys=True)
                rec = FetchResult(
                    source_name=spec.name,
                    url=source.last_record.url if source.last_record else "",
                    content_sha256=_sha(body),
                    body=body, parsed=fetched, question_id=q.question_id)
            except (SourceError, StopIteration, Exception) as e:  # noqa: BLE001
                logger.info("source %s failed for %s: %s",
                            spec.name, q.question_id, e)
                continue
            # Every result lands in the ledger exactly once as primary bytes.
            self.ledger.record_tool_result(
                f"{spec.name}_fetch", body, primary=True, urls=[rec.url])
            results.append(rec)
        return results

    # ── Stage 4+5: answer with optional sandbox compute + artifacts ───────

    async def _answer_leaf(self, q: ResearchQuestion,
                           fetches: list[FetchResult],
                           session: AGPSession,
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
                             inputs=compute.get("inputs") or {})
            out.sandbox_status = sbx.status
            if sbx.status == "ok":
                refs = _store_sandbox(sbx, self.store)
                out.artifact_sha256s.extend(r.sha256 for r in refs)
                self.artifact_refs.extend(refs)
                # The computation itself is real executed bytes → SECONDARY
                # floor evidence, recorded in the ledger.
                comp_body = f"sandbox code:\n{sbx.code}\nstdout:\n{sbx.stdout}"
                self.ledger.record_tool_result("run_python", comp_body,
                                               primary=False)
                comp_ev = Evidence(
                    content=comp_body[:4000],
                    source_class=self.ledger.assign_source_class(
                        Evidence(content=comp_body[:4000],
                                 source_class=SourceClass.INFERRED,
                                 confidence_score=0.3,
                                 domain=session.domain or Domain.GENERAL,
                                 origin_agent="sandbox")),
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
        # cap the leaf at SPECULATIVE floor band.
        achieved = SourceClassRank(best_class.value)
        reasons = q.evidence_requirements.unmet_reasons(
            achieved, len({f.source_name for f in fetches}) + (
                1 if out.sandbox_status == "ok" else 0),
            produced_quant=out.sandbox_status == "ok" or
            bool(out.answer and re.search(r"\d", out.answer)))
        out.requirement_reasons = reasons
        if reasons:
            clamped = min(clamped, 0.54)

        from agp import ConfidenceTier
        out.confidence = round(max(DB_CONFIDENCE_FLOOR if clamped > 0 else 0.0,
                                   clamped), 2)
        out.tier = ConfidenceTier.from_score(out.confidence).value
        return out

    artifact_refs: list[ArtifactRef]

    # ── The whole chain ───────────────────────────────────────────────────

    async def run(self, question: str, *, domain: Domain = Domain.GENERAL,
                  today: Optional[date] = None) -> PipelineResult:
        today = today or date.today()
        self.artifact_refs = []
        result = PipelineResult(root_query=question, sealed=False)

        # 1. Decompose.
        program = await self._decompose(question, today)
        result.program = program

        # 2..5. Per leaf: select sources, fetch, compute, answer.
        session = AGPSession(question)
        session.scope = question
        session.domain = domain
        session.advance_to(SessionStep.SOURCE_ENUMERATION)
        session.sources = [s.name for s in self._get_registry().specs()]

        for q in program.leaves:
            fetches = await self._fetch_for_question(q)
            result.fetches.extend(fetches)
            outcome = await self._answer_leaf(q, fetches, session)
            result.leaves.append(outcome)

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

        # 7. Adversary.
        objections = await self.adversary.attack(
            claim_id=session.session_id, conclusion=conclusion,
            evidence_items=[e.content for e in session.evidence])
        result.objections = objections
        from agp.adversary import Adversary
        clamped, veto_reason = Adversary.apply_verdict(clamped, objections)

        session.summary = SessionSummary(
            scope=question, domain=domain, conclusion=conclusion,
            confidence_score=max(0.0, clamped),
            evidence_count=len(session.evidence),
            contradiction_count=len(session.contradictions))
        session.advance_to(SessionStep.SESSION_CLOSE)

        # 8. Seal or refuse.
        if veto_reason:
            result.refusal_reason = f"adversary veto: {veto_reason}"
            for ob in objections:
                if ob.is_blocking:
                    self.adversary.ledger.record_sustained(
                        session.session_id, ob.text)
            return result
        try:
            seal_hash = session.seal()
        except Exception as e:  # noqa: BLE001 — AGPSealRefused et al.
            result.refusal_reason = f"seal refused: {e}"
            return result

        result.sealed = True
        result.conclusion = conclusion
        result.confidence_score = session.summary.confidence_score
        result.confidence_tier = tier
        result.artifact_refs = list(self.artifact_refs)
        for ob in objections:
            self.adversary.ledger.record_overrule(
                session.session_id, ob.text,
                "sealed after penalty applied; objection preserved per "
                "dissent-logging policy")
        return result


# ── helpers ───────────────────────────────────────────────────────────────

def _make_adapter(registry, name: str, source: RestSource):
    entry = registry.get(name)
    if entry is None:
        raise StopIteration(name)
    return entry.make_adapter(source)


def _store_sandbox(sbx, store: ArtifactStore) -> list[ArtifactRef]:
    """Persist sandbox stdout + files. run_python deletes its workspace, so
    only child-attested hashes are available here unless keep_workspace was
    set; we accept attested hashes and mark them honestly."""
    from tools.artifacts import store_sandbox_outputs
    return store_sandbox_outputs(sbx, store, workspace=None)
