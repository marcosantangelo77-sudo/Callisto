"""The published smoke5 measurement, reproduced offline and deterministically.

data/retro_batch/report_smoke5.json committed as an all-null error run (the
batch driver's event-loop bug). The MEASUREMENT quoted in the diagnosis brief
— mean_brier 0.3129, bin [0.2,0.4) predicted 0.33 realised 0.60,
beat_market_rate 0.40, mean_edge_taken -0.31 — is arithmetically EXACT for
five predictions all at p=0.33 against outcomes T,T,F,F,T:

    Brier   = (3*(1-.33)^2 + 2*.33^2)/5          = 0.31290
    mean_p  = 0.33 ; realised = 3/5               = 0.60
    market  = (0.72+0.88+0.35+0.45+0.80)/5 = 0.64 ; edge = 0.33-0.64 = -0.31
    beat_market: Boeing+Tesla only                = 2/5  = 0.40

So every question produced confidence ~0.34 on the NO side of 0.5. This
module scripts exactly that behaviour class — cautious researcher, thin
evidence, hedged answers, four adversary objections — so the instrumentation
can be developed and tested offline against the same signature the live run
produced. Raw estimates are REPRESENTATIVE ASSUMPTIONS (documented below),
not observations; the mechanism magnitudes around them are measured by this
package, not asserted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tools.pipeline.model import PipelineModel, ScriptedModel
from tools.pipeline.retro import _AdversaryRouterStub
from tools.retrodiction.questions import RetrodictionQuestion, QuestionType

REPO = Path(__file__).resolve().parents[2]

#: The five questions actually run in the published measurement.
SMOKE5_IDS = ["c1672ca2fb5444c9", "5b03b4a3a36c4be0", "ca8b500dd9b1417e",
              "f5de4cd259114c7f", "7317d0d47f554e38"]

#: Representative RAW model estimates (pre-clamp) and the model's own
#: P(True) per question. ASSUMPTIONS for offline reproducibility — chosen
#: inside the band a cautious-but-directionally-informed researcher shows;
#: swap in captured live proposals when available (the package consumes any
#: (estimate, p_hat) pairs).
SIGNATURE_ESTIMATES = {
    # qid: (proposed_confidence, p_model_true)
    "c1672ca2fb5444c9": (0.80, 0.72),   # Apple      -> TRUE
    "5b03b4a3a36c4be0": (0.85, 0.70),   # Nvidia     -> TRUE
    "ca8b500dd9b1417e": (0.75, 0.40),   # Boeing     -> FALSE
    "f5de4cd259114c7f": (0.78, 0.38),   # Tesla      -> FALSE
    "7317d0d47f554e38": (0.80, 0.68),   # Microsoft  -> TRUE
}

#: The four MINOR objections shape observed in the live sealed-at-0.34 run.
FOUR_MINOR_OBJECTIONS = [
    {"kind": "refuting_evidence", "severity": "MINOR",
     "text": "single-source dependence: every admitted item indexes one host"},
    {"kind": "selection_effect", "severity": "MINOR",
     "text": "retrieval may have missed contrary reports before cutoff"},
    {"kind": "false_positive", "severity": "MINOR",
     "text": "string-coincidence selection produced the lone hit"},
    {"kind": "alternative_explanation", "severity": "MINOR",
     "text": "consensus drift explains the pattern without the mechanism"},
]


def load_smoke5_questions(path: Path | None = None
                          ) -> list[RetrodictionQuestion]:
    path = path or (REPO / "data/retro_batch/questions.json")
    raw = json.loads(Path(path).read_text())
    out = []
    for d in raw:
        if d["question_id"] not in SMOKE5_IDS:
            continue
        out.append(RetrodictionQuestion(
            question_id=d["question_id"], text=d["text"],
            domain=d["domain"],
            question_type=QuestionType(d["question_type"]),
            claim_date=date.fromisoformat(d["claim_date"]),
            resolution_date=date.fromisoformat(d["resolution_date"]),
            answer_binary=bool(d["answer_binary"]),
            answer_confidence=float(d.get("answer_confidence", 1.0))))
    out.sort(key=lambda q: SMOKE5_IDS.index(q.question_id))
    return out


def load_markets(path: Path | None = None) -> dict[str, float]:
    path = path or (REPO / "data/retro_batch/questions.json.extras.json")
    return {d["question_id"]: d.get("market_implied")
            for d in json.loads(Path(path).read_text())
            if d["question_id"] in SMOKE5_IDS}


def _hedge(ticker_hint: str, proposed: float) -> str:
    """The cautious wording class live research answers actually take."""
    return json.dumps({
        "answer": (f"The admitted coverage of {ticker_hint} does not "
                   f"establish the claim; no evidence in the admitted items "
                   f"supports a confident yes."),
        "proposed_confidence": proposed,
    })


def make_signature_model(q: RetrodictionQuestion) -> ScriptedModel:
    """Scripted Architect+Manager for ONE question, behaviour-matched to the
    live run: two leaves (one plannable source, one honest-skip), hedged
    answers proposing high confidence."""
    proposed, _ = SIGNATURE_ESTIMATES[q.question_id]
    short = q.text.split()[1] if len(q.text.split()) > 1 else "the subject"
    decomp = json.dumps({"sub_questions": [
        {"text": f"news coverage: {q.text}",
         "kind": "descriptive", "question_type": "event outcome",
         "min_source_tier": 2, "min_independent_sources": 2,
         "quant_required": False, "horizon_days": None},
        {"text": f"market pricing check: {q.text}",
         "kind": "predictive", "question_type": "beat_or_miss",
         "min_source_tier": 2, "min_independent_sources": 2,
         "quant_required": False, "horizon_days": 30},
    ]})
    m = ScriptedModel(default={"content": "{}"})
    m.script("Architect", {"content": decomp})
    m.script("Manager", {"content": _hedge(short, proposed)})
    m.script("Manager", {"content": _hedge(short, proposed)})
    return m


def signature_adversary_router() -> _AdversaryRouterStub:
    return _AdversaryRouterStub(list(FOUR_MINOR_OBJECTIONS))


#: Fixture bodies served to plannable sources. Worded to clear the 25%
#: relevance gate for each question's topical tokens.
def signature_routes() -> dict[str, str]:
    def art(subject: str) -> str:
        return json.dumps({"articles": [
            {"title": f"{subject} quarterly results above Wall Street "
                      f"consensus expectations earnings report",
             "url": f"https://example.com/{subject.lower()}"},
            {"title": f"analysts expect {subject} earnings report event "
                      f"outcome next quarter",
             "url": f"https://example.com/{subject.lower()}-forecast"},
        ]})

    return {
        "gdeltproject": art("Apple Nvidia Boeing Tesla Microsoft"),
        "openalex": json.dumps({"results": [
            {"title": "quarterly earnings consensus expectations study"}]}),
    }
