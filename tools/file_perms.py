"""Best-effort file-permission hardening for sensitive files.

On POSIX (macOS, Linux) this `chmod`s each path to 0o600 (owner-only
read/write). On Windows, POSIX permission bits are advisory at best —
the meaningful control is NTFS ACLs. Rather than shelling out to icacls
and deepening the Windows dependency surface, we leave Windows files
at their creation ACLs (which default to the owning user being the only
non-admin with write access in Marco's single-user setup).

Every failure is swallowed: hardening is always opportunistic. A live
API should not fail to start because a file perm tweak raised.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("callisto.file_perms")

_OWNER_ONLY = stat.S_IRUSR | stat.S_IWUSR  # 0o600


def harden_paths(paths: Iterable[str]) -> dict[str, str]:
    """Apply owner-only perms to each path that exists. Returns a
    {path: status} summary useful for diagnostics but safe to discard.
    """
    results: dict[str, str] = {}
    for raw in paths:
        try:
            p = Path(raw)
            if not p.exists():
                results[raw] = "absent"
                continue
            if sys.platform.startswith("win"):
                # NTFS ACLs govern on Windows; os.chmod() only toggles
                # the read-only attribute and is not a meaningful hardening.
                results[raw] = "skipped_windows"
                continue
            os.chmod(p, _OWNER_ONLY)
            results[raw] = "chmod_600"
        except Exception as e:
            results[raw] = f"error:{type(e).__name__}"
            logger.debug("harden_paths failed for %s: %r", raw, e)
    return results


__all__ = ["harden_paths"]
