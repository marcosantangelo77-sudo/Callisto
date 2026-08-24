"""Domain-general outcome resolution for the hypothesis lifecycle.

This package turns the betting-specific evidence model (won/lost/push read
off ``paper_trades.home_team``) into an interface any falsifiable claim can
implement. The lifecycle stages keep their storage names (backcompat with
the schema and every existing reader) but their *semantics* are general:

    draft          — claim registered, no test defined yet
    backtesting    — retrospective evaluation against historical records
    paper_trading  — PREREGISTERED FORWARD-TESTING: predictions recorded
                     before ground truth arrives (sports paper trades,
                     pre-registered forecasts, held-out benchmark runs)
    live           — a DEPLOYED CONCLUSION: the claim drives decisions
                     (bets, recommendations, triage flags), not necessarily
                     capital
    retired        — withdrawn or superseded

An OutcomeResolver answers exactly one question per claim:
"has this resolved, and how did it score?" — as a stream of EvidenceRecords.
Everything downstream (Brier, IC, calibration bins, binomial significance)
already operates on prediction-vs-outcome pairs; this package is what feeds
them without a sportsbook present.

Nothing in this package arms execution or touches money. It is read-only
over whatever store the resolver's domain uses.
"""

from tools.resolvers.base import (
    BETTING_OUTCOME_MAP,
    OUTCOME_INDETERMINATE,
    OUTCOME_NEGATIVE,
    OUTCOME_POSITIVE,
    EvidenceRecord,
    OutcomeResolver,
    ResolutionSummary,
    STAGE_SEMANTICS,
)
from tools.resolvers.betting import BettingOutcomeResolver
from tools.resolvers.generic import (
    GenericPredictionResolver,
    InMemoryOutcomeResolver,
    PredictionJournal,
    PredictionJournalError,
    SqlitePredictionResolver,
)

__all__ = [
    "EvidenceRecord",
    "ResolutionSummary",
    "OutcomeResolver",
    "BettingOutcomeResolver",
    "GenericPredictionResolver",
    "InMemoryOutcomeResolver",
    "SqlitePredictionResolver",
    "PredictionJournal",
    "PredictionJournalError",
    "STAGE_SEMANTICS",
    "BETTING_OUTCOME_MAP",
    "OUTCOME_POSITIVE",
    "OUTCOME_NEGATIVE",
    "OUTCOME_INDETERMINATE",
]
