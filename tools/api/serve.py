"""Standalone-serve entry point for api.py's ``__main__`` block.

Slice 6 of the api.py split: the port-free wait loop and the uvicorn
invocation live here; api.py's ``if __name__ == "__main__":`` block
delegates to :func:`wait_for_port_free` + :func:`serve`.

The port-wait exists because crash-loops were the #1 operational failure:
Windows holds TCP sockets in TIME_WAIT for up to 4 minutes after the
process dies. Without this check, uvicorn bind fails silently with exit
code 0xC0000142 and the watchdog loops forever.
"""

from __future__ import annotations

import logging
import socket
import sys

logger = logging.getLogger("callisto.api")

MAX_PORT_ATTEMPTS = 30  # 30 × 2s = 60s max wait
PORT_RETRY_DELAY_S = 2


def wait_for_port_free(host: str, port: int, max_attempts: int = MAX_PORT_ATTEMPTS) -> None:
    """Block until ``port`` can be bound on ``host``, else exit(1).

    Waits for the port to be free — the #1 cause of crash-loops.
    """
    for attempt in range(max_attempts):
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_sock.bind((host, port))
            test_sock.close()
            break  # Port is free
        except OSError:
            if attempt < max_attempts - 1:
                import time as _time
                logger.warning(
                    f"Port {port} in use, waiting... (attempt {attempt+1}/{max_attempts})"
                )
                _time.sleep(PORT_RETRY_DELAY_S)
            else:
                logger.error(
                    f"Port {port} still in use after {max_attempts * PORT_RETRY_DELAY_S}s — exiting"
                )
                sys.exit(1)


def serve(host: str, port: int) -> None:
    """Wait for a free port, then run uvicorn on api:app."""
    wait_for_port_free(host, port)
    import uvicorn

    uvicorn.run("api:app", host=host, port=port, reload=False)
