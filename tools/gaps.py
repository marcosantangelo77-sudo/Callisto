"""Evidence gaps — what a null answer is HIDING, stated structurally.

The second live run sealed at SPECULATIVE 0.34 and the adversary named the
reason in prose: "the null findings are an artifact of the retrieval method,
not the literature." That distinction — WE COULD NOT FIND IT vs IT DOES NOT
EXIST — is the most important thing a research system can tell a human, and
before this module it existed only as a low confidence score. A number
carries no instruction; a researcher cannot act on 0.34.

This module turns every thin-or-empty leaf into a structured EVIDENCE GAP:

  - what KIND of evidence would settle the claim
  - which KNOWN source would plausibly hold it (from the source specs,
    including each source's declared cannot_answer)
  - WHY it was not obtained — one of a fixed obstacle taxonomy
  - what the OWNER can do about it — add a key, wait out a rate limit,
    buy access, add query authoring, add an adapter, or accept that it
    is genuinely absent from the accessible literature

The central classification is HONEST NULL vs RETRIEVAL FAILURE:

  HONEST NULL        the system searched competently — real queries, to the
                     sources that should hold the evidence, got valid
                     responses, and judged them — and found nothing.
  RETRIEVAL FAILURE  the system never looked properly: no query was issued,
                     a source that should have been tried was never tried,
                     a fetch failed, or a key/rate limit/paywall blocked it.

Conflating these is how a research system quietly lies: a retrieval failure
dressed as a null reads as "the literature is silent" when the truth is
"we never asked."

GATE RULE: nothing here raises confidence, lowers a threshold, or invents
evidence. Gaps only ADD structure to a failure that already happened; the
classification can only make a null LESS reassuring (by revealing it was a
retrieval failure), never more.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional


# ── Obstacle taxonomy ──────────────────────────────────────────────────────

class Obstacle(str, Enum):
    NONE = "none"                        # searched competently; genuinely absent
    NO_QUERY_ISSUED = "no_query_issued"  # selection produced nothing routable
    NO_ADAPTER = "no_adapter"            # source selected, no fetch route
    NO_API_KEY = "no_api_key"            # source requires a key we don't have
    RATE_LIMITED = "rate_limited"        # blocked / throttled (429, 403 cooldown)
    PAYWALLED = "paywalled"              # source declares the full text is paywalled
    QUERY_FAILED = "query_failed"        # the fetch itself errored
    NOT_INDEXED = "not_indexed"          # source declares it cannot hold this kind


class OwnerAction(str, Enum):
    ADD_API_KEY = "add_api_key"
    WAIT_RATE_LIMIT = "wait_out_rate_limit"
    BUY_ACCESS = "buy_access"
    ADD_QUERY_AUTHORING = "add_query_authoring"
    ADD_ADAPTER = "add_adapter"
    RETRY = "retry_later"
    ACCEPT_UNKNOWABLE = "accept_unknowable"


class GapKind(str, Enum):
    HONEST_NULL = "honest_null"
    RETRIEVAL_FAILURE = "retrieval_failure"


_HTTP_STATUS_RE = re.compile(r"\b(4\d\d|5\d\d)\b")
_RATE_WORDS = ("429", "rate limit", "too many requests", "cooldown")
_KEY_WORDS = ("api key", "api_key", "unauthorized", "401", "forbidden")
_PAYWALL_WORDS = ("paywall", "paywalled", "subscription", "full text")


def _classify_error(message: str, spec: Any = None) -> Optional[Obstacle]:
    m = (message or "").lower()
    if any(w in m for w in _RATE_WORDS):
        return Obstacle.RATE_LIMITED
    # A source that DECLARES its texts are paywalled, returning an HTTP
    # error, is a paywall problem even when the status word reads generic.
    if spec is not None and any(
            w in " ".join(getattr(spec, "cannot_answer", ()) or ()).lower()
            for w in _PAYWALL_WORDS) and _HTTP_STATUS_RE.search(m):
        return Obstacle.PAYWALLED
    if any(w in m for w in _KEY_WORDS):
        return Obstacle.NO_API_KEY
    return Obstacle.QUERY_FAILED  # an exception occurred; fetch did not complete


def _missing_key(spec: Any) -> bool:
    env = getattr(spec, "key_env_var", "") or ""
    return bool(env) and not os.environ.get(env)


def _action_for(obstacle: Obstacle) -> OwnerAction:
    return {
        Obstacle.NONE: OwnerAction.ACCEPT_UNKNOWABLE,
        Obstacle.NO_QUERY_ISSUED: OwnerAction.ADD_QUERY_AUTHORING,
        Obstacle.NO_ADAPTER: OwnerAction.ADD_QUERY_AUTHORING,
        Obstacle.NO_API_KEY: OwnerAction.ADD_API_KEY,
        Obstacle.RATE_LIMITED: OwnerAction.WAIT_RATE_LIMIT,
        Obstacle.PAYWALLED: OwnerAction.BUY_ACCESS,
        Obstacle.QUERY_FAILED: OwnerAction.RETRY,
        Obstacle.NOT_INDEXED: OwnerAction.ACCEPT_UNKNOWABLE,
    }[obstacle]


# ── The structured gap ─────────────────────────────────────────────────────

@dataclass
class CandidateSource:
    """A known source that would plausibly hold the settling evidence."""
    name: str
    why_plausible: str            # grounded in the spec's own `answers` clauses
    cannot_answer: tuple = ()     # the spec's declared limits, verbatim
    tried: bool = False
    obstacle: Obstacle = Obstacle.NONE
    detail: str = ""


@dataclass
class EvidenceGap:
    question_id: str
    question_text: str
    kind: GapKind
    #: what kind of evidence WOULD settle this claim (registry vocabulary)
    evidence_needed: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    obstacle: Obstacle = Obstacle.NONE
    why_not_obtained: str = ""
    owner_action: OwnerAction = OwnerAction.ACCEPT_UNKNOWABLE
    queries_issued: list = field(default_factory=list)
    n_admitted: int = 0
    n_rejected: int = 0

    @property
    def is_honest_null(self) -> bool:
        return self.kind is GapKind.HONEST_NULL

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "question_text": self.question_text,
            "kind": self.kind.value,
            "evidence_needed": list(self.evidence_needed),
            "candidates": [
                {"name": c.name, "why_plausible": c.why_plausible,
                 "cannot_answer": list(c.cannot_answer), "tried": c.tried,
                 "obstacle": c.obstacle.value, "detail": c.detail}
                for c in self.candidates],
            "obstacle": self.obstacle.value,
            "why_not_obtained": self.why_not_obtained,
            "owner_action": self.owner_action.value,
            "queries_issued": list(self.queries_issued),
            "n_admitted": self.n_admitted,
            "n_rejected": self.n_rejected,
        }

    def statement(self) -> str:
        """The researcher-facing sentence. This is the deliverable."""
        head = ("HONEST NULL — searched competently and not found"
                if self.is_honest_null
                else "RETRIEVAL FAILURE — never looked properly")
        lines = [f"[{head}] {self.question_text.strip()}"]
        if self.evidence_needed:
            lines.append(
                "  Settled by: " + "; ".join(self.evidence_needed))
        for c in self.candidates:
            state = ("tried" if c.tried else "NOT tried")
            lines.append(f"  Would plausibly hold it: {c.name} ({state}) — "
                         f"{c.why_plausible}")
            if c.cannot_answer:
                lines.append(f"    but that source cannot answer: "
                             f"{'; '.join(str(x) for x in c.cannot_answer)}")
        if self.why_not_obtained:
            lines.append(f"  Why not obtained: {self.why_not_obtained}")
        lines.append(f"  Owner action: {self.owner_action.value}")
        return "\n".join(lines)


# ── Candidate sources: who SHOULD hold the settling evidence ───────────────

_STOP = {
    "what", "does", "the", "say", "about", "recent", "research", "have",
    "has", "how", "why", "whether", "and", "for", "with", "into", "from",
    "that", "this", "which", "are", "was", "were", "been", "their", "its",
    "can", "should", "would", "will", "did", "between", "across",
}


def _tokens(text: str):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _STOP}


def _prefix_hit(a: str, b: str) -> bool:
    return a == b or a.startswith(b) or b.startswith(a)


def candidate_sources(registry, question_text: str, question_type: str,
                      tried_names: Iterable[str],
                      selected_names: Iterable[str] = ()) -> list:
    """Rank every registered source by how well its own `answers` clauses
    cover the question, and keep the plausible ones. A source whose
    cannot_answer declarations cover the question's subject is kept too —
    flagged NOT_INDEXED — because 'source X would hold this except it
    declares it cannot' is exactly what an owner needs to read."""
    q_tokens = _tokens(question_text) | _tokens(question_type)
    tried = set(tried_names)
    selected = set(selected_names)
    scored = []
    for name in registry.names():
        adapter = registry.get(name)
        if adapter is None:
            continue
        spec = adapter.spec
        answers = list(getattr(spec, "answers", ()) or ())
        ans_tokens = set()
        for clause in answers:
            ans_tokens |= _tokens(clause)
        if not q_tokens:
            overlap = 0.0
        else:
            matched = sum(1 for t in q_tokens
                          if any(_prefix_hit(t, a) for a in ans_tokens))
            overlap = matched / len(q_tokens)
        # cannot_answer overlap: does the source DECLARE it can't hold this?
        cannot = " ".join(str(x) for x in
                          (getattr(spec, "cannot_answer", ()) or ())).lower()
        declared_cannot = any(_prefix_hit(t, cw) or _prefix_hit(cw, t)
                              for t in q_tokens
                              for cw in _tokens(cannot))
        if overlap <= 0.0 and not declared_cannot:
            continue
        scored.append((overlap, declared_cannot, name, spec, answers,
                       declared_cannot and overlap <= 0.0))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    out = []
    for overlap, _dc, name, spec, answers, only_cannot in scored:
        if only_cannot:
            why = "declares it cannot answer this kind of evidence"
            obstacle = Obstacle.NOT_INDEXED
        else:
            why = "covers " + "; ".join(answers[:2])
            obstacle = Obstacle.NONE
        out.append(CandidateSource(
            name=name, why_plausible=why,
            cannot_answer=tuple(getattr(spec, "cannot_answer", ()) or ()),
            tried=name in tried, obstacle=obstacle))
    return out


