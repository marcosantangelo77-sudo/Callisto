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
    purely for display. It cannot make a score friendlier.
  - Domain-general: nothing here knows what a wager or a semiconductor is.

Usage:
    expl = explain_result(pipeline_result, ledger=pipeline.ledger)
    print(expl.narrative())        # plain language
    expl.to_dict()                 # machine-readable, attachable to a claim

Refused runs get the same treatment: the chain is walked up to the point of
refusal and the refusal itself is explained.
"""
from __future__ import annotations


from agp.thresholds import floor_conf
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from agp.thresholds import (
    DB_CONFIDENCE_FLOOR,
    MAX_CONFIDENCE_BY_SOURCE,
    MAX_CONFIDENCE_NO_TOOL,
)
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


# ═════════════════════════════════════════════════════════════════════════
# Explanation records
# ═════════════════════════════════════════════════════════════════════════


@dataclass
class EvidenceWhy:
    """One evidence item: the class provenance assigned it, and WHY."""
    label: str                       # short identifier (source name / index)
    source_class: str
    reason: str                      # the provenance rule that fired
    ceiling: float                   # confidence ceiling implied by the class

    def to_dict(self) -> dict:
        return {"label": self.label, "source_class": self.source_class,
                "reason": self.reason, "ceiling": self.ceiling}


@dataclass
class CeilingWhy:
    """One constraint that bounded the score."""
    kind: str            # source_class | requirement_gate | inheritance | ...
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
# Provenance replay: why did THIS item get THIS class?
# ═════════════════════════════════════════════════════════════════════════

_PRIMARY_RULE = ("exact bytes returned by a real tool call this session "
                 "(content-hash match on a primary observation)")
_OBSERVED_RULE = ("bytes matching something a tool returned this session "
                  "(hash match, non-primary observation)")
_CITED_RULE = ("cites a URL this session genuinely fetched "
               "(citation grounding)")
_INFERRED_RULE = ("no tool bytes or fetched URL back it — model output "
                  "without verification")


class _probe:
    """Minimal Evidence-shaped object for ledger queries."""

    def __init__(self, content: str):
        self.content = content
        self.source_class = None


def assignment_reason(evidence_content: str,
                      ledger) -> tuple[str, str]:
    """(source_class_value, reason) for one evidence item.

    Replays the assignment with the SAME ledger rules the pipeline used and
    names the specific rule that fires. With no ledger available, returns
    ("", "") — callers fall back to the recorded class, honestly marked.
    """
    if ledger is None:
        return "", ""
    assigned = ledger.assign_source_class(_probe(content=evidence_content))
    if ledger.is_primary_bytes(evidence_content):
        reason = _PRIMARY_RULE
    elif ledger.has_observation(evidence_content):
        reason = _OBSERVED_RULE
    elif ledger.cites_verified_url(evidence_content):
        reason = _CITED_RULE
    else:
        reason = _INFERRED_RULE
    return assigned.value, reason


# ═════════════════════════════════════════════════════════════════════════
# Ingestion rejections: parsed from result.notes when traces are gone
# ═════════════════════════════════════════════════════════════════════════

_REJECT_NOTE_RE = re.compile(
    r"leaf '(?P<leaf>.{0,80}?)': (?P<n>\d+) fetch\(s\) rejected at ingestion: (?P<rest>.*)")
_REJECT_ITEM_RE = re.compile(r"\[(?P<src>[^\]]+)\] (?P<reason>[^;]+)")


def parse_rejections(notes: Optional[Iterable[str]]) -> list[RejectedWhy]:
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
                status = f" [{o.status}]" if o.status else ""
                lines.append(f"  - [{o.severity}/{o.kind}] {cost}{status}: "
                             f"{o.text}")
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

        if self.proposed > 0:
            lines.append("SCORE WALK")
            lines.append(f"  start {self.proposed:.2f} "
                         "(best leaf confidence)")
            if not self.steps:
                lines.append("  =   unchanged  (proposal already sat at or "
                             "below every binding ceiling)")
            for s in self.steps:
                delta = abs(s.before - s.after)
                if delta > 1e-9:
                    sign = "-" if s.after < s.before else "+"
                    lines.append(
                        f"  {sign}{delta:.2f}  {s.stage} ({s.rule})"
                        f" -> {s.after:.2f}")
                else:
                    lines.append(f"  =   unchanged  {s.stage} ({s.rule})")
            lines.append("")

        lines.append(f"THE SHORT ANSWER: {self.largest_constraint}")
        if self.refusal_reason:
            lines.append(f"REFUSAL: {self.refusal_reason}")
        return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# Independence accounting
# ═════════════════════════════════════════════════════════════════════════


def independence_from_fetches(fetches) -> IndependenceWhy:
    from tools.pipeline.retrieval import in_family as _in_family
    """Count independent sources exactly as retrieval does, with the
    family-collapse statements spelled out."""
    keys: set = set()
    collapses: list[str] = []
    seen_family_members: dict[str, set] = {}
    for f in fetches or []:
        key = independence_key(getattr(f, "source_name", ""),
                               getattr(f, "url", "") or
                               getattr(f, "source_name", ""))
        keys.add(key)
        for family, members in _OVERLAP_FAMILIES.items():
            if _in_family(getattr(f, "source_name", ""), members):
                seen_family_members.setdefault(family, set()).add(
                    f.source_name)
    for family, members in sorted(seen_family_members.items()):
        names = ", ".join(sorted(members))
        if len(members) > 1:
            collapses.append(
                f"'{family}' collapse: {names} count as ONE independent "
                "source (they index the same underlying pool)")
        elif members:
            collapses.append(
                f"'{family}' collapse applies to {names}; any other member "
                "would not have added an independent source")
    return IndependenceWhy(
        n_fetches=len(fetches or []),
        independent_keys=sorted(str(k) for k in keys),
        n_independent=len(keys),
        collapses=collapses)


# ═════════════════════════════════════════════════════════════════════════
# The walker
# ═════════════════════════════════════════════════════════════════════════


def explain_result(result, ledger=None,
                   descendant_resolutions=None) -> WhyExplanation:
    """Walk a pipeline result's whole scoring chain and assemble the answer.

    Pure read: nothing passed in is mutated, nothing is written anywhere.
    ``ledger`` is the run's ProvenanceLedger (for replaying per-item class
    assignments); without it the recorded classes are reported but marked
    as unreplayable.
    """
    answered = [l for l in result.leaves if l.answer]
    proposed = max((l.confidence for l in answered), default=0.0)

    # ── evidence ──
    evidence_whys: list[EvidenceWhy] = []
    best_class_value = "INFERRED"
    if result.session is not None:
        for i, ev in enumerate(result.session.evidence):
            label = ev.source_name or f"evidence#{i}"
            cls_val, reason = assignment_reason(ev.content, ledger)
            if not cls_val:
                cls_val = getattr(ev.source_class, "value",
                                  str(ev.source_class))
                reason = ("as recorded by the pipeline at ingestion; no "
                          "provenance ledger supplied to replay the decision")
            evidence_whys.append(EvidenceWhy(
                label=label, source_class=cls_val, reason=reason,
                ceiling=MAX_CONFIDENCE_BY_SOURCE.get(cls_val, 0.55)))
            if _CLASS_RANK.get(cls_val, 0) > _CLASS_RANK.get(best_class_value, 0):
                best_class_value = cls_val

    # ── ceilings ──
    ceilings: list[CeilingWhy] = []
    src_cap = MAX_CONFIDENCE_BY_SOURCE.get(
        best_class_value, MAX_CONFIDENCE_NO_TOOL)
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
    n_resolved = len(list(descendant_resolutions or []))
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
    statuses = pipeline_adversary_ledger_statuses(result)
    for ob in result.objections or []:
        pen = getattr(ob, "penalty", 0.0)
        blocking = bool(getattr(ob, "is_blocking", False))
        total_penalty += 0.0 if blocking else pen
        text = getattr(ob, "text", "")
        if blocking and not veto_text:
            veto_text = text
        objection_whys.append(ObjectionWhy(
            text=text,
            kind=getattr(ob, "kind", "unspecified"),
            severity=getattr(ob, "severity", "?"),
            penalty=pen, veto=blocking,
            status=statuses.get(text, "")))
    total_penalty = round(total_penalty, 4)

    # ── independence + rejections ──
    independence = independence_from_fetches(list(result.fetches or []))
    rejected = parse_rejections(result.notes or [])

    # ── the arithmetic walk (display-only replay of the same mins/minus) ──
    steps: list[StepWhy] = []
    cur = proposed
    if proposed > 0:
        after = min(cur, src_cap)
        if after < cur - 1e-9:
            steps.append(StepWhy(
                stage="source-class clamp", before=cur, after=after,
                rule=f"min(proposed, {best_class_value} ceiling "
                     f"{src_cap:.2f})"))
            cur = after
        gate = next((c for c in ceilings if c.kind == "requirement_gate"),
                    None)
        if gate is not None and cur > _REQUIREMENT_GATE_CAP:
            steps.append(StepWhy(
                stage="evidence-requirement gate", before=cur,
                after=_REQUIREMENT_GATE_CAP,
                rule=f"unmet requirements cap at "
                     f"{_REQUIREMENT_GATE_CAP:.2f}"))
            cur = _REQUIREMENT_GATE_CAP
        if inh_cap < cur - 1e-9:
            steps.append(StepWhy(
                stage="inheritance rule", before=cur, after=inh_cap,
                rule="min(score, inherited_ceiling(descendants))"))
            cur = inh_cap
        if veto_text:
            steps.append(StepWhy(
                stage="adversary veto", before=cur,
                after=result.confidence_score,
                rule=f'BLOCKING objection vetoes the seal: '
                     f'"{veto_text[:80]}"'))
            cur = result.confidence_score
        elif total_penalty > 0:
            after = round(max(0.0, cur - total_penalty), 2)
            steps.append(StepWhy(
                stage="adversary penalties", before=cur, after=after,
                rule=f"-{total_penalty:.2f} across "
                     f"{len(objection_whys)} objection(s)"))
            cur = after
        if cur < DB_CONFIDENCE_FLOOR and result.refusal_reason \
                and "floor" in result.refusal_reason:
            steps.append(StepWhy(
                stage="db floor check", before=cur, after=cur,
                rule=(f"below the DB floor {DB_CONFIDENCE_FLOOR}, which "
                      "refuses rather than stores")))

    # ── binding ceiling + the short answer ──
    numeric = [c.value for c in ceilings if c.value is not None]
    if numeric:
        min_cap = min(numeric)
        # Every ceiling sitting exactly at the effective minimum binds.
        for c in ceilings:
            if c.value == min_cap:
                c.binding = True
        # The inheritance rule with too-few resolutions is a STRUCTURAL cap
        # ("SPECULATIVE forever"), binding even when a lower numeric cap
        # happens to sit beneath it.
        n_res = len(list(descendant_resolutions or []))
        if n_res < MIN_RESOLVED_FOR_LIFT:
            next(c for c in ceilings if c.kind == "inheritance").binding = True

    largest = _largest_constraint(steps, ceilings, veto_text,
                                  total_penalty, result, independence)

    return WhyExplanation(
        root_query=result.root_query,
        sealed=bool(result.sealed),
        refusal_reason=result.refusal_reason,
        score=float(result.confidence_score),
        tier=str(result.confidence_tier),
        proposed=floor_conf(proposed),
        evidence=evidence_whys,
        ceilings=ceilings,
        objections=objection_whys,
        total_penalty=total_penalty,
        independence=independence,
        rejected=rejected,
        steps=steps,
        largest_constraint=largest,
    )


def _largest_constraint(steps: list[StepWhy], ceilings: list[CeilingWhy],
                        veto_text: str, total_penalty: float,
                        result, independence: IndependenceWhy) -> str:
    """The single sentence: this is X because Y."""
    q = result.root_query
    score = result.confidence_score
    if result.refusal_reason and not result.sealed:
        if veto_text:
            return (f'"{q}" was REFUSED, not scored: the adversary raised a '
                    f"BLOCKING objection — {veto_text}")
        return f'"{q}" was REFUSED: {result.refusal_reason}'
    if not steps:
        # No drop anywhere: name the binding ceiling if one exists, else say
        # nothing constrained it.
        binding = [c for c in ceilings if c.binding]
        if binding and score > 0:
            names = ", ".join(sorted({c.kind for c in binding}))
            vals = "/".join(f"{c.value:.2f}" for c in binding)
            return (f'"{q}" is {score:.2f} because it is held at the '
                    f"binding {names} ceiling ({vals}); the proposal never "
                    "exceeded it.")
        if binding:
            names = ", ".join(sorted({c.kind for c in binding}))
            return (f'"{q}" scored 0.00: the proposal itself was 0, with the '
                    f"binding {names} ceiling in force.")
        return (f'"{q}" scores {score:.2f} because nothing constrained it '
                "beyond the proposal itself.")
    biggest = max(steps, key=lambda s: s.drop)
    if biggest.drop <= 1e-9:
        return (f'"{q}" is {score:.2f} because every constraint already '
                "applied left the proposal unchanged.")
    if biggest.stage == "adversary penalties":
        return (f'"{q}" is {score:.2f} because the adversary\'s objections '
                f"subtracted {total_penalty:.2f} — the largest single "
                "constraint on the score.")
    if biggest.stage == "inheritance rule":
        return (f'"{q}" is {score:.2f} because the inheritance rule capped it '
                "at the track-record ceiling of its resolved descendants.")
    if biggest.stage == "evidence-requirement gate":
        return (f'"{q}" is {score:.2f} because its evidence requirements were '
                f"unmet (capped at {_REQUIREMENT_GATE_CAP:.2f}); independence "
                f"counted {independence.n_independent} independent source(s) "
                f"from {independence.n_fetches} fetch(es).")
    if biggest.stage == "source-class clamp":
        return (f'"{q}" is {score:.2f} because its best evidence never rose '
                "above a source class whose provenance ceiling sits below "
                "what was proposed.")
    if biggest.stage == "adversary veto":
        return f'"{q}" was vetoed by the adversary: {veto_text}'
    return (f'"{q}" is {score:.2f} because of {biggest.stage} '
            f"(-{biggest.drop:.2f}).")


def pipeline_adversary_ledger_statuses(result) -> dict:
    """Best-effort objection statuses from the dissent ledger.

    Returns {objection_text: status}. Only reads the state-dir JSONL when it
    exists; absence is normal for stored claims and simply leaves statuses
    blank. Never raises.
    """
    out: dict = {}
    session = getattr(result, "session", None)
    if session is None:
        return out
    try:
        import json
        import os
        state_dir = os.environ.get(
            "CALLISTO_STATE_DIR",
            os.path.expanduser("~/.local/state/callisto"))
        path = os.path.join(state_dir, "adversary_dissent.jsonl")
        if not os.path.exists(path):
            return out
        want = getattr(session, "session_id", "")
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
                out[rec.get("text", "")] = rec.get("status", "")
    except Exception:  # noqa: BLE001 — enrichment must never break explaining
        return {}
    return out


# ═════════════════════════════════════════════════════════════════════════
# Stored-claim seam: rehydrate from the machine-readable dict alone
# ═════════════════════════════════════════════════════════════════════════


def explain_stored(payload: dict) -> WhyExplanation:
    """Rehydrate a WhyExplanation from its to_dict() form.

    This is how the same explanation attaches to a stored claim: store
    ``why.to_dict()`` beside the seal, reload it weeks later, and the
    plain-language rendering survives intact.
    """
    independence = None
    if payload.get("independence"):
        ind = payload["independence"]
        independence = IndependenceWhy(
            n_fetches=int(ind.get("n_fetches", 0)),
            independent_keys=list(ind.get("independent_keys", [])),
            n_independent=int(ind.get("n_independent", 0)),
            collapses=list(ind.get("collapses", [])))
    _expl = WhyExplanation(
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
        rejected=[RejectedWhy(**r)
                  for r in payload.get("rejected_at_ingestion", [])],
        steps=[StepWhy(**{k: v for k, v in s.items()
                         if k in StepWhy.__dataclass_fields__})
               for s in payload.get("score_walk", [])],
        largest_constraint=payload.get("largest_constraint", ""),
    )
    expl = _expl
    if not expl.largest_constraint:
        # Older payloads may lack the short answer; regenerate it from
        # whatever sections survived storage.
        expl.largest_constraint = _largest_constraint(
            expl.steps, expl.ceilings,
            next((o.text for o in expl.objections if o.veto), ""),
            expl.total_penalty,
            _StoredShim(expl), expl.independence or IndependenceWhy(0, [], 0, []))
    return expl


class _StoredShim:
    """Minimal .root_query/.sealed/.refusal_reason/.confidence_score view."""

    def __init__(self, expl: WhyExplanation):
        self.root_query = expl.root_query
        self.sealed = expl.sealed
        self.refusal_reason = expl.refusal_reason
        self.confidence_score = expl.score
