"""Instance 7 — deep-research capability gap analysis (tests).

These tests pin the VERIFIED structural claims made in DEEP_RESEARCH.md so each
one is falsifiable in CI rather than asserted in prose. They are
characterization tests of ABSENCE and PRESENCE:

  - absence: no question decomposition, no code-execution tool, no artifact
    surface, citation check is substring-only, seal is still unkeyed on this
    branch (keyed HMAC lives unmerged on audit/tier3-epistemics)
  - presence: AGP core is domain-neutral; the lifecycle stages exist and are
    domain-neutral strings; search stack is domain-general

If one of these fails because the code CHANGED, update both the test and the
corresponding claim in DEEP_RESEARCH.md — that is the point.
"""

import ast
import inspect
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ── helpers ──────────────────────────────────────────────────────────────────

def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _parse(rel: str) -> ast.AST:
    return ast.parse(_read(rel))


def _tool_dispatch_names(orch_src: str) -> list[str]:
    """Extract the `if name == "..."` tool names from Orchestrator._execute_tool."""
    tree = ast.parse(orch_src)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_tool":
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Compare)
                    and isinstance(sub.left, ast.Name)
                    and sub.left.id == "name"
                ):
                    for comp in sub.comparators:
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                            names.append(comp.value)
    return sorted(set(names))


# ── Q1: intake and decomposition ─────────────────────────────────────────────

class TestNoDecomposition:
    def test_orchestrator_has_no_decompose_step(self):
        """VERIFIED claim: nothing between classify-budget and evidence collection
        decomposes a query into sub-questions. Falsified by a _step_decompose
        (or equivalent) appearing — at which point DEEP_RESEARCH §1 is stale."""
        src = _read("orchestrator.py")
        assert "_step_decompose" not in src
        # no sub-question structures anywhere in agp/ or orchestrator
        for rel in ("orchestrator.py", "agp/__init__.py", "agp/thresholds.py"):
            text = _read(rel).lower()
            assert "sub_question" not in text
            assert "researchprogram" not in text.replace("_", "")

    def test_task_classifier_only_assigns_budget(self):
        """classify_query returns a TaskType (budget bucket), never a plan."""
        from tools.task_classifier import classify_query, TaskType
        result = classify_query("Is Bitcoin a good buy right now? 10 year price target?")
        assert isinstance(result, TaskType)
        # it must NOT return any decomposition structure
        assert not isinstance(result, (list, dict, tuple))

    def test_hypothesis_intake_is_betting_shaped(self):
        """The hypotheses schema carries sport/market columns — cannot represent
        an arbitrary-domain research program without migration."""
        src = _read("tools/hypothesis.py")
        assert "sport" in src and "market_type" in src  # betting fields exist
        # and no generic-domain field exists yet
        assert "claim_type" not in src


# ── Q2: horizon problem — current state pins ────────────────────────────────

class TestHorizonStatePins:
    def test_paper_trade_signal_requires_paper_trading_status(self):
        """backtest.generate_paper_trade_signal hard-returns [] unless status ==
        'paper_trading' — the lifecycle's resolution producer is status-gated.
        The H0 generalization must preserve this gate for sports claims.
        (Source-scanned rather than imported: tools/backtest.py pulls optional
        deps like polars that are not installed on every audit machine.)"""
        src = _read("tools/backtest.py")
        fn_start = src.index("async def generate_paper_trade_signal")
        fn_body = src[fn_start : src.index("\n    async def", fn_start + 10)]
        # Gate is the direct status check, membership in the paper-only
        # frozenset, or the extracted reject_non_paper() helper — all
        # hard-return [] for any non-paper_trading status (including live).
        assert (
            'h["status"] != "paper_trading"'
            in fn_body
            or 'not in _PAPER_TRADE_SIGNAL_STATUSES' in fn_body
            or 'reject_non_paper(' in fn_body
        )
        assert "return []" in fn_body

    def test_lifecycle_stages_exist_and_are_domain_neutral_strings(self):
        """STAGE_ORDER exists with exactly the five stages; they are plain strings
        with no betting vocabulary baked into the stage names themselves."""
        from tools.hypothesis import STAGE_ORDER
        assert list(STAGE_ORDER) == [
            "draft", "backtesting", "paper_trading", "live", "retired"
        ]
        for stage in STAGE_ORDER:
            assert isinstance(stage, str)
            assert "sport" not in stage and "bet" not in stage

    def test_promotion_gates_are_betting_parameterized(self):
        """Gate keys reference CLV/Brier/IC (portable stats) but thresholds live
        alongside betting-specific config — documents the §5 'config split' need."""
        from tools.hypothesis import PROMOTION_GATES
        assert "backtesting→paper_trading" in PROMOTION_GATES
        gate = PROMOTION_GATES["backtesting→paper_trading"]
        for stat_key in ("max_p_value", "max_brier", "min_ic"):
            assert stat_key in gate  # portable statistics
        assert "edge_threshold" not in gate  # edge_threshold is per-hypothesis, good


# ── Q3: quantitative artifacts ───────────────────────────────────────────────