# ── The classifier ─────────────────────────────────────────────────────────

def classify_gap(registry, trace: Any, question, question_type: str = "",
                 generic_calls: Optional[dict] = None) -> EvidenceGap:
    """Turn one leaf's RetrievalTrace into a structured EvidenceGap.

    `trace` is tools.pipeline.retrieval.RetrievalTrace (duck-typed: needs
    rounds, rejected, queries, admitted, stop_reason, question_id).
    `question` needs .question_id, .text, and optionally
    .evidence_requirements. Nothing here mutates the trace.
    """
    generic_calls = generic_calls or {}
    qt = question_type or question.text
    queries = list(getattr(trace, "queries", []) or [])
    admitted = list(getattr(trace, "admitted", []) or [])
    rejected = list(getattr(trace, "rejected", []) or [])
    rounds = list(getattr(trace, "rounds", []) or [])
    stop_reason = getattr(trace, "stop_reason", "") or ""
    qtext = getattr(question, "text", "")

    tried_names: set = set()
    errors: list = []
    skipped: list = list(getattr(trace, "skipped_sources", []) or [])
    for rd in rounds:
        for s in rd.get("sources", []):
            if s.get("name") and not s.get("skipped"):
                # a source the planner SKIPPED was never touched — it must
                # not count as tried
                tried_names.add(s["name"])
            if "error" in s:
                errors.append((s.get("name", ""), s["error"]))
            if "skipped" in s and s.get("name") not in \
                    {x["name"] for x in skipped}:
                skipped.append({"name": s.get("name", ""),
                                "reason": s["skipped"]})

    # Which sources were SELECTED but never tried, and why?
    from tools.pipeline.retrieval import translate_question_type
    try:
        _translated, chosen = translate_question_type(
            registry, qtext, qt)
    except Exception:  # noqa: BLE001 — a broken registry degrades, not fails
        chosen = []

    gap = EvidenceGap(
        question_id=getattr(question, "question_id", ""),
        question_text=qtext,
        kind=GapKind.HONEST_NULL,          # revised below; default is the
        evidence_needed=[],                # STRONGER claim, only kept when
        candidates=[],                     # the evidence supports it
        obstacle=Obstacle.NONE,
        queries_issued=queries,
        n_admitted=len(admitted),
        n_rejected=len(rejected),
    )

    # Evidence needed: the selected sources' own answers clauses ARE the
    # registry's vocabulary for "what would settle this".
    needed: list = []
    for name in chosen:
        adapter = registry.get(name)
        if adapter is not None:
            needed.extend(list(getattr(adapter.spec, "answers", ()) or ()))
    gap.evidence_needed = needed or [f"evidence of type: {qt}"]

    # Candidates: who plausibly holds it (including untried sources).
    gap.candidates = candidate_sources(
        registry, qtext, qt, tried_names, selected_names=chosen)

    # ── RETRIEVAL FAILURE signals ──────────────────────────────────────────
    failure_reasons: list = []

    # 0. Fetches that FAILED — classified first, an observed failure beats
    #    an inferred one.
    for name, err in errors:
        adapter = registry.get(name)
        spec = adapter.spec if adapter is not None else None
        obs = _classify_error(err, spec)
        failure_reasons.append(f"{name} fetch failed: {err[:120]}")
        if gap.obstacle is Obstacle.NONE and obs is not Obstacle.NONE:
            gap.obstacle = obs
        for c in gap.candidates:
            if c.name == name:
                c.obstacle = obs
                c.detail = err[:160]

    # 1. A key was required and missing for a plausible holder of THIS
    #    question's evidence.
    keyless = []
    for c in gap.candidates:
        adapter = registry.get(c.name)
        if adapter is None or c.obstacle is Obstacle.NOT_INDEXED:
            continue
        spec = adapter.spec
        if _missing_key(spec):
            keyless.append(c.name)
            if not c.tried:
                c.obstacle = Obstacle.NO_API_KEY
                c.detail = f"{spec.key_env_var} not set"
    if keyless:
        failure_reasons.append(
            "API key missing for: " + ", ".join(keyless))
        gap.obstacle = gap.obstacle if gap.obstacle is not Obstacle.NONE \
            else Obstacle.NO_API_KEY

    # 1. No query was ever issued — nothing routable was selected.
    if not queries:
        if errors:
            failure_reasons.append(
                "no query was issued and sources errored during selection")
        else:
            failure_reasons.append(
                "no query was ever issued — source selection produced "
                "nothing routable, so the literature was never asked")
        if gap.obstacle is Obstacle.NONE:
            gap.obstacle = Obstacle.NO_QUERY_ISSUED

    # 2. A plausible source was never tried because the planner could not
    #    serve it (no authored query) — query authoring backlog — or, in
    #    legacy-call-table mode, because it has no fetch route.
    skipped_names = {x.get("name", "") for x in skipped}
    untried_skipped = [
        c for c in gap.candidates
        if not c.tried and c.name in skipped_names]
    untried_no_route = [
        c for c in gap.candidates
        if not c.tried and c.obstacle is not Obstacle.NOT_INDEXED
        and c.name in chosen and c.name not in (generic_calls or {})
        and generic_calls]
    if untried_no_route or untried_skipped:
        names = ", ".join(c.name for c in untried_no_route + untried_skipped)
        reasons = []
        if untried_no_route:
            reasons.append("no fetch route")
        if untried_skipped:
            detail = "; ".join(
                f"{x['name']}: {(x.get('reason') or '')[:60]}"
                for x in skipped if x["name"] in
                {c.name for c in untried_skipped})
            reasons.append(f"planner could not author a query ({detail})")
        failure_reasons.append(
            f"plausible source(s) never tried — {'; '.join(reasons)}: "
            f"{names}")
        if gap.obstacle is Obstacle.NONE:
            gap.obstacle = (Obstacle.NO_ADAPTER if untried_no_route
                            else Obstacle.NO_QUERY_ISSUED)

    # 3. A plausible source was never tried at all (not selected, not
    #    failed, not skipped) — selection never pointed at it.
    untried_plausible = [
        c for c in gap.candidates
        if not c.tried and c.obstacle is not Obstacle.NOT_INDEXED
        and c.name not in chosen and c.name not in skipped_names]
    if untried_plausible:
        names = ", ".join(c.name for c in untried_plausible[:4])
        failure_reasons.append(
            f"plausible source(s) never consulted: {names}")
        if gap.obstacle is Obstacle.NONE:
            gap.obstacle = Obstacle.NO_QUERY_ISSUED

    # 5. Fetches failed — already handled as signal 0 above.

    # 6. Queries were issued but the sources that returned responses were
    #    never among the question's plausible holders — hits came from
    #    string coincidence, not from where the evidence lives.
    if queries and gap.obstacle is Obstacle.NONE:
        relevant_tried = [c for c in gap.candidates
                          if c.tried
                          and c.obstacle is not Obstacle.NOT_INDEXED]
        if not rejected and not admitted and not relevant_tried \
                and len(tried_names) > 0 and chosen and \
                not set(chosen) & {c.name for c in gap.candidates}:
            failure_reasons.append(
                "queries ran only against sources that do not plausibly "
                "hold this kind of evidence")

    if failure_reasons:
        gap.kind = GapKind.RETRIEVAL_FAILURE
        gap.why_not_obtained = "; ".join(failure_reasons)
    else:
        # HONEST NULL: queries ran to real sources, responses came back,
        # and nothing admissible survived. Say what was searched.
        gap.kind = GapKind.HONEST_NULL
        parts = []
        if queries:
            parts.append(f"searched {len(queries)} query round(s) across "
                         f"{len(tried_names)} source(s)")
        if rejected:
            parts.append(f"{len(rejected)} result(s) judged and rejected "
                         f"as not answering the question")
        if admitted:
            parts.append(f"{len(admitted)} result(s) admitted but "
                         f"insufficient to settle the claim")
        parts.append(f"stop reason: {stop_reason}" if stop_reason
                     else "search exhausted")
        gap.why_not_obtained = "; ".join(parts) + \
            " — genuinely absent from the sources searched"

    gap.owner_action = _action_for(gap.obstacle)
    return gap


