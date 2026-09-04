"""POST /task worker must honor CALLISTO_LOCAL_ONLY even if research_loop is None.

The orchestrator path still calls Claude. Under LOCAL_ONLY that is hosted
work and must fail-close. research_loop._local_only already skipped tasks,
but only when the loop object existed — env CALLISTO_LOCAL_ONLY was ignored
if the loop was None.
"""
from __future__ import annotations

from types import SimpleNamespace

from pathlib import Path

import pytest

from tools.api.workers import (
    _post_task_orchestrator_forbidden,
    _task_blocked_by_local_only,
)

REPO = Path(__file__).resolve().parent.parent


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


def test_wiki_hit_allows_submit_under_local_only(clean_env, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    assert _post_task_orchestrator_forbidden({"wiki_topic": "x"}) is False


def test_no_wiki_forbids_submit_under_local_only(clean_env, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    assert _post_task_orchestrator_forbidden(None) is True


def test_no_wiki_allows_submit_when_local_only_unset(clean_env):
    assert _post_task_orchestrator_forbidden(None) is False


def test_submit_task_source_403s_before_enqueue():
    src = (REPO / "tools" / "api" / "task_routes.py").read_text(encoding="utf-8")
    body = src.split("async def submit_task", 1)[1].split("async def get_task", 1)[0]
    assert "_post_task_orchestrator_forbidden" in body
    assert body.find("_post_task_orchestrator_forbidden") < body.find(
        "queue.submit_task")
    assert "status_code=403" in body
    assert "except HTTPException" in body
    assert body.rfind("except HTTPException") < body.rfind("except Exception")
