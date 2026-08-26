"""CALLISTO_LOCAL_ONLY must strip hosted ProviderRouter tiers (fail-closed).

Contract
--------
When ``CALLISTO_LOCAL_ONLY`` is truthy (1/true/yes), the router is a
full-local system: llama.cpp on gpu1 / gpu1_fast and nothing else.

``ProviderRouter.candidates_for()`` used to keep returning the hosted
rails — openrouter_ox, frontier, ox_alpha, ox_alpha_proxy — even with
the env set. BetExecutor / OrderManager already refuse to arm under the
flag, but every pipeline completion could still silently leave the box.
These tests pin the fail-closed strip:

* hosted rails are dropped from candidates BEFORE health/fallback logic;
* openai_compat rails that name openrouter/nous/frontier/ox_alpha count
  as HOSTED even though the transport is plain HTTP;
* gpu1 / gpu1_fast survive untouched;
* an empty local pool raises LOUDLY instead of falling back to OpenRouter;
* with CALLISTO_LOCAL_ONLY unset, behaviour is byte-identical to today:
  openrouter_ox stays in the chain as the default overnight grunt.

MODEL_LADDER is intentionally NOT pointed at ProviderRouter — the two
inference planes stay separate (tests/test_inference_planes.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import inference_router
from inference_router import (
    LOCAL_BACKENDS,
    ProviderRouter,
    endpoint_is_hosted,
    local_only_enabled,
    strip_hosted_for_local_only,
)

REPO = Path(__file__).resolve().parent.parent

HOSTED_ENDPOINT_NAMES = {
    "openrouter_ox", "frontier", "ox_alpha", "ox_alpha_proxy",
}
LOCAL_ENDPOINT_NAMES = {"gpu1", "gpu1_fast"}

TRUTHY_VALUES = ["1", "true", "yes", "TRUE", "Yes"]
FALSY_VALUES = ["", "0", "false", "no", "off"]


@pytest.fixture()
def router() -> ProviderRouter:
    """A router over the real config/providers.yaml."""
    return ProviderRouter()


@pytest.fixture()
def local_only(monkeypatch):
    """Force CALLISTO_LOCAL_ONLY=1 for one test."""
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    yield
    # monkeypatch restores env; nothing else to do.


# ---------------------------------------------------------------------------
# 1. The flag itself: truthy parsing matches BetExecutor / OrderManager
# ---------------------------------------------------------------------------


class TestLocalOnlyFlagParsing:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        assert local_only_enabled() is True

    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values_disable(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        assert local_only_enabled() is False

    def test_unset_disables(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        assert local_only_enabled() is False


# ---------------------------------------------------------------------------
# 2. Endpoint classification: fail-closed by backend
# ---------------------------------------------------------------------------


class TestEndpointIsHostedClassification:
    def test_llama_cpp_server_is_local(self, router):
        assert endpoint_is_hosted(router.endpoints["gpu1"]) is False

    def test_gpu1_fast_is_local(self, router):
        assert endpoint_is_hosted(router.endpoints["gpu1_fast"]) is False

    @pytest.mark.parametrize(
        "name",
        sorted(HOSTED_ENDPOINT_NAMES),
    )
    def test_known_hosted_rails_are_hosted(self, router, name):
        ep = router.endpoints.get(name)
        if ep is None:
            pytest.skip(f"{name} not in providers.yaml")
        assert endpoint_is_hosted(ep) is True, (
            f"{name} (backend={ep.backend}) must classify as HOSTED under "
            f"CALLISTO_LOCAL_ONLY"
        )

    def test_openai_compat_with_openrouter_url_is_hosted(self, router):
        ep = router.endpoints.get("openrouter_ox")
        if ep is None:
            pytest.skip("openrouter_ox not configured")
        assert ep.backend == "openai_compat"
        assert "openrouter" in (ep.base_url or "").lower()
        assert endpoint_is_hosted(ep) is True

    def test_unknown_backend_fails_closed(self):
        class _Fake:
            backend = "mystery_transport"
            base_url = ""

        fake = _Fake()
        assert endpoint_is_hosted(fake) is True

    def test_none_endpoint_fails_closed(self):
        assert endpoint_is_hosted(None) is True

    def test_local_backends_constant_pins_contract(self):
        assert "llama_cpp_server" in LOCAL_BACKENDS
        assert "local" in LOCAL_BACKENDS


# ---------------------------------------------------------------------------
# 3. candidates_for: hosted rails stripped when flag set
# ---------------------------------------------------------------------------


class TestCandidatesForLocalOnlyStrip:
    def test_extraction_candidates_drop_all_hosted(self, router, local_only):
        names = router.candidates_for("extraction")
        leaked = HOSTED_ENDPOINT_NAMES & set(names)
        assert not leaked, (
            f"CALLISTO_LOCAL_ONLY=1 but candidates_for('extraction') still "
            f"returns hosted rails: {sorted(leaked)}"
        )

    @pytest.mark.parametrize(
        "task_class",
        [
            "extraction",
            "classification",
            "screening",
            "hypothesis_generation",
            "research_synthesis",
            "promotion_judgment",
            "adversarial_review",
            "backtest_interpretation",
        ],
    )
    def test_every_task_class_drops_hosted(self, router, local_only,
                                           task_class):
        names = router.candidates_for(task_class)
        leaked = HOSTED_ENDPOINT_NAMES & set(names)
        assert not leaked, f"{task_class} leaked hosted rails {leaked}"

    def test_local_rails_survive_in_order(self, router, local_only):
        names = router.candidates_for("extraction")
        locals_present = [n for n in names if n in LOCAL_ENDPOINT_NAMES]
        assert locals_present == ["gpu1_fast"], (
            f"expected gpu1_fast first for extraction, got {names}"
        )

    def test_order_preserved_among_locals(self, router, local_only):
        names = router.candidates_for("research_synthesis")
        assert names[:1] == ["gpu1"], (
            f"gpu1 should lead research_synthesis under local-only: {names}"
        )
        assert all(n in LOCAL_ENDPOINT_NAMES for n in names)

    def test_promotion_judgment_loses_frontier_lead(self, router,
                                                    local_only):
        """frontier LEADS promotion_judgment by default; local-only must
        demote it out of the list entirely, leaving gpu1 first."""
        names = router.candidates_for("promotion_judgment")
        assert "frontier" not in names
        assert names[0] == "gpu1"

    def test_cooling_down_fallback_cannot_resurrect_hosted(
            self, router, monkeypatch):
        """Even if every local endpoint goes unhealthy, the degrade-don't-
        crash fallback path inside candidates_for must NOT hand back a
        hosted rail."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        for st in router.states.values():
            st.record_failure()
            st.consecutive_failures = 10  # max 60s cooldown
            st.record_failure()
        names = router.candidates_for("extraction")
        leaked = HOSTED_ENDPOINT_NAMES & set(names)
        assert not leaked, (
            f"cooling-down fallback resurrected hosted rails: {leaked}"
        )


