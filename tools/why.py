"""WHY — explain a sealed (or refused) conclusion's confidence in plain language.

A live run sealed at SPECULATIVE 0.34. Nothing explains WHY 0.34. The number
is the product of provenance assignments, source-class ceilings, the evidence-
requirement gate, the inheritance rule, and adversary penalties — every one of
which is recorded somewhere, and none of which was assembled into an answer a
human can read. This module is that assembly.

HARD RULES (mirroring BUILD_MANDATE §4):
  - READ-ONLY. Nothing here writes to any component, mutates any object it
    is handed, or computes a new confidence. Every number reported is either
    read off the result or recomputed FROM THE SAME RULES the scorers used,
    purely for display. If a rule value drifts, this module reports the
    drifted world faithfully — it cannot make a score friendlier.
  - Domain-general: nothing here knows what a bet or a semiconductor is.

Usage:
    expl = explain_result(pipeline_result, ledger=pipeline.ledger)
    print(expl.narrative())        # plain language
    expl.to_dict()                 # machine-readable, attachable to a claim

Refused runs get the same treatment: the chain is walked up to the point of
refusal and the refusal itself is explained.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from agp.adversary import (
    DISAGREEMENT_CEILING,
    DISAGREEMENT_SPREAD_THRESHOLD,
    MILD_DISAGREEMENT_CEILING,
)
from agp.provenance import ProvenanceLedger
from agp.thresholds import (
    DB_CONFIDENCE_FLOOR,
    MAX_CONFIDENCE_BY_SOURCE,
    MAX_CONFIDENCE_NO_TOOL,
)
from tools.pipeline.engine import FetchResult, PipelineResult
from tools.pipeline.retrieval import _OVERLAP_FAMILIES, independence_key
from tools.research_program import (
    INHERITED_CEILING_BY_SOURCE,
    MIN_RESOLVED_FOR_LIFT,
    inherited_ceiling,
)

SCHEMA_VERSION = 1

_CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}

# Requirement-gate cap applied by tools/pipeline/engine.py when a leaf's
# evidence requirements are unmet (engine.py: min(clamped, 0.54)).
_REQUIREMENT_GATE_CAP = 0.54


def _sha12(text: str) -> str:
    return hashlib.sha256(
        (text or "").encode("utf-8", errors="replace")).hexdigest()[:12]


# ═════════════════════════════════════════════════════════════════════════
# Explanation records
# ═════════════════════════════════════════════════════════════════════════


@dataclass
class EvidenceWhy:
    """One evidence item: the class provenance assigned it, and WHY."""
    label: str                       # short identifier (source name / hash prefix)
    source_class: str
    reason: str                      # the provenance rule that fired
    ceiling: float                   # confidence ceiling implied by the class

    def to_dict(self) -> dict:
        return {"label": self.label, "source_class": self.source_class,
                "reason": self.reason, "ceiling": self.ceiling}


@dataclass
class CeilingWhy:
    """One constraint that bounded the score."""
    kind: str            # source_class | self_review | inheritance | requirement_gate | ensemble_spread | db_floor
    value: Optional[float]   # the numeric ceiling (None for vetoes/floors)
    detail: str          # plain-language why
    binding: bool = False

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value,
                "detail": self.detail, "binding": self.binding}


@dataclass
class ObjectionWhy:
    """One adversary objection and what it cost."""
    text: str
    kind: str
    severity: str
    penalty: float
    veto: bool
    status: str = ""

    def to_dict(self) -> dict:
        return {"text": self.text, "kind": self.kind, "severity": self.severity,
                "penalty": self.penalty, "veto": self.veto, "status": self.status}


@dataclass
class IndependenceWhy:
    """How many INDEPENDENT sources there were, and which families collapsed."""
    n_fetches: int
    independent_keys: list[str]
    n_independent: int
    collapses: list[str]     # human-readable family-collapse statements

    def to_dict(self) -> dict:
        return {"n_fetches": self.n_fetches,
                "independent_keys": self.independent_keys,
                "n_independent": self.n_independent,
                "collapses": self.collapses}


@dataclass
class RejectedWhy:
    """A fetch rejected at ingestion, and for what reason."""
    source_name: str
    url: str
    reason: str
    relevance_score: float = -1.0
    content_sha256: str = ""

    def to_dict(self) -> dict:
        return {"source_name": self.source_name, "url": self.url,
                "reason": self.reason, "relevance_score": self.relevance_score,
                "content_sha256": self.content_sha256}


@dataclass
class StepWhy:
    """One arithmetic step in the walk from proposal to final score."""
    stage: str
    before: float
    after: float
    rule: str

    @property
    def drop(self) -> float:
        return round(self.before - self.after, 4)

    def to_dict(self) -> dict:
        return {"stage": self.stage, "before": self.before,
                "after": self.after, "rule": self.rule, "drop": self.drop}


# ═════════════════════════════════════════════════════════════════════════
# Provenance: why did THIS item get THIS class?
# ═════════════════════════════════════════════════════════════════════════

_PRIMARY_RULE = ("exact bytes returned by a real tool call this session "
                 "(content-hash match on a primary observation)")
_OBSERVED_RULE = ("bytes matching something a tool returned this session "
                  "(hash match, non-primary observation)")
_CITED_RULE = ("cites a URL this session genuinely fetched "
               "(citation grounding)")
_INFERRED_RULE = ("no tool bytes or fetched URL back it — model output "
                  "without verification")


def assignment_reason(evidence_content: str,
                      ledger: Optional[ProvenanceLedger]) -> tuple[str, str]:
    """(source_class_value, reason) for one evidence item.

    Recomputes the assignment with the SAME ledger the pipeline used and
    names the specific rule that fires. With no ledger available, reports
    the class as recorded and says the deciding rule could not be replayed.
    """
    from agp import SourceClass
    if ledger is None:
        return "", ""      # caller falls back to the recorded class
    ev_probe = _probe(content=evidence_content)
    assigned = ledger.assign_source_class(ev_probe)
    if ledger.is_primary_bytes(evidence_content):
        reason = _PRIMARY_RULE
    elif ledger.has_observation(evidence_content):
        reason = _OBSERVED_RULE
    elif ledger.cites_verified_url(evidence_content):
        reason = _CITED_RULE
    else:
        reason = _INFERRED_RULE
    return assigned.value, reason


class _probe:
    """Minimal Evidence-shaped object for ledger queries."""

    def __init__(self, content: str):
        self.content = content
        self.source_class = None


# ═════════════════════════════════════════════════════════════════════════
# Ingestion rejections: from notes when traces are unavailable
# ═════════════════════════════════════════════════════════════════════════

_REJECT_NOTE_RE = re.compile(
    r"leaf '(?P<leaf>.{0,80}?)': (?P<n>\d+) fetch\(s\) rejected at ingestion: (?P<rest>.*)")
_REJECT_ITEM_RE = re.compile(r"\[(?P<src>[^\]]+)\] (?P<reason>[^;]+)")


def _parse_rejections(notes: Iterable[str]) -> list[RejectedWhy]:
    out: list[RejectedWhy] = []
    for note in notes or ():
        m = _REJECT_NOTE_RE.search(note)
        if not m:
            continue
        for item in _REJECT_ITEM_RE.finditer(m.group("rest")):
            out.append(RejectedWhy(source_name=item.group("src").strip(),
                                   url="", reason=item.group("reason").strip()))
    return out


# ═════════════════════════════════════════════════════════════════════════
# The explanation itself
# ═════════════════════════════════════════════════════════════════════════


@dataclass
class WhyExplanation:
    root_query: str
    sealed: bool
    refusal_reason: str = ""
    score: float = 0.0
    tier: str = ""
    proposed: float = 0.0           # best leaf confidence entering the parent clamp
    evidence: list[EvidenceWhy] = field(default_factory=list)
    ceilings: list[CeilingWhy] = field(default_factory=list)
    objections: list[ObjectionWhy] = field(default_factory=list)
    total_penalty: float = 0.0
    independence: Optional[IndependenceWhy] = None
    rejected: list[RejectedWhy] = field(default_factory=list)
    steps: list[StepWhy] = field(default_factory=list)
    largest_constraint: str = ""

    # ── machine-readable ────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "root_query": self.root_query,
            "sealed": self.sealed,
            "refusal_reason": self.refusal_reason,
            "confidence_score": self.score,
            "tier": self.tier,
            "proposed_confidence": self.proposed,
            "evidence": [e.to_dict() for e in self.evidence],
            "ceilings": [c.to_dict() for c in self.ceilings],
            "objections": [o.to_dict() for o in self.objections],
            "adversary_total_penalty": self.total_penalty,
            "independence": self.independence.to_dict()
            if self.independence else None,
            "rejected_at_ingestion": [r.to_dict() for r in self.rejected],
            "score_walk": [s.to_dict() for s in self.steps],
            "largest_constraint": self.largest_constraint,
        }

    # ── plain language ──────────────────────────────────────────────────

    def narrative(self) -> str:
        lines: list[str] = []
        head = f"WHY {self.score:.2f}" if not self.refusal_reason \
            else "WHY REFUSED"
        tier_bit = f" ({self.tier})" if self.tier else ""
        lines.append(f'{head}{tier_bit} — "{self.root_query}"')
        lines.append("")

        lines.append(f"EVIDENCE ({len(self.evidence)} item(s))")
        if self.evidence:
            for e in self.evidence:
                lines.append(f"  - [{e.source_class:<9}] {e.label}: "
                             f"{e.reason} Ceiling {e.ceiling:.2f}.")
        else:
            lines.append("  - none admitted.")
        lines.append("")

        lines.append("CONSTRAINTS ON THE SCORE")
        if self.ceilings:
            for c in self.ceilings:
                star = "  <-- BINDING" if c.binding else ""
                val = f"{c.value:.2f}" if c.value is not None else "—"
                lines.append(f"  - {c.kind}: ceiling {val}. {c.detail}{star}")
        else:
            lines.append("  - no structural ceiling applied.")
        lines.append("")

        lines.append(f"ADVERSARY ({len(self.objections)} objection(s), "
                     f"-{self.total_penalty:.2f} total)")
        if self.objections:
            for o in self.objections:
                cost = "VETO" if o.veto else f"-{o.penalty:.2f}"
                lines.append(f"  - [{o.severity}/{o.kind}] {cost}: {o.text}")
        else:
            lines.append("  - the conclusion withstood attack unchanged.")
        lines.append("")

        if self.independence:
            ind = self.independence
            lines.append(f"INDEPENDENCE: {ind.n_fetches} fetch(es) -> "
                         f"{ind.n_independent} independent source(s)")
            for c in ind.collapses:
                lines.append(f"  - {c}")
            lines.append("")

        if self.rejected:
            lines.append(f"REJECTED AT INGESTION ({len(self.rejected)})")
            for r in self.rejected:
                cov = (f" (relevance {r.relevance_score:.0%})"
                       if r.relevance_score >= 0 else "")
                src = f"[{r.source_name}] " if r.source_name else ""
                lines.append(f"  - {src}{r.reason}{cov}")
            lines.append("")

        if self.steps:
            lines.append("SCORE WALK")
            cur = self.steps[0].before
            lines.append(f"  start {cur:.2f} (best leaf confidence)")
            for s in self.steps:
                arrow = "->" if s.after >= s.before else "-="
                delta = abs(s.before - s.after)
                if delta > 1e-9:
                    lines.append(f"  {arrow} {delta:.2f}  {s.stage} "
                                 f"({s.rule}) -> {s.after:.2f}")
                else:
                    lines.append(f"  =  unchanged  {s.stage} ({s.rule})")
            lines.append("")

        lines.append(f"THE SHORT ANSWER: {self.largest_constraint}")
        if self.refusal_reason:
            lines.append(f"REFUSAL: {self.refusal_reason}")
        return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# The walker
# ═════════════════════════════════════════════════════════════════════════


def _independence_from_fetches(fetches: list[FetchResult]) -> IndependenceWhy:
    keys: set[str] = set()
    collapses: list[str] = []
    seen_family_members: dict[str, set[str]] = {}
    for f in fetches or []:
        key = independence_key(f.source_name, f.url or f.source_name)
        keys.add(key)
        for family, members in _OVERLAP_FAMILIES.items():
            if f.source_name in members:
                seen_family_members.setdefault(family, set()).add(f.source_name)
    for family, members in sorted(seen_family_members.items()):
        if len(members) > 1:
            collapses.append(
                f"'{family}' collapse: {', '.join(sorted(members))} count as "
                "ONE independent source (they index the same underlying pool)")
        elif members:
            collapses.append(
                f"'{family}' collapse applies to {next(iter(members))}; any "
                "other member would not have added an independent source")
    return IndependenceWhy(
        n_fetches=len(fetches or []),
        independent_keys=sorted(keys),
        n_independent=len(keys),
        collapses=collapses)


def explain_result(result: PipelineResult,
                   ledger: Optional[ProvenanceLedger] = None,
                   descendant_resolutions: Optional[list] = None) -> WhyExplanation:
    """Walk a pipeline result's whole scoring chain and assemble the answer.

    Pure read: nothing passed in is mutated, nothing is written anywhere.
    """
    from agp import ConfidenceTier

    session = result.session
    answered = [l for l in result.leaves if l.answer]
    proposed = max((l.confidence for l in answered), default=0.0)

    # ── evidence ──
    evidence_whys: list[EvidenceWhy] = []
    best_class_value = "INFERRED"
    if session is not None:
        for i, ev in enumerate(session.evidence):
            label = ev.source_name or f"evidence#{i}"
            cls_val, reason = assignment_reason(ev.content, ledger)
            if not cls_val:
                # No ledger to replay: report the class the pipeline
                # recorded, honestly marked as not independently replayed.
                cls_val = getattr(ev.source_class, "value", str(ev.source_class))
                reason = ("as recorded by the pipeline at ingestion; no "
                          "provenance ledger supplied to replay the decision")
            evidence_whys.append(EvidenceWhy(
                label=label, source_class=cls_val, reason=reason,
                ceiling=MAX_CONFIDENCE_BY_SOURCE.get(cls_val, 0.55)))
            if _CLASS_RANK.get(cls_val, 0) > _CLASS_RANK.get(best_class_value, 0):
                best_class_value = cls_val

    # ── ceilings ──
    ceilings: list[CeilingWhy] = []
    src_cap = MAX_CONFIDENCE_BY_SOURCE.get(best_class_value,
                                           MAX_CONFIDENCE_NO_TOOL)
    ceilings.append(CeilingWhy(
        kind="source_class", value=src_cap,
        detail=(f"best provenance-assigned evidence class is "
                f"{best_class_value}, whose ceiling is {src_cap:.2f}")))
    if any(l.requirement_reasons for l in result.leaves):
        reasons = sorted({r for l in result.leaves for r in l.requirement_reasons})
        ceilings.append(CeilingWhy(
            kind="requirement_gate", value=_REQUIREMENT_GATE_CAP,
            detail=("evidence requirements unmet on some leaf: "
                    + "; ".join(reasons))))
    inh_cap = inherited_ceiling(descendant_resolutions or [])
    n_resolved = len([r for r in (descendant_resolutions or [])])
    inh_detail = (f"{n_resolved} resolved descendant(s) feed the inheritance "
                  f"rule; ceiling from their track record is {inh_cap:.2f}")
    if n_resolved < MIN_RESOLVED_FOR_LIFT:
        inh_detail += (f"; fewer than {MIN_RESOLVED_FOR_LIFT} resolutions ever "
                       "caps the parent at SPECULATIVE regardless of eloquence")
    ceilings.append(CeilingWhy(kind="inheritance", value=inh_cap,
                               detail=inh_detail))

    # ── adversary ──
    objection_whys: list[ObjectionWhy] = []
    total_penalty = 0.0
    veto_text = ""
    statuses: dict[str, str] = {}
    if session is not None:
        try:
            for ob in (pipeline_adversary_ledger_statuses(result) or {}).values():
                statuses[ob[0]] = ob[1]
        except Exception:  # noqa: BLE001 — status enrichment is best-effort
            statuses = {}
    for ob in result.objections or []:
        pen = getattr(ob, "penalty", 0.0)
        blocking = bool(getattr(ob, "is_blocking", False))
        total_penalty += 0.0 if blocking else pen
        if blocking and not veto_text:
            veto_text = getattr(ob, "text", "")
        objection_whys.append(ObjectionWhy(
            text=getattr(ob, "text", str(ob)),
            kind=getattr(ob, "kind", "unspecified"),
            severity=getattr(ob, "severity", "?"),
            penalty=pen, veto=blocking,
            status=statuses.get(getattr(ob, "text", ""), "")))
    total_penalty = round(total_penalty, 4)

    # ── independence + rejections ──
    independence = _independence_from_fetches(list(result.fetches or []))
    rejected = _parse_rejections(result.notes or [])

    # ── the arithmetic walk (display-only recomputation of the same mins) ──
    steps: list[StepWhy] = []
    cur = proposed
    if proposed > 0:
        after = min(cur, src_cap)
        if after < cur - 1e-9:
            steps.append(StepWhy(
                stage="source-class clamp", before=cur, after=after,
                rule=f"min(proposed, {best_class_value} ceiling {src_cap:.2f})"))
            cur = after
        gate = next((c for c in ceilings if c.kind == "requirement_gate"), None)
        if gate is not None and cur > _REQUIREMENT_GATE_CAP:
            steps.append(StepWhy(
                stage="evidence-requirement gate", before=cur,
                after=_REQUIREMENT_GATE_CAP,
                rule=f"unmet requirements cap at {_REQUIREMENT_GATE_CAP:.2f}"))
            cur = _REQUIREMENT_GATE_CAP
        if inh_cap < cur - 1e-9:
            steps.append(StepWhy(
                stage="inheritance rule", before=cur, after=inh_cap,
                rule="min(score, inherited_ceiling(descendants))"))
            cur = inh_cap
        if veto_text:
            steps.append(StepWhy(
                stage="adversary veto", before=cur, after=result.confidence_score,
                rule=f"BLOCKING objection vetoes the seal: \"{veto_text[:80]}\""))
            cur = result.confidence_score
        elif total_penalty > 0:
            after = round(max(0.0, cur - total_penalty), 2)
            steps.append(StepWhy(
                stage="adversary penalties", before=cur, after=after,
                rule=f"-{total_penalty:.2f} across {len(objection_whys)} "
                     "objection(s)"))
            cur = after
        floor_note = ""
        if cur < DB_CONFIDENCE_FLOOR:
            floor_note = (f" (below the DB floor {DB_CONFIDENCE_FLOOR}, "
                          "which refuses rather than stores)")
            steps.append(StepWhy(
                stage="db floor check", before=cur, after=cur,
                rule=floor_note.strip()))

    # ── binding ceiling + the short answer ──
    numeric = [c.value for c in ceilings if c.value is not None]
    binding_kind = ""
    if numeric:
        min_cap = min(numeric)
        for c in ceilings:
            if c.value == min_cap:
                c.binding = True
                binding_kind = c.kind
                break

    largest = _largest_constraint(steps, binding_kind, veto_text,
                                  total_penalty, result, independence)

    return WhyExplanation(
        root_query=result.root_query,
        sealed=bool(result.sealed),
        refusal_reason=result.refusal_reason,
        score=float(result.confidence_score),
        tier=str(result.confidence_tier),
        proposed=round(proposed, 2),
        evidence=evidence_whys,
        ceilings=ceilings,
        objections=objection_whys,
        total_penalty=total_penalty,
        independence=independence,
        rejected=rejected,
        steps=steps,
        largest_constraint=largest,
    )


def _largest_constraint(steps: list[StepWhy], binding_kind: str,
                        veto_text: str, total_penalty: float,
                        result: PipelineResult,
                        independence: IndependenceWhy) -> str:
    """The single sentence: this is X because Y."""
    q = result.root_query
    score = result.confidence_score
    if result.refusal_reason and not result.sealed:
        if veto_text:
            return (f'"{q}" was REFUSED, not scored: the adversary raised a '
                    f"BLOCKING objection — {veto_text}")
        return f'"{q}" was REFUSED: {result.refusal_reason}'
    if not steps:
        return (f'"{q}" scores {score:.2f} because nothing constrained it '
                "beyond the proposal itself.")
    biggest = max(steps, key=lambda s: s.drop)
    if biggest.stage == "adversary penalties":
        return (f'"{q}" is {score:.2f} because the adversary\'s objections '
                f"subtracted {total_penalty:.2f} — the largest single "
                "constraint on the score.")
    if biggest.stage == "inheritance rule":
        return (f'"{q}" is {score:.2f} because the inheritance rule capped it '
                "at the track-record ceiling of its resolved descendants.")
    if biggest.stage == "evidence-requirement gate":
        return (f'"{q}" is {score:.2f} because its evidence requirements were '
                f"unmet (capped at {_REQUIREMENT_GATE_CAP:.2f}); "
                f"independence counted {independence.n_independent} "
                f"independent source(s) from {independence.n_fetches} "
                "fetches.")
    if biggest.stage == "source-class clamp":
        return (f'"{q}" is {score:.2f} because its best evidence never rose '
                "above a source class whose provenance ceiling sits below "
                "what was proposed.")
    if biggest.stage == "adversary veto":
        return f'"{q}" was vetoed by the adversary: {veto_text}'
    return (f'"{q}" is {score:.2f} because of {biggest.stage} '
            f"(-{biggest.drop:.2f}).")


def pipeline_adversary_ledger_statuses(result: PipelineResult) -> dict:
    """Best-effort objection statuses from the run's dissent ledger.

    Returns {objection_text: (text, status)}. Only works when the pipeline's
    adversary ledger is reachable and contains this session id; absence is
    normal for stored claims and simply leaves statuses blank.
    """
    out: dict[str, tuple[str, str]] = {}
    session = result.session
    if session is None:
        return out
    try:  # the engine does not retain its AdversaryLedger on the result;
         # look for a state-dir ledger only if explicitly cheap to reach.
        import os
        state_dir = os.environ.get(
            "CALLISTO_STATE_DIR",
            os.path.expanduser("~/.local/state/callisto"))
        path = os.path.join(state_dir, "adversary_dissent.jsonl")
        if not os.path.exists(path):
            return out
        import json
        want = session.session_id
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("claim_id") != want:
                    continue
                out[rec.get("text", "")] = (
                    rec.get("text", ""), rec.get("status", ""))
    except Exception:  # noqa: BLE001 — enrichment must never break explaining
        return {}
    return out


# ═════════════════════════════════════════════════════════════════════════
# Stored-claim seam: explain from the machine-readable dict alone
# ═════════════════════════════════════════════════════════════════════════


def explain_stored(payload: dict) -> WhyExplanation:
    """Rehydrate a WhyExplanation from its to_dict() form.

    This is how the same explanation attaches to a stored claim: store
    ``why.to_dict()`` beside the seal, reload it here weeks later, and the
    plain-language rendering survives intact.
    """
    independence = None
    if payload.get("independence"):
        ind = payload["independence"]
        independence = IndependenceWhy(
            n_fetches=ind.get("n_fetches", 0),
            independent_keys=list(ind.get("independent_keys", [])),
            n_independent=ind.get("n_independent", 0),
            collapses=list(ind.get("collapses", [])))
    return WhyExplanation(
        root_query=payload.get("root_query", ""),
        sealed=bool(payload.get("sealed")),
        refusal_reason=payload.get("refusal_reason", ""),
        score=float(payload.get("confidence_score", 0.0)),
        tier=str(payload.get("tier", "")),
        proposed=float(payload.get("proposed_confidence", 0.0)),
        evidence=[EvidenceWhy(**e) for e in payload.get("evidence", [])],
        ceilings=[CeilingWhy(**c) for c in payload.get("ceilings", [])],
        objections=[ObjectionWhy(**o) for o in payload.get("objections", [])],
        total_penalty=float(payload.get("adversary_total_penalty", 0.0)),
        independence=independence,
        rejected=[RejectedWhy(**r) for r in payload.get("rejected_at_ingestion", [])],
        steps=[StepWhy(**s) for s in payload.get("score_walk", [])],
        largest_constraint=payload.get("largest_constraint", ""),
    )
