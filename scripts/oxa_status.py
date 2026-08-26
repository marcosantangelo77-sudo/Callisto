#!/usr/bin/env python3
"""Print Ox Alpha / Hermes / Nous Portal status. Never prints secrets.

    python3 scripts/oxa_status.py

Exit 0 if Hermes is installed AND a Nous session exists.
Exit 2 if the binary is missing.
Exit 3 if installed but not logged into Nous Portal.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.pipeline.hermes_cli import (  # noqa: E402
    hermes_available,
    hermes_logged_in,
    resolve_binary,
    _auth_store_path,
)


def _store_shape() -> str:
    """Describe auth.json shape without token material."""
    path = _auth_store_path()
    if not path.exists():
        return f"auth_store=missing path={path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"auth_store=unreadable path={path}"
    providers = data.get("providers") if isinstance(data, dict) else None
    pool = data.get("credential_pool") if isinstance(data, dict) else None
    nous = providers.get("nous") if isinstance(providers, dict) else None
    pool_nous = pool.get("nous") if isinstance(pool, dict) else None
    nous_keys = sorted(nous.keys()) if isinstance(nous, dict) else []
    # Strip anything that looks like a secret field name from the key list
    # we print — still useful to see whether a session object exists.
    public_keys = [k for k in nous_keys if k not in
                   {"access_token", "refresh_token", "id_token", "token"}]
    n_pool = len(pool_nous) if isinstance(pool_nous, list) else 0
    relogin = False
    if isinstance(nous, dict):
        err = nous.get("last_auth_error")
        relogin = bool(isinstance(err, dict) and err.get("relogin_required"))
    return (
        f"auth_store=present path={path} "
        f"nous_public_keys={public_keys} "
        f"nous_pool_entries={n_pool} "
        f"relogin_required={relogin}"
    )


def main() -> int:
    binary = resolve_binary()
    which = shutil.which("hermes") or ""
    print(f"hermes_binary={binary}")
    print(f"hermes_which={which or 'none'}")
    print(f"hermes_available={hermes_available()}")
    print(f"hermes_home={os.getenv('HERMES_HOME', '') or str(Path.home() / '.hermes')}")
    print(_store_shape())
    logged = hermes_logged_in()
    print(f"nous_logged_in={logged}")
    if not hermes_available():
        print("ACTION: install Hermes (curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash)")
        return 2
    if not logged:
        print("ACTION: hermes auth add nous --type oauth --no-browser")
        print("        then: hermes config set model.provider nous")
        print("        then: hermes -z PONG --provider nous -m stealth/ox-alpha --in /tmp")
        return 3
    print("ACTION: none — Portal session present. Smoke with:")
    print("        hermes -z PONG --provider nous -m stealth/ox-alpha --in /tmp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
