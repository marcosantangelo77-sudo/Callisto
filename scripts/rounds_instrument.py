"""JOB 1+2 — measure retrieval-round marginal value on golden runs.

Instrumentation, not behavior change: IterativeRetriever.retrieve gains an
optional ``round_observer`` callback. After each retrieval round it is called
with the CUMULATIVE conclusion-relevant state:

    {
      "round": int,
      "indep_keys": sorted list of independent-source keys,
      "admitted":   [(source_name, sha256), ...] cumulative, ordered,
      "rejected_n": int,
    }

A downstream model call can only return something different if the evidence
set it sees differs. The leaf's sealed number depends exactly on
(best source class of admitted fetches, len(indep_keys), sandbox-ok); the
stance depends on the admitted bodies. So the state above is a complete
determinant of (tier, stance, confidence) up to the model's own response to
identical input — which is by definition unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

# Patch point: retrieve() records round snapshots into trace.rounds already;
# this module derives cumulative state from those snapshots plus trace fields
# WITHOUT re-fetching. See collect_round_states().
