"""Cross-source synthesis — triangulate, contradict, extract (WAVE 5, I3).

The pipeline handed the synthesizer a flat list of evidence contents and
asked for an answer. That is concatenation. This module makes the
STRUCTURE of the evidence — who agrees, who conflicts, how many
INDEPENDENT parties are on each side, and which numbers were actually
stated — the object the conclusion is reasoned over.

Five jobs, all domain-general (a scholarly question, a market claim and a
materials-science prediction produce identical structures):

1. TRIANGULATION. Evidence is grouped by CLAIM, not by document. Within a
   group, corroboration is counted in independence units —
   ``retrieval.independence_key`` is reused verbatim; no second notion of
   independence is invented. "Three independent sources agree" and "one
   source says" become different epistemic objects.

2. CONTRADICTION AS A FIRST-CLASS OUTPUT. When independent sources state
   conflicting values for the same claim, a Contradiction is produced:
   the disagreement, the sources on each side, and what would settle it.
   It is never averaged away — it lowers the group's confidence ceiling
   and is carried into the report.

3. CONFIDENCE FROM AGREEMENT STRUCTURE. The score starts from the
   provenance-assigned source-class ceiling (agp.thresholds) and may rise
   only with additional DISTINCT independent sources — and never above
   that ceiling. Ten documents from one publisher move nothing; two
   independent sources agreeing is worth more than ten dependent ones.
   Every adjustment here is min(...) or a capped add — the module can
   only lower or cap-within-ceiling what provenance permits.

4. STRUCTURED EXTRACTION. Numbers and claims land in a table with
   provenance per cell (source, independence key, content hash, URL).
   Summarising destroys the numbers; the table keeps them checkable.

5. HONEST NULLS. ``classify_null`` distinguishes "the literature does not
   address this" (fetches ran, the relevance gate rejected with reasons)
   from "we failed to retrieve" (sources errored, no routable source, or
   nothing was even attempted) using the retrieval trace's recorded
   rejections. Conflating the two is how a research system quietly lies.

GATE RULE: nothing here raises any ceiling. Corroboration may raise a
score only up to the ceiling the source class already permits; a live
contradiction caps the group at SPECULATIVE (0.54), matching the
requirement-floor used by engine._answer_leaf.
"""
from __future__ import annotations


from agp.thresholds import floor_conf
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE

# Reuse THE independence notion; do not define a second one.
from tools.pipeline.retrieval import independence_key  # noqa: F401  (re-export)

_SPECULATIVE_CAP = 0.54          # same floor-band cap engine applies on unmet
_DB_FLOOR = 0.30
_CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}

_NUM_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(%|pp|bps|"
    r"billion|million|trillion|bn|mn|nm|µm|um|kg|mt|GW|nm)?(?![\w])",
    re.IGNORECASE)

_SCALE = {"%": 1e-2, "pp": 1e-2, "bps": 1e-4, "billion": 1e9, "bn": 1e9,
          "million": 1e6, "mn": 1e6, "trillion": 1e12}

_WORD_RE = re.compile(r"[a-z0-9]+")

# Words that carry no claim-discriminating weight when normalising a claim.
_CLAIM_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is",
    "are", "was", "were", "be", "been", "that", "this", "it", "its", "by",
    "with", "at", "as", "about", "from", "into", "than", "then",
}


# ── Evidence items ────────────────────────────────────────────────────────


@dataclass
class EvidenceItem:
    """One admitted piece of evidence, in the shape synthesis reasons over.

    Built from an engine FetchResult via ``from_fetch``; ``claim`` is what
    this item asserts (extractor-injected or model-provided — synthesis
    never trusts a model's self-description of its own provenance, only
    of its content).
    """
    claim: str
    source_name: str
    base_url: str
    source_class: str            # agp SourceClass value
    content_sha256: str = ""
    url: str = ""
    values: tuple[float, ...] = ()   # numbers the item states (normalised)
    value_units: tuple[str, ...] = ()
    stance: str = ""             # "supports" | "refutes" | "" (unspecified)
    indep_key: str = ""

    def __post_init__(self):
        if not self.indep_key:
            self.indep_key = independence_key(self.source_name, self.base_url)

    @classmethod
    def from_fetch(cls, fetch: Any, claim: str, source_class: str,
                   base_url: str = "", values: Optional[Iterable[float]] = None,
                   value_units: Optional[Iterable[str]] = None,
                   stance: str = "") -> "EvidenceItem":
        return cls(
            claim=claim,
            source_name=fetch.source_name,
            base_url=base_url or f"https://{fetch.source_name}",
            source_class=source_class,
            content_sha256=getattr(fetch, "content_sha256", ""),
            url=getattr(fetch, "url", ""),
            values=tuple(values) if values is not None
            else extract_values(getattr(fetch, "body", "") or ""),
            value_units=tuple(value_units) if value_units else (),
            stance=stance,
        )


