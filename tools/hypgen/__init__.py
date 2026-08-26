"""
tools.hypgen — split modules for the hypothesis generator.

Layout:
  templates.py    — HYPOTHESIS_TEMPLATES, generator constants, variable expansion
  prompts.py      — LLM prompt assembly, candidate parsing, variance enforcement
  seeds.py        — underexplored-seed selection
  persistence.py  — DB lifecycle, wiki/rejection retrieval, sharpening write-back

The public class remains tools.hypothesis_generator.HypothesisGenerator,
which is now a facade over these modules.
"""

from tools.hypgen.templates import (  # noqa: F401
    CANDIDATE_DEDUP_SIM,
    NEGATIVE_EXAMPLES_N,
    PRIOR_CORPUS_SIM,
    WIKI_CONTEXT_TOP_K,
    HYPOTHESIS_TEMPLATES,
    expand_variables,
)
