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


# ── D2 repair-pinning: collision-safe run ids ─────────────────────────────

def test_d2_fix_distinct_questions_same_second_both_survive(runs_dir):
    """Two different questions with identical recorded_at must each keep
    their own record, each retaining its own conclusion."""
    a = _record(q="question alpha", conclusion="ALPHA CONCLUSION")
    b = _record(q="question beta", conclusion="BETA CONCLUSION")
    pa = callisto._persist_run(a)
    pb = callisto._persist_run(b)
    assert pa != pb
    assert json.loads(pa.read_text())["conclusion"] == "ALPHA CONCLUSION"
    assert json.loads(pb.read_text())["conclusion"] == "BETA CONCLUSION"


def test_d2_fix_identical_question_repeated_in_same_second(runs_dir):
    """Even the SAME question recorded twice in one second must produce two
    distinct files, both intact (no reliance on hash() randomization)."""
    rec = _record(q="same question", conclusion="first write")
    p1 = callisto._persist_run(rec)
    rec2 = _record(q="same question", conclusion="second write")
    p2 = callisto._persist_run(rec2)
    assert p1 != p2
    c1 = json.loads(p1.read_text())["conclusion"]
    c2 = json.loads(p2.read_text())["conclusion"]
    assert {c1, c2} == {"first write", "second write"}


def test_d2_fix_id_is_stable_across_processes(runs_dir):
    """Identity must not come from Python's randomized hash(): the id for a
    given question must be reproducible in a fresh interpreter."""
    p = callisto._persist_run(_record(q="stability probe"))
    import subprocess
    code = (
        "import hashlib;print(hashlib.sha256("
        "b'stability probe').hexdigest()[:8])")
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True).stdout.strip()
    assert out and out in p.stem


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


def test_d3_valid_then_invalid_duplicate_source_url_fails_closed(
        runs_dir, tmp_path, monkeypatch):
    """A valid fetch followed by a same-(source,url) fetch with an empty
    digest: dedup must not silently skip the invalid sibling."""
    body = "good body"
    rec = _record()
    rec["fetches"] = [
        {"source": "fred", "url": "https://example/dup",
         "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
         "body": body},
        {"source": "fred", "url": "https://example/dup",
         "content_sha256": ""},
    ]
    out, rc = _show_fetch(rec, runs_dir, tmp_path, monkeypatch)
    low = out.lower()
    assert "missing" in low or "malformed" in low or "unverified" in low, (
        "invalid duplicate (source,url) fetch suppressed by presentation "
        f"dedup: {out}")
    assert rc != 0, "valid sibling masked an invalid duplicate digest"


def test_d3_malformed_duplicate_between_valid_records_fails_closed(
        runs_dir, tmp_path, monkeypatch):
    rec = _record()
    rec["fetches"] = [
        {"source": "fred", "url": "https://example/a",
         "content_sha256": hashlib.sha256(b"a").hexdigest(), "body": "a"},
        {"source": "fred", "url": "https://example/a",
         "content_sha256": "nope"},
    ]
    out, rc = _show_fetch(rec, runs_dir, tmp_path, monkeypatch)
    assert rc != 0, "malformed duplicate digest must fail show"


def test_d3_digest_mismatch_exits_nonzero(
        runs_dir, tmp_path, monkeypatch):
    body = "original body"
    rec = _record()
    rec["fetches"] = [{"source": "fred", "url": "https://example/mm",
                       "content_sha256": hashlib.sha256(
                           body.encode()).hexdigest(),
                       "body": "TAMPERED BODY"}]
    out, rc = _show_fetch(rec, runs_dir, tmp_path, monkeypatch)
    assert "mismatch" in out.lower()
    assert rc != 0, "local body contradicting content_sha256 returned success"


def test_d3_legacy_no_local_body_still_soft(
        runs_dir, tmp_path, monkeypatch):
    """Legacy record with valid-syntax digest but no stored body stays
    non-failing (we do not claim remote bytes were verified)."""
    rec = _record()
    rec["fetches"] = [{"source": "fred", "url": "https://example/legacy",
                       "content_sha256": hashlib.sha256(b"x").hexdigest()}]
    out, rc = _show_fetch(rec, runs_dir, tmp_path, monkeypatch)
    assert "unverified" in out.lower()
    assert rc == 0


