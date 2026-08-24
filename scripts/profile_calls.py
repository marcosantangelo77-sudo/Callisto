"""PERF — instrumented end-to-end run: the CALL TABLE.

Produces the measurement brief's table for one full pipeline run offline:
which model call fired, in what role, what it accomplished, tokens in/out
(chars/4 estimate), duration. Runs against fixture transport + scripted
responses so it is deterministic and repeatable; the STRUCTURE of calls
(decompose / N leaf answers / 1 adversary) is the production structure
because the real engine is what runs.

Usage:
  python3 scripts/profile_calls.py [--leaves 5] [--json out.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agp import Domain  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.cache import CountingModel  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tests.test_speed_parallel_leaves import (  # noqa: E402
    ROUTES,
    _Adversary,
    _answer,
    _decompose,
)


class ProfileScripted(ScriptedModel):
    name = "scripted-profile"


def profile_run(n_leaves: int = 5) -> dict:
    """One full offline pipeline run with per-call accounting."""
    inner = ProfileScripted({"Architect": [_decompose(n_leaves)]},
                            default=_answer(0.7))
    model = CountingModel(
        inner,
        purpose_of=lambda role, prompt: (
            "decompose root question" if role == "Architect"
            else "leaf answer synthesis"))
    ledger = ProvenanceLedger()
    with tempfile.TemporaryDirectory() as td:
        pipeline = ResearchPipeline(
            model=model, adversary_router=_Adversary(),
            transport=fixture_transport(dict(ROUTES)),
            store=ArtifactStore(root=str(Path(td) / "art")),
            ledger=ledger)
        result = asyncio.run(pipeline.run(
            "Will Apple report quarterly results above Wall Street consensus "
            "expectations in its next earnings report?",
            domain=Domain.FINANCIAL, today=date(2026, 8, 22)))
    return {"sealed": result.sealed,
            "confidence": result.confidence_score,
            "n_leaves": len(result.leaves),
            "n_fetches": len(result.fetches),
            "counting": model.summary(),
            "rows": model.rows}


def render(p: dict) -> str:
    hdr = (f"{'#':>3} {'role':<10} {'purpose':<26} "
           f"{'in_tok':>7} {'out_tok':>7}")
    lines = ["", f"sealed={p['sealed']} conf={p['confidence']} "
             f"leaves={p['n_leaves']} fetches={p['n_fetches']}", "",
             "CALL TABLE (offline deterministic run)", "", hdr,
             "-" * len(hdr)]
    for i, r in enumerate(p["rows"]):
        lines.append(f"{i:>3} {r['role']:<10} {r['purpose'][:26]:<26} "
                     f"{r['in_tokens_est']:>7} {r['out_tokens_est']:>7}")
    c = p["counting"]
    lines.append("-" * len(hdr))
    lines.append(f"TOTAL {c['calls']} calls · "
                 f"in_tokens≈{c['in_tokens_est']} · "
                 f"out_tokens≈{c['out_tokens_est']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaves", type=int, default=5)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    p = profile_run(args.leaves)
    print(render(p))
    if args.json:
        Path(args.json).write_text(json.dumps(p, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