class TestNoCodeExecutionOrArtifacts:
    def test_tool_dispatch_has_no_code_execution_tool(self):
        """VERIFIED claim: _execute_tool dispatches odds/search/reasoning tools
        only. Any run_python/exec tool appearing falsifies §3's gap verdict."""
        names = _tool_dispatch_names(_read("orchestrator.py"))
        forbidden = {
            "run_python", "execute_code", "python_repl", "run_code",
            "sandbox_exec", "compute",
        }
        assert not (set(names) & forbidden), f"new exec tool appeared: {names}"

    def test_no_artifact_return_path_in_synthesis(self):
        """SessionSummary has no artifact references — synthesis returns prose+scores."""
        src = _read("agp/__init__.py")
        assert "artifact" not in src.lower()

    def test_local_compute_is_wrapped_betting_math_not_a_sandbox(self):
        """local_compute wraps devig/significance helpers; it is not a general
        code-execution surface. Its public API stays sports-scoped until S0 lands."""
        src = _read("tools/local_compute.py")
        assert "exec(" not in src
        assert "subprocess" not in src
        assert "devig" in src  # still the wrapped-betting-math module we documented


# ── Q4: evidence acquisition / provenance ────────────────────────────────────

class TestCitationGrounding:
    def test_citation_check_is_provenance_backed(self):
        """E0 LANDED (build/tool-registry): `_response_cites_urls` (substring
        check) is deleted. Citations are verified against the per-session
        ProvenanceLedger — only URLs actually fetched count. Update
        DEEP_RESEARCH §4 accordingly."""
        src = _read("orchestrator.py")
        assert "_response_cites_urls" not in src
        assert "cites_verified_url" in src
        assert "ProvenanceLedger" in src

    def test_search_stack_is_domain_general(self):
        """search.py exposes backend-agnostic web_search — no sports vocabulary."""
        src = _read("tools/search.py").lower()
        assert "searxng" in src and "brave" in src
        assert "sport" not in src

    def test_collection_prompt_offers_only_self_labels(self):
        """Evidence-collection prompts offer SECONDARY/INFERRED only — no PRIMARY
        producer exists in the research path (instance 4's unreachable-VERIFIED
        finding). Falsified when a fetch-record-based source_class assigner lands."""
        src = _read("orchestrator.py")
        assert '"SECONDARY"' in src and '"INFERRED"' in src
        # no provenance-derived assignment exists yet
        assert "fetch_record" not in src
        assert "source_kind" not in src


# ── Q5: what generalises ─────────────────────────────────────────────────────

class TestWhatGeneralises:
    def test_agp_core_has_no_domain_vocabulary(self):
        """The protocol core (agp/) contains no betting/sports terms — it is
        domain-general as claimed. A hit here means someone coupled the moat."""
        src = _read("agp/__init__.py").lower()
        for term in ("sport", "bet", "odds", "game", "wager", "bookmaker"):
            assert term not in src, f"domain term '{term}' leaked into agp core"

    def test_tier_boundaries_are_evidence_authority_statements(self):
        """agp/thresholds ceilings map SOURCE CLASS → confidence, i.e. statements
        about evidence authority, not about any domain."""
        from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
        assert set(MAX_CONFIDENCE_BY_SOURCE) >= {"PRIMARY", "SECONDARY", "SIGNAL", "INFERRED"}
        assert MAX_CONFIDENCE_BY_SOURCE["PRIMARY"] > MAX_CONFIDENCE_BY_SOURCE["SECONDARY"]

    def test_seal_roundtrips_and_tamper_breaks_it(self):
        """Pin the CURRENT state on this branch: canonical-JSON SHA-256 seal,
        verified via verify_seal(session.to_dict()). NOTE: instance 4's keyed
        HMAC upgrade lives on audit/tier3-epistemics (commit eb6151b) and is
        NOT merged here — verified by reading agp/__init__.py:352-354, which
        still computes plain sha256. This test falsifies itself the moment
        the HMAC lands (hashlib.sha256 replaced by hmac), at which point
        update this pin. The artifact-sealing design (§3-S1) depends on the
        KEYED version being merged first."""
        import agp
        session = agp.AGPSession(query="test: does the seal roundtrip")
        # walk the full 7-step sequence — the protocol enforces it
        for step in list(agp.SessionStep)[1:]:
            session.advance_to(step)
        session.add_evidence(
            agp.Evidence(
                content="x",
                source_class=agp.SourceClass.SECONDARY,
                confidence_score=0.7,
                domain=agp.Domain.GENERAL if hasattr(agp.Domain, "GENERAL") else list(agp.Domain)[0],
                origin_agent="tier7-test",
            )
        )
        session.summary = agp.SessionSummary(
            scope="tier7 seal roundtrip",
            domain=list(agp.Domain)[0],
            conclusion="seal roundtrips",
            confidence_score=0.7,
            evidence_count=1,
            contradiction_count=0,
        )
        # seal() returns the digest string; verification goes through to_dict()
        sealed = session.seal()
        assert sealed
        assert agp.AGPSession.verify_seal(session.to_dict())
        # tampering with content breaks verification
        tampered = session.to_dict()
        tampered["summary"]["conclusion"] = "tampered conclusion"
        assert not agp.AGPSession.verify_seal(tampered)
