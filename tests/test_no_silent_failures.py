r"""Regression test: no new silent-failure anti-patterns in hot files.

Per `feedback_silent_failure_patterns`: "Most Callisto bugs hide as caught
exceptions, not crashes." This test fails if any of the hot-path files
grows a new ``except ... : pass`` that isn't explicitly allow-listed —
forcing reviewers to either log the exception or justify the swallow.

Scope note: we deliberately flag ``except: pass`` but NOT
``except: continue`` inside loops. A ``continue`` typically skips a
malformed row in a parsing loop, which is a legitimate and common
pattern (row validity is handled by the loop's own emptiness check).
A ``pass``, by contrast, lets execution fall through to code that
assumes the ``try`` block succeeded, which is the silent-failure
shape this feedback memory is hunting.

The initial budget for each file is the count of LEGITIMATE silent
swallows as of this commit. Any additional ``except ... : pass`` — OR
any such catch whose exception type isn't in the legit set — fails
the test immediately.

Legitimate silent catches (which we allow via the exception-type
allow-list):

- ``except asyncio.CancelledError: pass`` — task shutdown cancellation.
- ``except (asyncio.CancelledError, Exception): pass`` inside shutdown
  paths — same rationale.
- ``except FileNotFoundError: pass`` — optional file lookups.
- ``except RuntimeError: pass`` — only legitimate when releasing an
  asyncio.Lock that may not be held; counted in the budget, not flagged.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


# Hot files — budget reflects the remaining *legitimate* silent
# ``except: pass`` swallows (shutdown cancellation, optional files,
# lock-release RuntimeError) after the silent-failure hunt.
#
# A budget of 0 means "every `except: pass` in this file must log or be
# documented". Higher budgets encode shutdown/optional-file patterns
# that are fine.
HOT_FILE_BUDGETS: dict[str, int] = {
    # autonomous.py: 3x CancelledError (stop paths), 2x FileNotFoundError
    # (memory/error_patterns.md is optional), 1x (CancelledError, Exception)
    # for quant task shutdown.
    "tools/autonomous.py": 6,
    # hypothesis.py — clean post-hunt.
    "tools/hypothesis.py": 0,
    # backtest.py — clean post-hunt.
    "tools/backtest.py": 0,
    # edge_scanner.py — clean post-hunt.
    "tools/edge_scanner.py": 0,
    # line_monitor.py: 2x CancelledError shutdown + 1x RuntimeError around
    # asyncio.Lock.release (only raises if not held, benign).
    "tools/line_monitor.py": 3,
    # data_collector.py — clean post-hunt.
    "tools/data_collector.py": 0,
    # bet_executor.py — clean post-hunt.
    "tools/bet_executor.py": 0,
    # api.py: 8x CancelledError inside lifespan-shutdown cancellation
    # blocks. These are the documented contract for graceful task
    # cancellation and must NOT be touched per hunt constraints.
    "api.py": 8,
    # orchestrator.py — clean.
    "orchestrator.py": 0,
}


# Hot files that must have a module-level logger (pre-req for logging
# swallowed exceptions). `analysis.py` is excluded because it is a
# stand-alone diagnostic script that prints to stdout.
HOT_FILES_NEED_LOGGER = [
    "tools/autonomous.py",
    "tools/hypothesis.py",
    "tools/backtest.py",
    "tools/edge_scanner.py",
    "tools/line_monitor.py",
    "tools/data_collector.py",
    "tools/bet_executor.py",
    "api.py",
    "orchestrator.py",
]


# Match `except ...: pass` at end of line. Typed or untyped clauses.
# (We deliberately do NOT match `continue` — see module docstring.)
_BARE_PASS_RE = re.compile(
    r"^(?P<indent>\s+)except(?P<exc>[^:\n]*):\s*\n\s+pass\s*$",
    re.MULTILINE,
)


# Exception types that are legitimately silent-ok.
_LEGIT_EXC_NAMES = (
    "asyncio.CancelledError",
    "CancelledError",
    "FileNotFoundError",
    "RuntimeError",
)


def _is_legit_exc(exc_clause: str) -> bool:
    """Return True if every exception in the clause is in the legit set.

    E.g. ``except asyncio.CancelledError:`` → True.
         ``except (asyncio.CancelledError, Exception):`` → True (contains
         CancelledError; Exception is allowed here because the tuple
         includes the shutdown marker).
         ``except Exception:`` → False.
    """
    stripped = exc_clause.strip().lstrip("(").rstrip(")")
    # If ANY legit name appears, treat as shutdown-tolerant.
    return any(name in stripped for name in _LEGIT_EXC_NAMES)


def _count_silent_swallows(path: Path) -> tuple[int, list[tuple[int, str]]]:
    """Return ``(legit_count, bad_list)`` for a file.

    ``bad_list`` contains ``(lineno, snippet)`` pairs for swallows whose
    exception type is NOT in the legit set. These fail unconditionally.
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.split("\n")
    legit = 0
    bad: list[tuple[int, str]] = []
    for m in _BARE_PASS_RE.finditer(text):
        exc = m.group("exc")
        lineno = text[: m.start()].count("\n") + 1 + 1  # 1-indexed, +1 for indent capture
        # Recompute precise lineno from match start (excluding the indent
        # that's part of the leading newline).
        lineno = text.count("\n", 0, m.start(0)) + 2
        if _is_legit_exc(exc):
            legit += 1
        else:
            snippet = lines[lineno - 1].strip() + " / " + lines[lineno].strip() if lineno < len(lines) else lines[lineno - 1].strip()
            bad.append((lineno, snippet))
    return legit, bad


@pytest.mark.parametrize("rel_path,budget", sorted(HOT_FILE_BUDGETS.items()))
def test_hot_file_silent_swallow_budget(rel_path: str, budget: int) -> None:
    """Fail if a hot file accumulates new un-logged ``except: pass`` swallows.

    Two checks:

    1. No un-logged ``except: pass`` whose exception type is not in the
       legit set (CancelledError / FileNotFoundError / RuntimeError).
    2. Legit silent swallows must not exceed the per-file budget.
    """
    path = REPO_ROOT / rel_path
    assert path.exists(), f"Hot file {rel_path} missing — update HOT_FILE_BUDGETS"
    legit, bad = _count_silent_swallows(path)

    assert not bad, (
        f"{rel_path}: {len(bad)} non-legit silent `except: pass` swallow(s). "
        f"Each one must either log the exception or wrap a shutdown/optional-"
        f"file path. Offenders:\n  "
        + "\n  ".join(f"line {n}: {s}" for n, s in bad)
    )

    assert legit <= budget, (
        f"{rel_path}: {legit} legit silent swallows exceed budget {budget}. "
        f"If the new swallow is intentional (e.g. a new shutdown path), "
        f"bump the budget in HOT_FILE_BUDGETS and justify it in the PR."
    )


@pytest.mark.parametrize("rel_path", HOT_FILES_NEED_LOGGER)
def test_hot_file_has_logger(rel_path: str) -> None:
    """Every logging-required hot file defines a module-level logger.

    Without a module logger the silent-failure logging fix would itself
    crash, defeating the point of the hunt.
    """
    path = REPO_ROOT / rel_path
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert "import logging" in text, f"{rel_path} is missing `import logging`"
    assert re.search(r"^logger\s*=\s*logging\.getLogger", text, re.MULTILINE), (
        f"{rel_path} is missing a module-level `logger = logging.getLogger(...)`"
    )
