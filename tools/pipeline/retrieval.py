"""Retrieval — iterative, gated, fanned-out evidence acquisition (W1).

The live end-to-end run showed five sub-questions producing ONE fetch that
returned ONE irrelevant paper, accepted because it existed. This module
fixes the three defects the adversary named afterwards:

1. ITERATIVE RETRIEVAL. Fetching is a loop: query -> INSPECT what came
   back -> decide whether it answers the sub-question -> refine and
   re-query if not -> stop when evidence is sufficient or the budget is
   spent. The stopping rule is tools.loop_quality.InformationGainTerminator,
   reused verbatim — we do not invent a second stopping rule, and its
   decisions arrive with reasons.

2. RELEVANCE GATING AT INGESTION. Every fetched item is judged against
   the sub-question BEFORE it can enter the evidence set. Rejected items
   are recorded — with the reason — on the leaf outcome. A sub-question
   ending with zero admissible evidence is an HONEST NULL surfaced to the
   synthesizer, never silently dropped and never dressed up as a hit.

3. FAN-OUT WITH ENFORCED SOURCE INDEPENDENCE. Each round queries every
   selected source, and sufficiency counts DISTINCT independent sources —
   EvidenceRequirement.min_independent_sources finally means something:
   two results from one publisher/index corroborate nothing.

GATE RULE: nothing here lowers anything. The relevance gate only rejects;
the terminator only stops SPENDING budget; independence counting only
raises the bar for calling a leaf satisfied.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from tools.loop_quality import InformationGainTerminator

logger = logging.getLogger("callisto.retrieval")

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Legacy generic-call table (W1): registry name -> adapter method spec.
#: SUPERSEDED for the engine by tools.sources.query_builder.build_plan —
#: kept ONLY so a retriever constructed with an explicit generic_calls=
#: dict (older tests, ad-hoc scripts) behaves exactly as before. When
#: generic_calls is None the retriever authors queries per source with
#: build_plan, which covers every plannable adapter and honestly reports
#: the rest.
_DEFAULT_GENERIC_CALLS = {
    "openalex": ("works_search", ("term",), {"limit": 3}),
    "federalregister": ("search", (), {"query_term": "term", "limit": 3}),
    "clinicaltrials": ("search_studies", (), {"query_term": "term"}),
    "gdelt": ("doc_query", ("term",)),
}


# Words that carry no topical weight for relevance judging or query building.
_QUERY_STOPWORDS = {
    "what", "does", "the", "say", "about", "recent", "research", "have",
    "has", "how", "why", "whether", "and", "for", "with", "into", "from",
    "that", "this", "which", "are", "was", "were", "been", "their", "its",
    "can", "should", "would", "will", "did", "between", "across",
    # generic research-process vocabulary: carries no source-selection signal
    # and dilutes coverage scores (e.g. 'scholarly', 'paper', 'search')
    "scholarly", "scholar", "literature", "academic", "academics",
    "study", "studies", "papers", "search", "sources", "source",
}


def _prefix_hit(a: str, b: str) -> bool:
    return a == b or a.startswith(b) or b.startswith(a)


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower())
            if len(w) >= 3 and w not in _QUERY_STOPWORDS]


# ── Relevance gating ───────────────────────────────────────────────────────


@dataclass
class RejectedItem:
    """A fetch that the gate refused BEFORE ingestion, with its reason.
    Recorded on the leaf so a null answer is auditable, not invisible."""
    source_name: str
    url: str
    reason: str
    relevance_score: float
    content_sha256: str = ""


def extract_text(parsed: Any, depth: int = 0) -> str:
    """Flatten a parsed JSON response into judgeable text.

    Keeps strings only — titles, abstracts, names, dates — which is what
    relevance is actually judged on."""
    if depth > 6:
        return ""
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        return " ".join(extract_text(v, depth + 1)
                        for v in parsed.values() if v is not None)
    if isinstance(parsed, (list, tuple)):
        return " ".join(extract_text(v, depth + 1) for v in parsed[:50])
    return ""


class RelevanceGate:
    """Judge one fetched result against its sub-question before ingestion.

    Scored on coverage of the question's topical tokens by the result's
    textual content, prefix-matched so 'semiconductor' matches
    'semiconductors'. ``min_coverage`` is the admission threshold; a caller
    may raise it, and this module never lowers it.
    """

    def __init__(self, min_coverage: float = 0.25):
        if not 0.0 < min_coverage <= 1.0:
            raise ValueError("min_coverage must be in (0, 1]")
        self.min_coverage = min_coverage

    def judge(self, question_text: str, question_type: str,
              parsed: Any) -> tuple[bool, float, str]:
        """(admitted, coverage 0..1, reason)."""
        q_tokens = set(_tokens(question_text)) | set(_tokens(question_type))
        if not q_tokens:
            return False, 0.0, "question has no judgeable topical words"
        hay = set()
        for w in _WORD_RE.findall(extract_text(parsed).lower()):
            if len(w) >= 3:
                hay.add(w)
        matched = [t for t in q_tokens
                   if any(h == t or h.startswith(t) or t.startswith(h)
                          for h in hay)]
        coverage = len(matched) / len(q_tokens)
        if coverage < self.min_coverage:
            missed = sorted(q_tokens - set(matched))
            return False, coverage, (
                f"content covers {coverage:.0%} of the question's topical "
                f"words (need {self.min_coverage:.0%}); missing: "
                f"{', '.join(missed[:8])}")
        return True, coverage, (
            f"content covers {coverage:.0%} of the question's topical words")


# ── Source independence ────────────────────────────────────────────────────

# Sources that aggregate the SAME underlying corpus are not independent
# corroboration even when their APIs differ. The declaration lives with the
# adapters (tools.sources.base.INDEPENDENCE_FAMILIES) so the honest answer
# sits next to the specs that know each source's actual upstream; this
# mapping is derived from it, never re-declared.
from tools.sources.base import INDEPENDENCE_FAMILIES as _DECLARED_FAMILIES

_OVERLAP_FAMILIES = {
    family: set(members) for family, members in _DECLARED_FAMILIES.items()
}


def in_family(source_name: str, members) -> bool:
    """Is this source a member of an overlap family?

    Normalised: 'semantic_scholar', 'semanticscholar' and 'Semantic-Scholar'
    are the same source. Exposed so callers reuse ONE membership rule —
    tools/why.py previously reimplemented it without normalisation, so a
    family collapse silently failed to register and two dependent sources
    read as two independent voices.
    """
    n = re.sub(r"[^a-z0-9]", "", str(source_name).lower())
    return any(re.sub(r"[^a-z0-9]", "", str(m).lower()) == n for m in members)


def independence_key(source_name: str, base_url: str) -> str:
    """The unit that counts toward min_independent_sources: the publisher
    host, collapsed into declared overlap families."""
    host = re.sub(r"^https?://", "", base_url).split("/")[0]
    # Normalise before matching. The family list is keyed by adapter name,
    # so 'semantic_scholar' vs 'semanticscholar' silently fell through to the
    # host — making two sources that index the SAME literature count as two
    # INDEPENDENT voices, which inflates confidence. Naming drift must not be
    # able to manufacture independence.
    _norm = lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower())
    nname = _norm(source_name)
    for family, members in _OVERLAP_FAMILIES.items():
        if any(_norm(m) == nname for m in members):
            return family
    for family, members in _OVERLAP_FAMILIES.items():
        if source_name in members:
            return family
    return host or source_name


# ── Question-type translation (JOB 4) ──────────────────────────────────────

def translate_question_type(registry, question_text: str,
                            question_type: str) -> tuple[str, list[str]]:
    """Map free-text decomposition output onto the registry's own vocabulary.

    Returns (best_query_text, selected_source_names). The decomposer emits
    phrases like 'scholarly literature about semiconductor supply chains';
    adapters declare 'scholarly work search by title/author/topic'. Rather
    than hoping word overlap lands, we ask each adapter's own answer
    clauses which one covers the question best and adopt ITS phrasing for
    the selection call — selection then matches against vocabulary the
    registry itself wrote.

    Falls back to the raw inputs when nothing covers well, so behaviour
    degrades to today's selection rather than below it.
    """
    candidates = f"{question_type} {question_text}".strip()
    best_score = 0.0
    best_names: list[str] = []
    for d in registry.select_explained(candidates):
        if d.included and d.score > best_score:
            best_score = d.score
            best_names = [d.name]
        elif d.included and best_names and \
                d.score >= best_score * 0.9:
            best_names.append(d.name)
    if not best_names:
        return candidates, []
    # Adopt the winning adapters' own answer-clause wording, so the term
    # 'clinical trials' finds the ClinicalTrials adapter whose clause says
    # 'trial design/arms/endpoints ... search' — matched through the same
    # diagnostic-term rule selection already applies.
    adopted_terms: set[str] = set()
    for name in best_names:
        entry = registry.get(name)
        if entry is None:
            continue
        for clause in entry.spec.answers:
            adopted_terms.update(_tokens(clause))
    # Keep the question's own topical words too — they carry the subject.
    subject = set(_tokens(question_text)) | set(_tokens(question_type))
    translated = " ".join(sorted(subject | adopted_terms))
    return translated, best_names


# ── Query construction ─────────────────────────────────────────────────────


def build_query(question_text: str) -> str:
    """A search-engine-shaped query from the sub-question: topical tokens
    only, original order preserved."""
    keep = set(_tokens(question_text))
    out = []
    for w in _WORD_RE.findall(question_text.lower()):
        if w in keep:
            out.append(w)
            keep.discard(w)
    return " ".join(out) or question_text.strip()


def refine_query(previous_query: str, relevant_titles: list[str]) -> str:
    """Round-N query: previous terms plus distinctive tokens harvested from
    the relevant hits so far. Deterministic, no model call needed."""
    add: list[str] = []
    seen = set(_tokens(previous_query))
    for title in relevant_titles[:5]:
        for w in _tokens(title):
            if w not in seen:
                seen.add(w)
                add.append(w)
            if len(add) >= 4:
                break
        if len(add) >= 4:
            break
    return (previous_query + " " + " ".join(add)).strip()


# ── Expected information gain ranking ──────────────────────────────────────


@dataclass
class GainEstimate:
    """Whether one candidate fetch could, IF it succeeds, satisfy an UNMET
    declared requirement — and which one. This is the smallest viable
    expected-information-gain test: not a Bayesian VOI model, just the
    question 'could this call change the answer?' asked BEFORE spending
    the call. A fetch that cannot move any unmet requirement has zero
    possible effect on the leaf's conclusion and is skipped."""
    source_name: str
    #: requirement reasons this fetch could plausibly satisfy on success
    satisfiable: list[str] = field(default_factory=list)
    #: requirement reasons NO result from this source could ever satisfy
    unsatisfiable: list[str] = field(default_factory=list)
    #: sources of the same independence family already admitted — a second
    #: member adds corroboration from ZERO new voices
    duplicate_voice: bool = False

    @property
    def worth_the_call(self) -> bool:
        return bool(self.satisfiable) and not self.duplicate_voice