def extract_values(text: str) -> tuple[float, ...]:
    """Numbers stated in *text*, normalised (%, bn, ... → plain magnitudes).

    Structured extraction, not summarisation: these are the checkable
    cells of the table."""
    out = []
    for m in _NUM_RE.finditer(text or ""):
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        out.append(v * _SCALE.get(unit, 1.0))
    return tuple(out)


def claim_key(claim: str) -> tuple[str, ...]:
    """Normalised grouping key for a claim: salient tokens, sorted."""
    return tuple(sorted({
        w for w in _WORD_RE.findall((claim or "").lower())
        if len(w) >= 3 and w not in _CLAIM_STOPWORDS
    }))


def has_content_words(claim: str) -> bool:
    """True iff the claim carries at least one non-stopword word token.

    A claim with no content words cannot corroborate anything: there is
    nothing for two independent sources to agree ON. Used by triangulate()
    to refuse grouping such items (S5).

    Deliberately a WEAKER bar than claim_key(): claim_key drops short
    tokens because they make poor grouping keys; here even a two- or
    one-letter real word ("AI", a symbol, an initialism) counts as
    content. Only claims made of nothing — empty, whitespace,
    punctuation-only, or stopword-only text — fail.
    """
    return any(w not in _CLAIM_STOPWORDS
               for w in _WORD_RE.findall((claim or "").lower()))


# ── 1. Triangulation ─────────────────────────────────────────────────────


@dataclass
class ClaimGroup:
    """Every evidence item asserting one claim, with the agreement structure."""
    claim: str
    items: list[EvidenceItem] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, ...]:
        return claim_key(self.claim)

    @property
    def independent_sources(self) -> int:
        return len({i.indep_key for i in self.items})

    @property
    def indep_keys(self) -> set[str]:
        return {i.indep_key for i in self.items}

    @property
    def best_class(self) -> str:
        return max((i.source_class for i in self.items),
                   key=lambda c: _CLASS_RANK.get(c, 0), default="INFERRED")


def triangulate(items: Iterable[EvidenceItem]) -> list[ClaimGroup]:
    """Group evidence by claim. Order of groups is deterministic (by key).

    Items whose claim has NO content words are never grouped (S5): the
    empty claim_key would otherwise collapse every vacuous item into one
    group, manufacturing "independent voices agree" out of extractor junk.
    They are still returned — each as its own single-item group with
    confidence 0 — so no evidence is silently dropped from the report; a
    vacuous group simply cannot corroborate, because there is nothing for
    two sources to agree ON.
    """
    groups: dict[tuple[str, ...], ClaimGroup] = {}
    vacuous: list[EvidenceItem] = []
    for it in items:
        if not has_content_words(it.claim):
            vacuous.append(it)
            continue
        k = claim_key(it.claim)
        g = groups.get(k)
        if g is None:
            g = groups[k] = ClaimGroup(claim=it.claim)
        g.items.append(it)
    # deterministic order: real groups by key, then vacuous ones by source
    out = [groups[k] for k in sorted(groups)]
    for it in sorted(vacuous, key=lambda x: x.indep_key):
        g = ClaimGroup(claim=it.claim)
        g.items.append(it)
        out.append(g)
    return out


# ── 2. Contradiction ─────────────────────────────────────────────────────


@dataclass
class Contradiction:
    """A live disagreement between INDEPENDENT sources on one claim."""
    claim: str
    kind: str                      # "numeric" | "stance"
    sides: list[dict]              # [{value|stance, sources, indep_keys, provenance}]
    what_would_settle_it: str
    severity: str = "MAJOR"

    def to_dict(self) -> dict:
        return {"claim": self.claim, "kind": self.kind, "sides": self.sides,
                "what_would_settle_it": self.what_would_settle_it,
                "severity": self.severity}


