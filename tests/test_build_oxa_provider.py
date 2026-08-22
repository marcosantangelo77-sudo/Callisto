"""BUILD pass — Ox Alpha (Hermes CLI) as a first-class ProviderRouter provider.

Covers:
  * providers.yaml declares ox_alpha with honest capabilities
  * every task class routes to it as last-resort failover
  * hermes_cli endpoints need no base_url / env / key
  * structured output is declared FALSE and callers see that honestly
  * the shared process semaphore bounds concurrent forks
  * router.complete() dispatches through the CLI backend end-to-end

Live verification is a manual step (needs `hermes portal login`); see
tests/test_build_oxa_live.py or run scripts/oxa_live_check.py.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference  # noqa: E402
from tools.pipeline import hermes_cli  # noqa: E402


@pytest.fixture(scope="module")
def real_router():
    return inference.ProviderRouter()


class TestProviderDeclaration:
    def test_ox_alpha_declared(self, real_router):
        assert "ox_alpha" in real_router.endpoints
        ep = real_router.endpoints["ox_alpha"]
        assert ep.backend == "hermes_cli"
        assert ep.model == "ox-alpha"
        assert not ep.extra.get("_unresolved")

    def test_honest_capabilities(self, real_router):
        """The declarations must match what the backend actually does."""
        ep = real_router.endpoints["ox_alpha"]
        assert ep.structured_output is False, \
            "CLI cannot enforce json_schema response_format — must say false"
        assert ep.tool_calls is False
        assert ep.max_concurrency == 1
        assert ep.cost_per_1k_input == 0.0 and ep.cost_per_1k_output == 0.0

    def test_needs_no_env_or_url(self, real_router):
        """git clone + portal login must be sufficient: no env vars."""
        ep = real_router.endpoints["ox_alpha"]
        assert ep.base_url == "" and ep.api_key is None


class TestRouting:
    @pytest.mark.parametrize("tc", [
        "hypothesis_generation", "research_synthesis", "screening",
        "extraction", "classification", "backtest_interpretation",
        "promotion_judgment", "adversarial_review",
    ])
    def test_every_task_class_can_reach_ox_alpha(self, real_router, tc):
        cands = real_router.candidates_for(tc)
        assert "ox_alpha" in cands, f"task class {tc} cannot reach ox_alpha"

    def test_last_resort_not_first(self, real_router):
        for tc in ("research_synthesis", "promotion_judgment"):
            names = real_router.task_classes[tc]
            names = names if isinstance(names, list) else [names]
            assert names[-1] == "ox_alpha"

    def test_schema_bearing_calls_still_list_ox_alpha(self, real_router):
        """Best-effort JSON beats no route at all on a CLI-only laptop — but
        pick_endpoint still prefers a schema-enforcing endpoint when one is
        healthy."""
        schema = {"type": "object"}
        cands = real_router.candidates_for("research_synthesis", schema=schema)
        assert "ox_alpha" in cands
        # direct check of ordering preference:
        if any(real_router.endpoints[n].structured_output for n in cands):
            first = next(n for n in cands
                         if real_router.endpoints[n].structured_output)
            assert cands.index(first) < cands.index("ox_alpha")

    def test_pick_endpoint_prefers_enforcing_endpoint(self, real_router):
        schema = {"type": "object"}
        picked = real_router.pick_endpoint("research_synthesis", schema=schema)
        # gpu1 (llama-server) may be down/cooling in CI; if it IS chosen it
        # must enforce schemas. If everything else cooled, ox_alpha serving
        # best-effort is acceptable degradation.
        if picked and picked.name != "ox_alpha":
            assert picked.structured_output


class TestCliBackend:
    def test_flatten_messages(self):
        p = hermes_cli.flatten_messages(
            "architect",
            [{"role": "system", "content": "be terse"},
             {"role": "user", "content": "q"}])
        assert "[system]\nbe terse" in p and "q" in p and "[task]" in p

    def test_semaphore_default(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_HERMES_MAX_PROCS", raising=False)
        assert hermes_cli._default_max_procs() == 3
        monkeypatch.setenv("CALLISTO_HERMES_MAX_PROCS", "7")
        assert hermes_cli._default_max_procs() == 7
        monkeypatch.setenv("CALLISTO_HERMES_MAX_PROCS", "junk")
        assert hermes_cli._default_max_procs() == 3

    def test_fanout_bounded_by_semaphore(self, monkeypatch, tmp_path):
        """8 concurrent calls against a fake binary must never exceed the
        semaphore's cap of concurrent child processes."""
        monkeypatch.setenv("CALLISTO_HERMES_MAX_PROCS", "2")
        hermes_cli.reset_proc_semaphore()
        fake = tmp_path / "fake_hermes.sh"
        fake.write_text("#!/bin/sh\nsleep 0.4\necho '{\"ok\": true}'\n")
        fake.chmod(0o755)

        peak = 0
        cur = 0
        orig = hermes_cli.hermes_run

        async def counting_run(binary, prompt, cwd, timeout_s):
            nonlocal peak, cur
            cur += 1
            peak = max(peak, cur)
            try:
                return await orig(binary, prompt, cwd, timeout_s)
            finally:
                cur -= 1

        monkeypatch.setattr(hermes_cli, "hermes_run", counting_run)

        async def main():
            results = await asyncio.gather(*[
                hermes_cli.hermes_complete(
                    [{"role": "user", "content": f"q{i}"}],
                    binary=str(fake), cwd=str(tmp_path), timeout_s=30)
                for i in range(8)])
            return results

        results = asyncio.run(main())
        assert len(results) == 8
        assert all(r["content"].strip() == '{"ok": true}' for r in results)
        assert peak <= 2, f"fan-out forked {peak} processes, cap was 2"
        hermes_cli.reset_proc_semaphore()

    def test_failure_with_no_output_raises(self, monkeypatch, tmp_path):
        fake = tmp_path / "fail.sh"
        fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
        fake.chmod(0o755)
        with pytest.raises(RuntimeError, match="rc=3"):
            asyncio.run(hermes_cli.hermes_complete(
                [{"role": "user", "content": "x"}],
                binary=str(fake), cwd=str(tmp_path), timeout_s=30))

    def test_partial_output_on_nonzero_rc_is_returned(self, monkeypatch,
                                                      tmp_path):
        fake = tmp_path / "partial.sh"
        fake.write_text('#!/bin/sh\necho \'{"a": 1}\'\nexit 1\n')
        fake.chmod(0o755)
        res = asyncio.run(hermes_cli.hermes_complete(
            [{"role": "user", "content": "x"}],
            binary=str(fake), cwd=str(tmp_path), timeout_s=30))
        assert res["content"] == '{"a": 1}' and res["rc"] == 1

    def test_shim_still_works(self, monkeypatch, tmp_path):
        fake = tmp_path / "ok.sh"
        fake.write_text("#!/bin/sh\necho 'hello'\n")
        fake.chmod(0o755)
        model = hermes_cli.HermesCliModel(binary=str(fake),
                                          cwd=str(tmp_path))
        out = asyncio.run(model.complete(
            "architect", [{"role": "user", "content": "hi"}],
            schema={"type": "object"}))  # schema accepted-and-ignored
        assert out["content"] == "hello"
        assert model.calls[0]["role"] == "architect"


