"""`callisto ask` — one question through the full AGP pipeline.

Fail-closed: `check_seal_key()` must pass before any research runs. The
seal key value itself is never printed. Under CALLISTO_LOCAL_ONLY, a
hosted `--backend` is refused before any health probe or research.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _runs_dir() -> Path:
    """Directory where ask() results are persisted as JSON records.

    Uses the off-OneDrive state dir (tools.state_paths) so a run record
    never freezes under a sync lock; overridable with CALLISTO_RUNS_DIR.
    """
    override = os.getenv("CALLISTO_RUNS_DIR", "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        try:
            from tools.state_paths import state_dir
            root = state_dir() / "runs"
        except Exception:
            root = Path.home() / ".local" / "state" / "callisto" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _default_providers_path() -> str:
    return str(REPO / "config" / "providers.yaml")


# Characterization stubs store endpoint names in a list, not EndpointConfig.
# Name fallback so `--backend ox_alpha` still fails closed under LOCAL_ONLY.
_WELL_KNOWN_HOSTED_RAILS = frozenset({
    "openrouter_ox", "frontier", "ox_alpha", "ox_alpha_proxy",
})


def _local_only_forbids_backend(router, name: str) -> bool:
    """True when CALLISTO_LOCAL_ONLY is on and `name` is a hosted rail.

    Real ProviderRouter.endpoints is a dict of EndpointConfig (URL/backend
    classification from tools.infrouter.local_only). Do not health-check
    a hosted pin — that would leave the box before the strip inside
    candidates_for() can raise.
    """
    from tools.infrouter.local_only import (
        endpoint_is_hosted,
        local_only_enabled,
    )
    if not local_only_enabled():
        return False
    eps = getattr(router, "endpoints", None)
    if isinstance(eps, dict):
        return endpoint_is_hosted(eps.get(name))
    return name in _WELL_KNOWN_HOSTED_RAILS


# ── ask ────────────────────────────────────────────────────────────────────

def _entry():
    """The entry script (callisto.py) owns the test seams (_load_router,
    _make_engine, _result_record, _runs_dir). Resolving them lazily here
    avoids a circular import while keeping `monkeypatch.setattr(callisto,
    ...)` effective when the command body runs from tools.cli."""
    import callisto
    return callisto


def check_seal_key() -> bool:
    """Fail-closed gate: True only when CALLISTO_SEAL_KEY is set and valid
    hex. Unset/blank/non-hex keys mean unkeyed (forgeable) session hashes,
    which the front door must refuse rather than silently write. The key
    value itself is never printed."""
    raw = os.getenv("CALLISTO_SEAL_KEY", "").strip()
    if not raw:
        print("FAIL: CALLISTO_SEAL_KEY is not set — seals would be unkeyed "
              "(forgeable SHA-256 checksums)")
        return False
    try:
        bytes.fromhex(raw)
    except ValueError:
        print("FAIL: CALLISTO_SEAL_KEY is not valid hex — seals would fall "
              "back to unkeyed (forgeable); fix the key value")
        return False
    return True


def _result_record(result, question: str) -> dict:
    """Serialise a PipelineResult into the persisted run record.

    Everything needed to re-check the conclusion later: the conclusion
    text itself, every artifact hash (resolvable against the artifact
    store), and per-fetch source/URL provenance.
    """
    return {
        "recorded_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "question": question,
        "sealed": bool(getattr(result, "sealed", False)),
        "refusal_reason": getattr(result, "refusal_reason", ""),
        "conclusion": getattr(result, "conclusion", ""),
        "confidence": {
            "score": getattr(result, "confidence_score", 0.0),
            "tier": getattr(result, "confidence_tier", "UNVERIFIED"),
        },
        "leaves": [
            {"text": lf.text, "answer": lf.answer or "",
             "tier": lf.tier, "confidence": lf.confidence}
            for lf in getattr(result, "leaves", [])
        ],
        "artifacts": [r.to_dict() for r in getattr(result, "artifact_refs", [])],
        "fetches": [
            {"source": getattr(f, "source_name", "?"),
             "url": getattr(f, "url", ""),
             "content_sha256": getattr(f, "content_sha256", "")}
            for f in getattr(result, "fetches", [])
        ],
        "objections": [getattr(o, "text", str(o))
                       for o in getattr(result, "objections", [])],
        "notes": list(getattr(result, "notes", [])),
    }


def _persist_run(record: dict) -> Path:
    """Write the run record atomically; returns its path. The filename is
    the timestamped run id — `callisto runs` / `callisto show` read it."""
    stamp = record["recorded_at"].replace(":", "").replace("-", "")
    run_id = f"{stamp}_{abs(hash(record['question'])) % 10000:04d}"
    path = _runs_dir() / f"{run_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


async def cmd_ask(args: argparse.Namespace) -> int:
    if not check_seal_key():
        return 2
    router = _entry()._load_router(args.providers)
    if args.backend:
        if args.backend not in router.endpoints:
            print(f"unknown provider tier '{args.backend}'. configured: "
                  f"{', '.join(router.endpoints) or '(none)'}")
            return 2
        if _local_only_forbids_backend(router, args.backend):
            print(
                f"FAIL: CALLISTO_LOCAL_ONLY forbids --backend "
                f"'{args.backend}' (hosted). Pin gpu1/gpu1_fast or unset "
                f"CALLISTO_LOCAL_ONLY."
            )
            return 2
        # Route every task class at the requested endpoint.
        router.task_classes = {tc: args.backend
                               for tc in (router.task_classes or {})}
        router.default_tier_name = args.backend
        # A pinned backend disables failover, so refuse early if it is
        # unreachable. With no --backend, skip the preflight and let the
        # ProviderRouter candidate chain (which includes the OX fallback)
        # make the routing decision per task.
        try:
            health = await router.check_health(router.default_tier_name)
        except Exception as exc:                   # pragma: no cover
            print(f"provider '{router.default_tier_name}' unreachable: {exc}")
            print("run `python callisto.py doctor` to see what is configured")
            return 2
        if health.get("status") != "ok":
            print(f"provider '{router.default_tier_name}' unhealthy: "
                  f"{json.dumps(health)[:300]}")
            return 2

    engine = _entry()._make_engine(
        router, self_review=args.self_review)
    result = await engine.run(args.question)
    print("=" * 72)
    if result.sealed:
        print(f"SEALED   confidence {result.confidence_score:.2f} "
              f"tier={result.confidence_tier}")
    else:
        print("REFUSED")
        if result.refusal_reason:
            print(f"reason   : {result.refusal_reason}")
    for leaf in result.leaves:
        ans = (leaf.answer or "").replace("\n", " ")
        print(f"leaf [{leaf.tier} {leaf.confidence:.2f}] "
              f"{leaf.text[:90]}")
        if ans:
            print(f"     {ans[:400]}")
    srcs = sorted({f.source_name for f in result.fetches})
    print(f"sources  : {len(srcs)} distinct ({', '.join(srcs) or 'none'})"
          f" / {len(result.fetches)} fetches")
    if result.objections:
        print(f"objections ({len(result.objections)}):")
        for ob in result.objections[:5]:
            text = str(getattr(ob, "text", ob))[:220].replace("\n", " ")
            print(f"  - {text}")
    if result.notes:
        print(f"notes    : {'; '.join(result.notes)[:300]}")
    snap = router.cost_ledger.snapshot()
    print(f"cost     : {json.dumps(snap.get('by_tier', {}))}")

    # Persist the full record — conclusion, artifact hashes, fetch
    # provenance — so a human can re-check it after the terminal scrolls.
    try:
        record = _entry()._result_record(result, args.question)
        path = _entry()._persist_run(record)
        print(f"run      : {path}")
        for ref in getattr(result, "artifact_refs", []):
            print(f"artifact : {ref.kind:<5} {ref.sha256[:16]}…  {ref.name}")
    except Exception as exc:
        print(f"run      : NOT SAVED ({exc})")
    return 0 if result.sealed else 1


_cmd_ask = cmd_ask  # backwards-compatible alias


def _load_router(config_path: str):
    """Seam for tests; production returns a real ProviderRouter."""
    from inference import ProviderRouter
    return ProviderRouter(config_path=config_path)


def _make_engine(router, self_review: bool):
    """Seam for tests; production wires RouterModel + ResearchPipeline."""
    from tools.pipeline.engine import ResearchPipeline
    from tools.pipeline.model import RouterModel
    return ResearchPipeline(
        model=RouterModel(router),
        adversary_router=(None if self_review else router))
