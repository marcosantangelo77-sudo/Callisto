"""Battery D3 (2026-08-24): transport noise must not masquerade as epistemic
judgement.

Seven of 41 bad battery runs died because hermes_cli wrapped its JSON in
prose, the adversary's parse failed, and the fail-closed veto fired — making
a formatting quirk indistinguishable from a genuine veto.

Contract under test:
  1. prose-wrapped JSON (fences, leading commentary, trailing explanation)
     parses via the ONE shared parser;
  2. a genuine BLOCKING objection still vetoes;
  3. an unparseable response after bounded retries still fails CLOSED but is
     labelled "adversary transport failure" — a different fact from "the
     critic vetoed" — and the retries happen only on parse failure, never
     on a real verdict.
"""
import asyncio

import pytest

from agp.adversary import Adversary, AdversaryObjection
from tools.pipeline.model import extract_json, parse_model_json


# ── 1. prose-wrapped JSON parses ──────────────────────────────────────────

PROSE_WRAPPED = '''Sure — here is the verdict you asked for.

```json
{"objections": [{"kind": "false_positive", "severity": "BLOCKING",
                 "text": "the sample is survivorship-biased"}]}
```

Let me know if you need the reasoning behind each axis.'''


def test_prose_wrapped_fenced_json_parses():
    got = parse_model_json({"content": PROSE_WRAPPED})
    assert got == {"objections": [
        {"kind": "false_positive", "severity": "BLOCKING",
         "text": "the sample is survivorship-biased"}]}


def test_leading_commentary_then_bare_json_parses():
    text = ('The critic examined the claim and concluded:\n'
            '{"objections": []}\n\nThat concludes the attack.')
    assert parse_model_json({"content": text}) == {"objections": []}


def test_unparseable_first_brace_does_not_poison_extraction():
    # Old behaviour: first balanced {...} span failed json.loads -> None,
    # even though a valid object followed.
    text = 'As noted {see appendix for the full table} the verdict is: {"ok": true}'
    assert extract_json(text) == {"ok": True}


def test_genuinely_unparseable_content_returns_none():
    assert extract_json("no structured content at all") is None
    assert extract_json("") is None


def test_adversary_accepts_prose_wrapped_verdict():
    class ProseRouter:
        async def complete(self, *a, **k):
            return {"content": PROSE_WRAPPED, "model": "m1"}

    adv = Adversary(router=ProseRouter(), ledger=_SilentLedger())
    obs = asyncio.run(adv.attack("c", "conclusion", ["e"]))
    assert len(obs) == 1 and obs[0].is_blocking
    assert obs[0].text == "the sample is survivorship-biased"


class _SilentLedger:
    def record_objection(self, ob):
        pass
    def record_sustained(self, *a):
        pass
    def record_overrule(self, *a):
        pass


# ── 2. a genuine veto is final ────────────────────────────────────────────

def test_real_blocking_objection_still_vetoes_and_is_not_rerolled():
    class VerdictRouter:
        def __init__(self):
            self.calls = 0
        async def complete(self, *a, **k):
            self.calls += 1
            return {"content":
                    '{"objections": [{"kind": "selection_effect", '
                    '"severity": "BLOCKING", "text": "veto: no control group"}]}',
                    "model": "m1"}

    router = VerdictRouter()
    adv = Adversary(router=router, ledger=_SilentLedger())
    obs = asyncio.run(adv.attack("c", "conclusion", ["e"]))
    assert obs and obs[0].is_blocking
    assert router.calls == 1, "a real verdict was re-rolled"


def test_real_veto_reason_says_adversary_veto_not_transport():
    objs = [AdversaryObjection(claim_id="c", text="real flaw",
                               severity="BLOCKING")]
    score, reason = Adversary.apply_verdict(0.8, objs)
    assert reason.startswith("real flaw") or "real flaw" in reason
    assert "transport" not in reason.lower()


# ── 3. unparseable after retries: fail closed AS TRANSPORT FAILURE ────────

def test_unparseable_after_retries_fails_closed_labelled_transport():
    class GarbageRouter:
        def __init__(self):
            self.calls = 0
        async def complete(self, *a, **k):
            self.calls += 1
            return {"content": "I have thoughts but not in JSON.", "model": "m1"}

    router = GarbageRouter()
    adv = Adversary(router=router, ledger=_SilentLedger())
    obs = asyncio.run(adv.attack("c", "conclusion", ["e"]))
    assert obs and obs[0].is_blocking, "unparseable critic read as approval"
    assert router.calls == 1 + adv.PARSE_RETRIES, "retries were not bounded"
    text = obs[0].text
    assert text.startswith("adversary transport failure"), (
        f"transport noise recorded as something else: {text[:80]!r}")
    assert "backend failed" not in text.split("(")[0], (
        "crash path and parse path share a reason string")


def test_parse_failure_retry_recovers_when_second_response_parses():
    class FlakyFormatRouter:
        def __init__(self):
            self.calls = 0
        async def complete(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                return {"content": "...transmission garbled...", "model": "m"}
            return {"content": '{"objections": []}', "model": "m"}

    router = FlakyFormatRouter()
    adv = Adversary(router=router, ledger=_SilentLedger())
    obs = asyncio.run(adv.attack("c", "conclusion", ["e"]))
    assert obs == []
    assert router.calls == 2


def test_backend_crash_path_keeps_its_own_distinct_reason_string():
    class BoomRouter:
        async def complete(self, *a, **k):
            raise RuntimeError("connection reset")

    adv = Adversary(router=BoomRouter(), ledger=_SilentLedger())
    obs = asyncio.run(adv.attack("c", "conclusion", ["e"]))
    assert obs[0].is_blocking
    assert obs[0].text.startswith("adversary backend failed")
    assert "transport failure" not in obs[0].text
