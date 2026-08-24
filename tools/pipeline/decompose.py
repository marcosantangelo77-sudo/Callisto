"""Decomposition diversity — the Architect proposes questions the registry
can actually serve, and the program is graded on how many DISTINCT
independence families its sub-questions could plausibly reach.

The bottleneck this module exists for, from a live run: five sub-questions
that all wanted scholarly papers produced one fetch family no matter how
many adapters existed. Source diversity needs more than routable adapters —
it needs the decomposer to ask questions DIFFERENT source KINDS can answer.

Design constraints:
  * The registry's own vocabulary (each spec's `answers` clauses) is fed to
    the model so it phrases sub-questions in words selection can match —
    free text like "clinical trials" used to select nothing.
  * `families_reachable` is computed by asking the registry itself which
    sources answer each sub-question and collapsing to independence keys.
    It reports; it never fabricates. A genuinely single-kind question
    scores 1 and that is an honest answer, not a defect.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Sub-questions per decomposition. Five leaves was the live-run shape; a
# wider fan-out multiplies cost without adding independence once families
# repeat, so the cap stays.
MAX_SUB_QUESTIONS = 5


def registry_catalog(registry) -> str:
    """One line per registered source: name + what it answers, in ITS OWN
    vocabulary. This is the menu the Architect picks from, so a proposed
    question_type is matchable by word overlap rather than hope."""
    lines = []
    for spec in registry.specs():
        answers = "; ".join(spec.get("answers") or [])
        lines.append(f"- {spec['name']}: {answers}")
    return "\n".join(lines)


DECOMPOSE_SYSTEM_TEMPLATE = (
    "You are the Architect. Decompose the research question into 2-{max_q} "
    "sub-questions that would settle it.\n\n"
    "DIVERSITY MANDATE: sub-questions must span SOURCE KINDS, not just "
    "facets of one topic. Each sub-question should be answerable by a "
    "DIFFERENT kind of source from the catalog below. For example, for a "
    "supply-chain question good decompositions reach: scholarly literature, "
    "official trade/production statistics, regulatory filings and rules, "
    "patents, market-implied probabilities, and news coverage volume. Five "
    "sub-questions that all want scholarly papers are ONE independent voice "
    "and will be rejected as weak.\n\n"
    "HONESTY CONSTRAINT: do not invent a source kind the question does not "
    "need. If the root question is genuinely answerable only from one kind "
    "of source (e.g. pure literature review), a single-family decomposition "
    "is correct — say so via \"single_family_ok\": true rather than "
    "fabricating a market or news angle.\n\n"
    "AVAILABLE SOURCES (use their vocabulary when phrasing question_type):\n"
    "{catalog}\n\n"
    "Return JSON only: "
    '{{"sub_questions": [{{"text": ..., "kind": "descriptive|causal|predictive", '
    '"question_type": short phrase naming what kind of source answers it, '
    '"min_source_tier": 1-3, "min_independent_sources": int, '
    '"quant_required": bool, "horizon_days": int or null}}], '
    '"single_family_ok": bool}}.'
    "\n\nHARD CONSTRAINT: if kind is \"predictive\", horizon_days MUST be a "
    "positive integer — an undated prediction cannot ever resolve, so it is "
    "rejected. If you cannot name a resolution horizon in days, the question "
    "is not predictive: use \"descriptive\" or \"causal\" instead."
)


def build_decompose_system(registry) -> str:
    return DECOMPOSE_SYSTEM_TEMPLATE.format(
        max_q=MAX_SUB_QUESTIONS,
        catalog=registry_catalog(registry))


@dataclass
class DiversityReport:
    """How many distinct independence families could plausibly answer the
    decomposed program. Computed against the registry, not asserted."""
    n_sub_questions: int = 0
    families: list[str] = field(default_factory=list)
    family_sources: dict[str, list[str]] = field(default_factory=dict)
    single_family_ok: bool = False   # model's own honesty claim
    weak: bool = False               # 1 family AND no honesty claim
    note: str = ""

    @property
    def n_families(self) -> int:
        return len(self.families)

    def to_dict(self) -> dict:
        return {"n_sub_questions": self.n_sub_questions,
                "n_families": self.n_families,
                "families": list(self.families),
                "family_sources": {k: sorted(v) for k, v in
                                   self.family_sources.items()},
                "single_family_ok": self.single_family_ok,
                "weak": self.weak,
                "note": self.note}


def _family_for(registry, name: str) -> tuple[str, str]:
    """(independence_key, base_url) for a registered source name."""
    from tools.pipeline.retrieval import independence_key
    entry = registry.get(name)
    base_url = entry.spec.base_url if entry is not None else name
    return independence_key(name, base_url), base_url


def assess_diversity(registry, question_types: list[str], *,
                     max_tier: int = 5,
                     exclude: set[str] | None = None,
                     single_family_ok: bool = False) -> DiversityReport:
    """Ask the registry which sources could serve EACH sub-question's
    question_type, collapse to independence keys, count distinct families.

    This is a PLANNING-time check: it says whether the decomposition as
    written could ever produce independent corroboration. A program whose
    every sub-question routes to the scholarly-aggregator family has
    n_families == 1 regardless of how many adapters exist — exactly the
    live-run failure. Weak only when the model ALSO did not claim the
    question is honestly single-family; the check reports, it does not
    force diversity where none exists.
    """
    fams: dict[str, set[str]] = {}
    seen_sources: set[str] = set()
    for qt in question_types:
        if not qt:
            continue
        for spec in registry.select(qt, max_tier=max_tier,
                                    exclude=(exclude or set()) | seen_sources):
            key, _ = _family_for(registry, spec.name)
            fams.setdefault(key, set()).add(spec.name)
            seen_sources.add(spec.name)
    rep = DiversityReport(
        n_sub_questions=len([q for q in question_types if q]),
        families=sorted(fams),
        family_sources={k: set(v) for k, v in fams.items()},
        single_family_ok=bool(single_family_ok))
    if rep.n_families <= 1:
        if rep.single_family_ok:
            rep.note = ("decomposition reaches 1 independence family, which "
                        "the Architect declared honest for this question")
        else:
            rep.weak = True
            rep.note = (
                "WEAK decomposition: all sub-questions route to at most 1 "
                "independence family; no cross-source corroboration is "
                "possible as written")
    else:
        rep.note = (f"decomposition spans {rep.n_families} distinct "
                    f"independence families")
    return rep


def assess_program_diversity(registry, program, question_types: dict,
                             *, single_family_ok: bool = False
                             ) -> DiversityReport:
    """Convenience over assess_diversity for a decomposed ResearchProgram:
    reads each leaf's stored free-text question_type (the same strings the
    fetch stage routes on) and returns the family-reachability report.
    Callers append rep.note to the pipeline result notes."""
    qts = [question_types.get(q.question_id) or "" for q in program.leaves]
    return assess_diversity(registry, qts,
                            single_family_ok=single_family_ok)
