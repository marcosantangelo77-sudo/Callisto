"""R5 — Reference-class first.

Before reasoning about a specific claim, find the base rate for its class.
Largest single accuracy gain in the forecasting literature (Tetlock): the
reference-class rate is the STARTING point for a forecast, and specific
evidence moves you away from it only with a recorded justification.

Related but distinct from tools/resolvers/base_rates.py — that file holds
GATE thresholds for lifecycle promotion. This is the research-side
counterpart: given a claim's text and tags, identify its reference class and
return the class's base rate with an honest sample size.

Domain-general by construction: classes are matched on structural features
of the claim (domain tags, event-type tokens), never on subject matter.
A "Phase 3 trial success" claim about any molecule uses the same class as
any other; a supply-chain disruption claim likewise.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Built-in reference classes — domain-general event types with literature
# base rates. These are STARTING POINTS to be overridden by empirical data
# when a class has resolved outcomes of its own.
# ---------------------------------------------------------------------------

BUILTIN_CLASSES: dict[str, dict] = {
    # key -> {description, base_rate, n_note, source, keywords}
    "clinical_trial_phase3": {
        "base_rate": 0.49,
        "source": "BIO/Informa/QLS Clinical Development Success Rates (2011-2020)",
        "keywords": ["phase 3", "phase iii", "trial", "fda approval", "drug",
                     "molecule", "indication"],
    },
    "clinical_trial_phase2": {
        "base_rate": 0.29,
        "source": "BIO/Informa/QLS (Ph2 -> approval)",
        "keywords": ["phase 2", "phase ii", "trial"],
        "excludes": ["phase 3", "phase iii"],   # a Ph3 claim is not a Ph2 claim
    },
    "earnings_beat": {
        "base_rate": 0.50,
        "source": "roughly balanced by construction; override empirically",
        "keywords": ["beat earnings", "eps above", "revenue miss", "guidance"],
    },
    "ma_deal_completion": {
        "base_rate": 0.90,
        "source": "announced-deal completion rates run high; regulatory block is the tail",
        "keywords": ["acquisition", "merger", "deal close", "deal", "takeover",
                     "regulatory approval of deal"],
    },
    "startup_default_within_5y": {
        "base_rate": 0.60,
        "source": "venture-backed firm failure rates within 5 years",
        "keywords": ["startup fail", "company default", "bankruptcy", "insolvency"],
    },
    "supply_chain_disruption": {
        "base_rate": 0.30,
        "source": "annual likelihood a major supply chain suffers a reportable disruption",
        "keywords": ["supply chain", "shortage", "disruption", "delay in delivery",
                     "shipping", "logistics"],
    },
    "geo_political_conflict_escalation": {
        "base_rate": 0.20,
        "source": "historical escalation rates of militarized disputes",
        "keywords": ["war", "invasion", "conflict", "escalation", "sanctions"],
    },
    "tech_prediction_slip": {
        "base_rate": 0.75,
        "source": "announced technology timelines slip more often than they land on time",
        "keywords": ["will ship", "launch by", "release by", "deployment by", "milestone"],
    },
}


@dataclass
class ReferenceClass:
    """The identified class for a claim plus its honest starting point."""

    class_key: str
    description: str
    base_rate: float
    source: str
    n: int                      # empirical resolved-outcome count behind the rate
    empirical: bool             # True = computed from this system's outcomes
    match_score: float          # keyword-overlap strength of the identification
    note: str = ""

    def summary(self) -> dict:
        return {
            "class_key": self.class_key,
            "base_rate": self.base_rate,
            "n": self.n,
            "empirical": self.empirical,
            "source": self.source,
            "match_score": round(self.match_score, 4),
            "note": self.note,
        }


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def classify_claim(
    claim_text: str,
    domain_tags: Optional[list[str]] = None,
) -> Optional[tuple[str, float]]:
    """Best-matching built-in class and its match score.

    Score = fraction of the class's keywords present in the claim text
    (case-insensitive phrase matching). Returns None below a floor — refusing
    to name a class is more honest than guessing one.
    """
    text = claim_text.lower()
    best_key, best_score = None, 0.0
    for key, spec in BUILTIN_CLASSES.items():
        kws = spec["keywords"]
        hits = sum(1 for kw in kws if re.search(r"\b" + re.escape(kw) + r"\b", text))
        score = hits / len(kws)
        # A class whose exclusion terms appear in the claim cannot be it
        # (e.g. "phase iii" rules out the phase-2 class).
        if any(re.search(r"\b" + re.escape(x) + r"\b", text)
               for x in spec.get("excludes", [])):
            score = 0.0
        if score > best_score:
            best_key, best_score = key, score
    if best_score < 0.25:
        return None
    return best_key, best_score


# ---------------------------------------------------------------------------
# Empirical layer: resolved outcomes recorded by THIS system override the
# literature priors. Stored as JSON at $CALLISTO_REFCLASS_DB or
# <repo>/data/reference_classes.json: {class_key: [1,0,1,...]} outcome list.
# ---------------------------------------------------------------------------

def _db_path() -> str:
    env = os.environ.get("CALLISTO_REFCLASS_DB", "").strip()
    if env:
        return env
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "reference_classes.json",
    )


def record_outcome(class_key: str, positive: bool) -> int:
    """Append one resolved outcome (1/0) to the class's empirical record."""
    path = _db_path()
    db = {}
    try:
        with open(path) as f:
            db = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        db = {}
    outcomes = db.get(class_key, [])
    outcomes.append(1 if positive else 0)
    db[class_key] = outcomes
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(db, f)
    os.replace(tmp, path)
    return len(outcomes)


