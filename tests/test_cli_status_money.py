"""Tests: callisto status prints bind host and money-switch env safely."""
from __future__ import annotations

import argparse
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import callisto  # noqa: E402


def _status(capsys):
    rc = callisto._cmd_status(argparse.Namespace())
    return rc, capsys.readouterr().out


def _with_db(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    monkeypatch.setattr(callisto, "_db_path", lambda: str(db))
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE hypotheses (hypothesis_id TEXT, name TEXT, "
                 "sport TEXT, market_type TEXT, status TEXT)")
    conn.execute("CREATE TABLE backtest_events (id INTEGER PRIMARY KEY, "
                 "hypothesis_id TEXT, signal_generated INTEGER, edge REAL)")
    conn.commit()
    conn.close()
    return db


def test_status_unset_env_shows_loopback_and_off(capsys, tmp_path,
                                                 monkeypatch):
    _with_db(tmp_path, monkeypatch)
    for name in ("CALLISTO_BIND_HOST", "CALLISTO_LOCAL_ONLY",
                 "CALLISTO_ALLOW_LIVE_EXECUTE",
                 "CALLISTO_ALLOW_SIGNAL_REFRESH"):
        monkeypatch.delenv(name, raising=False)
    rc, out = _status(capsys)
    assert rc == 0
    assert "127.0.0.1" in out
    assert "ALLOW_LIVE_EXECUTE" in out
    assert "off" in out


def test_status_never_prints_seal_key(capsys, tmp_path, monkeypatch):
    _with_db(tmp_path, monkeypatch)
    secret = "deadbeef" * 8
    monkeypatch.setenv("CALLISTO_SEAL_KEY", secret)
    rc, out = _status(capsys)
    assert rc == 0
    assert secret not in out


def test_status_live_execute_on(capsys, tmp_path, monkeypatch):
    _with_db(tmp_path, monkeypatch)
    monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", "1")
    rc, out = _status(capsys)
    assert rc == 0
    assert "ALLOW_LIVE_EXECUTE: on" in out


def test_status_no_db_still_prints_switches(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(callisto, "_db_path",
                        lambda: str(tmp_path / "missing.sqlite"))
    rc, out = _status(capsys)
    assert rc == 0
    assert "127.0.0.1" in out
    assert "LOCAL_ONLY" in out
