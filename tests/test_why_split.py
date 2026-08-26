"""Split-parity tests for tools.whyexp / tools.why facade.

tools/why.py was extracted into the tools.whyexp package. These tests pin
the contract of that split:

  - every name re-exported by tools.why resolves to the SAME object as its
    tools.whyexp home (facade must be transparent, not a copy);
  - each submodule is importable standalone and exposes its public surface;
  - the package __init__ mirrors the facade's __all__ exactly;
  - behavioral spot-checks per submodule (records, provenance, rejections,
    independence, walker) so the split did not change semantics;
  - structural guards: no live-betting vocabulary, no shadowing
    ``tools/why/`` directory, read-only mandate intact.

No sockets, no fixtures beyond what the pipeline helpers provide.
"""
from __future__ import annotations

import dataclasses
import importlib
import inspect
import json

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()


import tools.why as why_mod  # noqa: E402
import tools.whyexp as whyexp  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.whyexp.explanation import WhyExplanation  # noqa: E402
from tools.whyexp.independence import (  # noqa: E402
    independence_from_fetches,
)
from tools.whyexp.provenance import assignment_reason  # noqa: E402
from tools.whyexp.records import (  # noqa: E402
    REQUIREMENT_GATE_CAP,
    SCHEMA_VERSION,
    CeilingWhy,
    EvidenceWhy,
    IndependenceWhy,
    ObjectionWhy,
    RejectedWhy,
    StepWhy,
)
from tools.whyexp.rejections import parse_rejections  # noqa: E402
from tools.whyexp.walker import (  # noqa: E402
    _StoredShim,
    _largest_constraint,
    explain_stored,
    pipeline_adversary_ledger_statuses,
)


# ── facade parity: tools.why is a transparent re-export ───────────────────


def test_facade_reexports_are_identity_with_submodules():
    pairs = [
        ("SCHEMA_VERSION", SCHEMA_VERSION),
        ("CeilingWhy", CeilingWhy),
        ("EvidenceWhy", EvidenceWhy),
        ("IndependenceWhy", IndependenceWhy),
        ("ObjectionWhy", ObjectionWhy),
        ("RejectedWhy", RejectedWhy),
        ("StepWhy", StepWhy),
        ("WhyExplanation", WhyExplanation),
        ("_StoredShim", _StoredShim),
        ("_largest_constraint", _largest_constraint),
        ("assignment_reason", assignment_reason),
        ("explain_stored", explain_stored),
        ("independence_from_fetches", independence_from_fetches),
        ("parse_rejections", parse_rejections),
        ("pipeline_adversary_ledger_statuses",
         pipeline_adversary_ledger_statuses),
    ]
    for name, sub_obj in pairs:
        assert hasattr(why_mod, name), f"tools.why lost {name}"
        assert getattr(why_mod, name) is sub_obj, \
            f"tools.why.{name} is not the same object as tools.whyexp"


def test_facade_exposes_explain_result():
    from tools.whyexp.walker import explain_result
    assert why_mod.explain_result is explain_result


def test_package_all_matches_facade_all():
    assert sorted(whyexp.__all__) == sorted(why_mod.__all__)


def test_no_shadowing_tools_why_directory():
    import os
    pkg_dir = os.path.join(os.path.dirname(why_mod.__file__), "why")
    assert not os.path.isdir(pkg_dir), (
        "tools/why/ would shadow the tools.why module — use tools/whyexp/")


# ── records module ────────────────────────────────────────────────────────


def test_records_schema_version_is_one():
    assert SCHEMA_VERSION == 1
    assert REQUIREMENT_GATE_CAP == 0.54


def test_record_to_dicts_are_json_clean_and_round_trip():
    recs = [
        EvidenceWhy(label="src", source_class="PRIMARY", reason="rule",
                    ceiling=1.0),
        CeilingWhy(kind="source_class", value=0.75, detail="d"),
        ObjectionWhy(text="t", kind="k", severity="MINOR", penalty=0.05,
                     veto=False, status="acknowledged"),
        IndependenceWhy(n_fetches=2, independent_keys=["a"], n_independent=1,
                        collapses=["collapse"]),
        RejectedWhy(source_name="s", url="u", reason="r",
                    relevance_score=0.1, content_sha256="ab"),
        StepWhy(stage="clamp", before=0.9, after=0.75, rule="min(...)"),
    ]
    for r in recs:
        blob = json.dumps(r.to_dict())
        back = json.loads(blob)
        field_names = {f.name for f in dataclasses.fields(type(r))}
        rebuilt = type(r)(**{k: v for k, v in back.items()
                             if k in field_names})
        assert rebuilt.to_dict() == r.to_dict()


