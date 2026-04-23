"""ML-backed promotion gate for the paper_trading → live transition.

Wiring between ``tools.ml_backtest`` (orphan), ``tools.ml_drift``
(sidecar-only), and ``tools.hypothesis.check_promotion_readiness`` (the gate
that matters).

Decision tree (paper_trading → live):

    1. Look up the most recent joblib under ``models/`` whose filename prefix
       matches the hypothesis's ``(sport, ml_market)``. If none exist the gate
       returns ``{"applicable": False}`` — promotion falls through to the
       hand-crafted backtest_events gate unchanged.
    2. Load the model's ``.drift.json`` sidecar. If ``is_stale == True`` the
       gate also returns ``{"applicable": False, "stale_model": path}`` so
       the caller can fall back to the hand-crafted path AND surface the
       stale model to /health.
    3. Otherwise run ``ml_backtest(model_path, threshold=0.55)`` and evaluate
       the thresholds:
         * hit_rate >= MIN_HIT_RATE
         * n_signals < MIN_SIGNALS_FOR_CLV  OR  clv_implied_mean >= MIN_CLV
         * sharpe >= MIN_SHARPE  (only enforced when sharpe is not None)
       Fail any of these → block promotion with a ``gate_decision="FAIL"``
       and a structured reason. Passing all → ``gate_decision="PASS"``.
    4. Persist the report to ``ml_backtest_reports`` (migration 013) whether
       the gate passed or failed — so the promotion audit trail carries the
       ML verdict alongside the hand-crafted one.

Threshold constants are module-level so Marco can tune them in one place.
No env-var knobs — per the feature brief.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

try:
    from tools.ml_backtest import MLBacktestReport, ml_backtest
    from tools.ml_classifier import load_model
except Exception:  # pragma: no cover
    MLBacktestReport = None  # type: ignore
    ml_backtest = None  # type: ignore
    load_model = None  # type: ignore

logger = logging.getLogger("callisto.ml_promotion_gate")


# ── Thresholds (fail promotion if any trip) ───────────────────────────────
MIN_HIT_RATE: float = 0.52
MIN_CLV_BPS: float = 0.0          # CLV expressed as implied-prob delta (bps-equiv)
MIN_SIGNALS_FOR_CLV: int = 100
MIN_SHARPE: float = 0.0
ML_BACKTEST_THRESHOLD: float = 0.55
MODELS_DIR_ENV: str = "CALLISTO_MODELS_DIR"
DEFAULT_MODELS_DIR: str = "models"


# ── Market mapping ────────────────────────────────────────────────────────
# Hypotheses carry ``market_type`` ∈ {h2h, spreads, totals, player_props,
# synthetic:*}. The ML classifier uses ``market`` ∈ {totals,
# player_prop_{stat_type}}. We only have ML coverage for two of those.
def _hypothesis_to_ml_market(
    market_type: str,
    model_config: dict[str, Any] | None,
) -> Optional[str]:
    if not market_type:
        return None
    if market_type == "totals":
        return "totals"
    if market_type == "player_props":
        stat = (model_config or {}).get("stat_type")
        if not stat:
            return None
        return f"player_prop_{stat}"
    return None


def _models_dir() -> Path:
    return Path(os.getenv(MODELS_DIR_ENV, DEFAULT_MODELS_DIR))


def _find_latest_model(sport: str, ml_market: str) -> Optional[Path]:
    """Return the most recently trained joblib for ``(sport, ml_market)``.

    Filename shape from ``ml_classifier._persist``:
      ``{sport}_{market}_{trained_at}.joblib``
    We sort by file mtime (simplest, monotonic), not by the trained_at
    tag in the filename — either works; mtime is cheaper.
    """
    d = _models_dir()
    if not d.is_dir():
        return None
    prefix = f"{sport}_{ml_market}_"
    cands = sorted(
        (p for p in d.iterdir() if p.is_file() and p.suffix == ".joblib" and p.name.startswith(prefix)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return cands[0] if cands else None


def _load_drift_sidecar(model_path: Path) -> Optional[dict[str, Any]]:
    sidecar = model_path.with_suffix(".drift.json")
    if not sidecar.is_file():
        return None
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("drift sidecar read failed for %s: %s", sidecar, exc)
        return None


def _is_stale(drift: Optional[dict[str, Any]]) -> bool:
    if not drift:
        return False
    return bool(drift.get("is_stale", False))


# ── Public API ────────────────────────────────────────────────────────────

def list_stale_models() -> list[dict[str, Any]]:
    """Scan ``models/`` for every ``*.drift.json`` with ``is_stale=true``.

    Used by the /health endpoint. Cheap filesystem walk; no DB reads.
    Returns dicts with minimum fields useful to an operator.
    """
    out: list[dict[str, Any]] = []
    d = _models_dir()
    if not d.is_dir():
        return out
    for p in d.glob("*.drift.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            continue
        if not payload.get("is_stale"):
            continue
        out.append(
            {
                "model": payload.get("model_path") or p.with_suffix("").with_suffix(".joblib").name,
                "n_recent": payload.get("n_recent"),
                "shift_fraction": payload.get("shift_fraction"),
                "recent_date_range": payload.get("recent_date_range"),
                "evaluated_at": payload.get("evaluated_at"),
                "drift_sidecar": str(p),
            }
        )
    return out


def evaluate_ml_gate(
    *,
    hypothesis_id: str,
    sport: Optional[str],
    market_type: Optional[str],
    model_config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run the ML backtest gate for a hypothesis.

    Returns a dict with keys:
        ``applicable`` (bool)      — whether a model existed AND was non-stale
        ``ready`` (bool)           — True iff applicable and all thresholds pass
        ``reasons`` (list[str])    — PASS/FAIL lines for each threshold
        ``model_path`` (str|None)
        ``report`` (dict|None)     — ``asdict(MLBacktestReport)`` when available
        ``stale_model`` (str|None) — set when drift sidecar flagged is_stale
        ``error`` (str|None)

    Never raises. A failure inside ``ml_backtest`` is surfaced via ``error``
    and ``applicable=False`` so the caller falls through to the hand-crafted
    gate — never block a promotion on a broken ML path.
    """
    result: dict[str, Any] = {
        "applicable": False,
        "ready": False,
        "reasons": [],
        "model_path": None,
        "report": None,
        "stale_model": None,
        "error": None,
    }

    if ml_backtest is None or load_model is None:
        result["error"] = "ml_backtest module unavailable"
        return result

    if not sport or not market_type:
        result["reasons"].append("no sport/market_type on hypothesis")
        return result

    ml_market = _hypothesis_to_ml_market(market_type, model_config or {})
    if not ml_market:
        result["reasons"].append(
            f"market_type={market_type!r} has no ML coverage"
        )
        return result

    model_path = _find_latest_model(sport, ml_market)
    if model_path is None:
        result["reasons"].append(
            f"no trained model under {_models_dir()}/ for sport={sport} market={ml_market}"
        )
        return result

    drift = _load_drift_sidecar(model_path)
    if _is_stale(drift):
        logger.warning(
            "ML gate SKIPPED for %s: model %s flagged stale by drift sidecar "
            "(shift_fraction=%s, n_recent=%s) — falling back to hand-crafted gate",
            hypothesis_id,
            model_path.name,
            drift.get("shift_fraction") if drift else None,
            drift.get("n_recent") if drift else None,
        )
        result["stale_model"] = str(model_path)
        result["reasons"].append(
            f"model {model_path.name} is drift-stale "
            f"(shift_fraction={drift.get('shift_fraction') if drift else '?'})"
        )
        return result

    try:
        report: MLBacktestReport = ml_backtest(  # type: ignore[assignment]
            str(model_path),
            threshold=ML_BACKTEST_THRESHOLD,
        )
    except Exception as exc:
        logger.warning(
            "ml_backtest failed for hypothesis %s against model %s: %s",
            hypothesis_id, model_path, exc,
        )
        result["error"] = f"ml_backtest raised: {exc!r}"
        return result

    result["applicable"] = True
    result["model_path"] = str(model_path)
    result["report"] = asdict(report)

    reasons: list[str] = []
    ready = True

    if report.hit_rate is None:
        reasons.append("FAIL: hit_rate unavailable (no resolved signals)")
        ready = False
    elif report.hit_rate < MIN_HIT_RATE:
        reasons.append(
            f"FAIL: ML hit_rate {report.hit_rate:.3f} < {MIN_HIT_RATE:.3f}"
        )
        ready = False
    else:
        reasons.append(
            f"PASS: ML hit_rate {report.hit_rate:.3f} >= {MIN_HIT_RATE:.3f}"
        )

    clv = report.clv_implied_mean
    n_sig = int(report.n_signals or 0)
    if n_sig >= MIN_SIGNALS_FOR_CLV:
        if clv is None:
            reasons.append(
                f"FAIL: ML CLV unavailable at n_signals={n_sig} "
                f">= {MIN_SIGNALS_FOR_CLV}"
            )
            ready = False
        elif clv < MIN_CLV_BPS:
            reasons.append(
                f"FAIL: ML CLV {clv:.5f} < {MIN_CLV_BPS} "
                f"over {n_sig} signals"
            )
            ready = False
        else:
            reasons.append(
                f"PASS: ML CLV {clv:.5f} >= {MIN_CLV_BPS} "
                f"over {n_sig} signals"
            )
    else:
        reasons.append(
            f"SKIP: ML CLV not evaluated at n_signals={n_sig} "
            f"< {MIN_SIGNALS_FOR_CLV} (threshold waived)"
        )

    if report.sharpe is None:
        reasons.append("SKIP: ML Sharpe unavailable (fewer than 2 trading days)")
    elif report.sharpe < MIN_SHARPE:
        reasons.append(
            f"FAIL: ML Sharpe {report.sharpe:.3f} < {MIN_SHARPE:.3f}"
        )
        ready = False
    else:
        reasons.append(
            f"PASS: ML Sharpe {report.sharpe:.3f} >= {MIN_SHARPE:.3f}"
        )

    result["ready"] = ready
    result["reasons"] = reasons
    return result


