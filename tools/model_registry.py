"""S3 — Model registry: long-lived quantitative models as falsifiable entities.

A "model" here is any registered predictor — a BTC stock-to-flow fit, a
game-pace model, a protein-structure scorer. The registry makes a model's
PREDICTIONS first-class, preregistered objects:

- register(name, ...) → model_id, with the producing code/artifacts sealed
  by hash (the model is reproducible or it doesn't count).
- predict(model_id, claim, target_value?, horizon, made_at) appends an
  immutable prediction record (append-only log; edits are rejected).
- resolve(model_id, prediction_id, realized) closes it against ground truth
  from ANY domain — sports stays green because resolution semantics are the
  caller's, not the registry's.
- track_record(model_id) → hit rate, Brier score, calibration-by-bin.
  This is the point: **a model's accuracy is measured the same way a
  hypothesis's is**, closing the last self-report loop. A model that has
  never been resolved cannot claim a track record; track_record returns
  resolved=False and callers must treat its confidence as unearned.

Storage: JSON log under data/models/, one file per model. No DB schema
changes — nothing here touches the betting tables.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tools.artifacts import ArtifactRef, sha256_bytes

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_VOID = "void"  # target never realised / cancelled; excluded from scoring


def _default_root() -> Path:
    override = os.environ.get("CALLISTO_MODEL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "data" / "models"


@dataclass
class Prediction:
    model_id: str
    prediction_id: str
    claim: str                      # domain-general: what is predicted
    horizon: str                    # e.g. "1y", "5y", "next_game", "10y"
    probability: Optional[float] = None   # for probabilistic claims (0..1)
    target_value: Optional[float] = None  # for point claims
    tolerance: Optional[float] = None     # |realized - target| <= tolerance ⇒ hit
    made_at: str = ""
    artifact_refs: list[str] = field(default_factory=list)  # supporting artifacts
    status: str = STATUS_OPEN
    realized: Any = None
    resolved_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class RegisteredModel:
    model_id: str
    name: str
    description: str = ""
    code_sha256: str = ""          # sandbox code that defines/fit the model
    artifact_refs: list[str] = field(default_factory=list)
    registered_at: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class ModelRegistry:
    """Append-only per-model logs under one root directory."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else _default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- paths -------------------------------------------------------------

    def _model_path(self, model_id: str) -> Path:
        safe = "".join(c for c in model_id if c.isalnum() or c in "_-")
        if not safe or safe != model_id:
            raise ValueError(f"invalid model_id: {model_id!r}")
        return self.root / f"{safe}.json"

    # -- registration ------------------------------------------------------

    def register(
        self,
        name: str,
        description: str = "",
        code_sha256: str = "",
        artifact_refs: Optional[list[str]] = None,
        meta: Optional[dict] = None,
    ) -> RegisteredModel:
        model_id = _slug(name)
        with self._lock:
            path = self._model_path(model_id)
            if path.exists():
                raise ValueError(f"model {model_id!r} already registered")
            m = RegisteredModel(
                model_id=model_id,
                name=name,
                description=description,
                code_sha256=code_sha256,
                artifact_refs=list(artifact_refs or []),
                registered_at=_now(),
                meta=dict(meta or {}),
            )
            path.write_text(json.dumps({"model": m.to_dict(), "predictions": []},
                                       indent=1), encoding="utf-8")
        return m

    def get(self, model_id: str) -> tuple[RegisteredModel, list[Prediction]]:
        path = self._model_path(model_id)
        if not path.exists():
            raise KeyError(f"unknown model {model_id!r}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        m = RegisteredModel(**raw["model"])
        preds = [Prediction(**p) for p in raw["predictions"]]
        return m, preds

    def list_models(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))

    # -- predictions -------------------------------------------------------

    def add_prediction(
        self,
        model_id: str,
        claim: str,
        horizon: str,
        *,
        probability: Optional[float] = None,
        target_value: Optional[float] = None,
        tolerance: Optional[float] = None,
        artifact_refs: Optional[list[str]] = None,
        notes: str = "",
    ) -> Prediction:
        if probability is None and target_value is None:
            raise ValueError("prediction needs probability or target_value")
        if probability is not None and not (0.0 <= probability <= 1.0):
            raise ValueError("probability must be within [0,1]")
        pred_id = sha256_bytes(
            json.dumps([claim, horizon, probability, target_value,
                        _now()], sort_keys=True).encode()
        )[:16]
        pred = Prediction(
            model_id=model_id,
            prediction_id=pred_id,
            claim=claim,
            horizon=horizon,
            probability=probability,
            target_value=target_value,
            tolerance=tolerance,
            made_at=_now(),
            artifact_refs=list(artifact_refs or []),
            notes=notes,
        )
        with self._lock:
            path = self._model_path(model_id)
            raw = json.loads(path.read_text(encoding="utf-8"))
            if any(p["prediction_id"] == pred_id for p in raw["predictions"]):
                raise ValueError("duplicate prediction id")
            raw["predictions"].append(pred.to_dict())
            _atomic_write(path, raw)
        return pred

    def resolve_prediction(
        self,
        model_id: str,
        prediction_id: str,
        realized: Any,
        void: bool = False,
        notes: str = "",
    ) -> Prediction:
        """Close a prediction against ground truth. Immutable-once: an
        already-resolved record can only be corrected by an explicit
        supersede note, never silently."""
        with self._lock:
            path = self._model_path(model_id)
            raw = json.loads(path.read_text(encoding="utf-8"))
            for p in raw["predictions"]:
                if p["prediction_id"] == prediction_id:
                    if p["status"] == STATUS_RESOLVED and not notes:
                        raise ValueError(
                            f"prediction {prediction_id} already resolved; "
                            "pass notes to supersede explicitly"
                        )
                    p["status"] = STATUS_VOID if void else STATUS_RESOLVED
                    p["realized"] = realized
                    p["resolved_at"] = _now()
                    if notes:
                        p["notes"] = (p.get("notes", "") +
                                      f" [superseded {_now()}: {notes}]").strip()
                    _atomic_write(path, raw)
                    return Prediction(**p)
        raise KeyError(f"prediction {prediction_id} not found in {model_id}")

    # -- scoring -----------------------------------------------------------

    def track_record(self, model_id: str) -> dict:
        """Measured performance over RESOLVED predictions only.

        Probabilistic claims → Brier score + reliability bins.
        Point claims → hit rate within tolerance.
        Zero resolved predictions → {"resolved": False}; confidence on this
        model is unearned until at least one outcome lands.
        """
        _, preds = self.get(model_id)
        scored = [p for p in preds if p.status == STATUS_RESOLVED]
        prob = [p for p in scored if p.probability is not None]
        points = [p for p in scored if p.target_value is not None]
        out: dict[str, Any] = {
            "model_id": model_id,
            "total_predictions": len(preds),
            "resolved": bool(scored),
            "n_resolved": len(scored),
            "n_void": sum(1 for p in preds if p.status == STATUS_VOID),
        }
        if prob:
            brier = mean((p.probability - _binary(p)) ** 2 for p in prob)
            out["probabilistic"] = {
                "n": len(prob),
                "brier": round(brier, 6),
                "hit_rate": round(mean(_binary(p) for p in prob), 4),
                "reliability_bins": _reliability(prob),
            }
        if points:
            hits = [_within(p) for p in points]
            known = [h for h in hits if h is not None]
            out["point"] = {
                "n": len(points),
                "hit_rate": round(mean(known), 4) if known else None,
                "unscorable_missing_tolerance": len(hits) - len(known),
            }
        return out


