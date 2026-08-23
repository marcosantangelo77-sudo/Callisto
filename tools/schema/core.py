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
-- PREDICTIONS / OUTCOMES: the domain-general resolution record.
--
-- One prediction = one falsifiable instance of a recurring claim (claim_id
-- matches hypotheses.hypothesis_id for lifecycle-tracked claims), with the
-- claim-time probability AND the market's implied probability at that
-- moment — CLV generalised (NEXT.md §2). One outcome = ground truth when it
-- arrives. tools/resolvers/generic.py reads exactly this pair; nothing may
-- UPDATE a prediction after commit (the first committed probability stands,
-- preregistration-style) and UNIQUE(claim_id, event_id) means the same
-- event can never double-count toward a claim's sample.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    predicted_prob REAL,
    book_implied_prob REAL,
    odds_american INTEGER,
    model_fair_prob REAL,
    clv_prob_bp REAL,
    context_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(claim_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_predictions_claim ON predictions(claim_id);

CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id),
    resolved_outcome TEXT NOT NULL
        CHECK(resolved_outcome IN ('positive', 'negative', 'indeterminate')),
    payoff REAL,
    resolved_at TEXT
);
"""


# Domain-neutral extension point: a plugin may register a side table keyed
# to hypotheses(hypothesis_id) without touching this file. See
# tools/schema/registry.py and plugins/sports/schema.py.
