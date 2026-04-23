"""Task classifier + adaptive budget table for the orchestrator task_worker.

Five buckets, each with a default timeout. Every value is overridable by env
var so an operator can tune without a code change:

    CALLISTO_TIMEOUT_QUICK_S     (default 60)
    CALLISTO_TIMEOUT_NEWS_S      (default 180)
    CALLISTO_TIMEOUT_HYPGEN_S    (default 600)
    CALLISTO_TIMEOUT_DEEP_S      (default 900)
    CALLISTO_TIMEOUT_DEFAULT_S   (default 300 — legacy TASK_WORKER_TIMEOUT_S)
    CALLISTO_TIMEOUT_HARD_CEILING_S (default 1800 — absolute max w/ extensions)

Classifier rules (first match wins — ordered most-specific → least-specific):

    DEEP    — "analyze/investigate/deep dive/evaluate/verify/audit/research"
    HYPGEN  — "generate hypothesis/find new edges/find new signals"
    NEWS    — "injuries/weather/lineup/starting pitcher/news"
    QUICK   — "current odds/current score/who's winning/current line/price check"
    DEFAULT — everything else (keeps backward-compat 300s)

The order matters: "analyze the latest injuries" would match NEWS on the
injuries keyword AND DEEP on "analyze" — DEEP wins because analysis almost
always requires more tool hops than a news lookup. Misclassification is
tolerated: every bucket still beats the current single-value fixed budget.
"""

from __future__ import annotations

import os
import re
from enum import Enum


class TaskType(str, Enum):
    QUICK = "quick"
    NEWS = "news"
    HYPGEN = "hypgen"
    DEEP = "deep"
    DEFAULT = "default"


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except (TypeError, ValueError):
        return default


def get_budget_s(task_type: TaskType) -> float:
    """Resolve the initial budget for a task_type.

    Reads env vars at call time so tests can monkey-patch os.environ.
    """
    if task_type == TaskType.QUICK:
        return _env_float("CALLISTO_TIMEOUT_QUICK_S", 60.0)
    if task_type == TaskType.NEWS:
        return _env_float("CALLISTO_TIMEOUT_NEWS_S", 180.0)
    if task_type == TaskType.HYPGEN:
        return _env_float("CALLISTO_TIMEOUT_HYPGEN_S", 600.0)
    if task_type == TaskType.DEEP:
        return _env_float("CALLISTO_TIMEOUT_DEEP_S", 900.0)
    # DEFAULT: honor legacy CALLISTO_TASK_TIMEOUT_S for backward-compat first,
    # then CALLISTO_TIMEOUT_DEFAULT_S, then 300s.
    legacy = os.getenv("CALLISTO_TASK_TIMEOUT_S")
    if legacy:
        try:
            return float(legacy)
        except ValueError:
            pass
    return _env_float("CALLISTO_TIMEOUT_DEFAULT_S", 300.0)


def get_hard_ceiling_s() -> float:
    """Absolute maximum after extensions. Used by task_worker for safety."""
    return _env_float("CALLISTO_TIMEOUT_HARD_CEILING_S", 1800.0)


# Keyword rules. Order matters — first matching bucket wins.
_DEEP_PATTERNS = [
    r"\banalyze\b",
    r"\banalysis\b",
    r"\binvestigate\b",
    r"\binvestigation\b",
    r"\bdeep[- ]?dive\b",
    r"\bevaluate\b",
    r"\bevaluation\b",
    r"\bverify\b",
    r"\bverification\b",
    r"\baudit\b",
    r"\bdeep research\b",
    r"\bdeep analysis\b",
    r"\bcross[- ]check\b",
    r"\bback ?test\b",
]

_HYPGEN_PATTERNS = [
    # "generate hypothesis", "generate a hypothesis", "generate a new hypothesis",
    # "generate another hypothesis" — up to two optional modifier words.
    r"\bgenerate\b[^\n]{0,30}\bhypothes(is|es)\b",
    r"\bhypothesis generation\b",
    r"\bfind (new |novel )?edges?\b",
    r"\bfind (new |novel )?signals?\b",
    r"\bpropose (a |new )?strateg(y|ies)\b",
    r"\bbrainstorm\b",
    r"\bnew hypotheses\b",
]

_NEWS_PATTERNS = [
    r"\binjur(y|ies|ed)\b",
    r"\bweather\b",
    r"\blineup(s)?\b",
    r"\bstarting pitcher\b",
    r"\bprobable pitcher\b",
    r"\bstarting goalie\b",
    r"\binactives?\b",
    r"\bscratches?\b",
    r"\bnews\b",
    r"\bdepth chart\b",
    r"\broster move\b",
]

_QUICK_PATTERNS = [
    r"\bcurrent odds\b",
    r"\bcurrent score\b",
    r"\bcurrent line\b",
    r"\bcurrent price\b",
    r"\bwho'?s winning\b",
    r"\blive score\b",
    r"\blive odds\b",
    r"\bprice check\b",
    r"\bquick (check|lookup)\b",
    r"\bwhat'?s the score\b",
    r"\bwhat is the score\b",
]


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_RX_DEEP = _compile(_DEEP_PATTERNS)
_RX_HYPGEN = _compile(_HYPGEN_PATTERNS)
_RX_NEWS = _compile(_NEWS_PATTERNS)
_RX_QUICK = _compile(_QUICK_PATTERNS)


def _any_match(rxs: list[re.Pattern], text: str) -> bool:
    return any(rx.search(text) for rx in rxs)


def classify_query(query: str, scope: str | None = None) -> TaskType:
    """Map a raw query string → TaskType using a layered heuristic.

    Args:
        query: The free-text research query. Case-insensitive match.
        scope: Optional orchestrator scope. If present and non-empty, it's
            searched *in addition to* query — scope text usually restates
            intent in a more uniform vocabulary.

    Returns:
        A TaskType. Never None; fallback is TaskType.DEFAULT.

    Ordering is deep → hypgen → news → quick → default because deeper
    buckets swallow mixed-intent queries safely. A wrong DEEP classification
    spends extra wall-clock but doesn't prematurely kill the session; a
    wrong QUICK classification *will* kill a genuinely-slow session before
    it finishes. So we err toward generous.
    """
    text = query or ""
    if scope:
        text = f"{text}\n{scope}"

    if _any_match(_RX_DEEP, text):
        return TaskType.DEEP
    if _any_match(_RX_HYPGEN, text):
        return TaskType.HYPGEN
    if _any_match(_RX_NEWS, text):
        return TaskType.NEWS
    if _any_match(_RX_QUICK, text):
        return TaskType.QUICK
    return TaskType.DEFAULT


def classify_and_budget(
    query: str,
    explicit_task_type: str | None = None,
    scope: str | None = None,
) -> tuple[TaskType, float]:
    """Classify + resolve the initial wall-clock budget in one call.

    Args:
        query: raw query text.
        explicit_task_type: optional caller-provided task_type string. If it
            matches a TaskType value, that wins over the heuristic — lets
            submitters override the classifier without changing their query.
        scope: optional orchestrator scope for the heuristic.

    Returns:
        (TaskType, budget_seconds)
    """
    if explicit_task_type:
        try:
            tt = TaskType(explicit_task_type.strip().lower())
            return tt, get_budget_s(tt)
        except ValueError:
            pass  # fall through to heuristic
    tt = classify_query(query, scope=scope)
    return tt, get_budget_s(tt)
