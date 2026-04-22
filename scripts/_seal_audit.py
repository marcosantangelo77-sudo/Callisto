"""Read-only audit: how many existing sealed sessions in the live DB would
verify_seal() accept vs reject?

Usage:
    python scripts/_seal_audit.py [/path/to/callisto.db]
"""

import json
import sqlite3
import sys
import tempfile
import shutil
from pathlib import Path

# Make `agp` importable when run from anywhere in the worktree
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agp import AGPSession  # noqa: E402


def audit(db_path: str) -> dict:
    """Open the live DB read-only (via a copy so we never touch WAL)
    and tally verify_seal results across every row in the sessions table."""
    # Copy the live DB to a temp path so live WAL is untouched; we only read
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "audit_copy.db"
        shutil.copy2(db_path, copy_path)
        # Also copy wal/shm if present — else sqlite may complain
        for suffix in ("-wal", "-shm"):
            src = Path(db_path + suffix)
            if src.exists():
                shutil.copy2(src, Path(tmp) / f"audit_copy.db{suffix}")

        conn = sqlite3.connect(str(copy_path))
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id, seal_hash, full_session FROM sessions"
        )
        total = 0
        accepted = 0
        rejected_missing_hash = 0
        rejected_tampered = 0
        rejected_corrupt_json = 0
        examples_bad = []
        for session_id, seal_hash, full_session in cur:
            total += 1
            if not seal_hash:
                rejected_missing_hash += 1
                continue
            try:
                data = json.loads(full_session)
            except json.JSONDecodeError:
                rejected_corrupt_json += 1
                continue
            if AGPSession.verify_seal(data):
                accepted += 1
            else:
                rejected_tampered += 1
                if len(examples_bad) < 5:
                    examples_bad.append(session_id)
        conn.close()
    return {
        "total": total,
        "accepted": accepted,
        "rejected_missing_hash": rejected_missing_hash,
        "rejected_tampered": rejected_tampered,
        "rejected_corrupt_json": rejected_corrupt_json,
        "examples_bad": examples_bad,
    }


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "../Callisto/memory/callisto.db"
    if not Path(db).exists():
        print(f"DB not found: {db}")
        sys.exit(1)
    print(f"Auditing: {db}\n")
    result = audit(db)
    for k, v in result.items():
        print(f"  {k}: {v}")
