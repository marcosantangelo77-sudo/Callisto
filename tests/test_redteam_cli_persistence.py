"""RED TEAM — CLI front door / run-record persistence / migrations.

Every test here reproduces a confirmed defect found in the 2026-08-24
red-team pass (see findings/redteam_cli_persistence.md). Each fails on
pre-fix code.

Defects covered:
  D1. Run records are unsealed plaintext — `sealed: true` with a tampered
      conclusion/confidence still displays as SEALED (family 1/9).
  D2. `_persist_run` id = timestamp + hash(question) % 10000 → two asks in
      the same second silently overwrite each other 1-in-10000 (family 3:
      loss treated as success; no error raised).
  D3. `show` re-verifies artifact hashes but never fetch content_sha256 —
      an empty or wrong digest displays without any warning (C1 pattern,
      family 3). And a tampered conclusion prints next to "[ok]" artifact
      marks, laundering the edit.
  D4. doctor reports OK while the DEFAULT provider tier is unreachable and
      the database file is garbage — its "checks" cannot fail on reachability
      (family 1).
  D5. Migration bootstrap marks 001-012 applied WITHOUT verifying they're
      satisfied, then 013 fails against the pre-framework schema — every
      subsequent startup raises forever (permanently wedged DB).
  D6. Migration runner's per-migration transaction does NOT roll back DDL
      executed through a *different* connection... (covered indirectly) —
      here we pin: mid-migration failure rolls back cleanly (regression
      guard for probe rt_mig_fail4 behaviour).
"""
import hashlib
import importlib
import io
import json
import os
import sqlite3
import sys
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import callisto


def _record(q="probe question", sealed=True, conclusion="honest conclusion",
            ts="2026-08-24T07:00:00+00:00"):
    return {"recorded_at": ts, "question": q, "sealed": sealed,
            "refusal_reason": "", "conclusion": conclusion,
            "confidence": {"score": 0.5, "tier": "X"},
            "leaves": [], "artifacts": [], "fetches": [],
            "objections": [], "notes": []}


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(d))
    return d


# ── D1: tampered record still displays as SEALED ──────────────────────────

def test_d1_tampered_record_must_not_display_as_sealed(runs_dir):
    """A record whose seal/integrity is checkable must not present an edited
    conclusion as the sealed verdict."""
    p = callisto._persist_run(_record(conclusion="the honest answer"))
    d = json.loads(p.read_text())
    d["conclusion"] = "TAMPERED — buy now"
    d["confidence"] = {"score": 0.95, "tier": "ESTABLISHED"}
    p.write_text(json.dumps(d))          # trivially editable

    rec, _ = callisto._load_run(p.stem)
    args = callisto.build_parser().parse_args(["show", p.stem])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = callisto._cmd_show(args)
    out = buf.getvalue()
    # The fix must EITHER carry an integrity tag that show verifies, or mark
    # unverified records. Displaying TAMPERED next to "SEALED" is the defect.
    assert "TAMPERED" not in out or "UNVERIFIED" in out or rc != 0, (
        "show displayed a tampered record verbatim as SEALED")


def test_d1_record_lacks_any_integrity_field(runs_dir):
    """The persisted record has no seal/hash of its own contents — nothing
    for `show` to verify even if it wanted to."""
    p = callisto._persist_run(_record())
    rec = json.loads(p.read_text())
    assert not any(k in rec for k in ("seal", "seal_hash", "record_hash",
                                      "content_sha256")), (
        "expected no integrity field pre-fix; if this fails, update D1 tests"
    )


# ── D2: same-second collision silently overwrites a run ───────────────────

def test_d2_same_second_same_bucket_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "r"))
    a = _record(q="q4")
    b = _record(q="q294")     # known bucket pair from the red-team probe;
    # find ANY colliding pair dynamically instead of trusting hash():
    seen = {}
    pair = None
    for i in range(200000):
        q = f"zz{i}"
        h = abs(hash(q)) % 10000
        if h in seen:
            pair = (seen[h], q)
            break
        seen[h] = q
    assert pair, "no collision found in 200k tries?! modulo changed?"
    a = _record(q=pair[0], conclusion="FIRST RUN'S CONCLUSION")
    b = _record(q=pair[1], conclusion="SECOND RUN'S CONCLUSION")
    pa = callisto._persist_run(a)
    pb = callisto._persist_run(b)
    assert pa != pb, (
        f"two different questions mapped to one path {pa}: first run's "
        "record was silently destroyed (os.replace over it, no error)")


