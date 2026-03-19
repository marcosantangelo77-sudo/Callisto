# Upstream Tracking — hermes-function-calling

## Pinned Commit

**Repository:** NousResearch/hermes-function-calling
**Commit:** `ea3c4723` — "using only valid json in examples instead of python dicts"
**Pinned on:** 2026-03-18

## Import Policy

Hermes is a **parts supplier**, not a framework dependency. We import selectively:

| File | Imported | Purpose |
|------|----------|---------|
| `functions.py` | Yes | Tool schema definitions, `get_openai_tools()` |
| `prompter.py` | Yes | Prompt formatting utilities |
| `schema.py` | Yes | JSON schema handling |
| `utils.py` | Yes | General utilities |
| `validator.py` | Yes | Function call validation |
| `functioncall.py` | **No** | Requires torch/transformers |
| `jsonmode.py` | **No** | Requires torch/transformers |

Import mechanism: `sys.path.insert(0, "hermes-function-calling")` — no modifications to upstream files.

## Review Process

1. Run `python upstream_review.py` to fetch and evaluate new commits
2. The Architect evaluates each commit under AGP TECHNICAL domain rules
3. Each commit receives a verdict: **PULL**, **SKIP**, or **MODIFY_THEN_PULL**
4. Only commits affecting imported files are considered for PULL
5. Cherry-picks are applied individually, never bulk-merged
6. Callisto divergences (AGP, memory, orchestrator) are never upstreamed

## Updating the Pin

After cherry-picking approved commits:

```bash
cd hermes-function-calling
git checkout <new-commit-hash>
```

Update this file with the new pinned commit hash and date.
