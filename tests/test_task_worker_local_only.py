"""POST /task worker must honor CALLISTO_LOCAL_ONLY even if research_loop is None.

The orchestrator path still calls Claude. Under LOCAL_ONLY that is hosted
work and must fail-close. research_loop._local_only already skipped tasks,
but only when the loop object existed — env CALLISTO_LOCAL_ONLY was ignored
if the loop was None.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.api.workers import _task_blocked_by_local_only


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)


def test_env_blocks_when_loop_missing(clean_env, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    assert _task_blocked_by_local_only(None) is True


@pytest.mark.parametrize("val", ["true", "TRUE", "yes", "YES"])
def test_env_truthy_spellings_block(clean_env, monkeypatch, val):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
    assert _task_blocked_by_local_only(None) is True


@pytest.mark.parametrize("val", ["0", "false", "no", "", "2"])
def test_env_falsy_spellings_do_not_block(clean_env, monkeypatch, val):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
    assert _task_blocked_by_local_only(None) is False


def test_unset_env_loop_none_allows(clean_env):
    assert _task_blocked_by_local_only(None) is False


def test_loop_flag_blocks_without_env(clean_env):
    assert _task_blocked_by_local_only(SimpleNamespace(_local_only=True)) is True


def test_loop_flag_false_without_env_allows(clean_env):
    assert _task_blocked_by_local_only(SimpleNamespace(_local_only=False)) is False


def test_env_wins_over_loop_toggled_off(clean_env, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    assert _task_blocked_by_local_only(SimpleNamespace(_local_only=False)) is True