def _settle_suggestion(kind: str, claim: str) -> str:
    if kind == "numeric":
        return (f"an authoritative PRIMARY-source measurement of '{claim}' "
                f"(e.g. an official statistic or the original dataset) to "
                f"arbitrate between the conflicting values")
    return (f"a PRIMARY-source statement of '{claim}' to establish which "
            f"reading is correct")


def detect_contradictions(group: ClaimGroup,
                          rel_tolerance: float = 0.10,
                          ) -> list[Contradiction]:
    """Conflicts within one claim group.

    Numeric: sources in DIFFERENT independence units stating values that
    differ by more than *rel_tolerance* relative to the larger. Values from
    the same independence unit are collapsed first (one publisher stating
    a number twice is still one voice). Stance: explicit support vs
    refutation from different independence units.
    """
    out: list[Contradiction] = []

    # numeric — one value per independence unit (first stated wins; the
    # provenance of that exact item is carried on the side).
    by_ikey: dict[str, EvidenceItem] = {}
    for it in group.items:
        if it.values:
            by_ikey.setdefault(it.indep_key, it)
    voices = [(k, it) for k, it in by_ikey.items()]
    for i in range(len(voices)):
        for j in range(i + 1, len(voices)):
            ka, ia = voices[i]
            kb, ib = voices[j]
            va, vb = max(ia.values, key=abs), max(ib.values, key=abs)
            denom = max(abs(va), abs(vb))
            if denom > 0 and abs(va - vb) / denom > rel_tolerance:
                out.append(Contradiction(
                    claim=group.claim, kind="numeric",
                    sides=[
                        {"value": va, "sources": [ia.source_name],
                         "indep_keys": [ka],
                         "provenance": [{"sha256": ia.content_sha256,
                                         "url": ia.url}]},
                        {"value": vb, "sources": [ib.source_name],
                         "indep_keys": [kb],
                         "provenance": [{"sha256": ib.content_sha256,
                                         "url": ib.url}]},
                    ],
                    what_would_settle_it=_settle_suggestion(
                        "numeric", group.claim),
                    severity="MAJOR" if abs(va - vb) / denom > 0.5
                    else "MINOR",
                ))

    # stance
    sup = {it.indep_key: it for it in group.items if it.stance == "supports"}
    ref = {it.indep_key: it for it in group.items if it.stance == "refutes"}
    if sup and ref and not (set(sup) & set(ref)):
        sa, ra = next(iter(sup.values())), next(iter(ref.values()))
        out.append(Contradiction(
            claim=group.claim, kind="stance",
            sides=[
                {"stance": "supports", "sources": [sa.source_name],
                 "indep_keys": sorted(sup),
                 "provenance": [{"sha256": x.content_sha256, "url": x.url}
                                for x in sup.values()]},
                {"stance": "refutes", "sources": [ra.source_name],
                 "indep_keys": sorted(ref),
                 "provenance": [{"sha256": x.content_sha256, "url": x.url}
                                for x in ref.values()]},
            ],
            what_would_settle_it=_settle_suggestion("stance", group.claim),
            severity="MAJOR",
        ))
    return out


# ── 3. Confidence from agreement structure ────────────────────────────────

#: share of the source-class ceiling granted for a SINGLE independent voice.
_SINGLE_VOICE_FRACTION = 0.7
#: additional share of the ceiling per extra DISTINCT independent voice.
_PER_EXTRA_VOICE = 0.15


def confidence_from_agreement(group: ClaimGroup,
                              contradictions: Iterable[Contradiction] = (),
                              ) -> tuple[float, list[str]]:
    """(score, reasons) for one claim group.

    Invariants (property-tested):
      * never above MAX_CONFIDENCE_BY_SOURCE[best source class] —
        corroboration raises only WITHIN what provenance permits;
      * non-decreasing in the number of DISTINCT independent agreeing
        sources, and 10 items from one independence unit score exactly
        like 1;
      * any contradiction strictly lowers the score and caps at the
        SPECULATIVE band (0.54).
    """
    ceiling = MAX_CONFIDENCE_BY_SOURCE.get(group.best_class, 0.55)
    reasons: list[str] = [
        f"ceiling {ceiling:.2f} from provenance-assigned class "
        f"{group.best_class}"]
    n_indep = group.independent_sources
    # S5: a claim with no content words cannot corroborate anything —
    # there is nothing for independent sources to agree ON. Score 0,
    # regardless of how many voices repeat the nothing.
    if not has_content_words(group.claim):
        return 0.0, ["vacuous claim (no content words): agreement over "
                     "nothing is not evidence — score held at 0"]
    frac = min(1.0, _SINGLE_VOICE_FRACTION
               + _PER_EXTRA_VOICE * max(0, n_indep - 1))