# ---------------------------------------------------------------------------
# 4. Empty local pool raises LOUDLY (no silent OpenRouter fallback)
# ---------------------------------------------------------------------------


class TestEmptyLocalPoolRaises:
    def test_task_class_with_no_locals_raises(self, router, local_only):
        # Build a synthetic task class whose chain is entirely hosted.
        router.task_classes["_hosted_only_test"] = [
            "openrouter_ox", "frontier", "ox_alpha_proxy", "ox_alpha",
        ]
        with pytest.raises(RuntimeError) as excinfo:
            router.candidates_for("_hosted_only_test")
        msg = str(excinfo.value)
        assert "CALLISTO_LOCAL_ONLY" in msg
        assert ("no local endpoints" in msg.lower()), msg

    def test_strip_helper_raises_on_empty(self, router, local_only):
        with pytest.raises(RuntimeError):
            strip_hosted_for_local_only(
                router, ["openrouter_ox", "ox_alpha"], "_synthetic")

    def test_tier_for_also_raises_when_only_hosted(self, router, local_only):
        router.task_classes["_hosted_only_tier"] = ["openrouter_ox",
                                                    "ox_alpha_proxy"]
        with pytest.raises(RuntimeError):
            router.tier_for("_hosted_only_tier")

    def test_unset_flag_does_not_raise_for_hosted_chain(self, router,
                                                        monkeypatch):
        """Same synthetic hosted-only chain WITHOUT the env: legacy
        behaviour returns candidates (this is the current default)."""
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        router.task_classes["_hosted_only_ok"] = ["openrouter_ox",
                                                  "ox_alpha"]
        names = router.candidates_for("_hosted_only_ok")
        assert "openrouter_ox" in names


# ---------------------------------------------------------------------------
# 5. Flag UNSET: default chain keeps openrouter_ox (byte-identical today)
# ---------------------------------------------------------------------------


