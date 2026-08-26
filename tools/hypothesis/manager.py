"""
tools.hypothesis.manager — HypothesisManager assembled from mixins.

Split out of tools/hypothesis.py (facade re-exports everything).
"""
from __future__ import annotations

from typing import Optional

import aiosqlite

from tools.hypothesis.config import DB_PATH
from tools.hypothesis.store import HypothesisStoreMixin
from tools.hypothesis.significance import HypothesisSignificanceMixin
from tools.hypothesis.promote import HypothesisPromotionMixin


class HypothesisManager(
    HypothesisStoreMixin,
    HypothesisSignificanceMixin,
    HypothesisPromotionMixin,
):
    """Manages hypothesis lifecycle: draft → backtest → paper_trade → live → retired."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
