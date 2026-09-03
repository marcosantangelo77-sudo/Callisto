# OX TASK: split knowledge wiki compiler vs store (LONG)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-wiki-split-2ac0`
Worktree: `/tmp/callisto-ox-wiki-split`

`tools/knowledge_wiki.py` is ~1435 lines. Split store/query from compilation.
Do not add silent evidence rewrites (`signal_generated` / threshold UPDATEs).
If you find UPDATE of historical evidence flags, gate or delete the write
(fail-closed, tests).

## Exclusive files (HARD)

You MAY edit:
- `tools/knowledge_wiki.py`
- `tools/wiki/` (create)
- `tests/test_wiki_split.py` (create)
- wiki-related tests if imports break

Do NOT touch `api.py`. Do NOT arm betting.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. No merge to master.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_wiki_split.py -q
```

Commit: `refactor(wiki): split knowledge wiki store and compiler`

Write `OX_DONE.md`.
