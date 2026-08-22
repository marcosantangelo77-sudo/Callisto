"""Structural no-network guard for the B6 finance tests.

If any finance test opens a socket, this file makes the run FAIL with the
offending call in the traceback — so live-API usage can never quietly
re-enter the suite (the original build verified against the live SEC API,
which is neither reproducible nor rate-limit-safe).
"""
from __future__ import annotations

import socket

import pytest

# Block every outbound-socket entry point urllib/requests/httpx can reach.
_BLOCKED = [
    ("socket.socket", socket.socket),
    ("socket.create_connection", socket.create_connection),
    ("socket.getaddrinfo", socket.getaddrinfo),
    ("socket.gethostbyname", socket.gethostbyname),
]


@pytest.fixture(autouse=True, scope="module")
def _no_sockets():
    originals = {name: fn for name, fn in _BLOCKED}

    def _deny(name):
        real = originals.get(name)

        def impl(*args, **kwargs):
            # AF_UNIX is local IPC, not network egress. asyncio's event loop
            # creates a unix self-pipe internally, so blocking every family
            # breaks async tests instead of catching a real network call.
            if name == "socket.socket" and args:
                import socket as _s
                if args[0] not in (_s.AF_INET, _s.AF_INET6):
                    return real(*args, **kwargs)
            raise AssertionError(
                f"finance tests must not touch the network: "
                f"{name} was called — use tests/fixtures/edgar/ instead"
            )
        return impl

    for name, _fn in _BLOCKED:
        mod_path, attr = name.rsplit(".", 1)
        import importlib

        setattr(importlib.import_module(mod_path), attr, _deny(name))
    yield
    # restore regardless of test outcome
    for name, fn in originals.items():
        mod_path, attr = name.rsplit(".", 1)
        import importlib

        setattr(importlib.import_module(mod_path), attr, fn)


def test_guard_itself_blocks():
    """Sanity: the guard fires if a socket is requested."""
    with pytest.raises(AssertionError, match="must not touch the network"):
        socket.create_connection(("example.com", 80), timeout=0.1)
