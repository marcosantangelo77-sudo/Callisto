"""Family-1 regression tests: a verification layer that never actually runs.

Two instances of the family closed here:

  A7  verify_artifacts() had ZERO production callers. A sealed conclusion
      could cite artifacts nobody stored; nothing re-hashed anything between
      storage time and `callisto show`. Fix: an artifact_verifier hook on
      AGPSession, enforced inside seal(), wired by ResearchPipeline whenever
      the run produced artifacts.

  A8  The retrodiction batch — the system's scored track record — recorded
      fetch counts and objections but dropped every artifact ref, so scored
      conclusions could not be re-checked from the batch record at all.
      Fix: artifacts persisted per BatchResult row.

Method (PATTERNS #7): each test breaks the production code deliberately in
its head-commented "mutation" and asserts the test would fail — here, by
constructing exactly the state the gate exists to reject (a cited-but-
missing / cited-but-corrupt artifact) and asserting the seal is refused.
"""

import json

import pytest

import agp as agp_mod
from agp import AGPSession, AGPSealRefused, Domain, Evidence, SessionStep, \
    SourceClass
from tools.artifacts import ArtifactStore


def _ready_session(question="artifact gate test?") -> AGPSession:
    """A session driven to SESSION_CLOSE with one evidence item."""
    s = AGPSession(question)
    s.scope = question
    s.domain = Domain.GENERAL
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    for step in range(s.current_step.value + 1, SessionStep.SESSION_CLOSE.value + 1):
        s.advance_to(SessionStep(step))
    s.add_evidence(Evidence(
        content="some evidence content", source_class=SourceClass.SECONDARY,
        confidence_score=0.4, domain=Domain.GENERAL, origin_agent="test"))
    s.summary = agp_mod.SessionSummary(
        scope=question, domain=Domain.GENERAL,
        conclusion="a conclusion with actual content",
        confidence_score=0.4, evidence_count=1, contradiction_count=0)
    return s


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = ArtifactStore(root=tmp_path / "art")
    monkeypatch.setattr("tools.artifacts._default_store", st)
    return st


class TestSealArtifactGate:
    def test_no_verifier_is_unchanged(self, store):
        """Sessions without the hook behave exactly as before."""
        s = _ready_session()
        assert s.seal()  # legacy path untouched

    def test_missing_cited_artifact_refuses_seal(self, store):
        """MUTATION TARGET: delete this gate and this seal would succeed
        while citing bytes nobody stored — the A7 defect."""
        ghost = "f" * 64
        s = _ready_session()
        s.artifact_check = lambda session: f"1 missing (sha {ghost[:12]})"
        with pytest.raises(AGPSealRefused, match="missing"):
            s.seal()

    def test_corrupt_artifact_refuses_seal(self, store, tmp_path):
        ref = store.put_text("real bytes", "txt", name="evidence.txt")
        # Corrupt the stored bytes AFTER storing: hash no longer matches.
        obj = store.get_path(ref.sha256)
        obj.write_bytes(b"tampered bytes")
        report = store.verify_artifacts([ref])
        assert not report["ok"]
        s = _ready_session()
        s.artifact_check = lambda session: (
            "" if store.verify_artifacts([ref])["ok"] else "1 corrupt")
        with pytest.raises(AGPSealRefused, match="corrupt"):
            s.seal()

    def test_intact_artifacts_seal(self, store):
        ref = store.put_text("real bytes", "txt", name="evidence.txt")
        s = _ready_session()
        s.artifact_check = lambda session: (
            "" if store.verify_artifacts([ref])["ok"] else "missing")
        assert s.seal()

    def test_verifier_crash_fails_closed(self, store):
        def boom(_s):
            raise RuntimeError("verifier exploded")
        s = _ready_session()
        s.artifact_check = boom
        with pytest.raises(AGPSealRefused, match="crashed"):
            s.seal()


class TestEngineWiresTheGate:
    @pytest.mark.asyncio
    async def test_pipeline_run_with_artifacts_installs_gate(self, tmp_path):
        """The pipeline's compute stage produces artifacts; the run's session
        must carry the verifier so its own seal re-checks them."""
        from tests.test_build_p1_pipeline import _make, ScriptedModel, \
            _decompose_response, _answer  # reuse the scripted fixtures

        code = ("import json\n"
                "series = json.load(open('series.json'))\n"
                "result = {'mean': sum(series) / len(series)}")
        compute = {"code": code, "inputs": {"series": [1.0, 2.0, 3.0]}}
        model = ScriptedModel({
            "Architect": [{"content": _decompose_response()}],
            "Manager": [{"content": json.dumps({"answer": None,
                                                "compute": compute})},
                        {"content": json.dumps(
                            {"answer": "mean computed as 2.0",
                             "proposed_confidence": 0.8})},
                        {"content": _answer(0.7)}],
        })
        pipeline, result = None, None
        pipeline_obj, _ = _make(tmp_path, model=model)
        result = await pipeline_obj.run("Compute question?",
                                        today=__import__("datetime").date(2026, 8, 22))
        assert result.sealed, result.refusal_reason
        assert result.artifact_refs
        # The sealed session itself must have been gated.
        assert result.session.artifact_check is not None

    def test_batch_result_persists_artifact_refs(self, tmp_path):
        """A8: scored rows must carry their artifact chain."""
        from tools.retrodiction.batch import BatchResult
        br = BatchResult(question_id="q1", status="scored")
        assert br.to_dict()["artifacts"] == []
