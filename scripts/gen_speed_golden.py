"""Generate golden fingerprints for tests/test_speed_parallel_leaves.py.

MUST be run against the SERIAL engine (the pre-restructure code) — that is
the whole point: the goldens freeze what the serial run produced so the
parallel engine can be held to byte-identical outputs. See
findings/speed_2026-08-23.md.

Usage (from a checkout/snapshot of the serial engine):
  python3 scripts/gen_speed_golden.py <output_dir>
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_speed_parallel_leaves import (  # noqa: E402
    SCENARIOS,
    _fingerprint,
    _five_question_brier,
    _run_scenario,
)


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, spec in sorted(SCENARIOS.items()):
        with tempfile.TemporaryDirectory() as td:
            result, ledger = _run_scenario(Path(td), **dict(spec))
        fp = _fingerprint(result, ledger)
        (out / f"{name}.json").write_text(
            json.dumps(fp, indent=2, sort_keys=True))
        print(f"golden {name}: sealed={fp['sealed']} "
              f"conf={fp['confidence_score']}")
    brier, preds = _five_question_brier()
    (out / "five_question_brier.json").write_text(json.dumps(
        {"brier": round(brier, 9), "predictions": preds},
        indent=2, sort_keys=True))
    print(f"golden five_question_brier: {round(brier, 9)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
