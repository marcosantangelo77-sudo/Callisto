"""Content-addressed prompt caching for pipeline model calls.

Identical prompts recur — across leaves of one run, across reruns of the
same question, across resumed checkpoint runs that lost a stage. The same
(role, task_class, messages) pair always deserves the same answer, so the
cache key is a hash of exactly those inputs plus the cache-format version.

HONESTY RULES (this cache can silently invalidate scores if broken):

* TTL is real: an expired entry is a MISS, never a stale hit dressed up.
* RETRODICTION CUTOFF ISOLATION: a key always embeds the run's cutoff
  date (claim_date). An entry cached under one cutoff can never be served
  to a run with a different cutoff — that would leak future evidence into
  a past-dated run and silently invalidate every Brier score the batch
  produces. Live runs (no cutoff) live in their own namespace.
* Hits are labelled: a served response carries "cache": "hit" and the
  caller's counting wrapper records it as a call NOT made.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("callisto.pipeline.cache")

#: Bump when the key inputs change shape (e.g. a prompt builder edit that
#: changes what a cached answer means). Old entries simply miss.
CACHE_FORMAT_VERSION = 1

#: Default TTL: 7 days. Long enough to span a batch rerun and a resumed
#: checkpoint run; short enough that a stale registry catalog or a changed
#: prompt builder cannot haunt a run for weeks.
DEFAULT_TTL_S = 7 * 24 * 3600


def cache_key(role: str, messages: list[dict], *,
              task_class: str = "",
              cutoff_date: str = "",
              schema: Any = None,
              version: int = CACHE_FORMAT_VERSION) -> str:
    """Content-addressed key: everything that could change the answer.

    cutoff_date is part of the key, not an afterthought — see module
    docstring. schema (the adversary's JSON schema) is included because a
    response shaped for one schema is not the response another needs.
    """
    payload = json.dumps({
        "v": version,
        "role": role,
        "task_class": task_class,
        "cutoff_date": cutoff_date,
        "schema": schema,
        "messages": messages,
    }, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PromptCache:
    """File-backed KV store with real TTL. One JSON file per entry under
    root/ab/<key[:2]>/<key>.json — sharded so a big batch does not create
    a fifty-thousand-entry directory."""

    def __init__(self, root: Optional[str] = None,
                 ttl_s: int = DEFAULT_TTL_S):
        if root is None:
            root = os.path.join(
                os.environ.get("CALLISTO_STATE_DIR",
                               os.path.expanduser(
                                   "~/.local/state/callisto")),
                "prompt_cache")
        self.root = Path(root)
        self.ttl_s = max(1, int(ttl_s))
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        return self.root / "ab" / key[:2] / f"{key}.json"

    def get(self, key: str) -> Optional[dict]:
        p = self._path(key)
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            self.misses += 1
            return None
        age = time.time() - float(rec.get("stored_at", 0))
        if age > self.ttl_s:
            # Expired is expired: delete and report a miss. Never serve a
            # stale entry as a hit.
            try:
                p.unlink()
            except OSError:
                pass
            self.misses += 1
            return None
        self.hits += 1
        return rec.get("response")

    def put(self, key: str, response: dict) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "stored_at": time.time(),
            "ttl_s": self.ttl_s,
            "response": response,
        }))

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses,
                "root": str(self.root), "ttl_s": self.ttl_s}


class CachingModel:
    """Transparent cache in front of any PipelineModel-shaped object.

    complete() signature matches PipelineModel (role, messages, **ignored)
    AND the adversary backend shape (task_class, messages, schema=...):
    the first positional name tells us which world we are in. A cache hit
    returns the stored response with ``cache: "hit"`` added; the inner
    model is not called and the counting wrapper sees no call.
    """

    def __init__(self, inner, cache: PromptCache, *,
                 cutoff_date: str = ""):
        self.inner = inner
        self.cache = cache
        #: The retrodiction cutoff (ISO date) this run operates under.
        #: Empty string = live namespace. NEVER share entries across
        #: different values — that is the leak the mandate forbids.
        self.cutoff_date = str(cutoff_date or "")

    @property
    def name(self) -> str:
        return getattr(self.inner, "name", "cached")

    async def complete(self, *args, **kwargs) -> dict:
        role_or_task = str(args[0]) if args else ""
        messages = args[1] if len(args) > 1 else (kwargs.get("messages") or [])
        task_class = role_or_task if "task_class" in kwargs or \
            (args and args[0] not in ("Architect", "Manager", "Sentinel",
                                      "Adversary")) else ""
        # The adversary calls complete(task_class, messages, schema=...);
        # the pipeline calls complete(role, messages). Both flatten to the
        # same key shape: first arg is the routing label either way.
        schema = kwargs.get("schema")
        key = cache_key(role_or_task, list(messages),
                        task_class=task_class,
                        cutoff_date=self.cutoff_date, schema=schema)
        hit = self.cache.get(key)
        if hit is not None:
            resp = dict(hit)
            resp["cache"] = "hit"
            return resp
        resp = await self.inner.complete(*args, **kwargs)
        # Only cache dict responses with content — a transport error object
        # or empty response must not become a poisoned entry.
        if isinstance(resp, dict) and (resp.get("content") or
                                       resp.get("parsed_json")):
            self.cache.put(key, resp)
        return resp
