"""Lifecycle intake tests — questions become recurring hypotheses.

Covers the domain-general resolution seam end to end: core schema tables
(predictions/outcomes), PredictionJournal fail-closed rules, the read-back
through SqlitePredictionResolver, the handoff into research_program's
inheritance rule, and the CLI entry points.

Family discipline (PATTERNS #1/#3/#7):
  - every gate here is fed an EMPTY/bad input and must REFUSE;
  - mutation checks: break a validation and the suite must notice —
    including at the DB layer (CHECK constraints) with code disabled.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sqlite3

import aiosqlite
import pytest

os.environ.setdefault("CALLISTO_SILENCE_ONEDRIVE_WARNING", "1")

from tools.resolvers import (
    PredictionJournal,
    PredictionJournalError,
    SqlitePredictionResolver,
)
from tools.schema.engine import ensure_schema
from tools.research_program import (
    SPECULATIVE_CAP,
    clamp_parent_confidence,
    inherited_ceiling,
)


# ── helpers ───────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "lifecycle.db")


@pytest.fixture()
def journaled(db_path):
    """A real file DB with the full schema applied + one general claim."""

    async def _make():
        await ensure_schema(db_path)
        db = await aiosqlite.connect(db_path)
        j = PredictionJournal(db)
        cid = await j.create_claim(
            name="btc-hashrate-ath",
            thesis="Bitcoin hashrate makes a new ATH before 2026-12-31")
        return db, j, cid

    db, j, cid = asyncio.run(_make())
    yield db, j, cid
    asyncio.run(db.close())


def _run(coro):
    return asyncio.run(coro)


# ── schema seam ───────────────────────────────────────────────────────


def test_ensure_schema_creates_resolution_tables(db_path):
    async def t():
        await ensure_schema(db_path)
        db = await aiosqlite.connect(db_path)
        try:
            for table in ("predictions", "outcomes"):
                cur = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name=?", (table,))
                assert await cur.fetchone(), f"{table} missing from core DDL"
            # outcomes.prediction_id IS the primary key: one resolution each
            cur = await db.execute("PRAGMA table_info(outcomes)")
            pk = [r for r in await cur.fetchall() if r[5]]
            assert [c[1] for c in pk] == ["prediction_id"]
        finally:
            await db.close()

    _run(t())


def test_fresh_db_schema_is_quiet(db_path, caplog):
    """The five 'Failed to ADD COLUMN' warnings on every first boot were
    noise that made fresh-DB contact look broken. They must stay gone."""
    async def t():
        logging.getLogger("callisto.schema").setLevel(logging.DEBUG)
        with caplog.at_level(logging.WARNING, logger="callisto.schema"):
            await ensure_schema(db_path)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not [w for w in warnings
                    if "Failed to ADD COLUMN" in w.getMessage()], \
            "fresh-DB schema must not warn about owner-owned tables"

    _run(t())


def test_wiki_owner_create_carries_source_task_id():
    """Owner CREATE and the engine migration must agree (schema parity)."""
    import re
    from tools.knowledge_wiki import WIKI_SCHEMA_SQL
    ddl = re.sub(r"--[^\n]*", "", WIKI_SCHEMA_SQL)
    assert "source_task_id" in ddl


# ── claim registration ────────────────────────────────────────────────


def test_create_claim_idempotent_on_name(journaled):
    db, j, cid = journaled

    async def t():
        again = await j.create_claim(
            name="btc-hashrate-ath", thesis="whatever")
        assert again == cid
        cur = await db.execute(
            "SELECT sport, market_type, status FROM hypotheses "
            "WHERE hypothesis_id = ?", (cid,))
        row = await cur.fetchone()
        assert row == ("general", "forecast", "draft")

    _run(t())


@pytest.mark.parametrize("name,thesis", [
    ("", "thesis"),
    ("   ", "thesis"),
    ("name", ""),
    ("name", "   "),
])
def test_create_claim_refuses_empty_inputs(journaled, name, thesis):
    _, j, _ = journaled
    with pytest.raises(PredictionJournalError):
        _run(j.create_claim(name=name, thesis=thesis))


# ── prediction recording: absence fails closed ────────────────────────


def test_record_prediction_happy_path_and_stage_sync(journaled):
    db, j, cid = journaled
    pid = _run(j.record_prediction(
        claim_id=cid, event_id="hashrate-ath-2026",
        predicted_prob=0.65, due_at="2026-12-31"))
    assert isinstance(pid, int) and pid > 0

    async def t():
        cur = await db.execute(
            "SELECT status FROM hypotheses WHERE hypothesis_id=?", (cid,))
        return (await cur.fetchone())[0]

    assert _run(t()) == "paper_trading"


def test_prediction_without_claim_refused(journaled):
    _, j, _ = journaled
    with pytest.raises(PredictionJournalError, match="does not exist"):
        _run(j.record_prediction(
            claim_id="ghost", event_id="e", predicted_prob=0.5,
            due_at="2026-09-01"))


@pytest.mark.parametrize("prob", [None, "abc", -0.01, 1.0001, float("nan")])
def test_prediction_without_valid_probability_refused(journaled, prob):
    """K1's lesson: a 'prediction' without a number is not a prediction."""
    _, j, cid = journaled
    with pytest.raises(PredictionJournalError):
        _run(j.record_prediction(
            claim_id=cid, event_id="e", predicted_prob=prob,
            due_at="2026-09-01"))
    # nothing half-written
    db = journaled[0]

    async def count():
        cur = await db.execute("SELECT COUNT(*) FROM predictions")
        return (await cur.fetchone())[0]

    assert _run(count()) == 0


