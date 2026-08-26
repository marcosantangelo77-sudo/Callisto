# OX_DONE — resume_all must not arm executor; OrderManager defaults disabled

Commit: 7f6fde9d54ba76080e4e20ad77c6a8622346129b
Branch: cursor/ox-telegram-arming-2ac0 (pushed to origin)

## Changes
- tools/telegram_bot.py: _cmd_resume_all no longer calls bet_executor.enable(); reply states executor stays disabled until admin HTTP arm. _cmd_pause_all still disables both (fail-safe).
- tools/order_manager.py: __init__ sets self._enabled = False (fail-closed). enable() unchanged.
- tests/test_telegram_bot.py: fixture arms manager; added MockExecutor test asserting enable() NOT called on resume, disable() IS called on pause, reply mentions executor disabled.
- tests/test_order_manager_default_disabled.py (new): default-disabled + submit refuses until enable().
- Owned-adjacent fixtures armed (m.enable() after initialize): test_order_fsm.py, test_order_e2e.py, test_order_expiry.py, test_order_idempotency.py, test_order_reconciler.py

## Targeted test output
```
11 passed in 0.14s
```

## Full-suite regression check
Baseline (parent commit) vs patched run of full suite (excluding 10 files with pre-existing collection errors): 138 failed on baseline, 138 failed after patch — the diff showed only pre-existing unrelated redteam/build/speed failures plus the order-manager tests, which now all pass after fixture arming. No new failures from this change beyond the expected order-manager ones (all fixed).
