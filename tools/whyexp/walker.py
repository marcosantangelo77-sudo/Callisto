"""The walker: assemble a WhyExplanation from a pipeline result."""
from __future__ import annotations

import json
import os

from agp.thresholds import (
    DB_CONFIDENCE_FLOOR,
    MAX_CONFIDENCE_BY_SOURCE,
    MAX_CONFIDENCE_NO_TOOL,
    floor_conf,
)
from tools.research_program import (
    INHERITED_CEILING_BY_SOURCE,  # noqa: F401 — re-exported for parity
    MIN_RESOLVED_FOR_LIFT,
    inherited_ceiling,
    normalize_records,
    stale_penalty_rate,
    summarize_track_record,
)
from tools.whyexp.explanation import WhyExplanation
from tools.whyexp.independence import independence_from_fetches
from tools.whyexp.provenance import assignment_reason
from tools.whyexp.records import (
    REQUIREMENT_GATE_CAP as _REQUIREMENT_GATE_CAP,
    _CLASS_RANK,
    CeilingWhy,
    EvidenceWhy,
    IndependenceWhy,
    ObjectionWhy,
    RejectedWhy,  # noqa: F401 — re-exported for parity
    StepWhy,
)
from tools.whyexp.rejections import parse_rejections


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
    # Only genuine resolutions (hit/miss) count toward the lift gate — a
    # stale record is unresolved-at-deadline and must not be reported as
    # resolved evidence (mirrors tools/research_program.inherited_ceiling).
    _recs = normalize_records(descendant_resolutions or [])
    n_resolved = sum(1 for r in _recs if r.resolved)
    n_stale = sum(1 for r in _recs if r.outcome == "stale")
    # The staleness demotion this record set applies, recomputed FROM THE
    # SAME RULES as tools/research_program (display-only; never feeds back
    # into scoring): bounded rate x stale/(resolved + stale), so it can
    # never exceed its documented -0.20 maximum.
    self_stale_penalty = 0.0
    if n_stale and n_resolved >= MIN_RESOLVED_FOR_LIFT:
        tr_ = summarize_track_record(_recs)
        self_stale_penalty = round(
            stale_penalty_rate() * tr_.stale_fraction, 4)
    inh_detail = (f"{n_resolved} resolved descendant(s) feed the inheritance "
                  f"rule; ceiling from their track record is {inh_cap:.2f}")
    if n_resolved < MIN_RESOLVED_FOR_LIFT:
        inh_detail += (f"; fewer than {MIN_RESOLVED_FOR_LIFT} resolutions ever "
                       "caps the parent at SPECULATIVE regardless of eloquence")
    if n_stale:
        inh_detail += (f"; {n_stale} stale descendant(s) are NOT resolved "
                       "evidence — they only demote via the staleness penalty "
                       f"(-{self_stale_penalty:.2f})")
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
        if n_resolved < MIN_RESOLVED_FOR_LIFT:
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
        stale_descendants=n_stale,
        stale_penalty_applied=self_stale_penalty,
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
        stale_descendants=int(payload.get("stale_descendants", 0)),
        stale_penalty_applied=float(
            payload.get("stale_penalty_applied", 0.0)),
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
