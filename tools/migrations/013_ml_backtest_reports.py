"""Migration 013: ``ml_backtest_reports`` — per-hypothesis ML backtest rollups.

Why this exists
---------------
``feat/ml-features-and-classifier`` (commit daa453c) added ``tools.ml_backtest``
which runs an XGBoost classifier replay and produces an :class:`MLBacktestReport`
with hit_rate / ROI / CLV / Sharpe — the exact shape the hand-crafted
``backtest_runs`` rollups carry, but derived from the ML pipeline instead of
the hand-seeded thesis.

``ml_backtest.py`` was orphan code: nothing stored the reports, so the
promotion gate had no ML evidence to evaluate. This migration gives the
promotion path a place to durably record "what did the ML baseline say the
last time we checked it for this hypothesis?" — the row the paper→live gate
consults before allowing promotion.

One row per (hypothesis_id, model_path, evaluated_at). The model_path is
included because a hypothesis may be re-evaluated against a fresher model
after retraining; we want to keep the history, not overwrite. The gate
always reads the most recent row for a hypothesis.

Schema choices
--------------
* ``hit_rate``, ``roi_pct``, ``clv_implied_mean``, ``sharpe`` are nullable:
  a backtest can produce fewer than the gate's min_signals (defined in
  ``tools.ml_promotion_gate``), in which case the summary stats are NULL
  rather than misleading zero-of-zero values.
* ``report_json`` holds the full ``asdict(MLBacktestReport)`` payload so
  audits can reconstruct the per-day P/L curve without re-running the
  classifier.
* ``is_stale_model`` reflects the drift gate's answer at evaluation time —
  we write it here so the /health endpoint and operators can see in one
  place which hypotheses had their promotion decision taken with a
  drift-flagged model.

Indexing
--------
The dominant lookup is "latest row for hypothesis X" (covered by
``idx_ml_bt_hyp``). Secondary: "all rows for a given model" for drift
forensics (covered by ``idx_ml_bt_model``).

Down migration
--------------
Dropping the table loses the audit trail of what the ML gate saw at each
promotion decision — provided but guarded.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_backtest_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id TEXT NOT NULL,
            model_path TEXT NOT NULL,
            sport TEXT,
            market TEXT,
            threshold REAL,
            n_signals INTEGER,
            n_resolved INTEGER,
            hits INTEGER,
            pushes INTEGER,
            misses INTEGER,
            hit_rate REAL,
            roi_pct REAL,
            clv_implied_mean REAL,
            sharpe REAL,
            is_stale_model INTEGER NOT NULL DEFAULT 0,
            gate_decision TEXT,
            gate_reasons TEXT,
            report_json TEXT,
            evaluated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ml_bt_hyp "
        "ON ml_backtest_reports(hypothesis_id, evaluated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ml_bt_model "
        "ON ml_backtest_reports(model_path, evaluated_at DESC)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_ml_bt_model")
    conn.execute("DROP INDEX IF EXISTS idx_ml_bt_hyp")
    conn.execute("DROP TABLE IF EXISTS ml_backtest_reports")
