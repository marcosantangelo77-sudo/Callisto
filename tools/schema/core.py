"""Core schema — the domain-general claim lifecycle.

Everything here describes a falsifiable claim and the evidence for it.
No table in this module contains domain vocabulary: no sports, no books,
no markets, no players. Domain-specific structure belongs in plugin
schemas (see plugins/sports/schema.py) which register against this core.

The lifecycle tables (hypotheses, backtest_runs, backtest_events,
paper_trades, hypothesis_stats) are shared by every domain. Columns that
only make sense for one domain live in that domain's plugin-owned
extension table, keyed to the core row — adding a domain never alters
these definitions.
"""

CORE_SCHEMA_SQL = """
-- ──────────────────────────────────────────
-- EMBEDDINGS: semantic vector store
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_text TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    metadata_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(collection, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_collection ON embeddings(collection);

-- ──────────────────────────────────────────
-- EVENT LOG: audit trail for event bus
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    event_data TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_event_log_type_time ON event_log(event_type, created_at);

-- ──────────────────────────────────────────
-- INGESTION OBSERVABILITY: per-source run ledger
-- ──────────────────────────────────────────
-- Populated by @tracked_ingestion (tools/ingestion_tracking.py). Each call to
-- a wrapped ingestion function writes one row on entry (status='running') and
-- updates it on exit with the final status, duration, and row count.
--
-- Source tags are hierarchical (e.g. 'espn.scoreboard.mlb',
-- 'odds_api_io.v3.odds.updated') and STABLE — changing them loses history
-- for SLA evaluation.
--
-- Read by tools/health.py::_check_data_collector which compares each source's
-- most-recent `finished_at` against the SLA table and trips the breaker when
-- runs go stale. This is how Callisto notices that ESPN has been 500-looping
-- for six hours — something we previously had ZERO visibility into.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    status TEXT NOT NULL,
    rows_ingested INTEGER DEFAULT 0,
    error_class TEXT,
    error_message TEXT,
    duration_ms INTEGER,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_source_finished
    ON ingestion_runs(source, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_status
    ON ingestion_runs(status, finished_at DESC);

-- ──────────────────────────────────────────
-- PREDICTIONS + OUTCOMES: the domain-general resolution seam
-- ──────────────────────────────────────────
-- What tools/resolvers/generic.py::SqlitePredictionResolver has read since
-- B1, formalised at last: one PREREGISTERED prediction per row (a number
-- committed before ground truth), and at most one outcome per prediction
-- (outcomes.prediction_id IS the primary key — a second resolution of the
-- same prediction cannot be inserted, only refused).
--
-- predicted_prob is NOT NULL with a 0..1 CHECK: a prediction without a
-- number is not a prediction (K1's lesson — absence must fail closed).
-- claim_id deliberately carries NO foreign key to hypotheses(hypothesis_id):
-- that table is plugin-owned (plugins/sports/schema.py) and the core must
-- not depend on any plugin. Integrity is enforced in code
-- (tools/resolvers/generic.py::PredictionJournal) by requiring the claim
-- row to exist at write time.
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    predicted_prob REAL NOT NULL CHECK(predicted_prob >= 0.0 AND predicted_prob <= 1.0),
    context_key TEXT,
    due_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_predictions_claim ON predictions(claim_id);
CREATE INDEX IF NOT EXISTS idx_predictions_open ON predictions(due_at);

CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id INTEGER PRIMARY KEY,
    resolved_outcome TEXT NOT NULL
        CHECK(resolved_outcome IN ('positive','negative','indeterminate')),
    payoff REAL,
    resolved_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_by TEXT NOT NULL DEFAULT 'owner'
);
"""


# Domain-neutral extension point: a plugin may register a side table keyed
# to hypotheses(hypothesis_id) without touching this file. See
# tools/schema/registry.py and plugins/sports/schema.py.