@pytest.mark.parametrize("event_id", ["", "   ", None])
def test_prediction_without_event_id_refused(journaled, event_id):
    _, j, cid = journaled
    with pytest.raises(PredictionJournalError):
        _run(j.record_prediction(
            claim_id=cid, event_id=event_id, predicted_prob=0.5,
            due_at="2026-09-01"))


def test_bad_deadline_refused_not_silently_dropped(journaled):
    _, j, cid = journaled
    with pytest.raises(PredictionJournalError, match="unparseable date"):
        _run(j.record_prediction(
            claim_id=cid, event_id="e", predicted_prob=0.5,
            due_at="sometime soon"))


def test_db_check_constraint_is_the_second_gate(journaled):
    """Mutation check (PATTERNS #7): disable the code-level validation by
    inserting raw; the table CHECK must still refuse out-of-range probs."""
    db, _, cid = journaled
    with pytest.raises(sqlite3.IntegrityError):
        _run(db.execute(
            "INSERT INTO predictions (claim_id, event_id, predicted_prob) "
            "VALUES (?, 'raw', 1.5)", (cid,)))
    _run(db.rollback())


# ── resolution: explicit, once, vocabulary-checked ────────────────────


def test_resolve_then_conflicting_reResolution_refused(journaled):
    _, j, cid = journaled
    pid = _run(j.record_prediction(
        claim_id=cid, event_id="e1", predicted_prob=0.65,
        due_at="2026-12-31"))
    r = _run(j.resolve_prediction(pid, "yes"))
    assert r["resolved_outcome"] == "positive" and not r["idempotent"]
    dup = _run(j.resolve_prediction(pid, "true"))       # same meaning
    assert dup["idempotent"]
    with pytest.raises(PredictionJournalError, match="already resolved"):
        _run(j.resolve_prediction(pid, "no"))


@pytest.mark.parametrize("token", ["banana", "", "maybe", "probably yes"])
def test_unknown_outcome_tokens_refused_not_coerced(journaled, token):
    _, j, cid = journaled
    pid = _run(j.record_prediction(
        claim_id=cid, event_id="e2", predicted_prob=0.4, due_at="2026-10-01"))
    with pytest.raises(PredictionJournalError, match="not resolvable"):
        _run(j.resolve_prediction(pid, token))
    # refusal left no outcome behind (absence is not indeterminate)
    db = journaled[0]

    async def count():
        cur = await db.execute("SELECT COUNT(*) FROM outcomes")
        return (await cur.fetchone())[0]

    assert _run(count()) == 0


