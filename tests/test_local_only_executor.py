"""CALLISTO_LOCAL_ONLY must refuse to arm BetExecutor.

No browser, no network: the executor is instantiated but never initialized
(``initialize()`` is not called) and only ``enable``/``disable``/``is_enabled``
are exercised.
"""

import pytest

from tools.bet_executor import BetExecutor


TRUTHY = ["1", "true", "TRUE", "True", "yes", "YES", "Yes"]


def test_default_env_enable_disable(monkeypatch):
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
    ex = BetExecutor()
    assert ex.is_enabled is False  # __init__ default unchanged
    assert ex.enable() is True
    assert ex.is_enabled is True
    ex.disable()
    assert ex.is_enabled is False


@pytest.mark.parametrize("value", TRUTHY)
def test_local_only_refuses_enable(monkeypatch, value):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    ex = BetExecutor()
    assert ex.enable() is False
    assert ex.is_enabled is False


def test_falsy_value_still_enables(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "0")
    ex = BetExecutor()
    assert ex.enable() is True
    assert ex.is_enabled is True


def test_refusal_does_not_raise_when_return_ignored(monkeypatch):
    """Existing callers may ignore enable()'s return — refusal stays silent."""
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = BetExecutor()
    ex.enable()  # no exception
    assert ex.is_enabled is False
