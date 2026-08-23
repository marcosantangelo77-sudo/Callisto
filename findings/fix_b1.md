# B1 — Fetched evidence becomes live Excel formulas (FIXED)

Commit: a58cb80 on fix/b1-formula-injection (worktree serving/)

## The bug
`tools/charts.build_workbook` wrote spec content into xlsx cells verbatim.
openpyxl stores any string beginning `=` as a formula (data_type 'f'), which
Excel executes when the owner opens the "auditable" workbook. Demonstrated
with `=HYPERLINK("http://evil","click")` carried in a fetched Data row:
untrusted remote bytes executing in the audit surface.

## The fix
Added `_guarded_text()` in tools/charts.py and routed every spec-content cell
write through it. Guard rule (standard CSV-injection set):

- `=` lead → always neutralized to `'=...` (text).
- `+`, `-`, `@` leads → neutralized to text UNLESS the whole string parses as
  a number; numeric strings (`"-5"`, `"+3.5"`) are coerced to real numbers so
  they read as values, never formulas, never `"'-5"`.

### Writer coverage (grepped, not assumed)
`build_workbook` is the only xlsx writer — `domains/finance/plugin.py` and
`fermi.py` both emit via `store_workbook` → `build_workbook`. All guarded
write sites within it:

- Assumptions rows (value, source, note)
- every Data sheet: column headers AND every row cell (any fetched sheet name)
- Model listing sheet: label + cell columns
- ModelLive header labels
- Scenarios rows (name, assumption, value)

## How legitimate formulas are distinguished
Mechanically by write site, not by content sniffing. The ONLY cells written
as formulas are Model column C and ModelLive cells, both fed exclusively from
`spec["model"][*]["formula"]` — produced by our code paths (finance plugin,
fermi), never from fetched bytes. Everything else passes through the guard.
Pinned by test_legitimate_system_formulas_remain_live.

## Tests
- test_data_row_starting_with_equals_becomes_live_formula: now PASSES.
- NEW test_negative_number_string_stays_numeric_not_text: "-5" reads as -5
  (numeric data_type 'n', not "'-5"); "+3"→3; "@2" and "-SUM(A1)" → text.
- NEW test_plus_at_prefix_nonnumeric_strings_are_neutralized.
- NEW test_legitimate_system_formulas_remain_live: ModelLive B2 stays a live
  formula.

Full suite (serving/, excluding two pre-existing ml_* import-collection
errors): 38 failed before → 37 failed after; 11165 passed. Net −1 = exactly
the B1 test; no collateral breakage. Remaining failures are other pre-existing
red-team findings (artifact store, provenance misattribution, NaN SVG) out of
B1 scope.

No confidence scores raised. ~/Documents/GitHub/Callisto untouched.