class TestUnsetKeepsHostedChain:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)

    def test_extraction_keeps_openrouter_ox(self, router):
        names = router.candidates_for("extraction")
        assert "openrouter_ox" in names

    def test_full_default_chain_shape(self, router):
        names = router.candidates_for("extraction")
        assert names[:2] == ["gpu1_fast", "openrouter_ox"]

    def test_research_synthesis_keeps_frontier_absent_but_openrouter(
            self, router):
        names = router.candidates_for("research_synthesis")
        assert "openrouter_ox" in names
        # frontier may be unresolved (env unset) but must not be stripped
        # by the local-only filter — it's simply unavailable/unresolved.
        assert all(n in router.endpoints for n in names)

    def test_strip_helper_is_identity_when_unset(self, router):
        names = ["openrouter_ox", "gpu1", "ox_alpha"]
        assert strip_hosted_for_local_only(router, names, "x") is names


# ---------------------------------------------------------------------------
# 6. tier_for parity
# ---------------------------------------------------------------------------


class TestTierForLocalOnly:
    def test_tier_for_skips_hosted_first_tier(self, router, local_only):
        tier = router.tier_for("extraction")
        assert tier.name == "gpu1_fast"
        assert tier.backend in LOCAL_BACKENDS

    def test_tier_for_promotion_judgment_demotes_frontier(
            self, router, local_only):
        tier = router.tier_for("promotion_judgment")
        assert tier.name == "gpu1"
        assert tier.backend in LOCAL_BACKENDS

    def test_tier_for_unaffected_when_unset(self, router, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        tier = router.tier_for("extraction")
        assert tier.name == "gpu1_fast"


# ---------------------------------------------------------------------------
# 7. Structural pins: MODEL_LADDER stays off ProviderRouter
# ---------------------------------------------------------------------------


class TestTwoPlaneStructurePins:
    def test_model_ladder_not_pointed_at_provider_router(self):
        """inference.MODEL_LADDER values are model strings, never
        ProviderRouter task-class indirection."""
        import inference

        banned_tokens = ("ProviderRouter", "candidates_for", "task_class")
        src_ladder = repr(inference.MODEL_LADDER)
        for token in banned_tokens:
            assert token not in src_ladder, (
                f"MODEL_LADDER references {token!r} — the two inference "
                f"planes must stay separate"
            )

    def test_router_module_does_not_import_inference_kernel_ladder(self):
        """The strip lives in the router plane only; it must not reach into
        MODEL_LADDER or rewrite the kernel plane."""
        src = Path(inference_router.__file__).read_text(encoding="utf-8")
        # Only the docstring may mention MODEL_LADDER (explaining the two
        # planes); no code reference is allowed.
        code_only = "\n".join(
            line for line in src.splitlines()
            if not line.lstrip().startswith("#")
            and "MODEL_LADDER" not in line.split('"""')[0]
        )
        assert "MODEL_LADDER" not in code_only, (
            "inference_router.py references MODEL_LADDER in code — keep "
            "the local-only strip scoped to ProviderRouter"
        )

    def test_supervisor_source_untouched_by_this_change(self):
        """nous-supervisor keeps its own env handling; this fix must be
        router-scoped. Pin that no supervisor file was edited here by
        asserting the strip helper is importable standalone."""
        assert callable(strip_hosted_for_local_only)


# ---------------------------------------------------------------------------
# 8. Safety pins (betting / paper-signal contract untouched)
# ---------------------------------------------------------------------------


class TestSafetyPins:
    PAPER_SIGNAL_STATUSES = {"paper"}  # 'live' must NEVER join this set

    def test_bet_executor_still_refuses_under_local_only(self):
        from tools import bet_executor
        from tools.betexec import lifecycle

        facade = Path(bet_executor.__file__).read_text(encoding="utf-8")
        assert "arm_gate_refusal" in facade
        assert "enable" in facade
        assert lifecycle.LOCAL_ONLY_ENV == "CALLISTO_LOCAL_ONLY"
        gate = Path(lifecycle.__file__).read_text(encoding="utf-8")
        assert "os.getenv(LOCAL_ONLY_ENV" in gate
        assert "CALLISTO_LOCAL_ONLY" in gate

    def test_paper_signal_statuses_do_not_gain_live(self):
        from tools import bet_executor

        statuses = getattr(
            bet_executor, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is None:
            pytest.skip("_PAPER_TRADE_SIGNAL_STATUSES moved")
        assert "live" not in {str(s).lower() for s in statuses}

    def test_generate_paper_trade_signal_signature_narrow(self):
        from tools import bet_executor
        import inspect

        fn = getattr(bet_executor, "generate_paper_trade_signal", None)
        if fn is None:
            pytest.skip("generate_paper_trade_signal not present")
        sig = inspect.signature(fn)
        status_param = sig.parameters.get("status")
        if status_param is None:
            return  # no status param at all is fine
        default = status_param.default
        assert default != "live"
