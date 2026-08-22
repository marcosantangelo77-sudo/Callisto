"""W2 — empirical model routing.

Model selection becomes a measurement, not a vendor claim:

  run the same retrodiction question set through different models per role
    -> Brier score and cost per (model, role)   [scores.py]
    -> route each role to the model that measurably does it best
    -> re-measure when a new model appears      [policy.py]

The router consults measured scores where they exist and falls back to the
configured tier list where they do not — with zero measurements the system
degrades EXACTLY to today's configured behaviour. Nothing gets worse before
measurements exist.
"""