# ── Report over a whole run ────────────────────────────────────────────────

@dataclass
class GapReport:
    gaps: list = field(default_factory=list)

    @property
    def n_retrieval_failures(self) -> int:
        return sum(1 for g in self.gaps
                   if g.kind is GapKind.RETRIEVAL_FAILURE)

    @property
    def n_honest_nulls(self) -> int:
        return sum(1 for g in self.gaps if g.kind is GapKind.HONEST_NULL)

    def actions(self) -> list:
        """Deduplicated owner actions, most actionable first."""
        order = [OwnerAction.ADD_API_KEY, OwnerAction.WAIT_RATE_LIMIT,
                 OwnerAction.BUY_ACCESS, OwnerAction.ADD_QUERY_AUTHORING,
                 OwnerAction.ADD_ADAPTER, OwnerAction.RETRY,
                 OwnerAction.ACCEPT_UNKNOWABLE]
        seen, out = set(), []
        for a in order:
            for g in self.gaps:
                if g.owner_action is a and a not in seen:
                    seen.add(a)
                    out.append(a)
        return out

    def statement(self) -> str:
        lines = [f"EVIDENCE GAPS: {self.n_honest_nulls} honest null(s), "
                 f"{self.n_retrieval_failures} retrieval failure(s)"]
        for g in self.gaps:
            lines.append("")
            lines.append(g.statement())
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "n_honest_nulls": self.n_honest_nulls,
            "n_retrieval_failures": self.n_retrieval_failures,
            "actions": [a.value for a in self.actions()],
            "gaps": [g.to_dict() for g in self.gaps],
        }


def build_report(registry, traces_and_questions: list,
                 generic_calls: Optional[dict] = None) -> GapReport:
    """traces_and_questions: list of (trace, question[, question_type])."""
    report = GapReport()
    for item in traces_and_questions:
        trace, question = item[0], item[1]
        qtype = item[2] if len(item) > 2 else ""
        report.gaps.append(classify_gap(
            registry, trace, question, qtype, generic_calls))
    return report
