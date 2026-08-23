# RED TEAM — artifact store & chart regeneration

**Surface:** the artifact store (`tools/artifacts.py`), chart/workbook
emitters (`tools/charts.py`), and their contract with the seal.
**Method:** adversarial constructions against invariants stated by the code's
own docstrings — plus one concurrency probe. Not previously used on this
surface; distinct from last pass's before/after differential.

**Why this surface:** unattacked ground (per the rotation list: "artifact
store and chart regeneration"). BUILD_MANDATE property 3 — "evidence a human
can check" — rests entirely on this module's docstring claims: *immutable*,
*tampering detectable by re-hash*, *sealable: verifying the seal verifies the
artifacts*. Those claims had 28 passing tests exercising the happy path and
zero adversarial pressure. This pass attacks the claims, not the functions.

Deliverable: `tests/test_redteam_artifacts_store.py` — **17 fail on master**,
5 honest-negative pins pass. Run:

    python3 -m pytest tests/test_redteam_artifacts_store.py -q

---

## THE HEADLINE FINDING — A20 (CRITICAL): the seal does not cover artifacts at all

`agp/__init__.py::AGPSession.to_dict()` contains no artifact field. The keyed
HMAC seal is computed over that payload, so a sealed conclusion's quantitative
artifacts — the charts, workbooks, sandbox outputs that ARE "the math behind
it" — are outside the seal entirely. `tools/artifacts.py`'s docstring says
"the AGP keyed-HMAC seal covers those ids"; nothing in the code makes that
true. Worse, `PipelineResult.summary_dict()` cites artifacts as **12-hex
truncated ids**, which are not even resolvable back to objects.

Consequence: an attacker (or a later bug) can delete or replace every artifact
a sealed conclusion cites and every verification surface reports success.
Property 3 currently holds by convention, not mechanism.

## CONFIRMED BREAKS

### A2 (HIGH) — gc() + corrupt index destroys evidence permanently
`ArtifactStore.gc()` treats "not in index" as orphan. A corrupt/partially-
written `index.json` (which `_load_index` explicitly tolerates, promising
"objects survive") makes EVERY object look like an orphan. One gc call after
bit rot deletes the entire store, silently, returning the list of destroyed
hashes as if it were routine cleanup. Test:
`test_gc_after_corrupt_index_deletes_objects`.

### A3 (HIGH) — re-put rewrites provenance of an existing artifact
`_index_add` comments "an artifact's origin does not change because someone
later re-put identical bytes", but only guards `code_sha256`/`name`, and even
those only when non-empty. `data_refs` and ALL of `meta` are overwritten by
any later put of identical bytes. Demonstrated: `{"live_formulas": True}`
becomes `False`, empty `data_refs` becomes an injected ref. Bytes are
immutable; the *claims about the bytes* are not. Test:
`test_reput_rewrites_data_refs_and_meta_of_existing_artifact`.

### A9 (MEDIUM) — code_sha256 takeover via dedupe
Same hole from the other side: an entry stored without provenance (e.g. after
A16's rebuild) has its `code_sha256` claimed by whoever re-puts next with a
`code` argument. A fabricated computation can adopt pre-existing output bytes
as its own product. Test: `test_reput_with_code_overwrites_empty_code_sha256_provenance`.

### A4/A17 (MEDIUM) — export_ref path traversal + silent overwrite
`ArtifactRef.name` is attacker/model-writable and is joined into the delivery
path unvalidated: name `"../pwned"` writes outside `dest_dir`. Separately,
export_ref `copyfile`s over any existing file at the destination — a crafted
artifact named like a real report replaces it on delivery. Tests:
`test_export_ref_name_traversal_writes_outside_dest_dir`,
`test_export_ref_silently_overwrites_existing_file`.
(Honest sub-negative: `get_path` read traversal is NOT reachable above the
root — see pins.)

### A6 + engine wiring (HIGH) — phantom artifacts cited by sealed conclusions
Without a workspace (the attested path), `store_sandbox_outputs` mints refs
from hashes reported BY THE SANDBOX CHILD, marked `attested_by_child_only`.
The engine (`engine.py:367`) extends `artifact_sha256s` with these and cites
them in the leaf outcome — bytes nobody stored and nothing downstream
verifies. `verify_artifacts` correctly reports them missing, but **no caller
ever calls verify_artifacts** (grep confirms: zero production callers). Test:
`test_child_attested_ref_has_no_bytes_in_store`.

Related, fixed-then-noted: a FAILED sandbox run still seals its stdout as a
citable artifact (status recorded in meta, ref minted anyway). See
`test_failed_run_still_seals_stdout_and_attested_files_without_workspace` —
this passed after the status guard was confirmed for files but stdout refs
are unconditional.

### A14 (HIGH) — concurrent puts lose entries and crash
Three threads × 25 puts: 47/75 index entries survive, one thread dies with
`FileNotFoundError` on the unsuffixed shared temp name `index.tmp`
(read-modify-write with no lock AND a collision-prone tmp path). The repo has
a documented multi-agent concurrency history; this module predates that
lesson. Test: `test_concurrent_puts_do_not_lose_entries_or_crash`.

### A16 (MEDIUM) — rebuild_index launders origin
The documented recovery path resets `name`, `code_sha256`, `data_refs` to
empty for every object. Combined with A9, the "recovery" hands provenance of
every historical artifact to the next writer. Test:
`test_rebuild_index_erases_code_and_name_provenance`.

### B1 (HIGH) — fetched evidence becomes LIVE Excel formulas
`build_workbook` writes Data-sheet rows verbatim. A cell whose text begins
`=` is stored as a formula (`data_type 'f'`) and executes when the owner opens
the auditable workbook: demonstrated with `=HYPERLINK("http://evil","click")`
carried in a data row. The workbook's entire premise is "torture the math" —
this lets fetched content write into the math layer. Fix shape: prefix-guard
(`'` leading apostrophe) any string cell beginning with `=+-@`. Tests:
`test_data_row_starting_with_equals_becomes_live_formula`.

### B3 (LOW) — provenance comments silently misattach
A provenance record naming a nonexistent column attaches to column 1 instead
of being rejected — FRED attribution lands on whatever column happens to be
first. Test: `test_provenance_comment_for_unknown_column_lands_on_first_column`.

### A13 (LOW) — ModelLive contradicts its own audit listing
Duplicate target cells: Model lists BOTH formulas; ModelLive keeps only the
LAST written. An auditor reading the listing sheet audits formulas the
workbook does not compute. Test:
`test_duplicate_model_cell_listing_contradicts_live_sheet`.

### A11 (LOW) — NaN/Inf render straight into charts
`render_svg` accepts non-finite series/x values and emits literal `nan`/`inf`
into axis labels and polyline points (`x="nan"`), producing invalid SVG
geometry from upstream numeric bugs instead of rejecting them. Tests:
`test_render_svg_emits_nan_into_axis_labels`, `test_render_svg_accepts_inf_x_axis`.

### A12 (LOW) — kind sniffing by substring
Any zip → "xlsx"; `<svg` anywhere in first 512 bytes (including inside HTML)
→ "svg". Kind feeds delivery suffixes and downstream interpretation. Tests:
`test_sniff_kind_misclassifies[*]`.

### A18 (MEDIUM) — verification vouches beyond bytes
`verify_artifacts` re-hashes bytes but returns ok=True for a ref whose
declared `kind` and `code_sha256` are fabricated; `ArtifactRef.from_dict`
accepts any string as sha256 (no hex validation, unlike research_program's
copy — note TWO ArtifactRef classes exist with different rules). If a future
fix wires seals to artifacts (A20), these become seal-relevant immediately.
Tests: `test_verify_artifacts_accepts_ref_lying_about_kind_and_code`,
`test_from_dict_rejects_non_hex_sha256`.

---

## HONEST NEGATIVES — attacks that did NOT land (kept as pins)

- **get_path read traversal**: `objects/<a>/<b>/<id>` join semantics block
  classic `../../` climbs above the root (verified against several payloads).
- **Chart regeneration round-trip**: `store_chart`'s stored spec re-renders
  byte-identically through `render_svg`; the caller-spec mutation bug noted in
  its comment is genuinely fixed.
- **Object immutability**: `put()` never overwrites existing bytes at a hash
  path — the bytes half of the docstring's immutability claim holds.

## WHAT TO FIX (ordered by leverage)

1. **Bind artifacts to the seal** (A20): add `artifacts: [ref.to_dict()]` to
   the AGP session payload before sealing, call `verify_artifacts` on the seal
   path, and stop truncating ids in `summary_dict`. Without this, everything
   else on this surface is decoration.
2. Make `_index_add` merge-only (never overwrite existing non-empty fields;
   reject conflicting `data_refs`/`meta`), and give the index tmp file a
   unique name + lock (A3/A9/A14).
3. `gc()` must refuse to run when the index failed to load (A2) — cheap,
   prevents catastrophic loss.
4. Guard spreadsheet cells against formula injection; validate provenance
   columns (B1/B3); make ModelLive authoritative over the listing or refuse
   duplicates (A13).
5. Reject non-finite numbers in `chart_spec`/`render_svg` (A11).
6. Consolidate the two `ArtifactRef` classes and validate hex + declared-kind
   match in `verify_artifacts` (A18).

## Relation to prior passes

The confidence-inflation red team found laundering through evidence classes;
this pass finds laundering through the artifact layer — same theme (metadata
about immutable things is mutable and unverified), different module. The C1–C4
checkpoint findings showed integrity checks that quietly do nothing;
`verify_artifacts` having zero production callers is the same shape one level
up.