def empirical_base_rate(class_key: str, min_n: int = 5) -> Optional[tuple[float, int]]:
    """Empirical rate when the class has at least `min_n` resolved outcomes."""
    try:
        with open(_db_path()) as f:
            outcomes = json.load(f).get(class_key, [])
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    n = len(outcomes)
    if n < min_n:
        return None
    return sum(outcomes) / n, n


def wilson_interval(p_hat: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — honest uncertainty on a proportion, well-behaved
    near 0 and 1 where the normal approximation is not."""
    if n <= 0:
        return (0.0, 1.0)
    denom = 1 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denom
    half = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def reference_class_first(
    claim_text: str,
    domain_tags: Optional[list[str]] = None,
) -> Optional[ReferenceClass]:
    """The full R5 entry point: classify the claim and return its starting
    probability. Empirical outcomes override literature priors whenever the
    class has enough resolved history; otherwise the literature rate stands,
    explicitly marked non-empirical so downstream confidence ceilings can
    treat it accordingly."""
    hit = classify_claim(claim_text, domain_tags)
    if hit is None:
        return None
    key, score = hit
    spec = BUILTIN_CLASSES[key]

    emp = empirical_base_rate(key)
    if emp is not None:
        p_hat, n = emp
        lo, hi = wilson_interval(p_hat, n)
        return ReferenceClass(
            class_key=key,
            description=key.replace("_", " "),
            base_rate=p_hat,
            source="empirical: this system's resolved outcomes",
            n=n,
            empirical=True,
            match_score=score,
            note=f"Wilson 95% CI [{lo:.3f}, {hi:.3f}]",
        )

    return ReferenceClass(
        class_key=key,
        description=key.replace("_", " "),
        base_rate=spec["base_rate"],
        source=spec["source"],
        n=0,
        empirical=False,
        match_score=score,
        note="literature prior — no empirical outcomes recorded yet; "
             "treat as SPECULATIVE until this class accumulates resolutions",
    )


def adjust_from_reference(
    reference: ReferenceClass,
    evidence_shift: float,
) -> dict:
    """Apply a reasoned shift AWAY from the reference-class rate, logged.

    The superforecasting discipline: start at the outside view, then move
    inside only for claim-specific evidence, with the movement explicit.
    Returns the adjusted probability and the audit row.
    """
    if not -0.9 < evidence_shift < 0.9:
        raise ValueError("evidence_shift must stay within (-0.9, 0.9)")
    p = min(max(reference.base_rate + evidence_shift, 0.01), 0.99)
    return {
        "starting_rate": reference.base_rate,
        "evidence_shift": evidence_shift,
        "adjusted_prob": p,
        "empirical_start": reference.empirical,
        "note": "inside-view adjustment applied to reference-class starting "
                "point; both values recorded for later calibration scoring",
    }