# -- helpers ----------------------------------------------------------------


def _slug(name: str) -> str:
    s = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name.lower())
    s = "_".join(part for part in s.split("_") if part)
    if not s:
        raise ValueError("name must contain alphanumeric characters")
    return s[:64]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _binary(p: Prediction) -> int:
    r = p.realized
    if isinstance(r, bool):
        return int(r)
    if isinstance(r, (int, float)) and p.tolerance is not None:
        return int(abs(float(r) - float(p.target_value)) <= p.tolerance) if p.target_value is not None else int(bool(r))
    return int(bool(r))


def _within(p: Prediction):
    if p.tolerance is None or p.realized is None:
        return None
    try:
        return int(abs(float(p.realized) - float(p.target_value)) <= float(p.tolerance))
    except (TypeError, ValueError):
        return None


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs)


def _reliability(preds: list[Prediction], bins: int = 5) -> list[dict]:
    """Predicted-probability vs observed-frequency, the calibration curve."""
    edges = [i / bins for i in range(bins + 1)]
    out = []
    for lo, hi in zip(edges, edges[1:]):
        bucket = [p for p in preds
                  if (lo <= p.probability < hi)
                  or (hi == 1.0 and p.probability == 1.0)]
        if bucket:
            out.append({
                "bin": f"[{lo:.1f},{hi:.1f}]",
                "n": len(bucket),
                "mean_predicted": round(mean(p.probability for p in bucket), 3),
                "observed_frequency": round(mean(_binary(p) for p in bucket), 3),
            })
    return out


_default_registry: Optional[ModelRegistry] = None


def default_registry() -> ModelRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = ModelRegistry()
    return _default_registry
