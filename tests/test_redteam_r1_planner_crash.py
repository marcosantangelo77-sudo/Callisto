"""R1 (battery re-run, findings/battery_rerun.md): planner sort crash.

query_builder._plan_wikidata_concept sorted (Q-id, hint-word) tuples with
`-p[1]` — unary minus on a STRING — so any question containing a Wikidata
hint word raised TypeError and killed the whole question run. Regression
from the planner work merged 2026-08-24. gdelt_02 was the battery trigger.
"""
import sys

sys.path.insert(0, ".")

from tools.sources.query_builder import _plan_wikidata_concept, build_plan

GDELT_02 = ("Do any GDELT-indexed news articles mention the exact "
            "phrase 'callisto battery test'?")


def test_hint_match_does_not_raise():
    # 'companies' + 'people' both match; pre-fix this raised TypeError.
    resolved, cands = _plan_wikidata_concept(
        "which companies and people were involved?")
    assert resolved or cands


def test_longer_hint_wins():
    # 'companies' (9 chars) must outrank 'company' (7) for the same Q-id;
    # with a single distinct best class there is no ambiguity candidate.
    resolved, cands = _plan_wikidata_concept("companies involved")
    assert resolved.get("q_id") == "Q4830453"
    assert "q_id" not in cands


def test_gdelt_02_battery_question_no_crash():
    plan = build_plan("wikidata", GDELT_02)
    assert isinstance(plan.plannable, bool)
