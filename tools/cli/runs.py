"""`callisto runs` / `callisto show` — read back persisted ask() runs.

runs lists saved run records newest-first; show reprints one record and
RE-HASHES its artifacts against the artifact store and its fetch digests
against any local payload. A mismatch is reported loudly and exits
non-zero — never swallowed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from tools.cli.ask import _runs_dir


def _load_run(run_id: str) -> tuple[dict | None, Path | None]:
    """Load a run record by id (filename stem) or unique prefix."""
    runs = sorted(_runs_dir().glob(f"{run_id}*.json"))
    if not runs:
        return None, None
    if len(runs) > 1:
        raise SystemExit(
            f"ambiguous run id '{run_id}' matches {len(runs)} records; "
            "use a longer prefix")
    return json.loads(runs[0].read_text(encoding="utf-8")), runs[0]


def _verify_artifact(sha256: str) -> str:
    """Re-hash the artifact against its recorded hash. Returns a status."""
    try:
        from tools.artifacts import ArtifactStore, sha256_bytes
        store = ArtifactStore()
        actual = sha256_bytes(store.get_bytes(sha256))
        return "ok" if actual == sha256 else "CORRUPT"
    except Exception as exc:
        short = str(exc)
        return "missing" if "not found" in short else f"unverifiable: {short}"


def _cmd_runs(args: argparse.Namespace) -> int:
    paths = sorted(_runs_dir().glob("*.json"), reverse=True)[:args.limit]
    if not paths:
        print("no saved runs yet — `callisto ask \"...\"` creates one")
        return 0
    for p in paths:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            verdict = ("SEALED" if rec.get("sealed") else "REFUSED")
            conf = rec.get("confidence", {})
            q = (rec.get("question") or "?")[:60]
            print(f"{p.stem}  {verdict:<8} "
                  f"{conf.get('tier', '?')}/{conf.get('score', 0):.2f}  {q}")
        except Exception as exc:
            print(f"{p.stem}  (unreadable: {exc})")
    return 0


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _fetch_digest_status(f: dict) -> tuple[str, bool]:
    """Validate one persisted fetch's content_sha256.

    Returns (status, hard_fail). "ok" means verified; hard_fail marks a
    missing/non-string/wrong-length/non-hex digest — absence is failure
    (red-team C1/D3), and it makes `show` exit non-zero. A syntactically
    valid digest with no local payload cannot be checked against bytes here
    (no network fetch), so it is flagged unverified but keeps legacy
    compatibility (soft).
    """
    digest = f.get("content_sha256")
    if not isinstance(digest, str) or not digest:
        return "MISSING DIGEST", True
    d = digest.strip().lower()
    if len(d) != 64:
        return f"MALFORMED DIGEST ({len(d)} chars)", True
    if not _HEX64_RE.match(d):
        return "MALFORMED DIGEST (non-hex)", True
    body = None
    for k in ("body", "content", "payload"):
        v = f.get(k)
        if isinstance(v, str):
            body = v.encode("utf-8")
            break
        if isinstance(v, (bytes, bytearray)):
            body = bytes(v)
            break
    if body is None:
        # No local payload to hash — remote content is not fetched here, so
        # the recorded digest cannot be verified, only syntax-checked.
        return "unverified (no local payload)", False
    if hashlib.sha256(body).hexdigest() != d:
        return "DIGEST MISMATCH", True
    return "ok", False


def _cmd_show(args: argparse.Namespace) -> int:
    rec, path = _load_run(args.run_id)
    if rec is None:
        print(f"no run matching '{args.run_id}' — see `callisto runs`")
        return 1
    verdict = "SEALED" if rec.get("sealed") else "REFUSED"
    conf = rec.get("confidence", {})
    print(f"run      : {path.stem}")
    print(f"when     : {rec.get('recorded_at', '?')}")
    print(f"question : {rec.get('question', '?')}")
    print(f"{verdict:<9}: {conf.get('tier', '?')} {conf.get('score', 0):.2f}")
    if rec.get("refusal_reason"):
        print(f"reason   : {rec['refusal_reason']}")
    if rec.get("conclusion"):
        print("\n--- conclusion ---")
        print(rec["conclusion"])
    arts = rec.get("artifacts", [])
    if arts:
        print(f"\n--- artifacts ({len(arts)}) — re-hashed against the store ---")
        for a in arts:
            status = _verify_artifact(a["sha256"])
            print(f"  [{status:<12}] {a['kind']:<5} "
                  f"{a['sha256'][:16]}…  {a.get('name', '')}")
    fetches = rec.get("fetches", [])
    bad_fetches = 0
    if fetches:
        print(f"\n--- fetches ({len(fetches)}) — provenance digests checked ---")
        seen = set()
        # Validate EVERY persisted record first — deduplication must never
        # hide an invalid sibling behind an earlier valid (source, url).
        results = [(f, *_fetch_digest_status(f)) for f in fetches]
        for f, status, hard_fail in results:
            key = (f.get("source", "?"), f.get("url", ""))
            if key in seen and status == "ok":
                continue
            seen.add(key)
            if status != "ok":
                if hard_fail:
                    bad_fetches += 1
                print(f"  [{status:<22}] {key[0]:<18} {key[1][:70]}")
            else:
                print(f"  [ok]                  {key[0]:<18} {key[1][:70]}")
        if bad_fetches:
            print(f"  WARNING: {bad_fetches} fetch(es) have missing or "
                  "malformed content_sha256 provenance — UNVERIFIED.")
    obs = rec.get("objections", [])
    if obs:
        print(f"\nobjections ({len(obs)}):")
        for o in obs[:5]:
            print(f"  - {str(o)[:200]}")
    print(f"\nrecord   : {path}")
    return 1 if bad_fetches else 0
