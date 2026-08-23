"""Review findings for 2026-08-23 — see findings/review_2026-08-23.md.

Every test here FAILS on the code under review by design; this file is the
reproduction, not a fix. Reviewed heads:
  - origin/master fa2bea9 ("declared stance")
  - fix/confidence-laundering 156a837
"""
import subprocess

import pytest


# --------------------------------------------------------------------------- #
# R1 (HIGH): the alias/self-review fix (_same_weights) half-landed inside
# agp/ensemble.py. PanelVerdict.unanimous_unrebutted still compares raw model
# name spellings, so an author reviewing through an alias counts as an
# independent critic and triggers UNANIMITY_BONUS_PENALTY.
# Reproduced live at 156a837 before writing this test.
# --------------------------------------------------------------------------- #
class TestR1AliasUnanimityHalfLand:
    def _make(self):
        from agp.ensemble import (
            AdversaryObjection,
            PanelVerdict,
            ReviewProvenance,
        )

        objections = [
            AdversaryObjection(claim_id="c", text="v1", severity="MAJOR",
                               model="claude"),
            AdversaryObjection(claim_id="c", text="v2", severity="MAJOR",
                               model="gpt-4o-proxy-alias"),
        ]
        prov = ReviewProvenance(
            author_model="gpt-4o",
            reviewer_models=["claude", "gpt-4o-proxy-alias"],
        )
        return PanelVerdict(objections=objections, provenance=prov)

    def test_alias_reviewer_does_not_count_toward_unanimity(self):
        pv = self._make()
        # provenance.independent was fixed (True here, via 'claude'), but the
        # unanimity path must ALSO apply _same_weights: one of the two
        # objectors is the author itself, so this is NOT unanimous consensus
        # among independent critics.
        assert pv.unanimous_unrebutted is False, (
            "unanimous_unrebutted counted gpt-4o-proxy-alias as independent "
            "of author gpt-4o — spelling comparison, the F6b bug A2 fixed in "
            "only one property of the same module"
        )

    def test_unanimity_bonus_not_awarded_on_self_review_via_alias(self):
        from agp.ensemble import UNANIMITY_BONUS_PENALTY

        pv = self._make()
        out, reason = pv.apply(0.90)
        expected = round(0.90 - sum(o.penalty for o in pv.objections), 2)
        assert out == pytest.approx(expected), (
            f"apply(0.90) -> {out}; expected {expected} without the "
            f"UNANIMITY_BONUS_PENALTY ({UNANIMITY_BONUS_PENALTY}) awarded "
            "because the author attacked its own conclusion via an alias"
        )


# --------------------------------------------------------------------------- #
# R2 (MEDIUM, pre-existing since autosave 7e3d007): tools/calibration cannot
# be imported at all — its __init__ imports `replay_chain`, which no longer
# exists in instrument.py — and bridge.py does not exist despite being
# imported. tools/calibration/ablate.py (the empirical-routing scorer) is
# therefore inert: nothing can import it, no test references it.
# --------------------------------------------------------------------------- #
class TestR2CalibrationPackageBroken:
    def test_package_imports(self):
        import importlib

        try:
            importlib.import_module("tools.calibration")
        except ImportError as e:
            pytest.fail(f"tools.calibration is un-importable: {e}")

    def test_bridge_module_exists(self):
        import os

        import tools

        assert os.path.exists(os.path.join(tools.__path__[0],
                                           "calibration", "bridge.py")), (
            "__init__.py imports tools.calibration.bridge; the module does "
            "not exist"
        )


# --------------------------------------------------------------------------- #
# R3 (MERGE HAZARD): the integration train head (integration/merge-train ->
# local master 824a891) does NOT contain fa2bea9's declared-stance fix; its
# instrument.py still calls retro.PipelineResearcher._leans_yes. Merging that
# train into origin/master would silently revert the sign-of-forecast repair.
# This test pins that every candidate integration head must contain the fix.
# --------------------------------------------------------------------------- #
class TestR3IntegrationTrainLacksDeclaredStanceFix:
    def test_integration_head_contains_declared_stance(self):
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "master"],
            capture_output=True, text=True).stdout.strip()
        if not head:
            pytest.skip("no local master/integration ref in this worktree")
        retro = subprocess.run(["git", "show", f"{head}:tools/pipeline/retro.py"],
                               capture_output=True, text=True).stdout
        engine = subprocess.run(["git", "show", f"{head}:tools/pipeline/engine.py"],
                                capture_output=True, text=True).stdout
        assert "stance" in engine and "_leans_yes" not in retro.replace(
            "not a keyword scan", ""), (
            f"integration head {head[:12]} predates fa2bea9: merging it into "
            "master would revert the declared-stance fix and resurrect the "
            "keyword-sign defect pinned in 5e88b05"
        )
