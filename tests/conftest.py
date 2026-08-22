"""Test isolation.

Several modules hold process-level singletons or write env vars. Without
isolation a suite that mutates one leaks into every suite that runs after it —
tests pass alone and fail together, which is the most expensive kind of failure
to diagnose. Nine pipeline tests failed this way after a merge: they passed on
their own branch and in isolation, and only broke when the adversary and source
suites ran first.

Reset here rather than in the owning modules: those files are edited by
concurrent agents, and a shared conftest cannot conflict with them.
"""
from __future__ import annotations

import asyncio
import os

import pytest

@pytest.fixture(autouse=True)
def _fresh_event_loop():
    """Guarantee a usable default event loop for every test.

    asyncio.get_event_loop() raises RuntimeError once any earlier suite has
    closed the default loop, so a test using it passes alone and fails when run
    after an async suite. This has now bitten two separate instances (B5, then
    P1 reintroducing it), which makes it a property of the environment rather
    than of anyone's code — so it is fixed once, here.

    New tests should still prefer asyncio.run(); this is a safety net, not a
    licence to use the deprecated call.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)