<<<<<<< HEAD
    # F5/F4c: corroboration is counted per PROVENANCE CLASS, never pooled.
    # Under the previous formula every voice borrowed the group MAX class's
    # ceiling, so one PRIMARY item let INFERRED gossip lift a mixed group to
    # VERIFIED (1.0). Each class earns credit only within its own ceiling:
    #   score = max over classes of ceiling(class) * frac(class_voices).
    # Weak voices still corroborate each other (within INFERRED's 0.55), but
    # they cannot spend a strong member's headroom, and a strong member alone
    # cannot spend the voices of the weak.
    by_class: dict[str, set[str]] = {}
    for it in group.items:
        by_class.setdefault(it.source_class if it.source_class in _CLASS_RANK
                            else "INFERRED", set()).add(it.indep_key)
    score = 0.0
    for cls, ikeys in by_class.items():
        cls_voices = len(ikeys)
        cls_frac = min(1.0, _SINGLE_VOICE_FRACTION
                       + _PER_EXTRA_VOICE * max(0, cls_voices - 1))
        cls_ceiling = MAX_CONFIDENCE_BY_SOURCE.get(cls, 0.55)
        score = max(score, floor_conf(cls_ceiling * cls_frac))
        reasons.append(
            f"class {cls}: {cls_voices} independent voice(s) -> "
            f"{cls_frac:.0%} of its {cls_ceiling:.2f} ceiling")
=======
    score = floor_conf(ceiling * frac)
>>>>>>> origin/build/dd-decomposition-diversity
    reasons.append(
        f"{n_indep} independent source(s) agree overall -> "
        f"{frac:.0%} of best-class ceiling (per-class accounting governs)")
    if n_indep == 1 and len(group.items) > 1:
        reasons.append(
            f"{len(group.items)} items but ONE independence unit "
            f"({sorted(group.indep_keys)[0]}): volume is not corroboration")

    contradictions = list(contradictions)
    if contradictions:
        cap = _SPECULATIVE_CAP
        score = min(score, cap)
        reasons.append(
            f"live contradiction ({contradictions[0].kind}) caps at "
            f"SPECULATIVE {cap}: disagreement is surfaced, not averaged")
    return floor_conf(min(score, ceiling)), reasons


# ── 4. Structured extraction table ───────────────────────────────────────


@dataclass
class TableRow:
    claim: str
    value: Optional[float]
    unit: str
    source_name: str
    indep_key: str
    source_class: str
    provenance: dict            # {sha256, url}

    def to_dict(self) -> dict:
        return {"claim": self.claim, "value": self.value, "unit": self.unit,
                "source": self.source_name, "independence": self.indep_key,
                "source_class": self.source_class,
                "provenance": self.provenance}


def extraction_table(items: Iterable[EvidenceItem]) -> list[TableRow]:
    """One row per (item, stated value), provenance attached per cell."""
    rows: list[TableRow] = []
    for it in items:
        units = it.value_units or ("",)
        for n, v in enumerate(it.values):
            rows.append(TableRow(
                claim=it.claim, value=v,
                unit=units[n] if n < len(units) else "",
                source_name=it.source_name, indep_key=it.indep_key,
                source_class=it.source_class,
                provenance={"sha256": it.content_sha256, "url": it.url}))
    return rows


# ── 5. Honest nulls ──────────────────────────────────────────────────────

NULL_LITERATURE = "literature_null"       # the literature does not address it
NULL_RETRIEVAL = "retrieval_failure"     # we failed to retrieve it


