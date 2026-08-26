# OX_DONE — inference planes documentation

Branch: `cursor/ox-inference-planes-2ac0`
Commit: `docs(inference): pin dual MODEL_LADDER vs ProviderRouter planes`

## What was done

- `inference.py`: comment block above `MODEL_LADDER` documenting the two
  inference planes (kernel `MODEL_LADDER` used by `complete()` vs.
  `ProviderRouter` + `config/providers.yaml` used by the CLI/pipeline),
  stating ProviderRouter is canonical *future* routing and that neither
  plane may be deleted in drive-by refactors. No code behavior changed;
  `MODEL_LADDER` contents untouched.
- `tests/test_inference_planes.py` (new): pins that
  - `MODEL_LADDER` has `reasoning`, `classification`, `review` keys with non-empty ladders,
  - `load_providers_config()` loads and yields >= 1 provider.

## Verification

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_inference_planes.py -q
3 passed in 0.07s
```

No network, no latency measurement, no changes to routing/providers.yaml/tools/.