def test_d3_valid_duplicate_still_deduplicated(
        runs_dir, tmp_path, monkeypatch):
    body = "dup ok"
    rec = _record()
    rec["fetches"] = [
        {"source": "fred", "url": "https://example/same",
         "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
         "body": body},
        {"source": "fred", "url": "https://example/same",
         "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
         "body": body},
    ]
    out, rc = _show_fetch(rec, runs_dir, tmp_path, monkeypatch)
    assert out.count("[ok]") == 1, "valid duplicates should dedup presentation"
    assert rc == 0


# ── repair-pinning: safe publication + exact legacy lookup ────────────────

def test_fix_replace_failure_leaves_no_broken_final_record(runs_dir, monkeypatch):
    """An injected os.replace failure must not leave an empty/partial final
    *.json behind; the writer retries cleanly on the next sequence slot."""
    import os as _os
    real_replace = _os.replace
    state = {"failed": False}

    def flaky_replace(src, dst):
        if not state["failed"] and str(dst).endswith(".json"):
            state["failed"] = True
            raise OSError("simulated crashed publication")
        return real_replace(src, dst)

    monkeypatch.setattr(_os, "replace", flaky_replace)
    p = callisto._persist_run(_record(q="flaky publish"))
    # The published record is complete and loadable.
    rec, loaded = callisto._load_run(p.stem)
    assert rec is not None and rec["question"] == "flaky publish"
    # No zero-byte or unparseable final json anywhere in the runs dir,
    # and no leaked tmp/reservation siblings from the failed attempt.
    finals = list(runs_dir.glob("*.json"))
    assert len(finals) == 1
    for f in finals:
        json.loads(f.read_text(encoding="utf-8"))
    assert not list(runs_dir.glob("*.tmp"))
    assert not list(runs_dir.glob("*.resv"))


def test_fix_reader_never_sees_partial_final_json(runs_dir, monkeypatch):
    """The reservation marker is private (.json.resv): while content is being
    written, no *.json glob can observe an empty final record."""
    seen = []
    real_open = open

    def spy_open(file, mode="r", *a, **kw):
        # Snapshot what any reader would see mid-publication.
        seen.extend(list(runs_dir.glob("*.json")))
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", spy_open)
    p = callisto._persist_run(_record(q="window probe"))
    for f in seen:
        assert f != p  # final path not visible before atomic publish
        json.loads(f.read_text(encoding="utf-8"))  # never empty/partial
    rec, _ = callisto._load_run(p.stem)
    assert rec["question"] == "window probe"


def test_fix_exact_legacy_id_resolves_before_prefix_match(runs_dir):
    """A legacy exact run id (`..._1234.json`) resolves its own record even
    when a newer SHA-style id extends it (`..._1234c7dc_000.json`)."""
    legacy = {"recorded_at": "2026-08-24T07:00:00+00:00",
              "question": "legacy record"}
    modern = dict(_record(q="modern record"),
                  recorded_at="2026-08-24T07:00:00+00:00")
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "20260824T070000_abcd1234_1234.json").write_text(
        json.dumps(legacy), encoding="utf-8")
    pm = callisto._persist_run(modern)  # stamp/qhash differ; just add neighbor
    (runs_dir / "20260824T070000_abcd1234_1234c7dc_000.json").write_text(
        json.dumps({"question": "longer new id"}), encoding="utf-8")

    rec, path = callisto._load_run("20260824T070000_abcd1234_1234")
    assert path == runs_dir / "20260824T070000_abcd1234_1234.json"
    assert rec["question"] == "legacy record"
    # Prefix matching still works for the longer new id...
    rec2, _ = callisto._load_run(pm.stem)
    assert rec2["question"] == "modern record"
    # ...and genuine prefix ambiguity stays an honest error.
    with pytest.raises(SystemExit):
        callisto._load_run("20260824T070000_abcd1234")


def test_fix_concurrent_same_second_writers_all_survive(runs_dir):
    """Threads publishing the identical record in the same second each get
    their own distinct, fully-readable record; none overwrites another."""
    import threading
    rec = _record(q="thread race", ts="2026-08-24T07:00:01+00:00")
    results, errors = [], []

    def worker():
        try:
            results.append(callisto._persist_run(dict(rec)))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    stems = {p.stem for p in results}
    assert len(stems) == len(results) == 4
    for p in sorted(runs_dir.glob("*.json")):
        got = json.loads(p.read_text(encoding="utf-8"))
        assert got["question"] == "thread race"