def test_step_drop_rounds_before_minus_after():
    s = StepWhy(stage="x", before=0.9, after=0.733331, rule="r")
    assert s.drop == pytest.approx(0.1667)


def test_ceiling_binding_defaults_false_and_is_mutable():
    c = CeilingWhy(kind="inheritance", value=0.35, detail="d")
    assert c.binding is False
    c.binding = True
    assert c.binding is True


def test_dataclass_fields_frozen_shape_not_enforced_but_present():
    # WhyExplanation carries stale accounting fields added with the split.
    names = {f.name for f in dataclasses.fields(WhyExplanation)}
    assert {"stale_descendants", "stale_penalty_applied"} <= names


# ── provenance module ─────────────────────────────────────────────────────


def test_assignment_reason_without_ledger_returns_empty_pair():
    assert assignment_reason("anything", None) == ("", "")


def test_assignment_reason_names_each_rule():
    ledger = ProvenanceLedger()
    primary_bytes = '{"results": ["real tool bytes"]}'
    other_bytes = '{"x": 1}'
    cited = "see https://example.org/data for the table"
    ledger.record_tool_result("web_fetch", primary_bytes, primary=True,
                              urls=["https://example.org/data"])
    ledger.record_tool_result("web_search", other_bytes, primary=False)

    cls, reason = assignment_reason(primary_bytes, ledger)
    assert cls == "PRIMARY" and "primary observation" in reason

    cls, reason = assignment_reason(other_bytes, ledger)
    assert cls == "SECONDARY" and "hash match" in reason

    cls, reason = assignment_reason(cited, ledger)
    assert cls == "SECONDARY" and "fetched" in reason

    cls, reason = assignment_reason("nothing backs this at all", ledger)
    assert cls == "INFERRED" and "without verification" in reason


# ── rejections module ─────────────────────────────────────────────────────


def test_parse_rejections_extracts_source_and_reason():
    notes = [
        "leaf 'has X happened': 2 fetch(s) rejected at ingestion: "
        "[wire_service] too short; [blog] low relevance score",
        "unrelated note about something else entirely",
    ]
    out = parse_rejections(notes)
    assert len(out) == 2
    assert out[0].source_name == "wire_service"
    assert out[0].reason == "too short"
    assert out[1].source_name == "blog"
    assert all(r.url == "" for r in out)  # traces are gone; honest blanks


def test_parse_rejections_empty_and_none_inputs():
    assert parse_rejections([]) == []
    assert parse_rejections(None) == []


# ── independence module ───────────────────────────────────────────────────


class _F:
    def __init__(self, source_name, url):
        self.source_name = source_name
        self.url = url


def test_family_collapse_counts_one_independent_source():
    ind = independence_from_fetches([
        _F("openalex", "https://api.openalex.org/works?x=1"),
        _F("semantic_scholar",
           "https://api.semanticscholar.org/graph/v1/paper/search"),
    ])
    assert ind.n_fetches == 2
    assert ind.n_independent == 1          # same family -> ONE source
    assert any("scholarly-aggregator" in c for c in ind.collapses)


def test_distinct_hosts_count_separately():
    ind = independence_from_fetches([
        _F("openalex", "https://api.openalex.org/works"),
        _F("federalregister",
           "https://www.federalregister.gov/api/documents.json"),
    ])
    assert ind.n_independent == 2


def test_independence_handles_empty_and_none():
    for empty in ([], None):
        ind = independence_from_fetches(empty)
        assert isinstance(ind, IndependenceWhy)
        assert ind.n_fetches == 0
        assert ind.n_independent == 0
        assert ind.collapses == []


# ── walker: stored round trip + shim + statuses ───────────────────────────


def test_explain_stored_bare_payload_refused_rendering():
    bare = {
        "root_query": "old question",
        "sealed": False,
        "refusal_reason": "every leaf came back unanswered",
        "confidence_score": 0.0,
        "tier": "UNVERIFIED",
    }
    expl = explain_stored(bare)
    text = expl.narrative()
    assert "REFUSED" in text
    assert "unanswered" in expl.largest_constraint


def test_explain_stored_regenerates_missing_largest_constraint():
    expl = explain_stored({
        "root_query": "q?",
        "sealed": True,
        "refusal_reason": "",
        "confidence_score": 0.5,
        "tier": "SPECULATIVE",
        "score_walk": [{"stage": "source-class clamp", "before": 0.8,
                        "after": 0.5, "rule": "min", "drop": 0.3}],
    })
    assert "q?" in expl.largest_constraint
    assert "provenance ceiling" in expl.largest_constraint


