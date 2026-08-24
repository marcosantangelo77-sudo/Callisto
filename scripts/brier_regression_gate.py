"""Brier regression gate — the five scored data/retro_batch/ questions.

Runs each question through PipelineResearcher OFFLINE (scripted model,
fixture transport, no network) on the CURRENT code and on a pristine
checkout of the pre-change base commit, then compares per-question
predictions and mean Brier. A speed change must not move either.
"""
import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO = Path('/Users/marcosantangelo/callisto-wt/loop')
BASE = '/tmp/base_check'   # clean worktree at b75590a (pre-change)


def run_brier(repo_root):
    repo_root = Path(repo_root)
    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(repo_root / 'scripts'))
    for m in ['inference', 'tools.pipeline.retro', 'tools.retrodiction.scoring',
              'scripts.retro_questions_i4']:
        sys.modules.pop(m, None)
    from tools.pipeline.retro import PipelineResearcher
    from scripts.retro_questions_i4 import load_set
    questions = load_set(str(Path(repo_root) / 'data/retro_batch/questions.json'))
    questions = questions[:5]
    assert len(questions) == 5, len(questions)

    # Scripted model + fixture transport: fully offline, deterministic.
    from tests.helpers.no_socket import NoSocket
    _ns = NoSocket(); _ns.install()

    from tools.pipeline.engine import ResearchPipeline, fixture_transport
    from tools.pipeline.model import ScriptedModel
    from agp.provenance import ProvenanceLedger
    from tools.artifacts import ArtifactStore

    OPENALEX_BODY = json.dumps({"results": [
        {"id": "W1", "title": "Scholarly study on macro indicator revisions: "
         "evidence for the question under test",
         "publication_year": 2024, "cited_by_count": 12}]})
    ROUTES = {"/works": OPENALEX_BODY}

    decompose = json.dumps({"sub_questions": [
        {"text": f"leaf {i}: what does the evidence indicate",
         "kind": "descriptive", "question_type": "scholarly work search",
         "min_source_tier": 1, "min_independent_sources": 1,
         "quant_required": False, "horizon_days": None}
        for i in range(3)]})

    def answer(p):
        return json.dumps({"answer": "the evidence is inconclusive",
                           "proposed_confidence": 0.5, "stance": "UNDETERMINED",
                           "compute": None})

    class OfflineResearcher(PipelineResearcher):
        def __init__(self):
            model = ScriptedModel({})
            model.script("Architect", decompose)
            model.script("Manager", *[{"content": answer(0.5)}] * 64)
            super().__init__(model=model, routes=ROUTES,
                             adversary_router=None,
                             claim_date=date(2024, 1, 3))

    researcher = OfflineResearcher()
    prompts = [q.prompt_for_researcher() for q in questions]
    preds = asyncio.run(researcher.answer_async(prompts, [], loops=1))

    from tools.retrodiction.scoring import score_brier
    brier = score_brier(preds, questions)
    out = {"brier": round(float(brier), 9),
           "predictions": [{"question_id": p.question_id,
                            "probability": round(float(p.probability), 9)}
                           for p in preds]}
    sys.path.remove(str(repo_root))
    sys.path.remove(str(repo_root / 'scripts'))
    return out


def main():
    cur = run_brier(REPO)
    base = run_brier(BASE)
    print("CURRENT:", json.dumps(cur, sort_keys=True))
    print("BASELINE:", json.dumps(base, sort_keys=True))
    same = (cur["brier"] == base["brier"]
            and cur["predictions"] == base["predictions"])
    print("MATCH:", same)
    if not same:
        print("REGRESSION: predictions or Brier moved — revert")
        return 1
    print(f"OK: Brier {cur['brier']} byte-identical across the change")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
