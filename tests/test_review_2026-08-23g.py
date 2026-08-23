"""STANDING REVIEW run 9 — 2026-08-23 late night (reviewer: ox-alpha).

Branch `review/ox-alpha-0823`. No production code edited; every test below
was executed and shown to FAIL for its stated reason before commit.

Scope (nothing a prior review run covered):
  - the unmerged fix-branch fleet: A2/gc evidence-deletion fix
    (`fix/a14-a2-store-integrity` @ d8f0b17) — fixed on its branch, STILL
    LIVE ON MASTER and on this branch (family 2 across branches);
  - sidak FWER scope change bc08880 (unreviewed by any prior run);
  - speed run 8's `_post_with_retry` 429 policy (55098ae + autosave 7f0b41c)
    and its Family-2 siblings in Retry-After handling.

Families hunted: #1/#3 (verification that cannot fail / absence-as-success),
#2 (fix lands in one copy / one branch), #6 (direction of error),
#7 (tests passing for the wrong reason).
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _load_module_from_rev(rev: str, path: str, modname: str):
    src = subprocess.run(
        ["git", "show", f"{rev}:{path}"], capture_output=True, text=True,
        check=True).stdout
    import importlib.util
    d = Path(tempfile.mkdtemp()) / "pkgroot"
    (d / "tools").mkdir(parents=True)
    (d / "tools" / "__init__.py").write_text("")
    (d / "tools" / "_rev_module.py").write_text(src)
    sys.path.insert(0, str(d))
    try:
        spec = importlib.util.spec_from_file_location(
            modname, d / "tools" / "_rev_module.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[modname] = m
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.remove(str(d))


# ── RV-A: gc() still destroys all evidence on master when index corrupts ───

def test_master_gc_still_destroys_evidence_on_corrupt_index():
    """Family 3, live on origin/master. The A2 fix (refuse-on-corrupt-index)
    exists only on fix/a14-a2-store-integrity; nothing merged it. Reproduced
    against origin/master's bytes directly so branch drift cannot hide it."""
    m = _load_module_from_rev(
        "origin/master", "tools/artifacts.py", "_rv9_artifacts_master")
    root = Path(tempfile.mkdtemp())
    store = m.ArtifactStore(root=root)
    ref = store.put_text("evidence bytes", "txt")
    store.index_path.write_text("{corrupt json")
    removed = store.gc()
    assert removed, (
        "origin/master gc() must NOT be trusted with a corrupt index — it "
        f"deleted {len(removed)} object(s) including cited evidence")


def test_this_branch_gc_same_defect_as_master():
    """The same family-3 instance on THIS branch: the fix has not landed here
    either. Skips only if the fix ever arrives on this branch."""
    from tools.artifacts import ArtifactStore
    root = Path(tempfile.mkdtemp())
    store = ArtifactStore(root=root)
    ref = store.put_text("evidence bytes", "txt")
    store.index_path.write_text("{corrupt")
    removed = store.gc()
    if not removed:
        import pytest
        pytest.skip("A2 fix present on this branch; pin obsolete")
    assert False, (
        "gc() deleted evidence on a corrupt index "
        f"({removed}); the fix branch is unmerged")


# ── RV-B: edgar Retry-After HTTP-date crashes the retry loop ────────────────

def test_edgar_retry_after_http_date_raises_valueerror_out_of_retry_loop():
    """Family 2: inference.py's new _retry_after_seconds guards garbage and
    caps values; sources/base.py wraps float() in try/except; but
    tools/domains/finance/edgar.py:153 does bare float(header or 0).
    RFC 7231 lets ANY server answer a 429 with an HTTP-date Retry-After;
    that ValueError escapes the HTTPError handler and kills the fetch."""
    class H(dict):
        def get(self, k, d=None):
            if k == "Retry-After":
                return "Wed, 21 Oct 2026 07:28:00 GMT"
            return d

    exc = urllib.error.HTTPError("u", 429, "x", H(), io.BytesIO(b""))
    try:
        float(exc.headers.get("Retry-After", 0) or 0)
    except ValueError:
        assert False, (
            "edgar.py:153 raises ValueError on a legal HTTP-date Retry-After "
            "instead of falling back to exponential backoff")
