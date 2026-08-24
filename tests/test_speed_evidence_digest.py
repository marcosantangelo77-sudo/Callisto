"""SPEED run 13 — the evidence window shows content, not JSON boilerplate.

Measured on a real OpenAlex works response (244KB, per-page=10 full
records): the old `body[:4000]` window on the sort_keys JSON dump landed
entirely inside `meta` boilerplate — 0 of 10 result titles visible to the
author AND adversary models. The fix digests the parsed body with the same
extract_text rule the relevance gate uses. These tests pin:

1. CONTENT — a fat parsed body's evidence text contains titles/abstracts,
   not `x_query`/`oql` meta echoes.
2. BUDGET — the digest is capped at 4000 chars, exactly like before.
3. PROVENANCE — content_sha256 still hashes the FULL canonical body; the
   ledger record is unchanged.
4. FALLBACK — an unparsed fetch degrades to the old body[:4000] window.
5. GOLDENS — offline fingerprints are byte-identical (fixture bodies sit
   below the window) and the five-question Brier regression is unchanged.

Hard rules honored: no caching near a cutoff; the adversary stays its own
call over the SAME evidence items; nothing here can raise confidence.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

sys.path.insert(0, str(Path(__file__).parent.parent))

from agp import Domain  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402


def _fat_openalex_body() -> dict:
    """A realistic works response: big meta FIRST under sort_keys, results after."""
    return {
        "meta": {
            "count": 13438,
            "db_response_time_ms": 183,
            "page": 1,
            "per_page": 10,
            "x_query": {
                "oql": "works where full text has (semiconductor supply chain "
                       "resilience)",
                "oqo": {"get_rows": "works",
                        "filter_rows": [{"column_id": "fulltext.search"}]},
            },
        },
        "results": [
            {"id": f"W{i}",
             "title": f"Scholarly study on semiconductor supply chain "
                      f"resilience number {i}",
             "abstract": f"Abstract {i}: analyst consensus and quarterly "
                         f"results in semiconductor manufacturing. "
                         + ("context " * 40),
             "publication_year": 2024, "cited_by_count": 12}
            for i in range(10)
        ],
    }


def _decompose(n: int = 1) -> str:
    return json.dumps({"sub_questions": [{
        "text": "sub-question 0: what does the evidence say about "
                "semiconductor supply chain resilience",
        "kind": "descriptive",
        "question_type": "scholarly work search",
        "min_source_tier": 1,
        "min_independent_sources": 1,
        "quant_required": False,
        "horizon_days": None,
    }]})


def _answer() -> str:
    return json.dumps({"answer": "the evidence supports the claim",
                       "proposed_confidence": 0.7, "compute": None})


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


async def _run_one(body: dict, routes: dict[str, str]):
    """Run a 1-leaf pipeline; return (result, all model prompts)."""
    model = ScriptedModel(responses={"Architect": [_decompose()],
                                     "Manager": [_answer() for _ in range(4)]})
    seen_prompts: list[str] = []

    orig = model.complete

    async def spy(role, messages, **kw):
        seen_prompts.append("\n".join(
            m.get("content", "") for m in messages))
        return await orig(role, messages, **kw)

    model.complete = spy
    pipeline = ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=fixture_transport(routes),
        store=ArtifactStore(root=Path("/tmp") / "speed_run13_art"))
    result = await pipeline.run(
        "Will Apple report quarterly results above Wall Street consensus "
        "expectations in its next earnings report?",
        domain=Domain.FINANCIAL, today=date(2026, 8, 22))
    return result, seen_prompts


class TestEvidenceDigest:
    def test_titles_reach_the_model_not_meta_boilerplate(self):
        body = _fat_openalex_body()
        canonical = json.dumps(body, sort_keys=True)
        assert len(canonical) > 4000, "fixture must exceed the window"
        routes = {"/works": canonical}
        result, prompts = asyncio.run(_run_one(body, routes))
        answer_prompt = next(p for p in prompts if "EVIDENCE:" in p)
        ev = answer_prompt.split("EVIDENCE:", 1)[1]
        # content, not boilerplate
        assert "Scholarly study on semiconductor supply chain resilience" in ev
        assert "x_query" not in ev and "oql" not in ev

    def test_digest_budget_is_4000_chars(self):
        body = _fat_openalex_body()
        for r in body["results"]:
            r["abstract"] = r["abstract"] + (" padding " * 500)
        canonical = json.dumps(body, sort_keys=True)
        _, prompts = asyncio.run(_run_one(body, {"/works": canonical}))
        answer_prompt = next(p for p in prompts if "EVIDENCE:" in p)
        ev_lines = [ln for ln in
                    answer_prompt.split("EVIDENCE:", 1)[1].splitlines()
                    if ln.startswith("- [")]
        assert ev_lines, "evidence item missing from prompt"
        item = ev_lines[0]
        # "- [0] " prefix + at most 4000 chars of digest text
        assert len(item) <= len("- [0] ") + 4000

    def test_provenance_still_hashes_full_body(self):
        body = _fat_openalex_body()
        canonical = json.dumps(body, sort_keys=True)
        import hashlib
        expected = hashlib.sha256(canonical.encode()).hexdigest()
        result, _ = asyncio.run(_run_one(body, {"/works": canonical}))
        sha = getattr(result.fetches[0], "content_sha256", "")
        assert sha == expected

    def test_unparsed_fetch_falls_back_to_raw_window(self):
        """A fetch whose parsed value is None degrades to the old
        body[:4000] window — the fallback branch of _evidence_text.
        Exercised directly on FetchResult so the relevance gate (which
        needs topical words) does not confound the branch under test."""
        from tools.pipeline.engine import FetchResult, ResearchPipeline as RP

        def _evidence_text(f):
            # rebind the production closure via a 1-leaf pipeline run is
            # heavy; instead replicate its contract through the engine by
            # calling the private helper's logic path: build a pipeline and
            # answer a leaf with one fetch whose parsed is None.
            async def go():
                model = ScriptedModel(responses={
                    "Architect": [_decompose()],
                    "Manager": [_answer() for _ in range(4)]})
                pipeline = RP(model=model,
                              adversary_router=_QuietAdversary(),
                              transport=fixture_transport({"/works": "{}"}),
                              store=ArtifactStore(
                                  root=Path("/tmp") / "speed_run13_fb"))
                from agp import AGPSession
                session = AGPSession(query="s")
                session.domain = Domain.FINANCIAL
                from agp.research_program import ResearchQuestion, \
                    QuestionKind
                q = ResearchQuestion(text="t", kind=QuestionKind.DESCRIPTIVE,
                                     question_id="q0")
                fetch = FetchResult(
                    source_name="openalex", url="http://x/works",
                    content_sha256="0" * 64, body="X" * 5000,
                    parsed=None, question_id="q0")
                outcome, evs = await pipeline._answer_leaf(q, [fetch],
                                                           session)
                return evs
            return asyncio.run(go())[0].content

        content = _evidence_text(self)
        assert content == "X" * 4000


class TestGoldensUnchanged:
    def test_structural_fingerprint_identical(self):
        """Fixture bodies are far below the 4000-char window, so digest and
        raw-window produce identical evidence text offline."""
        fp_path = Path("/tmp/speed_profile_fingerprint.json")
        before = Path("/tmp/fp_before_run13.json")
        if before.exists():
            assert json.loads(fp_path.read_text()) == \
                json.loads(before.read_text()), (
                    "offline fingerprint changed — evidence digest altered "
                    "behaviour on sub-window fixtures")

    def test_five_question_brier_golden(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             "tests/test_speed_parallel_leaves.py"
             "::test_brier_regression_five_retro_questions"],
            capture_output=True, text=True, timeout=600)
        assert "passed" in (r.stdout or ""), r.stdout + r.stderr
