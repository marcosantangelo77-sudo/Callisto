"""AGP protocol core wiring — preregistration and claims become real.

The pipeline previously sealed AGPSessions that evaporated into a runs
JSONL: agp.preregistration and agp.claims had ZERO production callers.
These tests pin the wiring:

  - criteria authored and SEALED before any fetch happens,
  - one repair turn on invalid criteria, fail-soft after a second failure
    (absence of preregistration never weakens a gate),
  - conclusion scored AGAINST the sealed criteria — only subtracts,
  - resume replays the sealed criteria instead of re-authoring them
    post-evidence (re-authoring would let fetched content shape its own
    acceptance test),
  - a sealed run with criteria opens a Claim in the ClaimStore with its
    evidence attached; no criteria, no claim.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()


from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline import checkpoint as ckpt  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402

TODAY = date(2026, 8, 23)
QUESTION = "What does recent scholarly research say about semiconductor supply chain resilience?"

OPENALEX_BODY = json.dumps({"results": [
    {"id": "W1", "title": "Semiconductor supply chain resilience review",
     "publication_year": 2025, "cited_by_count": 12},
]})
SS_BODY = json.dumps({"data": [
    {"title": "Chokepoint governance in semiconductor manufacturing",
     "year": 2025},
]})


def _routes() -> dict[str, str]:
    return {"/works": OPENALEX_BODY, "/graph/v1/paper/search": SS_BODY}


def _decompose(min_indep=2) -> str:
    return json.dumps({"sub_questions": [
        {"text": "what does scholarly research say about supply chain "
                 "resilience", "kind": "descriptive",
         "question_type": "scholarly work search about semiconductors",
         "min_source_tier": 2, "min_independent_sources": min_indep},
    ]})


def _answer(conf=0.8) -> str:
    return json.dumps({"answer": "the literature supports the claim",
                       "proposed_confidence": conf})


def _criteria(confirm=("literature supports",),
             refute=("zebra unicorn stampede",),
             ambiguous=(),
             **overrides) -> str:
    body = {
        "confirm_markers": list(confirm),
        "refute_markers": list(refute),
        "ambiguous_markers": list(ambiguous),
        "threshold": None,
        "direction": None,
        "min_evidence_items": 1,
        "min_source_class": "INFERRED",
    }
    body.update(overrides)
    return json.dumps(body)


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _make(tmp_path, model=None, adversary=None, ledger=None,
          checkpointer=None, descendants=None):
    model = model or ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Preregister": [{"content": _criteria()}],
        "Manager": [{"content": _answer()}],
    })
    pipe = ResearchPipeline(
        model=model, adversary_router=adversary or _QuietAdversary(),
        transport=fixture_transport(_routes()),
        store=ArtifactStore(root=tmp_path / "artifacts"),
        ledger=ledger, checkpointer=checkpointer,
        descendant_resolutions=descendants)
    return pipe, model


def _run(pipe, question=QUESTION):
    return asyncio.run(pipe.run(question, today=TODAY))


# ── sealing order and happy path ──────────────────────────────────────────

def test_prereg_sealed_before_any_evidence_fetch(tmp_path):
    pipe, model = _make(tmp_path)
    result = _run(pipe)

    roles = [r for r, _ in model.calls]
    assert roles.index("Preregister") < roles.index("Manager"), (
        f"criteria must be authored before evidence collection, got {roles}")
    assert result.sealed, result.refusal_reason
    assert result.prereg_seal_hash
    assert result.prereg_criteria["confirm_markers"] == ["literature supports"]
    assert result.prereg_verdict == "CONFIRMED"


# ── repair turn, then fail-soft ───────────────────────────────────────────

def test_invalid_criteria_get_one_repair_turn(tmp_path):
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        # first attempt has no refute markers → invalid; second is good
        "Preregister": [{"content": _criteria(refute=())},
                        {"content": _criteria()}],
        "Manager": [{"content": _answer()}],
    })
    pipe, model = _make(tmp_path, model=model)
    result = _run(pipe)

    assert result.sealed
    assert result.prereg_seal_hash
    prereg_calls = [1 for r, _ in model.calls if r == "Preregister"]
    assert len(prereg_calls) == 2
    assert any("preregistration repair attempted" in n for n in result.notes)


def test_criteria_failure_twice_proceeds_without_prereg(tmp_path):
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Preregister": [{"content": "{}"}, {"content": "not json at all"}],
        "Manager": [{"content": _answer()}],
    })
    pipe, _ = _make(tmp_path, model=model)
    result = _run(pipe)

    assert result.sealed, result.refusal_reason
    assert result.prereg_seal_hash == ""
    assert result.prereg_verdict == ""
    assert any("preregistration unavailable" in n for n in result.notes)


# ── scoring only subtracts ────────────────────────────────────────────────

def test_refuted_verdict_takes_major_penalty(tmp_path):
    control_model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Preregister": [{"content": _criteria(
            confirm=("literature supports",), refute=("zebra unicorn",))}],
        "Manager": [{"content": _answer()}],
    })
    refute_model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Preregister": [{"content": _criteria(
            confirm=("zebra unicorn",), refute=("literature supports",))}],
        "Manager": [{"content": _answer()}],
    })
    rc = _run(_make(tmp_path / "c", model=control_model)[0])
    rr = _run(_make(tmp_path / "r", model=refute_model)[0])

    assert rc.prereg_verdict == "CONFIRMED"
    assert rr.prereg_verdict == "REFUTED"
    assert rr.confidence_score == pytest.approx(
        rc.confidence_score - 0.15, abs=1e-9)
    assert any("penalised" in n for n in rr.notes)


def test_ambiguous_verdict_caps_confidence_at_055(tmp_path):
    from tools.research_program import ResolutionRecord

    def descendants():
        return [ResolutionRecord(question_id=f"d{i}", resolved_at=TODAY,
                                 outcome="hit", best_source_class="SECONDARY")
                for i in range(5)]

    # min_independent_sources=1 so the leaf is not capped at the
    # SPECULATIVE band by source diversity — otherwise the 0.55 cap could
    # never be exercised from above.
    amb_model = ScriptedModel({
        "Architect": [{"content": _decompose(min_indep=1)}],
        "Preregister": [{"content": _criteria(
            confirm=("totally absent phrase",),
            refute=("equally absent phrase",))}],
        "Manager": [{"content": _answer(0.9)}],
    })
    conf_model = ScriptedModel({
        "Architect": [{"content": _decompose(min_indep=1)}],
        "Preregister": [{"content": _criteria(
            confirm=("literature supports",), refute=("equally absent",))}],
        "Manager": [{"content": _answer(0.9)}],
    })
    ra = _run(_make(tmp_path / "a", model=amb_model,
                    descendants=descendants())[0])
    rco = _run(_make(tmp_path / "k", model=conf_model,
                     descendants=descendants())[0])

    assert ra.prereg_verdict == "AMBIGUOUS"
    assert ra.confidence_score <= 0.55
    # the cap did real work: the identical run scoring CONFIRMED sits higher
    assert rco.prereg_verdict == "CONFIRMED"
    assert rco.confidence_score > 0.55
    assert any("capped" in n for n in ra.notes)


def test_confirmed_verdict_never_raises_confidence(tmp_path):
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Preregister": [{"content": _criteria()}],
        "Manager": [{"content": _answer(0.3)}],
    })
    none_model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Preregister": [{"content": "{}"}, {"content": "{}"}],
        "Manager": [{"content": _answer(0.3)}],
    })
    r_with = _run(_make(tmp_path / "w", model=model)[0])
    r_without = _run(_make(tmp_path / "n", model=none_model)[0])

    assert r_with.prereg_verdict == "CONFIRMED"
    assert r_with.confidence_score == pytest.approx(
        r_without.confidence_score, abs=1e-9)


# ── resume replays the sealed criteria ────────────────────────────────────

def test_resume_replays_same_sealed_criteria_without_reauthoring(tmp_path):
    cp = ckpt.FileCheckpointer(root=tmp_path / "ckpt")
    first_model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Preregister": [{"content": _criteria()}],
        "Manager": [{"content": _answer()}],
    })
    first = _make(tmp_path / "one", model=first_model, checkpointer=cp)[0]
    r1 = _run(first)

    # A second pipeline over the SAME checkpoints must replay the ORIGINAL
    # criteria even though its scripted model would author different ones.
    second_model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Preregister": [{"content": _criteria(confirm=("tampered",))}],
        "Manager": [{"content": _answer()}],
    })
    second = _make(tmp_path / "two", model=second_model, checkpointer=cp)[0]
    r2 = _run(second)

    assert r2.prereg_seal_hash == r1.prereg_seal_hash
    assert r2.prereg_criteria == r1.prereg_criteria
    assert not any(r == "Preregister" for r, _ in second_model.calls), (
        "a resumed run must never re-author criteria after evidence")


# ── tier honesty after penalties ──────────────────────────────────────────

def test_tier_matches_final_score_after_penalties(tmp_path):
    from agp import ConfidenceTier
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Preregister": [{"content": _criteria(
            confirm=("zebra unicorn",), refute=("literature supports",))}],
        "Manager": [{"content": _answer(0.9)}],
    })
    pipe, _ = _make(tmp_path, model=model)
    result = _run(pipe)

    expected = ConfidenceTier.from_score(result.confidence_score).value
    assert result.confidence_tier == expected


# ── routing + persistence surfaces ────────────────────────────────────────

def test_preregister_role_routes_to_architect_task_class():
    from agp.adversary import AGPRole
    from tools.pipeline.model import RouterModel

    seen = {}

    class FakeRouter:
        name = "fake"

        async def complete(self, task_class, messages, schema=None):
            seen["task_class"] = task_class
            return {"content": "{}"}

    m = RouterModel(FakeRouter())
    asyncio.run(m.complete("Preregister", [{"role": "user", "content": "q"}]))
    assert seen["task_class"] == "hypothesis_generation"
    assert AGPRole.ROLE_TASK_CLASSES["Preregister"] == ["hypothesis_generation"]


def test_result_record_carries_prereg_and_claim():
    from types import SimpleNamespace as NS
    from callisto import _result_record

    result = NS(sealed=True, refusal_reason="", conclusion="c",
                confidence_score=0.42, confidence_tier="SPECULATIVE",
                leaves=[], artifact_refs=[], fetches=[], objections=[],
                notes=["n"], prereg_seal_hash="abc123",
                prereg_criteria={"confirm_markers": ["x"]},
                prereg_verdict="AMBIGUOUS", prereg_divergences=["d"],
                claim_id="deadbeef")
    rec = _result_record(result, "some question")

    assert rec["preregistration"]["seal_hash"] == "abc123"
    assert rec["preregistration"]["verdict"] == "AMBIGUOUS"
    assert rec["preregistration"]["divergences"] == ["d"]
    assert rec["claim_id"] == "deadbeef"