def estimate_gain(spec, requirements, independent_keys: set,
                  question_type: str = "") -> GainEstimate:
    """Rank one candidate source against the leaf's CURRENT unknowns.

    Currently unknown = EvidenceRequirement.unmet_reasons over what the
    trace holds so far. What evidence would reduce it: the spec's own
    `answers` clauses (the registry's vocabulary for 'what would settle
    this', same one tools.gaps uses). Which query is most likely to
    produce it: the planner already authors per-source queries; here we
    only decide WHETHER that query deserves to be issued.

    Rules (each conservative — skips only provably useless calls):
      - quant_required with no quantitative evidence yet: any real fetch
        could carry numbers -> potentially satisfies.
      - independent-source shortfall: a source whose independence_key is
        NOT already in the trace can add a NEW voice -> potentially
        satisfies. One already counted (same overlap family or host)
        cannot, no matter how relevant its content.
      - source-class shortfall: class is assigned AFTER ingest from
        provenance, so a successful fetch could raise it — never skipped
        on this ground.
      - a source whose declared `cannot_answer` covers the question AND
        whose answers clauses have no overlap with it cannot produce
        admissible evidence for THIS question at all.
    """
    from agp.research_program import SourceClassRank

    achieved = SourceClassRank.SECONDARY   # provisional: best case assumed
    n_indep = len(independent_keys)
    reasons = requirements.unmet_reasons(
        achieved, n_indep, produced_quant=True)  # success-case upper bound
    if not reasons:
        # Everything already met on the success-case bound: nothing this
        # call could add. (Callers normally stop before this point via
        # sufficiency; kept as belt-and-braces.)
        return GainEstimate(source_name=spec.name)

    key = independence_key(spec.name, getattr(spec, "base_url", ""))
    duplicate_voice = key in set(independent_keys)

    qt_tokens = set(_tokens(question_type))
    answers = [c for c in (getattr(spec, "answers", ()) or ())]
    ans_tokens: set = set()
    for clause in answers:
        ans_tokens |= set(_tokens(clause))
    cannot = " ".join(str(x) for x in
                      (getattr(spec, "cannot_answer", ()) or ())).lower()
    declared_cannot_only = (
        bool(cannot) and not ans_tokens
        and any(_prefix_hit(t, cw) or _prefix_hit(cw, t)
                for t in qt_tokens
                for cw in _tokens(cannot)))

    est = GainEstimate(source_name=spec.name,
                       duplicate_voice=duplicate_voice)
    if declared_cannot_only:
        est.unsatisfiable = list(reasons)
        return est
    # On the optimistic bound every remaining reason is addressable by a
    # fresh-voice fetch; a duplicate voice can still serve quant/source-
    # class needs but cannot serve an independence shortfall.
    indep_short = [r for r in reasons if "independent sources <" in r]
    if duplicate_voice and indep_short == reasons:
        est.unsatisfiable = list(reasons)
        return est
    est.satisfiable = list(reasons)
    return est