# ── D3: show never checks fetch provenance digests ────────────────────────

def test_d3_empty_fetch_digest_displayed_without_warning(
        runs_dir, tmp_path, monkeypatch):
    arts = tmp_path / "arts"
    monkeypatch.setenv("CALLISTO_ARTIFACT_DIR", str(arts))
    rec = _record()
    rec["artifacts"] = []
    rec["fetches"] = [{"source": "fred",
                       "url": "https://example/x",
                       "content_sha256": ""}]   # C1: absent digest
    p = callisto._persist_run(rec)
    args = callisto.build_parser().parse_args(["show", p.stem])
    buf = io.StringIO()
    with redirect_stdout(buf):
        callisto._cmd_show(args)
    out = buf.getvalue()
    assert "unverified" in out.lower() or "missing" in out.lower(), (
        "show printed a fetch with EMPTY content_sha256 as ordinary "
        "provenance — absence passed as success")


# ── D3 repair-pinning: digest validation matrix ───────────────────────────

def _show_fetch(rec, runs_dir, tmp_path, monkeypatch):
    arts = tmp_path / "arts"
    monkeypatch.setenv("CALLISTO_ARTIFACT_DIR", str(arts))
    rec["artifacts"] = []
    p = callisto._persist_run(rec)
    args = callisto.build_parser().parse_args(["show", p.stem])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = callisto._cmd_show(args)
    return buf.getvalue(), rc


def test_d3_malformed_nonhex_digest_flagged(runs_dir, tmp_path, monkeypatch):
    rec = _record()
    rec["fetches"] = [{"source": "fred",
                       "url": "https://example/y",
                       "content_sha256": "z" * 64}]   # right length, not hex
    out, rc = _show_fetch(rec, runs_dir, tmp_path, monkeypatch)
    low = out.lower()
    assert ("malformed" in low or "invalid" in low or "unverified" in low
            or "missing" in low), (
        "show printed a NON-HEX 64-char content_sha256 as ordinary provenance")
    assert rc != 0, "malformed fetch digest must make show exit non-zero"


def test_d3_wrong_length_digest_flagged(runs_dir, tmp_path, monkeypatch):
    rec = _record()
    rec["fetches"] = [{"source": "fred",
                       "url": "https://example/z",
                       "content_sha256": "abc123"}]   # truthy but wrong length
    out, rc = _show_fetch(rec, runs_dir, tmp_path, monkeypatch)
    low = out.lower()
    assert ("malformed" in low or "invalid" in low or "unverified" in low), (
        "show printed a wrong-length content_sha256 as ordinary provenance")
    assert rc != 0


def test_d3_valid_digest_with_local_body_verified(
        runs_dir, tmp_path, monkeypatch):
    import hashlib as _hashlib
    body = "the exact fetched body"
    rec = _record()
    rec["fetches"] = [{"source": "fred", "url": "https://example/v",
                       "content_sha256": _hashlib.sha256(
                           body.encode()).hexdigest(),
                       "body": body}]
    out, rc = _show_fetch(rec, runs_dir, tmp_path, monkeypatch)
    assert "[ok]" in out, f"valid digest+matching body not shown ok: {out}"
    assert "mismatch" not in out.lower() and "unverified" not in out.lower()
    assert rc == 0


def test_d3_tampered_local_body_mismatch_flagged(
        runs_dir, tmp_path, monkeypatch):
    body = "original body"
    rec = _record()
    rec["fetches"] = [{"source": "fred", "url": "https://example/t",
                       "content_sha256": hashlib.sha256(
                           body.encode()).hexdigest(),
                       "body": "TAMPERED BODY"}]
    out, rc = _show_fetch(rec, runs_dir, tmp_path, monkeypatch)
    assert "mismatch" in out.lower(), (
        "local body contradicting its recorded digest displayed without "
        "a mismatch warning")