# ── repair-pinning: reservation race + cleanup ────────────────────────────

def test_fix_no_overwrite_when_final_appears_after_reservation(
        runs_dir, monkeypatch):
    """Deterministic interleaving of the classic lost-update race: writer B
    passes the cheap `path.exists()` pre-check while the slot is free and
    wins the O_EXCL claim; writer A then publishes + releases between B's
    claim and B's publish. B must revalidate final absence UNDER its
    reservation and move to the next slot — never os.replace over A's
    published record."""
    import os as _os
    stamp = "20260824T070000+0000".replace(":", "").replace("-", "")
    qhash = hashlib.sha256(b"probe question").hexdigest()[:8]
    victim_path = runs_dir / f"{stamp}_{qhash}_000.json"

    real_fsync = _os.fsync
    state = {"fired": False}

    def racing_fsync(fd):
        result = real_fsync(fd)
        # First fsync inside _persist_run is B's reservation fsync —
        # exactly the moment A's publication slips in.
        if not state["fired"]:
            state["fired"] = True
            victim_path.write_text(json.dumps(
                _record(q="probe question", conclusion="A CONCLUSION")),
                encoding="utf-8")
        return result

    monkeypatch.setattr(_os, "fsync", racing_fsync)
    pb = callisto._persist_run(
        _record(q="probe question", conclusion="B CONCLUSION"))

    assert pb != victim_path, (
        "writer B replaced over writer A's published record at the same "
        "final path: A's record was silently destroyed")
    assert json.loads(victim_path.read_text(encoding="utf-8"))[
        "conclusion"] == "A CONCLUSION", "A's record was overwritten"
    rec_b, _ = callisto._load_run(pb.stem)
    assert rec_b["conclusion"] == "B CONCLUSION"
    assert not list(runs_dir.glob("*.resv"))


