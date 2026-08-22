"""LIVE CHECK: run the pipeline end-to-end with Ox Alpha as the ONLY provider,
through ProviderRouter (backend=hermes_cli) — not the HermesCliModel shim.

This is the `git clone` + `hermes portal login` scenario: no local model
server, no API keys. Requires `hermes` on PATH / ~/.hermes/bin/hermes and an
active portal session.

    python3 scripts/oxa_live_check.py ["optional research question"]
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OX_ONLY_CFG = """
default_tier: ox_alpha
providers:
  ox_alpha:
    backend: hermes_cli
    model: ox-alpha
    context_tokens: 128000
    structured_output: false
    tool_calls: false
    max_concurrency: 1
    cost_per_1k_input: 0.0
    cost_per_1k_output: 0.0
routing:
  task_classes:
    hypothesis_generation: ox_alpha
    research_synthesis: ox_alpha
    screening: ox_alpha
    extraction: ox_alpha
    classification: ox_alpha
    backtest_interpretation: ox_alpha
    promotion_judgment: ox_alpha
    adversarial_review: ox_alpha
"""

TASK_CLASSES = [
    "hypothesis_generation", "research_synthesis", "screening", "extraction",
    "classification", "backtest_interpretation", "promotion_judgment",
    "adversarial_review",
]

QUESTION_DEFAULT = (
    "Did the U.S. federal minimum wage increase to $7.25/hour in July 2009, "
    "and what documented effect did that have on teen employment?"
)


async def main() -> int:
    from inference import ProviderRouter
    from tools.pipeline.engine import ResearchPipeline
    from tools.pipeline.model import RouterModel

    cfg_path = Path(tempfile.mkdtemp(prefix="oxa_live_")) / "ox_only.yaml"
    cfg_path.write_text(OX_ONLY_CFG)

    router = ProviderRouter(config_path=str(cfg_path))
    ep = router.endpoints["ox_alpha"]
    print(f"endpoint : {ep.name} backend={ep.backend} model={ep.model}")
    print(f"caps     : structured_output={ep.structured_output} "
          f"tool_calls={ep.tool_calls} max_concurrency={ep.max_concurrency}")

    health = await router.check_health("ox_alpha")
    print(f"health   : {health['status']}")
    if health["status"] != "ok":
        print("FATAL: hermes CLI not available — is it installed/logged in?")
        return 2

    # Capability smoke: one routed completion through the CLI backend.
    t0 = asyncio.get_event_loop().time()
    smoke = await router.complete(
        "classification",
        [{"role": "user", "content":
            'Classify into FINANCIAL/TECHNICAL/SIGNAL/SYNTHESIS/GENERAL. '
            'Reply with JSON only: {"domain": "..."} — input: "Is Bitcoin '
            'a good buy?"'}],
        timeout=240.0)
    dt = asyncio.get_event_loop().time() - t0
    print(f"smoke    : tier={smoke['tier']} model={smoke['model']} "
          f"latency={dt:.1f}s parsed={json.dumps(smoke['parsed_json'])[:120]}")

    # Full pipeline: RouterModel(ProviderRouter) — the first-class path.
    model = RouterModel(router)
    engine = ResearchPipeline(model=model, adversary_router=router)
    question = sys.argv[1] if len(sys.argv) > 1 else QUESTION_DEFAULT
    print(f"question : {question}")
    result = await engine.run(question)
    print("=" * 70)
    print(f"sealed   : {result.sealed}")
    if result.refusal_reason:
        print(f"refusal  : {result.refusal_reason}")
    if getattr(result, "conclusion", None):
        print(f"conf     : {result.confidence_score} tier={result.confidence_tier}")
    print(f"leaves   : {len(result.leaves)}")
    for leaf in result.leaves:
        ans = (leaf.answer or "")[:220].replace("\n", " ")
        print(f"  - [{leaf.tier} {leaf.confidence}] {leaf.text[:80]}")
        print(f"    {ans}")
    if result.notes:
        print(f"notes    : {'; '.join(result.notes)[:400]}")
    print(f"cost     : {json.dumps(router.cost_ledger.snapshot()['by_tier'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
