# P2 — DB migration proposal: preregistrations and long-lived claims

Written per the brief: storage for the new P2 objects is FILE-BACKED for now
(`agp/claims.ClaimStore`, `agp/research_program.ProgramStore`); no migration
has been run against any database. This is the design for when it lands,
against the `tools/schema.py` core/plugin seam.

## What exists now

- **Preregistration** (`agp/preregistration.py`) — sealed criteria, chained
  amendments, scored outcomes. Self-serializes via `to_dict()/from_dict()`.
- **Claim** (`agp/claims.py`) — evidence attachments, belief history
  (the calibration record), resolution/retraction. Journal is JSONL with a
  sha256 hash chain; tampering is detected on load.
- **ResearchProgram persistence** — full-state round-trip incl. nested
  question status; one atomic JSON file per program.

The file stores are correct-by-construction and tamper-evident, but they do
not support cross-claim queries ("all beliefs held at date D", "calibration
by domain") or concurrent multi-process writes. That is what the DB adds —
nothing else.

## Proposed schema (CORE tables — domain-general, plugin seam respected)

```sql
-- core: claims (domain-general lifecycle)
CREATE TABLE claims (
    claim_id      TEXT PRIMARY KEY,
    text          TEXT NOT NULL CHECK(length(text) > 0),
    domain        TEXT NOT NULL DEFAULT 'GENERAL',   -- mirrors agp.Domain values
    status        TEXT NOT NULL CHECK(status IN
        ('draft','open','suspended','confirmed','refuted','ambiguous','retracted')),
    confidence    REAL NOT NULL CHECK(confidence >= 0.30 AND confidence <= 1.0),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- core: append-only belief history. THE calibration record. No UPDATE/DELETE.
CREATE TABLE claim_beliefs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id      TEXT NOT NULL REFERENCES claims(claim_id),
    at            TEXT NOT NULL,
    confidence    REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    tier          TEXT NOT NULL,
    basis_evidence_count  INTEGER NOT NULL,
    basis_best_class      TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    prev_confidence REAL
);

-- core: attached evidence (provenance-assigned class, never self-declared)
CREATE TABLE claim_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id      TEXT NOT NULL REFERENCES claims(claim_id),
    content       TEXT NOT NULL,
    assigned_class TEXT NOT NULL,
    declared_class TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    attached_at   TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT ''
);

-- core: preregistration seal + amendment chain (criteria as canonical JSON)
CREATE TABLE preregistrations (
    prereg_query   TEXT NOT NULL,
    criteria_json  TEXT NOT NULL,     -- Criteria.to_dict(), sort_keys canonical
    created_at     TEXT NOT NULL,
    sealed_at      TEXT NOT NULL,
    seal_hash      TEXT NOT NULL,     -- same HMAC machinery as sessions
    PRIMARY KEY (prereg_query, sealed_at)
);
CREATE TABLE prereg_amendments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root_seal_hash TEXT NOT NULL REFERENCES preregistrations(seal_hash),
    prior_seal_hash TEXT NOT NULL,
    new_criteria_json TEXT NOT NULL,
    reason        TEXT NOT NULL CHECK(length(reason) > 0),
    amended_at    TEXT NOT NULL,
    amendment_seal TEXT NOT NULL
);
```

## Plugin-side

Nothing. Claims/preregistrations carry no domain vocabulary; sports-specific
linking stays in the sports plugin via `lifecycle_link` (already a free-text
bridge field).

## Rules honored

- Append-only history tables mean the calibration record cannot be rewritten
  with UPDATE grants revoked — strictly stronger than the JSONL chain, which
  this proposal should REPLACE once live (the journal format remains the
  transport/import path).
- Confidence floor 0.30 matches the existing DB CHECK convention.
- No gate values are stored here; thresholds remain in agp/thresholds.py.
  Nothing automated may write to `claims.confidence` except through the
  clamp path (`agp.claims.recompute_confidence`) — an application-layer
  invariant the schema cannot express, so the writer module is single-purpose.

## Migration mechanics (when approved)

1. Backup the owner's real DB first (HANDOFF hard rule 5).
2. Additive-only migration: new tables, no ALTER of existing ones.
3. Import path: replay each `ClaimStore` journal into
   claims/beliefs/evidence, verifying the hash chain before insert; skip
   already-imported claim_ids (idempotent).
4. Keep `ProgramStore` on files unless programs also need cross-program
   queries — program JSON is self-consistent and small.
