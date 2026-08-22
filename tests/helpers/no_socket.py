"""Test barrier: fail loudly if any test opens a real network socket.

Installed at import time by test_build_r4_sources.py BEFORE any source
module is imported. socket.socket is patched to raise on AF_INET /
AF_INET6 creation; Unix sockets are left alone so pytest internals and
any local DB fixtures keep working.
"""

from __future__ import annotations

import socket


class NoSocket:
    def __init__(self) -> None:
        self._orig_socket = None
        self.violations: list[tuple[str, int]] = []

    def install(self) -> None:
        if self._orig_socket is not None:
            return  # already installed

        outer = self
        orig = socket.socket
        self._orig_socket = orig

        def guarded_socket(family=socket.AF_INET, type_=socket.SOCK_STREAM,
                           *args, **kwargs):
            if family in (socket.AF_INET, socket.AF_INET6):
                outer.violations.append((str(family), int(type_)))
                raise AssertionError(
                    "TEST TRIED TO OPEN A NETWORK SOCKET — source-registry "
                    "tests must run against fixtures only. If you just added "
                    "a live-fetching test, stage a FakeTransport route "
                    "instead. This guard exists because a previous instance "
                    "got this machine rate-limited (HTTP 403) by the SEC.")
            return orig(family, type_, *args, **kwargs)

        socket.socket = guarded_socket

    def uninstall(self) -> None:
        if self._orig_socket is not None:
            socket.socket = self._orig_socket
            self._orig_socket = None
