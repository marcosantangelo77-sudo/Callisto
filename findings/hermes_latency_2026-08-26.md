# Hermes CLI one-shot latency — 2026-08-26

Measured `hermes --provider nous -m stealth/ox-alpha -z PONG --in <tmpdir>`
fork + round-trip latency on this VM (script: `scripts/measure_hermes_latency.py`).

## Results (n=3)

| run | elapsed_ms | exit | PONG in stdout |
|-----|-----------|------|----------------|
| 1   | 9624      | 0    | yes |
| 2   | 31402     | 0    | yes |
| 3   | 11931     | 0    | yes |

- **p50: 11.9s** · **max: 31.4s**
- hermes path: `/home/ubuntu/.local/bin/hermes` (Hermes Agent v0.20.5)
- model: `stealth/ox-alpha`, provider `nous`

## Implication for Stage C

The historical ~14s figure holds as the median, but tail latency is high
(one run at ~31s). Any Stage C "one inference plane" design that assumes
sub-10s hermes fork latency is not supported by this data; budget for
p50 ≈ 12s and max ≥ 30s.
