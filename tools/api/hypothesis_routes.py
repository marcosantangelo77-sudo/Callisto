"""Hypothesis route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singletons (``hypothesis_manager``,
``logger``) via a late ``from api import ...`` inside the function body to
avoid a circular import at module load time.
"""

from __future__ import annotations

import json as _json

from fastapi import HTTPException, Request


async def create_hypothesis(req):
    """Create a new testable betting hypothesis."""
    from api import hypothesis_manager
    hid = await hypothesis_manager.create_hypothesis(
        name=req.name,
        thesis=req.thesis,
        sport=req.sport,
        market_type=req.market_type,
        model_config=req.hypothesis_model_config,
        edge_threshold=req.edge_threshold,
        min_sample_size=req.min_sample_size,
        significance_level=req.significance_level,
        notes=req.notes,
    )
    return {"hypothesis_id": hid}


async def list_hypotheses(status=None):
    """List all hypotheses, optionally filtered by status."""
    from api import hypothesis_manager
    hypotheses = await hypothesis_manager.list_hypotheses(status=status)
    return {"count": len(hypotheses), "hypotheses": hypotheses}


async def get_hypothesis(hypothesis_id: str):
    """Get hypothesis details."""
    from api import hypothesis_manager
    h = await hypothesis_manager.get_hypothesis(hypothesis_id)
    if not h:
        raise HTTPException(status_code=404, detail="Hypothesis not found")
    return h


async def hypothesis_report(hypothesis_id: str):
    """Full statistical report across all stages."""
    from api import hypothesis_manager
    return await hypothesis_manager.get_hypothesis_report(hypothesis_id)


async def hypothesis_significance(hypothesis_id: str, stage: str = "backtest"):
    """Run significance tests on a hypothesis at a given stage."""
    from api import hypothesis_manager
    return await hypothesis_manager.evaluate_significance(hypothesis_id, stage)


async def promote_hypothesis(hypothesis_id: str):
    """Check readiness and promote to next stage if criteria are met."""
    from api import hypothesis_manager
    readiness = await hypothesis_manager.check_promotion_readiness(hypothesis_id)
    if readiness.get("ready"):
        result = await hypothesis_manager.auto_promote(hypothesis_id)
        return {"promoted": True, **result}
    return {"promoted": False, **readiness}


# SECURITY (audit C-4 / P2 #25): allowlist top-level fields for PATCH.
# Refuses unknown keys to prevent silent passthrough that downstream code
# may interpret unsafely.
_PATCH_ALLOWED_KEYS = {
    "status", "promoted_by", "force", "edge_threshold", "model_config", "notes",
}

_STAGE_ORDER = ["draft", "backtesting", "paper_trading", "live", "retired"]


def _validate_patch_body(req: dict) -> dict:
    """Validate + normalize a PATCH /hypothesis body in place. Raises 422s."""
    unknown = set(req.keys()) - _PATCH_ALLOWED_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown fields: {sorted(unknown)}")
    if "model_config" in req:
        mc = req["model_config"]
        if not isinstance(mc, dict):
            raise HTTPException(status_code=422, detail="model_config must be an object")
        from tools.hypothesis import validate_model_config
        try:
            req["model_config"] = validate_model_config(mc)
        except ValueError as ve:
            raise HTTPException(status_code=422, detail=f"model_config: {ve}")
    if "notes" in req:
        if not isinstance(req["notes"], str) or len(req["notes"]) > 5000:
            raise HTTPException(status_code=422, detail="notes must be string ≤5000 chars")
    if "edge_threshold" in req:
        try:
            et = float(req["edge_threshold"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="edge_threshold must be numeric")
        if not (0.0 <= et <= 1.0):
            raise HTTPException(status_code=422, detail="edge_threshold out of [0,1]")
        req["edge_threshold"] = et
    return req


async def update_hypothesis(hypothesis_id: str, request: Request):
    """Update hypothesis status, threshold, model_config, or notes.

    Uses a fresh DB connection per request to avoid stale-handle failures
    on the long-lived hypothesis_manager._db connection.
    """
    import logging
    from tools.schema import open_db

    logger = logging.getLogger("callisto.api")

    req = await request.json()
    if not isinstance(req, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")
    _validate_patch_body(req)

    from api import hypothesis_manager
    h = await hypothesis_manager.get_hypothesis(hypothesis_id)
    if not h:
        raise HTTPException(status_code=404, detail="Hypothesis not found")
    results = {}
    db = None
    try:
        db = await open_db()
        if "status" in req:
            new_status = req["status"]
            promoted_by = req.get("promoted_by", "api")
            force = req.get("force", False)
            old_status = h.get("status", "draft")

            # Enforce promotion gates for forward transitions unless force=True
            old_idx = _STAGE_ORDER.index(old_status) if old_status in _STAGE_ORDER else -1
            new_idx = _STAGE_ORDER.index(new_status) if new_status in _STAGE_ORDER else -1
            is_forward = new_idx > old_idx and new_status not in ("retired", "rejected")

            if is_forward and not force and old_status in ("backtesting", "paper_trading"):
                readiness = await hypothesis_manager.check_promotion_readiness(hypothesis_id)
                if not readiness.get("ready"):
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": f"Promotion gate failed: {old_status} → {new_status}",
                            "checks": readiness.get("checks", []),
                            "hint": "Pass force=true to override",
                        },
                    )

            import datetime as _dt
            now = _dt.datetime.now(_dt.timezone.utc).isoformat()
            await db.execute(
                "UPDATE hypotheses SET status = ?, updated_at = ?, "
                "promoted_at = ?, promoted_by = ? WHERE hypothesis_id = ?",
                (new_status, now, now, promoted_by, hypothesis_id),
            )
            results["status"] = new_status
            logger.info(f"Hypothesis {hypothesis_id} → {new_status} (by {promoted_by})")
        if "edge_threshold" in req:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE hypothesis_id = ?",
                (req["edge_threshold"], hypothesis_id),
            )
            results["edge_threshold"] = req["edge_threshold"]
        if "model_config" in req:
            raw = h.get("model_config", "{}")
            existing = _json.loads(raw) if isinstance(raw, str) else (raw or {})
            existing.update(req["model_config"])
            await db.execute(
                "UPDATE hypotheses SET model_config = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE hypothesis_id = ?",
                (_json.dumps(existing), hypothesis_id),
            )
            results["model_config"] = existing
        if "notes" in req:
            await db.execute(
                "UPDATE hypotheses SET notes = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE hypothesis_id = ?",
                (req["notes"], hypothesis_id),
            )
            results["notes"] = req["notes"]
        await db.commit()
    except Exception as e:
        logger.error(f"PATCH /hypothesis/{hypothesis_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if db:
            await db.close()
    return {"hypothesis_id": hypothesis_id, "updated": results}