async def record_ml_backtest_report(
    db,
    *,
    hypothesis_id: str,
    gate_result: dict[str, Any],
) -> None:
    """Persist a gate_result into ``ml_backtest_reports`` (migration 013).

    ``db`` is an aiosqlite connection from the hypothesis manager. Fire-and-
    forget: any failure is logged and swallowed — we must never block a
    promotion decision on the audit-log write.
    """
    if not gate_result.get("applicable") and not gate_result.get("stale_model"):
        return
    report = gate_result.get("report") or {}
    stale = bool(gate_result.get("stale_model"))
    decision = (
        "STALE_FALLBACK" if stale
        else ("PASS" if gate_result.get("ready") else "FAIL")
    )
    try:
        await db.execute(
            "INSERT INTO ml_backtest_reports "
            "(hypothesis_id, model_path, sport, market, threshold, "
            " n_signals, n_resolved, hits, pushes, misses, "
            " hit_rate, roi_pct, clv_implied_mean, sharpe, "
            " is_stale_model, gate_decision, gate_reasons, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                hypothesis_id,
                gate_result.get("model_path") or gate_result.get("stale_model"),
                report.get("sport"),
                report.get("market"),
                report.get("threshold"),
                report.get("n_signals"),
                report.get("n_resolved"),
                report.get("hits"),
                report.get("pushes"),
                report.get("misses"),
                report.get("hit_rate"),
                report.get("roi_pct"),
                report.get("clv_implied_mean"),
                report.get("sharpe"),
                1 if stale else 0,
                decision,
                json.dumps(gate_result.get("reasons") or []),
                json.dumps(report, default=str) if report else None,
            ),
        )
        try:
            await db.commit()
        except Exception:
            pass
    except Exception as exc:
        logger.warning(
            "record_ml_backtest_report failed for %s: %s", hypothesis_id, exc
        )


__all__ = [
    "MIN_HIT_RATE",
    "MIN_CLV_BPS",
    "MIN_SIGNALS_FOR_CLV",
    "MIN_SHARPE",
    "ML_BACKTEST_THRESHOLD",
    "evaluate_ml_gate",
    "list_stale_models",
    "record_ml_backtest_report",
]
