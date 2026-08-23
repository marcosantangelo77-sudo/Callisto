"""Review run 7, 2026-08-23 — reproductions against branches not yet reviewed.

Targets:
  RV4  fix/a14-a2-store-integrity (d8f0b17): A2 closed the CORRUPT-index hole
       in ArtifactStore.gc() but left the MISSING-index hole open — family 3
       (absence treated as success) one frame inside the fix.
  RV5  honest-null wiring (review/rotating-0823-184936):
       classify_null_kind() reads an EMPTY-but-successful source entry
       ({"name": ...} with neither admitted nor rejected nor error) as proof
       the source "was reached", so a source whose result list came back
       empty — the FDIC/ClinicalTrials live-API defect shape — is classified
       HONEST_NULL ("the literature does not address this") instead of a
       suspected retrieval failure.

Both tests fail on the branches under review BY DESIGN. Edit no production
code; these are the deliverable.
"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as SN

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.artifacts import ArtifactStore  # noqa: E402


def _store_with_one_object(root: Path) -> tuple[ArtifactStore, str]:
    s = ArtifactStore(root=root)
    ref = s.put(b"evidence bytes that must survive", kind="txt", name="ev.txt")
    return s, ref.sha256


def test_gc_with_missing_index_file_refuses_to_delete(tmp_path):
    """RV4: index.json ABSENT (not corrupt) -> gc() deletes all evidence.

    The A2 fix routes only _load_index(strict=True)->None (corrupt bytes)
    into the refuse path. A nonexistent index returns {} and gc reads that
    as 'everything is an orphan' — precisely the destruction the finding
    said must be impossible. An absent index proves even LESS than a
    corrupt one; it must fail closed the same way.
    """
    s, sha = _store_with_one_object(tmp_path / "store")
    s.index_path.unlink()
    assert not s.index_path.exists()
    removed = s.gc()
    assert removed == [], (
        f"gc destroyed evidence on a MISSING index: {removed} "
        f"(object {sha})")
    assert (tmp_path / "store" / "objects" / sha[:2] / sha[:2] /
            sha).exists(), "evidence object was deleted"


def test_gc_with_empty_index_object_deletes_evidence(tmp_path):
    """RV4 companion: '{}' IS valid JSON, so even the corrupt path never
    fires — the store wipes clean. Documents why emptiness itself, not just
    unreadability, must be treated as 'cannot prove orphanhood'."""
    s, sha = _store_with_one_object(tmp_path / "store")
    s.index_path.write_text("{}", encoding="utf-8")
    removed = s.gc()
    assert removed == [], f"gc destroyed evidence on empty index: {removed}"


def test_classify_null_kind_source_returned_nothing_is_not_honest_null():
    """RV5: a source entry with NO admitted items, NO rejections, and NO
    error means we do not know whether the source was silent or the fetch
    was empty/blocked. classify_null_kind currently treats any such entry
    as 'reachable_attempt' evidence via `"rejected" in s`, and otherwise
    falls through to HONEST_NULL — laundering 'we saw nothing come back'
    into 'the literature does not address this'.

    Family 3 (absence as success) / family 4 (a field's presence standing
    in for evidence of contact). Until the rule distinguishes an observed
    zero-result response from an unobserved one, this classification must
    NOT assert honest null.
    """
    from tools.gaps import NullKind, classify_null_kind

    # Source contacted successfully but returned an empty result list:
    # recorded as {"name": ..., "status": "ok"} with nothing else.
    trace = SN(
        rejected=[],
        rounds=[{"sources": [{"name": "openalex", "status": "ok"}]}],
        stop_reason="gain_exhausted",
    )
    kind, expl = classify_null_kind(trace)
    assert kind != NullKind.HONEST_NULL.value, (
        "empty-but-successful source read as 'literature is silent': "
        f"{kind} | {expl}")
