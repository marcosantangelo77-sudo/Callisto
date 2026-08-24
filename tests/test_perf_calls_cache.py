"""PERF WAVE 3 — call COUNT and token VOLUME (tests/test_perf_calls_*).

Unit 1 pins for tools/pipeline/cache.py:

CountingModel:
  - records exactly one row per completion with role, chars, est. tokens
  - delegation is transparent (responses unchanged, kwargs pass through)
  - summary()/table() totals are consistent with the rows

PromptCache / CachingModel:
  - identical (scope, role, messages) -> hit; anything else -> miss
  - CUTOFF SAFETY: a scope is an unbreakable key partition — no prompt,
    however identical, can match across two scopes. A retrodiction run
    scoped to its claim date can never read an entry written under
    another date's scope, so future evidence cannot leak into a
    past-dated run through the cache.
  - FAIL-CLOSED construction: CachingModel without scope and without
    mode="live" refuses to exist.
  - live mode day-stamps: entries cannot silently outlive their day.
  - TTL expiry is honoured (expired entry = miss).
  - THE ADVERSARY IS NEVER CACHED — every attack is a fresh independent
    call even when the prompt is byte-identical to one already served.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tools.pipeline.cache import (
    CachingModel,
    CountingModel,
    PromptCache,
    cache_key,
)
from tools.pipeline.model import ScriptedModel


# ── helpers ────────────────────────────────────────────────────────────────

def _msgs(q: str) -> list[dict]:
    return [{"role": "system", "content": "SYS"},
            {"role": "user", "content": f"QUESTION: {q}"}]


class _Inner(ScriptedModel):
    """ScriptedModel + a call counter that survives cache interception."""

    def __init__(self):
        super().__init__()
        self.n_calls = 0

    async def complete(self, role, messages, **kw):
        self.n_calls += 1
        return await super().complete(role, messages, **kw)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── CountingModel ──────────────────────────────────────────────────────────

def test_counting_records_one_row_per_call():
    inner = _Inner()
    m = CountingModel(inner)

    async def go():
        await m.complete("Architect", _msgs("q one"))
        await m.complete("Manager", _msgs("q two"))

    _run(go())
    assert len(m.rows) == 2 == inner.n_calls
    assert [r["role"] for r in m.rows] == ["Architect", "Manager"]
    assert all(r["in_tokens_est"] > 0 for r in m.rows)


def test_counting_is_transparent():
    inner = _Inner()
    inner.script("Architect", json.dumps({"sub_questions": []}))
    m = CountingModel(inner)

    async def go():
        return await m.complete("Architect", _msgs("x"))

    a = _run(go())
    assert a["content"] == json.dumps({"sub_questions": []})


def test_counting_summary_totals_match_rows():
    m = CountingModel(_Inner())

    async def go():
        await m.complete("Manager", _msgs("hello world"))
        await m.complete("Adversary", _msgs("attack"))

    _run(go())
    s = m.summary()
    assert s["calls"] == 2
    assert s["in_chars"] == sum(r["in_chars"] for r in m.rows)
    assert s["total_tokens_est"] == sum(
        r["in_tokens_est"] + r["out_tokens_est"] for r in m.rows)
    assert "TOTAL" in m.table()


def test_counting_survives_adversary_style_kwargs():
    """The Adversary calls complete(task_class, messages, schema=...); the
    counter must pass unknown kwargs through untouched."""
    seen = {}

    class _KW:
        name = "kw"

        async def complete(self, role, messages, **kw):
            seen.update(kw)
            return {"content": "{}"}

    m = CountingModel(_KW())

    async def go():
        await m.complete("adversarial_review", _msgs("c"), schema={"a": 1})

    _run(go())
    assert seen == {"schema": {"a": 1}}
    assert m.rows[0]["role"] == "adversarial_review"


# ── content addressing ─────────────────────────────────────────────────────

def test_same_inputs_same_key_different_inputs_different_key():
    k1 = cache_key("retro:2024-01-03", "Manager", _msgs("same"))
    k2 = cache_key("retro:2024-01-03", "Manager", _msgs("same"))
    assert k1 == k2
    assert cache_key("s", "a", _msgs("q")) != cache_key("s", "b", _msgs("q"))
    assert cache_key("s", "a", _msgs("q")) != cache_key("s", "a", _msgs("q2"))


def test_scope_partition_is_absolute():
    """The whole point: byte-identical prompts under different scopes never
    share an entry. This is what makes cross-cutoff leakage impossible."""
    assert cache_key("retro:2024-01-03", "M", _msgs("q")) != \
        cache_key("retro:2024-05-22", "M", _msgs("q"))
    assert cache_key("live:2026-08-24", "M", _msgs("q")) != \
        cache_key("retro:2026-08-24", "M", _msgs("q"))


# ── CachingModel ───────────────────────────────────────────────────────────

def test_hit_on_repeat_miss_on_new(tmp_path):
    inner = _Inner()
    c = CachingModel(inner, PromptCache(str(tmp_path)),
                     scope="retro:2024-01-03")

    async def go():
        r1 = await c.complete("Manager", _msgs("apple beat?"))
        r2 = await c.complete("Manager", _msgs("apple beat?"))
        r3 = await c.complete("Manager", _msgs("nvidia beat?"))
        return r1, r2, r3

    r1, r2, r3 = _run(go())
    assert r1 == r2                      # served identically
    assert inner.n_calls == 2            # only two real calls for three asks
    assert c.last_from_cache is False    # r3 was fresh
    assert c.cache.hits == 1 and c.cache.misses == 2


def test_no_scope_without_live_mode_refuses(tmp_path):
    with pytest.raises(ValueError, match="scope"):
        CachingModel(_Inner(), PromptCache(str(tmp_path)))


def test_scope_and_live_mode_are_exclusive(tmp_path):
    with pytest.raises(ValueError, match="not both"):
        CachingModel(_Inner(), PromptCache(str(tmp_path)),
                     scope="retro:2024-01-03", mode="live")


def test_live_mode_day_stamps(tmp_path):
    from datetime import datetime, timezone
    c = CachingModel(_Inner(), PromptCache(str(tmp_path)), mode="live")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert c.scope.startswith("live:") and today in c.scope


def test_ttl_expiry_is_a_miss(tmp_path):
    inner = _Inner()
    cache = PromptCache(str(tmp_path), ttl_s=-1.0)   # everything expires now
    c = CachingModel(inner, cache, scope="retro:2024-01-03")

    async def go():
        await c.complete("Manager", _msgs("q"))
        return await c.complete("Manager", _msgs("q"))

    _run(go())
    assert inner.n_calls == 2
    assert cache.expiries == 1


def test_corrupt_entry_is_a_miss_not_a_crash(tmp_path):
    import os
    cache = PromptCache(str(tmp_path))
    from tools.pipeline.cache import cache_key as ck
    key = ck("s", "Manager", _msgs("q"))
    p = cache._path("s", key)
    open(p, "w").write("{not json")
    inner = _Inner()
    c = CachingModel(inner, cache, scope="s")

    async def go():
        return await c.complete("Manager", _msgs("q"))

    _run(go())
    assert inner.n_calls == 1            # fell through and generated


# ── the adversary stays independent ────────────────────────────────────────

def test_adversary_is_never_cached_even_byte_identical(tmp_path):
    """A critic sharing context with the author is not a critic. The attack
    call is always a fresh generation, even for an identical prompt."""
    inner = _Inner()
    inner.script("Adversary", *[json.dumps({"objections": [
        {"kind": "false_positive", "severity": "MINOR",
         "text": f"objection {i}"}]}) for i in range(3)])
    c = CachingModel(inner, PromptCache(str(tmp_path)),
                     scope="retro:2024-01-03")
    msgs = _msgs("attack this conclusion")

    async def go():
        outs = []
        for _ in range(3):
            outs.append(await c.complete("Adversary", msgs))
        # Sentinel too:
        await c.complete("Sentinel", msgs)
        await c.complete("Sentinel", msgs)
        return outs

    outs = _run(go())
    assert inner.n_calls == 5            # zero cache service for critics
    texts = [json.loads(o["content"])["objections"][0]["text"]
             for o in outs]
    assert texts == ["objection 0", "objection 1", "objection 2"]
    assert c.cache.hits == 0


def test_author_roles_cached_but_distinct_from_each_other(tmp_path):
    """Same question text under Architect vs Manager roles must not share
    an entry — role is part of the address."""
    inner = _Inner()
    inner.default = {"content": '{"ok":true}'}
    c = CachingModel(inner, PromptCache(str(tmp_path)),
                     scope="retro:2024-01-03")

    async def go():
        await c.complete("Architect", _msgs("q"))
        await c.complete("Manager", _msgs("q"))
        await c.complete("Architect", _msgs("q"))
        await c.complete("Manager", _msgs("q"))

    _run(go())
    assert inner.n_calls == 2