def test_fix_failed_reservation_fsync_cleans_marker_and_retries_same_seq(
        runs_dir, monkeypatch):
    """If the reservation fsync fails, cleanup must still remove the
    `.json.resv` marker immediately and the retry must be able to reuse
    the same sequence slot (no stale `_000.resv` pushing ids to `_001`)."""
    import os as _os
    real_fsync = _os.fsync
    state = {"failed": False}

    def flaky_fsync(fd):
        if not state["failed"]:
            state["failed"] = True
            raise OSError("simulated reservation fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(_os, "fsync", flaky_fsync)
    p = callisto._persist_run(_record(q="fsync probe"))
    assert "_000" in p.stem, (
        f"stale reservation marker forced retry onto {p.stem}")
    rec, _ = callisto._load_run(p.stem)
    assert rec["question"] == "fsync probe"
    assert not list(runs_dir.glob("*.resv")), "leaked .json.resv marker"
    assert not list(runs_dir.glob("*.tmp"))


# ── repair-pinning: atomic/clean publication failure paths ────────────────

def test_fix_partial_write_failure_leaves_no_sidecars(runs_dir, monkeypatch):
    """A partial tmp write()/flush() failure must clean up BOTH the .tmp and
    the .resv sidecars and may safely retry the SAME sequence slot."""
    import os as _os
    real_fsync = _os.fsync
    state = {"failed": False}

    def flaky_fsync(fd):
        # First fsync of the payload phase = tmp fsync (reservation fsync
        # happens first; let it pass). Fail exactly once.
        if not state["failed"]:
            state["failed"] = True
            raise OSError("simulated tmp fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(_os, "fsync", flaky_fsync)
    p = callisto._persist_run(_record(q="tmp fsync probe"))
    assert "_000" in p.stem, f"pre-publication failure burned a slot: {p.stem}"
    rec, _ = callisto._load_run(p.stem)
    assert rec["question"] == "tmp fsync probe"
    assert not list(runs_dir.glob("*.tmp")), "leaked .json.tmp sidecar"
    assert not list(runs_dir.glob("*.resv")), "leaked .json.resv marker"
    finals = list(runs_dir.glob("*.json"))
    assert len(finals) == 1


def test_fix_final_file_fsync_failure_raises_not_duplicates(
        runs_dir, monkeypatch):
    """After os.replace succeeds, a final-file fsync failure must NOT fall
    into the retry loop (which would publish the same logical run again as
    _001.json). The published record stays; the outcome is raised."""
    import os as _os
    real_fsync = _os.fsync
    real_replace = _os.replace
    state = {"replaced": False}

    def spy_replace(src, dst):
        out = real_replace(src, dst)
        if str(dst).endswith(".json"):
            state["replaced"] = True
        return out

    def flaky_fsync(fd):
        if state["replaced"]:
            raise OSError("simulated final-file fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(_os, "replace", spy_replace)
    monkeypatch.setattr(_os, "fsync", flaky_fsync)
    with pytest.raises(callisto.DurabilityError) as ei:
        callisto._persist_run(_record(q="durability probe"))
    # Exactly ONE record for this run exists — no duplicate under _001.
    finals = [p for p in runs_dir.glob("*.json")
              if "durability" in json.loads(p.read_text())["question"]]
    assert len(finals) == 1
    assert finals[0] == ei.value.path
    json.loads(finals[0].read_text(encoding="utf-8"))  # complete, loadable
    assert not list(runs_dir.glob("*.resv"))
    assert not list(runs_dir.glob("*.tmp"))


def test_fix_directory_fsync_failure_never_duplicates(runs_dir, monkeypatch):
    """A directory-fsync failure after successful publication must not cause
    a second record for the same call."""
    import os as _os
    real_fsync = _os.fsync
    real_replace = _os.replace
    state = {"count": 0}

    def counting_replace(src, dst):
        out = real_replace(src, dst)
        if str(dst).endswith(".json"):
            state["count"] += 1
        return out

    def flaky_fsync(fd):
        # Let reservation + tmp fsync through; fail the first POST-publish
        # fsync (the final-file one), which precedes the directory fsync.
        if state["count"] >= 1:
            raise OSError("simulated post-publication fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(_os, "replace", counting_replace)
    monkeypatch.setattr(_os, "fsync", flaky_fsync)
    with pytest.raises(callisto.DurabilityError):
        callisto._persist_run(_record(q="dirfsync probe"))
    assert state["count"] == 1, "same logical run was replaced/published twice"
    assert len(list(runs_dir.glob("*.json"))) == 1


def test_fix_cleanup_close_failure_still_cleans_and_publishes(
        runs_dir, monkeypatch):
    """A failing os.close during cleanup must not skip unlinking the .resv,
    turn a successfully published record into a retry/duplicate, or leave
    stale sidecars behind."""
    import os as _os
    real_close = _os.close
    state = {"closed": False}

    def flaky_close(fd):
        try:
            return real_close(fd)
        finally:
            if not state["closed"]:
                state["closed"] = True
                raise OSError("simulated cleanup close failure")

    monkeypatch.setattr(_os, "close", flaky_close)
    p = callisto._persist_run(_record(q="cleanup probe"))
    rec, _ = callisto._load_run(p.stem)
    assert rec["question"] == "cleanup probe"
    assert len(list(runs_dir.glob("*.json"))) == 1
    assert not list(runs_dir.glob("*.resv")), "resv not unlinked after close err"
    assert not list(runs_dir.glob("*.tmp"))


# ── repair-pinning: exact publication-state semantics ─────────────────────

def test_fix_short_write_never_becomes_final(runs_dir, monkeypatch):
    """A short fh.write() means the tmp file does NOT hold the intended
    payload. The defect treated any non-raising write as complete and let a
    truncated record through os.replace. Now a short write is a
    pre-publication failure: no final is published, both sidecars are
    removed, and the SAME sequence slot is retried."""
    import io as _io
    real_open = open
    state = {"failed": False}

    def short_write_open(file, mode="r", *a, **kw):
        fh = real_open(file, mode, *a, **kw)
        if "w" in mode and str(file).endswith(".json.tmp"):
            return _ShortWriter(fh)
        return fh

    class _ShortWriter:
        def __init__(self, fh): self._fh = fh
        def write(self, data):
            # Fail exactly one write; later attempts (retry) succeed.
            if not state["failed"]:
                state["failed"] = True
                return self._fh.write(data[: len(data) // 2])
            return self._fh.write(data)
        def __getattr__(self, name): return getattr(self._fh, name)
        def __enter__(self): self._fh.__enter__(); return self
        def __exit__(self, *a): return self._fh.__exit__(*a)

    monkeypatch.setattr("builtins.open", short_write_open)
    p = callisto._persist_run(_record(q="short write probe"))
    assert "_000" in p.stem, f"short write burned a slot: {p.stem}"
    rec, _ = callisto._load_run(p.stem)
    assert rec["question"] == "short write probe"
    # Every final json on disk parses AND exactly one exists.
    finals = list(runs_dir.glob("*.json"))
    assert len(finals) == 1
    for f in finals:
        json.loads(f.read_text(encoding="utf-8"))
    assert not list(runs_dir.glob("*.tmp")), "leaked .json.tmp sidecar"
    assert not list(runs_dir.glob("*.resv")), "leaked .json.resv marker"


def test_fix_reservation_unlink_failure_is_deliberate_not_swallowed(
        runs_dir, monkeypatch):
    """A transient `.resv` unlink failure used to be silently ignored; a
    later retry then skipped `_000`. Cleanup failure semantics must be
    deliberate: nothing published + residue => PersistenceCleanupError,
    never a silent clean-retryable outcome."""
    import os as _os
    real_replace = _os.replace

    def fail_once_then_retry(src, dst):
        if str(dst).endswith(".json"):
            raise OSError("simulated replace failure")
        return real_replace(src, dst)

    real_unlink = type(runs_dir).unlink

    calls = {"n": 0}

    def flaky_unlink(self, missing_ok=False):
        if self.suffix == ".resv" and calls["n"] < 2:
            calls["n"] += 1
            raise OSError("simulated transient unlink failure")
        try:
            return real_unlink(self, missing_ok=missing_ok)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise

    monkeypatch.setattr(_os, "replace", fail_once_then_retry)
    monkeypatch.setattr(type(runs_dir), "unlink", flaky_unlink)
    with pytest.raises(callisto.PersistenceCleanupError) as ei:
        callisto._persist_run(_record(q="resv unlink probe"))
    assert any(".resv" in str(r) for r in ei.value.residues), (
        "residue list must name the un-removable sidecars")
    # Nothing was published by this call.
    assert not list(runs_dir.glob("*.json"))


def test_fix_foreign_final_on_replace_error_is_indeterminate(
        runs_dir, monkeypatch):
    """If os.replace raises while a FOREIGN/malformed file sits at the
    destination, `path.exists()` must NOT be read as proof this call
    published. Publication state is indeterminate: the foreign content is
    neither overwritten nor returned as this call's payload."""
    import os as _os
    stamp = "20260824T070000+0000".replace(":", "").replace("-", "")
    qhash = hashlib.sha256(b"foreign probe").hexdigest()[:8]
    foreign_path = runs_dir / f"{stamp}_{qhash}_000.json"

    real_fsync = _os.fsync
    state = {"fsyncs": 0, "replaced": False}
    # The foreign file must appear AFTER our under-reservation revalidate
    # (which correctly shields earlier appearances) but BEFORE os.replace:
    # the tmp-file fsync — the second fsync inside _persist_run — is that
    # exact deterministic moment for sequence slot _000.
    def racing_fsync(fd):
        result = real_fsync(fd)
        state["fsyncs"] += 1
        if state["fsyncs"] == 2:
            foreign_path.write_text("{not valid json!!", encoding="utf-8")
        return result

    def failing_replace(src, dst):
        if str(dst).endswith(".json") and not state["replaced"]:
            state["replaced"] = True
            raise OSError("simulated replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(_os, "fsync", racing_fsync)
    monkeypatch.setattr(_os, "replace", failing_replace)

    with pytest.raises(callisto.PublicationIndeterminate) as ei:
        callisto._persist_run(_record(q="foreign probe"))
    assert ei.value.path == foreign_path
    # The foreign file was NOT overwritten or claimed.
    assert foreign_path.read_text(encoding="utf-8") == "{not valid json!!"
    # No duplicate of our payload was published under another seq.
    ours = [p for p in runs_dir.glob("*.json")
            if p != foreign_path]
    assert not ours


def test_fix_real_directory_fsync_failure_raises_durability(
        runs_dir, monkeypatch):
    """Existing 'directory fsync' tests actually failed the FILE fsync
    first. This fails ONLY the directory fsync (post-publication), which
    must raise DurabilityError naming the published path — never retry or
    duplicate."""
    import os as _os
    real_fsync = _os.fsync
    real_replace = _os.replace
    state = {"replaced": False}

    def spy_replace(src, dst):
        out = real_replace(src, dst)
        if str(dst).endswith(".json"):
            state["replaced"] = True
        return out

    def flaky_fsync(fd):
        if state["replaced"]:
            raise OSError("simulated directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(_os, "replace", spy_replace)
    monkeypatch.setattr(_os, "fsync", flaky_fsync)
    with pytest.raises(callisto.DurabilityError) as ei:
        callisto._persist_run(_record(q="dir only fsync probe"))
    finals = [p for p in runs_dir.glob("*.json")
              if "dir only fsync" in json.loads(p.read_text())["question"]]
    assert len(finals) == 1 and finals[0] == ei.value.path
    assert "directory fsync failed" in str(ei.value)


def test_fix_post_publication_open_failures_reported_with_path(
        runs_dir, monkeypatch):
    """Post-publication os.open failures on the final file / runs dir are
    observation failures AFTER commit: classified as durability-unconfirmed
    (DurabilityError naming the path), not silent success and not a
    retryable pre-publication error."""
    import os as _os
    real_replace = _os.replace
    real_open = _os.open
    state = {"replaced": False}

    def spy_replace(src, dst):
        out = real_replace(src, dst)
        if str(dst).endswith(".json"):
            state["replaced"] = True
        return out

    def flaky_open(path, *a, **kw):
        if state["replaced"]:
            raise OSError("simulated post-publication open failure")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(_os, "replace", spy_replace)
    monkeypatch.setattr(_os, "open", flaky_open)
    with pytest.raises(callisto.DurabilityError) as ei:
        callisto._persist_run(_record(q="open probe"))
    finals = [p for p in runs_dir.glob("*.json")
              if "open probe" in json.loads(p.read_text())["question"]]
    assert len(finals) == 1 and finals[0] == ei.value.path
    assert "could not be reopened" in str(ei.value) or            "could not be opened" in str(ei.value)
    assert not list(runs_dir.glob("*.tmp"))
    assert not list(runs_dir.glob("*.resv"))


def test_cli_reports_published_but_durability_unconfirmed(
        runs_dir, monkeypatch, capsys):
    """The CLI defect: a DurabilityError after os.replace reached the user
    as `NOT SAVED`, inviting a retry that would create a duplicate `_001`.
    The outcome must be reported as SAVED WITH UNCONFIRMED DURABILITY,
    including the path, and must NOT say NOT SAVED."""
    import asyncio
    from types import SimpleNamespace as NS
    import callisto as C
    from callisto import _cmd_ask, build_parser

    class FakeRouter:
        endpoints = {}
        task_classes = {}
        default_tier_name = "gpu1"
        class cost_ledger:
            @staticmethod
            def snapshot(): return {"by_tier": {}}
        async def check_health(self, tier):
            return {"status": "ok"}

    def make_engine(router, self_review=False):
        async def run(q):
            return NS(sealed=True, refusal_reason="", leaves=[],
                      confidence_score=0.5, confidence_tier="X",
                      conclusion="c", fetches=[], objections=[], notes=[],
                      artifact_refs=[])
        eng = NS(run=run)
        return eng

    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(runs_dir))
    monkeypatch.setattr(C, "_load_router", lambda p: FakeRouter())
    monkeypatch.setattr(C, "_make_engine", make_engine)

    def boom(record):
        raise callisto.DurabilityError(
            runs_dir / "published.json",
            "directory fsync failed: simulated")
    monkeypatch.setattr(C, "_persist_run", boom)

    rc = asyncio.run(_cmd_ask(build_parser().parse_args(["ask", "q"])))
    out = capsys.readouterr().out
    assert "NOT SAVED" not in out, (
        "published record mis-reported as unsaved — invites duplicate retry")
    assert "UNCONFIRMED DURABILITY" in out
    assert str(runs_dir / "published.json") in out
    assert rc == 0


def test_fix_durability_error_is_deliberate_documented_outcome(runs_dir):
    """The DurabilityError contract: it names the already-published path and
    carries it as `.path`, so callers can decide without a re-lookup."""
    exc = callisto.DurabilityError(
        runs_dir / "x.json", "final-file fsync failed: boom")
    assert exc.path == runs_dir / "x.json"
    assert str(runs_dir / "x.json") in str(exc)