def test_stored_shim_mirrors_explanation_view():
    expl = WhyExplanation(root_query="rq", sealed=True,
                          refusal_reason="", score=0.42, tier="LOW")
    shim = _StoredShim(expl)
    assert shim.root_query == "rq"
    assert shim.sealed is True
    assert shim.refusal_reason == ""
    assert shim.confidence_score == 0.42


def test_pipeline_adversary_ledger_statuses_never_raises_on_bare_result():
    class _R:
        session = None

    assert pipeline_adversary_ledger_statuses(_R()) == {}


# ── largest-constraint sentence branches ──────────────────────────────────


class _Result:
    def __init__(self, q="query?", sealed=True, refusal="",
                 score=0.5):
        self.root_query = q
        self.sealed = sealed
        self.refusal_reason = refusal
        self.confidence_score = score


def test_largest_constraint_refusal_branches():
    r = _Result(sealed=False, refusal="floor refusal")
    out = _largest_constraint([], [], "", 0.0, r,
                              IndependenceWhy(0, [], 0, []))
    assert "REFUSED" in out and "floor refusal" in out

    r2 = _Result(sealed=False, refusal="x")
    out2 = _largest_constraint([], [], "veto text", 0.0, r2,
                               IndependenceWhy(0, [], 0, []))
    assert "BLOCKING" in out2 and "veto text" in out2


def test_largest_constraint_no_steps_binding_ceiling():
    r = _Result(score=0.55)
    ceilings = [CeilingWhy(kind="source_class", value=0.55, detail="d",
                           binding=True)]
    out = _largest_constraint([], ceilings, "", 0.0, r,
                              IndependenceWhy(0, [], 0, []))
    assert "held at the binding" in out and "source_class" in out


def test_largest_constraint_zero_proposal_with_binding_ceiling():
    r = _Result(score=0.0)
    ceilings = [CeilingWhy(kind="inheritance", value=0.35, detail="d",
                           binding=True)]
    out = _largest_constraint([], ceilings, "", 0.0, r,
                              IndependenceWhy(0, [], 0, []))
    assert "scored 0.00" in out


def test_largest_constraint_unconstrained():
    r = _Result(score=0.9)
    out = _largest_constraint([], [], "", 0.0, r,
                              IndependenceWhy(0, [], 0, []))
    assert "nothing constrained it" in out


def test_largest_constraint_biggest_step_branches():
    r = _Result(score=0.4)
    step_adv = StepWhy(stage="adversary penalties", before=0.6, after=0.4,
                       rule="-0.20")
    out = _largest_constraint([step_adv], [], "", 0.20, r,
                              IndependenceWhy(0, [], 0, []))
    assert "subtracted 0.20" in out

    step_gate = StepWhy(stage="evidence-requirement gate", before=0.6,
                        after=0.54, rule="cap")
    ind = IndependenceWhy(3, ["a", "b"], 2, [])
    out2 = _largest_constraint([step_gate], [], "", 0.0, r, ind)
    assert "requirements were unmet" in out2
    assert "independent source(s)" in out2 and "3 fetch(es)" in out2


def test_largest_constraint_generic_fallback():
    r = _Result(score=0.3)
    step = StepWhy(stage="mystery stage", before=0.6, after=0.3, rule="?")
    out = _largest_constraint([step], [], "", 0.0, r,
                              IndependenceWhy(0, [], 0, []))
    assert "mystery stage" in out and "-0.30" in out


# ── structural guards ─────────────────────────────────────────────────────


@pytest.mark.parametrize("modname", [
    "tools.whyexp",
    "tools.whyexp.records",
    "tools.whyexp.provenance",
    "tools.whyexp.rejections",
    "tools.whyexp.independence",
    "tools.whyexp.walker",
    "tools.whyexp.explanation",
])
def test_every_submodule_imports_clean(modname):
    mod = importlib.import_module(modname)
    assert mod.__doc__, f"{modname} lacks a docstring"


def test_no_live_betting_vocabulary_anywhere_in_package():
    import os
    root = os.path.dirname(whyexp.__file__)
    for fname in sorted(os.listdir(root)):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(root, fname), encoding="utf-8") as f:
            src = f.read().lower()
        assert "'live'" not in src and '"live"' not in src, \
            f"live betting status leaked into {fname}"
        assert "status == 'live'" not in src.replace('"', "'")


def test_paper_trade_signal_statuses_untouched_by_split():
    # The why split must not touch paper-trade signal handling at all.
    import subprocess
    out = subprocess.run(
        ["git", "diff", "HEAD", "--", "tools/paper_trade.py"],
        capture_output=True, text=True, cwd=os_get_repo_root())
    assert out.stdout.strip() == "", "paper_trade.py modified by why split"


def os_get_repo_root() -> str:
    import os
    return os.path.dirname(os.path.dirname(inspect.getfile(whyexp)))