@dataclass
class NullVerdict:
    status: str                 # NULL_LITERATURE | NULL_RETRIEVAL
    explanation: str
    rejected: list[dict] = field(default_factory=list)

    @property
    def is_honest_null(self) -> bool:
        return self.status == NULL_LITERATURE

    def to_dict(self) -> dict:
        return {"status": self.status, "explanation": self.explanation,
                "rejected": self.rejected}


def classify_null(trace: Any) -> NullVerdict:
    """Distinguish 'not addressed' from 'failed to retrieve'.

    DELEGATES to tools.gaps.classify_null_kind — the membership rule exists
    exactly once (tools/gaps.py is the canonical classifier; this module's
    earlier private copy drifted). The NullVerdict vocabulary is kept as a
    thin adapter for existing callers:

      NULL_LITERATURE == gaps.NullKind.HONEST_NULL
      NULL_RETRIEVAL  == gaps.NullKind.RETRIEVAL_FAILURE

    A retrieval failure NEVER reads as an honest null: the explanation says
    so in prose, and `status` carries the machine-checkable verdict.
    """
    from tools.gaps import classify_null_kind, NullKind

    kind, expl = classify_null_kind(trace)
    status = (NULL_LITERATURE if kind == NullKind.HONEST_NULL.value
              else NULL_RETRIEVAL)
    rejected = list(getattr(trace, "rejected", None) or [])
    return NullVerdict(
        status=status,
        explanation=expl,
        rejected=[{"source": r.source_name, "reason": r.reason,
                   "relevance": r.relevance_score} for r in rejected])


# ── The report ───────────────────────────────────────────────────────────


@dataclass
class SynthesisReport:
    """The structured object a conclusion is reasoned over (and shipped)."""
    question: str
    groups: list[ClaimGroup] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    table: list[TableRow] = field(default_factory=list)
    group_confidences: dict[str, tuple[float, list[str]]] = field(
        default_factory=dict)     # claim text -> (score, reasons)
    nulls: dict[str, NullVerdict] = field(default_factory=dict)  # qid -> verdict
    #: overall score: best group score, capped by any live contradiction.
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def max_independent_agreement(self) -> int:
        return max((g.independent_sources for g in self.groups), default=0)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "n_claims": len(self.groups),
            "max_independent_agreement": self.max_independent_agreement,
            "groups": [
                {"claim": g.claim,
                 "n_items": len(g.items),
                 "independent_sources": g.independent_sources,
                 "best_class": g.best_class,
                 "confidence": self.group_confidences.get(g.claim, (0.0, []))[0],
                 "confidence_reasons":
                     self.group_confidences.get(g.claim, (0.0, []))[1]}
                for g in self.groups],
            "contradictions": [c.to_dict() for c in self.contradictions],
            "table": [r.to_dict() for r in self.table],
            "nulls": {k: v.to_dict() for k, v in self.nulls.items()},
            "confidence": self.confidence,
            "notes": self.notes,
        }


def synthesize(question: str,
               items: Iterable[EvidenceItem],
               null_traces: Optional[dict[str, Any]] = None,
               rel_tolerance: float = 0.10,
               ) -> SynthesisReport:
    """Full cross-source synthesis over one question's evidence.

    Deterministic and model-free: the model reasons ON the returned
    report; this function establishes what the evidence structure
    actually is. ``null_traces`` maps question_id -> RetrievalTrace for
    leaves that came back empty, so their nulls are classified honestly.
    """
    items = list(items)
    rep = SynthesisReport(question=question)
    rep.groups = triangulate(items)
    for g in rep.groups:
        cons = detect_contradictions(g, rel_tolerance=rel_tolerance)
        rep.contradictions.extend(cons)
        rep.group_confidences[g.claim] = confidence_from_agreement(g, cons)
    rep.table = extraction_table(items)
    for qid, trace in (null_traces or {}).items():
        rep.nulls[qid] = classify_null(trace)
        rep.notes.append(f"leaf {qid}: {rep.nulls[qid].status}")
    if rep.groups:
        rep.confidence = max(
            s for s, _ in rep.group_confidences.values())
    if rep.contradictions:
        rep.confidence = min(rep.confidence, _SPECULATIVE_CAP)
        rep.notes.append(
            f"{len(rep.contradictions)} live contradiction(s): overall "
            f"confidence capped at SPECULATIVE {_SPECULATIVE_CAP}")
    rep.confidence = floor_conf(max(rep.confidence, 0.0))
    return rep