# ── The iterative retriever ────────────────────────────────────────────────


@dataclass
class RetrievalTrace:
    """Everything one leaf's acquisition did, for audit and tests."""
    question_id: str
    rounds: list[dict] = field(default_factory=list)
    admitted: list[Any] = field(default_factory=list)   # engine.FetchResult
    rejected: list[RejectedItem] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    #: sources the planner could not serve, with its honest reason
    skipped_sources: list[dict] = field(default_factory=list)
    independent_keys: set[str] = field(default_factory=set)
    #: sources skipped BEFORE fetching because no result they could return
    #: would satisfy an unmet declared requirement (expected-gain gate)
    gain_skipped: list[dict] = field(default_factory=list)
    stop_reason: str = ""

    @property
    def n_admitted(self) -> int:
        return len(self.admitted)


class IterativeRetriever:
    """Fetch-inspect-refine loop for one sub-question, across many sources.

    Injected like the rest of the pipeline: transport comes from the
    caller (fixtures in tests), the model seam is optional and used only
    for query authoring when provided. No socket is opened unless the
    caller's transport opens one.
    """

    def __init__(self, *, registry, ledger, transport=None,
                 gate: Optional[RelevanceGate] = None,
                 max_rounds: int = 3,
                 max_sources_per_leaf: int = 3,
                 max_fetches_per_round: int = 4,
                 generic_calls: Optional[dict] = None,
                 use_planner: bool = True,
                 adaptive_gain: bool = True,
                 source_order: Optional[Callable[[list], list]] = None):
        self.registry = registry
        self.ledger = ledger
        self.transport = transport
        self.gate = gate or RelevanceGate()
        self.max_rounds = max(1, int(max_rounds))
        self.max_sources_per_leaf = max(1, int(max_sources_per_leaf))
        self.max_fetches_per_round = max(1, int(max_fetches_per_round))
        #: None + use_planner=True -> per-source query authoring via
        #: tools.sources.query_builder.build_plan (W5). An explicit dict
        #: keeps the legacy W1 behaviour byte-for-byte.
        self.generic_calls = generic_calls
        self.use_planner = use_planner
        #: EXPECTED INFORMATION GAIN gating (this task): before a fetch is
        #: issued, ask whether its SUCCESS could satisfy an UNMET declared
        #: requirement. If it cannot move the leaf past the tier boundary
        #: the requirements define, the call is not worth making. Set False
        #: to recover the plan-then-fetch behaviour exactly (used by the
        #: golden-run comparison).
        self.adaptive_gain = adaptive_gain
        #: MEASUREMENT ONLY (JOB 1, stopping-rule study). Optional callback
        #: invoked after each retrieval round with the CUMULATIVE
        #: conclusion-relevant state: {"round", "indep_keys", "admitted",
        #: "rejected_n"}. Reads nothing the conclusion does not depend on;
        #: default None means exactly the pre-instrumentation behaviour.
        self.round_observer = None
        #: JOB 3 — optional stasis stop rule (tools.pipeline.stasis_stop).
        #: When set, the loop breaks after a round that changed neither the
        #: independent-key set nor the admitted-body set: every further
        #: fetch would hand the answer model an IDENTICAL evidence payload,
        #: so tier/stance/confidence provably cannot move. Opt-in; None is
        #: exactly the pre-change behaviour. The stop reason records
        #: "stasis:", distinct from "sufficient:" and from any null
        #: classification, so an honest null never reads as saturation.
        self.stasis_stop = None
        #: CROSS-RUN MEMORY seam (tools.pipeline.crossrun): an ORDER-ONLY
        #: re-ranking applied to each round's candidate specs BEFORE any
        #: fetch. The callable receives the routable spec list and MUST
        #: return a permutation of it (a stable partition that moves
        #: chronic-null sources last). It can never add, drop, or alter a
        #: source — memory informs order, nothing else. None = registry
        #: order, byte-for-byte the pre-change behaviour.
        self.source_order = source_order

    def retrieve(self, question, question_type: str,
                 min_independent: int) -> RetrievalTrace:
        from tools.pipeline.engine import _make_adapter, _sha
        from tools.sources.base import RestSource, SourceError

        legacy_calls = self.generic_calls
        if legacy_calls is None and not self.use_planner:
            # Explicit opt-out: behave as the W1 default table did.
            legacy_calls = _DEFAULT_GENERIC_CALLS
        trace = RetrievalTrace(question_id=question.question_id)

        translated, chosen = translate_question_type(
            self.registry, question.text, question_type)
        excluded: set[str] = set()
        unplannable: set[str] = set()
        query = build_query(question.text)
        relevant_titles: list[str] = []
        # Reuse loop_quality's terminator rather than inventing another
        # stopping rule: record a sufficiency estimate after each round and
        # let it decide, with reasons, whether another round can help.
        term = InformationGainTerminator(
            min_iterations=1, max_iterations=self.max_rounds,
            confidence_delta_threshold=0.05,
            stagnant_iterations_needed=2,
            subject=question.question_id)

        for rnd in range(1, self.max_rounds + 1):
            all_specs = self.registry.select(
                translated, max_tier=3, exclude=excluded)
            # Fan out across routable sources. With the planner (default)
            # every source the registry selected is routable — build_plan
            # either authors real queries or reports honestly why it
            # cannot. Under a legacy generic_calls table only listed
            # sources can contribute.
            if legacy_calls is not None:
                routable = [s for s in all_specs if s.name in legacy_calls]
            else:
                # Planner mode: a source the planner cannot serve must not
                # consume fan-out budget that a servable source could use.
                # Record the honest gap once, then keep only plannable
                # sources as round candidates.
                routable = []
                for s in all_specs:
                    if s.name not in unplannable:
                        from tools.sources import query_builder as _qb
                        plan = _qb.build_plan(s.name, question.text)
                        if plan.plannable and plan.queries:
                            routable.append(s)
                        else:
                            unplannable.add(s.name)
                            trace.skipped_sources.append(
                                {"name": s.name,
                                 "reason": (plan.reason or
                                            "no authored query")[:120]})
            if not routable:
                trace.stop_reason = (
                    "selected sources lack generic fetch routes")
                break
            # CROSS-RUN MEMORY: order-only re-rank before anything is
            # fetched. Applied defensively — a broken hint degrades to
            # registry order, it can never break retrieval.
            if self.source_order is not None:
                try:
                    ordered = self.source_order(list(routable))
                    if sorted(getattr(s, "name", "") for s in ordered) == \
                            sorted(getattr(s, "name", "") for s in routable):
                        routable = list(ordered)
                    else:
                        logger.warning("source_order returned a different "
                                       "candidate set; ignoring it")
                except Exception as e:  # noqa: BLE001 — memory must not
                    logger.warning("source_order failed: %s", e)  # alter run
            # ── EXPECTED INFORMATION GAIN gating ──────────────────────────
            # Before issuing round N+1, rank every candidate by whether its
            # SUCCESS could satisfy an UNMET declared requirement. Sources
            # that cannot change the answer (duplicate voice against a pure
            # independence shortfall; declared cannot-answer) are skipped —
            # the call is not worth making. Recorded in the trace so an
            # auditor can see WHY nothing more was fetched.
            if self.adaptive_gain and rnd > 1:
                reqs = getattr(question, "evidence_requirements", None)
                kept: list = []
                for s in routable:
                    est = estimate_gain(
                        s, reqs, trace.independent_keys,
                        question_type or question.text)
                    if est.worth_the_call:
                        kept.append(s)
                    else:
                        why = ("duplicate independent voice" if
                               est.duplicate_voice else
                               "declared cannot-answer / no addressable "
                               "requirement")
                        trace.gain_skipped.append(
                            {"round": rnd, "source": s.name,
                             "reason": why})
                        logger.info("gain-skip %s r%d: %s",
                                    s.name, rnd, why)
                if not kept:
                    trace.stop_reason = (
                        "no candidate fetch could satisfy any unmet "
                        "declared requirement — stopping before spend")
                    break
                specs = kept[:self.max_sources_per_leaf]
            else:
                specs = routable[:self.max_sources_per_leaf]
            trace.queries.append(query)
            round_admitted = 0
            round_detail = {"round": rnd, "query": query,
                            "sources": [], "admitted": 0}
            for spec in specs:
                # ── query authoring: planner (W5) or legacy table (W1) ──
                if legacy_calls is not None:
                    call = legacy_calls.get(spec.name)
                    if not call:
                        round_detail["sources"].append(
                            {"name": spec.name, "skipped": "no generic route"})
                        continue
                    method_name, pos_args, kw_args = call
                    kwargs = {k: (query if v == "term" else v)
                              for k, v in (kw_args or {}).items()}
                    args = tuple(query if a == "term" else a for a in pos_args)
                else:
                    from tools.sources import query_builder
                    plan = query_builder.build_plan(spec.name, question.text)
                    if not plan.plannable or not plan.queries:
                        reason = plan.reason or "no authored query possible"
                        logger.info("planner skipped %s: %s",
                                    spec.name, reason)
                        excluded.add(spec.name)
                        round_detail["sources"].append(
                            {"name": spec.name, "skipped": reason[:120]})
                        continue
                    pq = plan.queries[0]
                    method_name = pq.method
                    args, kwargs = tuple(pq.args), dict(pq.kwargs)
                try:
                    source = RestSource(spec, ledger=self.ledger,
                                        transport=self.transport)
                    adapter = _make_adapter(self.registry, spec.name, source)
                    fetched = getattr(adapter, method_name)(*args, **kwargs)
                    if source.last_record is None or \
                            source.last_record.status != 200:
                        raise SourceError(
                            f"{spec.name} returned "
                            f"{getattr(source.last_record, 'status', '?')}")
                    body = __import__("json").dumps(fetched, sort_keys=True)
                except Exception as e:  # noqa: BLE001 — a failed source skips
                    logger.info("source %s failed: %s", spec.name, e)
                    excluded.add(spec.name)
                    round_detail["sources"].append(
                        {"name": spec.name, "error": str(e)[:120]})
                    continue
                # GATE BEFORE INGESTION — the run's central fix.
                ok, cov, reason = self.gate.judge(
                    question.text, question_type, fetched)
                fr = _mk_fetch(spec.name, getattr(source.last_record,
                                                  "url", ""),
                               body, fetched, question.question_id, _sha)
                if not ok:
                    trace.rejected.append(RejectedItem(
                        source_name=spec.name,
                        url=fr.url, reason=reason,
                        relevance_score=round(cov, 3),
                        content_sha256=fr.content_sha256))
                    round_detail["sources"].append(
                        {"name": spec.name, "rejected": reason})
                    continue
                # Every result lands in the ledger exactly once as
                # primary bytes (RestSource already recorded the raw body;
                # this is the canonical sorted-JSON form the pipeline
                # carries forward, so it is registered too).
                self.ledger.record_tool_result(
                    f"{spec.name}_fetch", body, primary=True, urls=[fr.url])
                trace.admitted.append(fr)
                trace.independent_keys.add(
                    independence_key(spec.name, spec.base_url))
                relevant_titles.extend(_titles(fetched))
                round_admitted += 1
                round_detail["sources"].append(
                    {"name": spec.name, "admitted": True,
                     "relevance": round(cov, 3)})
            round_detail["admitted"] = round_admitted
            trace.rounds.append(round_detail)

            if self.round_observer is not None:
                # JOB 1 instrumentation: cumulative conclusion-relevant state.
                # The leaf's sealed (tier, confidence) depends exactly on the
                # best source class and count of these fetches plus the
                # independent-key set; stance depends on the same bodies.
                try:
                    self.round_observer({
                        "qid": question.question_id,
                        "round": rnd,
                        "indep_keys": sorted(trace.independent_keys),
                        "admitted": [
                            (f.source_name, f.content_sha256)
                            for f in trace.admitted],
                        "rejected_n": len(trace.rejected),
                    })
                except Exception as e:  # noqa: BLE001 — observation never
                    logger.warning("round_observer failed: %s", e)  # alters run

            sufficient = len(trace.independent_keys) >= min_independent
            dec = term.record(min(1.0, len(trace.independent_keys) /
                                  max(1, min_independent)))
            if self.stasis_stop is not None:
                # JOB 3: consult the stasis rule AFTER sufficiency — a
                # satisfied requirement still reports "sufficient:", never
                # "stasis:". Stasis fires only when this round changed
                # nothing AND the run is not already sufficient.
                if not sufficient and not dec.stop and \
                        self.stasis_stop.record(
                            rnd, trace.independent_keys,
                            [f.content_sha256 for f in trace.admitted]):
                    trace.stop_reason = (
                        f"stasis: round {rnd} changed neither independent "
                        f"sources nor admitted evidence; further rounds "
                        f"cannot alter tier/stance/confidence")
                    break
            if sufficient:
                trace.stop_reason = (
                    f"sufficient: {len(trace.independent_keys)} independent "
                    f"sources >= required {min_independent}")
                break
            if dec.stop:
                trace.stop_reason = f"terminator: {dec.reason}"
                break
            if not trace.admitted:
                # Nothing landed yet: try the OTHER selected sources next
                # round rather than re-querying the same failing ones.
                excluded.update(s.name for s in specs)
                continue
            query = refine_query(query, relevant_titles)

        trace.stop_reason = trace.stop_reason or "round budget exhausted"
        logger.info("retrieval %s: %s", question.question_id,
                    trace.stop_reason)
        return trace


def _mk_fetch(source_name, url, body, parsed, question_id, sha_fn):
    from tools.pipeline.engine import FetchResult
    return FetchResult(source_name=source_name, url=url,
                       content_sha256=sha_fn(body), body=body,
                       parsed=parsed, question_id=question_id)


def _titles(parsed: Any) -> list[str]:
    out: list[str] = []

    def walk(x, depth=0):
        if depth > 5:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("title", "display_name", "brief_title") and \
                        isinstance(v, str):
                    out.append(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x[:30]:
                walk(v, depth + 1)

    walk(parsed)
    return out