def test_db_check_constraint_blocks_garbage_outcomes(journaled):
    db, _, _ = journaled
    with pytest.raises(sqlite3.IntegrityError):
        _run(db.execute(
            "INSERT INTO outcomes (prediction_id, resolved_outcome) "
            "VALUES (999, 'banana')"))
    _run(db.rollback())


def test_open_predictions_orders_and_filters(journaled):
    _, j, cid = journaled
    _run(j.record_prediction(claim_id=cid, event_id="a",
                             predicted_prob=0.1, due_at="2027-06-30"))
    _run(j.record_prediction(claim_id=cid, event_id="b",
                             predicted_prob=0.2, due_at="2026-09-30"))
    rows = _run(j.open_predictions(cid))
    assert [r["event_id"] for r in rows] == ["b", "a"]     # soonest due first
    assert all("claim_name" in r for r in rows)


# ── read-back through the REAL resolver ───────────────────────────────


def test_sqlite_resolver_reads_journal_rows(journaled):
    db, j, cid = journaled
    p_yes = _run(j.record_prediction(claim_id=cid, event_id="hit-case",
                                     predicted_prob=0.7, due_at="2026-10-01"))
    p_no = _run(j.record_prediction(claim_id=cid, event_id="miss-case",
                                    predicted_prob=0.3, due_at="2026-10-01"))
    _run(j.resolve_prediction(p_yes, "yes"))
    _run(j.resolve_prediction(p_no, "no"))

    res = SqlitePredictionResolver(db)

    async def t():
        recs = [r async for r in res.iter_evidence(cid)]
        s = await res.summarize(cid)
        return recs, s

    recs, s = _run(t())
    by_event = {r.event_id: r for r in recs}
    assert by_event["hit-case"].binary_outcome == 1
    assert by_event["miss-case"].binary_outcome == 0
    assert s.total == 2 and s.positive == 1 and s.negative == 1
    assert s.fully_resolved


# ── into the inheritance rule ─────────────────────────────────────────


def _records_for(j, cid, **kw):
    async def t():
        return await j.track_records(cid)
    return _run(t())


def test_unsettled_predictions_are_excluded_not_scored(journaled):
    _, j, cid = journaled
    future = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(days=30)).date().isoformat()
    _run(j.record_prediction(claim_id=cid, event_id="pending",
                             predicted_prob=0.5, due_at=future))
    assert _records_for(j, cid) == []
    summ = _run(j.track_summary(cid))
    assert summ["n_resolved"] == 0
    assert summ["inherited_ceiling"] == pytest.approx(SPECULATIVE_CAP)


def test_overdue_unresolved_scores_stale(journaled):
    _, j, cid = journaled
    past = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=1)).date().isoformat()
    _run(j.record_prediction(claim_id=cid, event_id="never-graded",
                             predicted_prob=0.5, due_at=past))
    recs = _records_for(j, cid)
    assert len(recs) == 1 and recs[0]["outcome"] == "stale"
    summ = _run(j.track_summary(cid))
    assert summ["n_stale"] == 1
    assert summ["brier"] == pytest.approx(1.0)


def test_no_deadline_predictions_never_go_stale_only_resolve_or_wait(journaled):
    """Without a due date there is no deadline to miss: an unresolved
    open-ended prediction sits excluded forever (never stale), while a
    RESOLVED one still scores — the number was committed before ground
    truth either way."""
    _, j, cid = journaled
    pid_open = _run(j.record_prediction(
        claim_id=cid, event_id="open-ended", predicted_prob=0.9))  # no due_at
    pid_hit = _run(j.record_prediction(
        claim_id=cid, event_id="settled", predicted_prob=0.9))     # no due_at
    _run(j.resolve_prediction(pid_hit, "yes"))
    recs = _records_for(j, cid)
    assert [r["outcome"] for r in recs] == ["hit"]     # settled counts,
    # open-ended absent — not stale, not scored, just waiting
    summ = _run(j.track_summary(cid))
    assert summ["n_stale"] == 0 and summ["n_resolved"] == 1


