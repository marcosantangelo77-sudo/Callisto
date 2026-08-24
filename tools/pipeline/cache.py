"""Call-count and token accounting + content-addressed prompt caching.

THIRD LEVER (performance wave 2026-08-24): transport made calls cheap,
parallelism overlapped them; this module removes calls and shrinks what
each one carries.

Three pieces, all at the MODEL SEAM so engine.py/retrieval.py are untouched
(exclusive file ownership):

  CountingModel  — wraps any PipelineModel, records one row per completion:
                   role, prompt/response chars, estimated tokens (chars/4),
                   duration, and whether the answer came from cache. This is
                   the instrument: you cannot cut what you have not counted.

  PromptCache    — content-addressed store: sha256(scope | role | flattened
                   messages) -> response dict, file-backed, honest TTL.
                   CUTOFF SAFETY IS STRUCTURAL: every key includes a SCOPE
                   string. A retrodiction run MUST scope itself to its
                   claim date (CachingModel(..., scope=f"retro:{date}")),
                   which makes cross-boundary reuse impossible by
                   construction — different scope = different key = miss =
                   fresh generation from only pre-cutoff evidence. An
                   UNSCOPED CachingModel refuses to construct unless the
                   caller asserts mode="live"; live entries are additionally
                   day-stamped so nothing silently survives past its day.

  CachingModel   — PipelineModel wrapper: lookup, else delegate and store.
                   THE ADVERSARY IS NEVER CACHED. A critic sharing a stored
                   verdict with a later identical-looking authoring context
                   is exactly the contamination the separation of concerns
                   forbids; identical attack prompts each get a fresh call.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from tools.pipeline.model import PipelineModel

#: ~4 characters per token for English prose + JSON; an ESTIMATE used for
#: reporting only, never billed or asserted against a provider ledger.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(0, len(text or "") // CHARS_PER_TOKEN)


def _flatten(messages: list[dict]) -> str:
    return "\n".join(f"[{m.get('role', '?')}]\n{m.get('content', '')}"
                     for m in messages or [])


def cache_key(scope: str, role: str, messages: list[dict]) -> str:
    """Content address: scope FIRST, then role, then exact message bytes.

    Scope-first ordering is the cutoff guarantee: it is impossible to build
    a key that matches across scopes regardless of prompt contents.
    """
    h = hashlib.sha256()
    h.update(b"callisto.prompt.v1\x00")
    h.update(str(scope).encode("utf-8", errors="replace"))
    h.update(b"\x00")
    h.update(str(role).encode("utf-8", errors="replace"))
    h.update(b"\x00")
    h.update(_flatten(messages).encode("utf-8", errors="replace"))
    return h.hexdigest()


class CacheEntryMiss(Exception):
    pass


class PromptCache:
    """File-backed, TTL'd, scope-partitioned response store.

    Layout: <root>/<scope>/<key>.json holding
      {"response": {...}, "created": iso8601, "expires": epoch}
    Scope is a DIRECTORY partition, so pruning or inspecting one run's
    entries never touches another's.
    """

    def __init__(self, root: str, *, ttl_s: float = 24 * 3600):
        self.root = root
        self.ttl_s = float(ttl_s)
        self.hits = 0
        self.misses = 0
        self.expiries = 0

    def _path(self, scope: str, key: str) -> str:
        d = os.path.join(self.root, _safe_scope(scope))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{key}.json")

    def get(self, scope: str, key: str) -> Optional[dict]:
        path = self._path(scope, key)
        if not os.path.exists(path):
            self.misses += 1
            return None
        try:
            rec = json.loads(open(path, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None
        created = rec.get("created_epoch", 0)
        if time.time() - created > self.ttl_s:
            self.expiries += 1
            self.misses += 1
            return None
        self.hits += 1
        return dict(rec.get("response") or {})

    def put(self, scope: str, key: str, response: dict) -> None:
        now = time.time()
        rec = {
            "response": response,
            "created": datetime.now(timezone.utc).isoformat(),
            "created_epoch": now,
            "expires_epoch": now + self.ttl_s,
            "scope": scope,
        }
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", dir=os.path.dirname(self._path(scope, key)),
            delete=False, encoding="utf-8")
        try:
            json.dump(rec, tmp, ensure_ascii=False)
            tmp.close()
            os.replace(tmp.name, self._path(scope, key))
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses,
                "expired": self.expiries}


def _safe_scope(scope: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in scope)
    return out[:120] or "_"


class CountingModel(PipelineModel):
    """Transparent wrapper: counts every completion passing through.

    rows() gives one dict per call; summary() folds them into the table the
    measurement brief asks for. Delegation is unconditional — this changes
    no behaviour, it only sees.
    """

    name = "counting"

    def __init__(self, inner: PipelineModel,
                 purpose_of: Optional[Callable[[str, str], str]] = None):
        self.inner = inner
        self._purpose_of = purpose_of
        self.rows: list[dict] = []

    @property
    def real_name(self) -> str:
        return getattr(self.inner, "name", "model")

    async def complete(self, role: str, messages: list[dict],
                       **kw) -> dict:
        prompt = _flatten(messages)
        t0 = time.monotonic()
        resp = await self.inner.complete(role, messages, **kw)
        dur = time.monotonic() - t0
        content = resp.get("content") or ""
        row = {
            "role": str(role),
            "purpose": (self._purpose_of(role, prompt) if self._purpose_of
                        else ""),
            "in_chars": len(prompt),
            "out_chars": len(content),
            "in_tokens_est": estimate_tokens(prompt),
            "out_tokens_est": estimate_tokens(content),
            "dur_s": round(dur, 3),
        }
        self.rows.append(row)
        return resp

    def summary(self) -> dict:
        n = len(self.rows)
        return {
            "calls": n,
            "in_chars": sum(r["in_chars"] for r in self.rows),
            "out_chars": sum(r["out_chars"] for r in self.rows),
            "in_tokens_est": sum(r["in_tokens_est"] for r in self.rows),
            "out_tokens_est": sum(r["out_tokens_est"] for r in self.rows),
            "total_tokens_est": sum(r["in_tokens_est"] + r["out_tokens_est"]
                                    for r in self.rows),
        }

    def table(self) -> str:
        hdr = (f"{'#':>3} {'role':<10} {'purpose':<22} "
               f"{'in_tok':>7} {'out_tok':>7} {'dur_s':>7}")
        lines = [hdr, "-" * len(hdr)]
        for i, r in enumerate(self.rows):
            lines.append(
                f"{i:>3} {r['role']:<10} {r['purpose'][:22]:<22} "
                f"{r['in_tokens_est']:>7} {r['out_tokens_est']:>7} "
                f"{r['dur_s']:>7}")
        s = self.summary()
        lines.append("-" * len(hdr))
        lines.append(f"TOTAL calls={s['calls']} "
                     f"in_tok={s['in_tokens_est']} "
                     f"out_tok={s['out_tokens_est']} "
                     f"tok={s['total_tokens_est']}")
        return "\n".join(lines)


#: Roles whose completions may NEVER be served from or written to the cache.
#: The adversary (and any sentinel review) stays an independent, fresh call
#: every time — a critic does not share context with the author, including
#: context a previous run left behind.
NON_CACHEABLE_ROLES = frozenset({"Adversary", "Sentinel"})


def default_cacheable(role: str) -> bool:
    return str(role) not in NON_CACHEABLE_ROLES


class CachingModel(PipelineModel):
    """Serve repeated identical prompts from a content-addressed cache.

    Construction is FAIL-CLOSED on scope discipline:
      * scope given          -> all keys carry it. A retrodiction run scopes
                                itself to its claim date; entries can never
                                cross a cutoff boundary because they can
                                never match across scopes.
      * scope=None           -> refused UNLESS mode="live", which stamps
                                entries into a per-UTC-day scope so a cache
                                entry can never outlive the day it was
                                made without saying so loudly.

    Non-cacheable roles (the adversary) always pass through to the inner
    model, hit or miss.
    """

    name = "caching"

    def __init__(self, inner: PipelineModel, cache: PromptCache, *,
                 scope: Optional[str] = None, mode: Optional[str] = None,
                 cacheable: Callable[[str], bool] = default_cacheable):
        if scope is None:
            if mode != "live":
                raise ValueError(
                    "CachingModel without a scope is refused: pass the "
                    "run's cutoff scope (e.g. 'retro:2024-01-03') so "
                    "entries can never cross a retrodiction boundary, or "
                    "mode='live' to day-stamp a non-retro run explicitly.")
            scope = ("live:" +
                     datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        elif mode == "live":
            raise ValueError("pass scope OR mode='live', not both")
        self.inner = inner
        self.cache = cache
        self.scope = scope
        self._cacheable = cacheable
        #: filled per-call for observability
        self.last_from_cache: Optional[bool] = None

    async def complete(self, role: str, messages: list[dict],
                       **kw) -> dict:
        if not self._cacheable(str(role)):
            self.last_from_cache = False
            return await self.inner.complete(role, messages, **kw)
        key = cache_key(self.scope, str(role), messages)
        hit = self.cache.get(self.scope, key)
        if hit is not None:
            self.last_from_cache = True
            return hit
        resp = await self.inner.complete(role, messages, **kw)
        self.cache.put(self.scope, key, resp)
        self.last_from_cache = False
        return resp