# ── D4: doctor cannot fail on reachability ────────────────────────────────

def test_d4_doctor_reports_ok_with_dead_default_tier_and_garbage_db(
        tmp_path, monkeypatch):
    """doctor said 'OK' on this machine while the default tier gpu1 refused
    connections and CALLISTO_DB_PATH held non-database bytes."""
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    monkeypatch.setenv("CALLISTO_DB_PATH", str(garbage))
    # providers config pointing at a dead port
    cfg = tmp_path / "providers.yaml"
    cfg.write_text(textwrap.dedent("""
        default_tier: dead
        providers:
          dead:
            backend: openai_compat
            base_url: http://127.0.0.1:1/v1   # nothing listens on port 1
            model: whatever
        routing:
          task_classes: {}
    """))
    args = callisto.build_parser().parse_args(["doctor", "--providers",
                                               str(cfg)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = callisto._cmd_doctor(args)
    out = buf.getvalue()
    assert "doctor: OK" not in out, (
        "doctor reported OK with an unreachable default tier and a corrupt "
        "db file — its checks cannot fail on reachability (family 1)")


# ── D5/D6: migration semantics (throwaway databases only) ─────────────────

def test_d6_mid_migration_failure_rolls_back_ddl(tmp_path):
    """A failing up() must leave NO partial DDL and NO recorded version.
    Verified live in the red team: rollback worked via the module-level
    patch; this pins it so future edits don't regress it."""
    from tools.migrations import runner
    db = str(tmp_path / "mig.db")

    mod = importlib.import_module("tools.migrations.013_schema_seam_hypotheses")
    real_up = mod.up

    def sabotaged(conn):
        conn.execute("CREATE TABLE probe_marker (x TEXT)")
        raise RuntimeError("boom mid-migration-013")

    mod.up = sabotaged
    try:
        with pytest.raises(RuntimeError):
            runner.apply_pending_migrations(db)
    finally:
        mod.up = real_up

    conn = sqlite3.connect(db)
    marker = conn.execute("SELECT name FROM sqlite_master WHERE "
                          "name='probe_marker'").fetchone()
    versions = [r[0] for r in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version")]
    conn.close()
    assert marker is None, "DDL from failed migration survived rollback"
    assert 13 not in versions, "failed migration was recorded as applied"


def test_d5_bootstrap_wedges_pre_framework_db(tmp_path):
    """bootstrap_existing_db marks 001-012 applied purely because
    'hypotheses' exists, then 013 fails on the mismatched legacy schema —
    permanently wedging startup. The fix: verify satisfaction before
    marking applied (or apply rather than trust). Documents the wedge."""
    from tools.migrations import runner
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("""CREATE TABLE hypotheses (
        hypothesis_id TEXT PRIMARY KEY, name TEXT, sport TEXT NOT NULL,
        market_type TEXT, status TEXT)""")
    conn.execute("CREATE TABLE backtest_events (id INTEGER PRIMARY KEY,"
                 " hypothesis_id TEXT)")
    conn.close()

    raised = None
    try:
        runner.apply_pending_migrations(db)
    except Exception as e:
        raised = e
    assert raised is None or isinstance(raised, sqlite3.OperationalError), \
        "unexpected failure mode"

    c = sqlite3.connect(db)
    rows = sorted(x[0] for x in c.execute(
        "SELECT version FROM schema_migrations"))
    c.close()
    if raised is not None:
        # the defect: bootstrap trusted 1-12 onto an incompatible schema
        assert 12 in rows and 13 not in rows, "wedge state changed shape"
        pytest.fail(
            "pre-framework DB permanently wedged: bootstrap marked 1-12 "
            "applied without verification; 013 then fails on EVERY startup "
            f"({raised!r})")


# ── glob crash: show '*' ───────────────────────────────────────────────────

def test_show_glob_metachar_is_clean_error_not_traceback(runs_dir):
    callisto._persist_run(_record())
    args = callisto.build_parser().parse_args(["show", "*"])
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = callisto._cmd_show(args)
        assert rc == 1
    except SystemExit:
        pass  # clean message path also acceptable post-fix