def test_five_strong_hits_lift_parent_above_speculative_cap(journaled):
    """THE payoff: through the real DB path, five clean resolutions raise
    the inherited ceiling above what zero evidence permits."""
    _, j, cid = journaled
    due = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(days=7)).date().isoformat()

    async def seed():
        for i in range(5):
            p = await j.record_prediction(
                claim_id=cid, event_id=f"fut-{i}", predicted_prob=0.8,
                due_at=due)
            await j.resolve_prediction(p, "yes")

    _run(seed())
    recs = _records_for(j, cid)
    assert [r["outcome"] for r in recs] == ["hit"] * 5
    raw_ceiling = inherited_ceiling(recs)
    assert raw_ceiling > SPECULATIVE_CAP
    clamped, tier = clamp_parent_confidence(0.99, recs)
    assert clamped == pytest.approx(min(0.99, raw_ceiling))
    assert tier in ("PROBABLE", "SPECULATIVE")


def test_track_summary_shape(journaled):
    _, j, cid = journaled
    s = _run(j.track_summary(cid))
    assert set(s) == {"n_resolved", "n_hit", "n_stale", "brier",
                      "inherited_ceiling", "ceiling_tier"}


# ── CLI contract ──────────────────────────────────────────────────────


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.chdir(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    import callisto
    return callisto


def _cli(cli_env, *argv):
    return cli_env.main(list(argv))


def test_cli_predict_requires_deadline_and_range(cli_env, capsys):
    assert _cli(cli_env, "predict", "ev", "--claim", "c",
                "--prob", "0.5", "--by", "") != 0
    assert _cli(cli_env, "predict", "ev", "--claim", "c",
                "--prob", "1.5", "--by", "2026-09-01") == 2
    out = capsys.readouterr().out
    assert "deadline" in out or "[0,1]" in out


def test_cli_predict_resolve_roundtrip(cli_env, capsys):
    rc = _cli(cli_env, "predict", "Bitcoin hashrate new ATH",
              "--claim", "btc-hr", "--prob", "0.6", "--by", "2026-12-31")
    assert rc == 0
    out = capsys.readouterr().out
    assert "paper_trading" in out
    assert "#1" in out

    rc = _cli(cli_env, "resolve", "1", "yes")
    assert rc == 0
    out = capsys.readouterr().out
    assert "positive recorded" in out
    assert "track record: n=1 hits=1" in out
    assert "inherited ceiling" in out


def test_cli_predict_same_claim_twice_reuses_claim_row(cli_env, capsys):
    assert _cli(cli_env, "predict", "e one", "--claim", "dup-check",
                "--prob", "0.4", "--by", "2026-11-01") == 0
    assert _cli(cli_env, "predict", "e two", "--claim", "dup-check",
                "--prob", "0.6", "--by", "2027-01-01") == 0
    capsys.readouterr()
    rc = _cli(cli_env, "predictions")
    out = capsys.readouterr().out
    assert rc == 0 and out.count("[dup-check]") == 2


def test_cli_predictions_empty_and_resolve_unknown(cli_env, capsys):
    rc = _cli(cli_env, "predictions")
    out = capsys.readouterr().out
    assert rc == 0 and "no open predictions" in out
    assert _cli(cli_env, "resolve", "42", "yes") == 1
    assert "no prediction #42" in capsys.readouterr().out


def test_cli_status_shows_general_claims(cli_env, capsys, tmp_path):
    _cli(cli_env, "predict", "e", "--claim", "status-check",
         "--prob", "0.55", "--by", "2026-12-01")
    capsys.readouterr()
    rc = _cli(cli_env, "status")
    out = capsys.readouterr().out
    assert rc == 0 and "GENERAL CLAIMS" in out and "paper_trading" in out