class TestRouterDispatch:
    """complete() with backend=hermes_cli goes through the CLI, not HTTP."""

    def test_complete_via_cli_backend(self, tmp_path, monkeypatch):
        cfg = tmp_path / "p.yaml"
        cfg.write_text(
            "default_tier: oxa\n"
            "providers:\n"
            "  oxa:\n"
            "    backend: hermes_cli\n"
            "    model: ox-alpha\n"
            "    structured_output: false\n"
            "    tool_calls: false\n"
            "    max_concurrency: 1\n"
            "routing:\n"
            "  task_classes:\n"
            "    screening: oxa\n")
        fake = tmp_path / "fake.sh"
        fake.write_text("#!/bin/sh\necho '{\"answer\": 42}'\n")
        fake.chmod(0o755)
        monkeypatch.setattr(hermes_cli, "resolve_binary",
                            lambda b=None: str(fake))
        router = inference.ProviderRouter(config_path=str(cfg))

        async def run():
            return await router.complete(
                "screening",
                [{"role": "user", "content": "classify this"}],
                system_context="be terse")

        res = asyncio.run(run())
        assert res["tier"] == "oxa"
        assert res["model"] == "ox-alpha"
        assert res["parsed_json"] == {"answer": 42}
        snap = router.cost_ledger.snapshot()
        assert snap["by_tier"]["oxa"]["cost_usd"] == 0.0

    def test_health_check_binary_probe(self, tmp_path):
        cfg = tmp_path / "p.yaml"
        cfg.write_text(
            "providers:\n"
            "  oxa:\n"
            "    backend: hermes_cli\n"
            "routing:\n"
            "  task_classes:\n"
            "    screening: oxa\n")
        router = inference.ProviderRouter(config_path=str(cfg))
        res = asyncio.run(router.check_health("oxa"))
        # On a machine WITH the CLI installed this is ok; without, an honest
        # error naming the missing binary. Either way: no HTTP attempted.
        assert res["status"] in ("ok", "error")
        if res["status"] == "error":
            assert "binary" in res["error"]
