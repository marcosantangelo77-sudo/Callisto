"""S1 — Content-addressed artifact store.

Every quantitative output of a research session — a dataframe, a chart, a
spreadsheet, a model object — is stored once by the SHA-256 of its bytes and
referenced thereafter by that hash. Properties this buys:

- Immutable: an artifact id IS its content; citing `sha256:abc123` cites
  exact bytes forever. Tampering with the store is detectable by re-hash.
- Deduplicating: the same chart produced twice stores once.
- Sealable: a claim can list artifact ids in its evidence; the AGP keyed-HMAC
  seal covers those ids (via ArtifactRef.to_dict()), so verifying the seal
  verifies the artifacts still exist unmodified (verify_artifacts()).

Layout: <root>/objects/ab/cd/<sha256> (raw bytes), plus an index JSON at
<root>/index.json mapping sha256 → metadata {kind, name, code_sha256,
data_refs, created_at, meta}. The index is metadata only — the objects are
the source of truth; the index can be rebuilt from them (rebuild_index).

Storage root resolution mirrors tools/state_paths.py conventions:
$CALLISTO_ARTIFACT_DIR > <repo>/data/artifacts. Domain-general: kinds are
file formats, not subject matter.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import shutil
import tempfile
import fcntl
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ALLOWED_KINDS = {"csv", "json", "xlsx", "png", "svg", "txt", "ipynb", "pkl", "md"}

# Per-store-root index lock. In-process (threads) plus a lock file so
# independent processes sharing a store root also serialize (A14). The
# lockfile lives outside objects/ so gc/rebuild never see it as an object.
_locks_guard = threading.Lock()
_locks: dict[str, "threading.Lock"] = {}


class ArtifactIndexCorrupt(RuntimeWarning):
    """gc() refused to run: the index is unreadable and it will not delete
    on the strength of a file that may be corrupt."""


_locks_guard = threading.Lock()
# key -> {"lock": threading.Lock, "owner": ident or None, "depth": int}
_locks: dict[str, dict] = {}


class _IndexLock:
    """Serializes index read-modify-write per store root.

    Two layers:
    - in-process: a plain Lock with owner/depth tracking so nested use
      (_index_add -> _write_index) doesn't self-deadlock;
    - cross-process: an exclusive flock on <root>/.index.lock taken only at
      the outermost acquisition.
    """

    def __init__(self, root: Path):
        self.key = str(root)
        self.lock_path = Path(root) / ".index.lock"
        self._fh = None

    def __enter__(self):
        with _locks_guard:
            e = _locks.setdefault(
                self.key, {"lock": threading.RLock(), "owner": None, "depth": 0}
            )
        me = threading.get_ident()
        e["lock"].acquire()   # RLock: reentrant per-thread, blocks cross-thread
        self._recursed = e["owner"] == me
        try:
            if self._recursed:
                pass                     # outer acquisition holds the flock
            else:
                e["owner"] = me
                e["depth"] = 1
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self.lock_path, "a+")
                fcntl.flock(self._fh, fcntl.LOCK_EX)
        except BaseException:
            if not self._recursed:
                e["owner"] = None
            e["lock"].release()
            raise
        return self

    def __exit__(self, *exc):
        with _locks_guard:
            e = _locks[self.key]
        if not self._recursed:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
            e["owner"] = None
        e["depth"] -= 1
        e["lock"].release()
        return False


def _index_lock(root: Path) -> "_IndexLock":
    return _IndexLock(root)


def _default_root() -> Path:
    override = os.environ.get("CALLISTO_ARTIFACT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    # Repo layout: data/ already exists for persistent state.
    return Path(__file__).resolve().parent.parent / "data" / "artifacts"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass
class ArtifactRef:
    """A reference a conclusion carries. The dict form is what a seal covers."""

    sha256: str
    kind: str
    name: str = ""
    code_sha256: str = ""      # hash of the sandbox code that produced it
    data_refs: list[str] = field(default_factory=list)  # upstream artifact hashes
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "kind": self.kind,
            "name": self.name,
            "code_sha256": self.code_sha256,
            "data_refs": list(self.data_refs),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ArtifactRef":
        return cls(
            sha256=d["sha256"],
            kind=d["kind"],
            name=d.get("name", ""),
            code_sha256=d.get("code_sha256", ""),
            data_refs=list(d.get("data_refs", [])),
            meta=dict(d.get("meta", {})),
        )

    def __post_init__(self) -> None:
        # A18: a ref id IS a sha256 digest; anything else (path fragments,
        # truncated ids, junk) must never become a citable reference. This
        # also closes from_dict(), which previously accepted any string.
        if not _HEX64.fullmatch(self.sha256):
            raise ValueError(
                f"sha256 must be 64 hex chars, got {self.sha256!r}"
            )
        if self.code_sha256 and not _HEX64.fullmatch(self.code_sha256):
            raise ValueError(
                f"code_sha256 must be 64 hex chars or empty, got {self.code_sha256!r}"
            )


class ArtifactStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else _default_root()
        self.objects = self.root / "objects"
        self.index_path = self.root / "index.json"

    # -- creation ---------------------------------------------------------

    def put(
        self,
        data: bytes,
        kind: str,
        name: str = "",
        *,
        code_sha256: str = "",
        data_refs: Optional[list[str]] = None,
        meta: Optional[dict] = None,
    ) -> ArtifactRef:
        """Store bytes by content hash; returns the immutable ref."""
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"unsupported artifact kind {kind!r}; allowed: {sorted(ALLOWED_KINDS)}")
        digest = sha256_bytes(data)
        obj_dir = self.objects / digest[:2] / digest[2:4]
        obj_dir.mkdir(parents=True, exist_ok=True)
        obj_path = obj_dir / digest
        if not obj_path.exists():  # content-addressed: never overwrite
            fd, tmp = tempfile.mkstemp(dir=str(obj_dir))
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, obj_path)
        ref = ArtifactRef(
            sha256=digest,
            kind=kind,
            name=name,
            code_sha256=code_sha256,
            data_refs=list(data_refs or []),
            meta=dict(meta or {}),
        )
        self._index_add(ref)
        return ref

    def put_text(self, text: str, kind: str, **kwargs) -> ArtifactRef:
        return self.put(text.encode("utf-8"), kind, **kwargs)

    def put_json(self, value: Any, name: str = "", **kwargs) -> ArtifactRef:
        """Canonical serialisation: same value → same bytes → same hash."""
        return self.put_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            "json",
            name=name,
            **kwargs,
        )

    # -- retrieval --------------------------------------------------------

    def get_path(self, sha256: str) -> Path:
        p = self.objects / sha256[:2] / sha256[2:4] / sha256
        if not p.exists():
            raise KeyError(f"artifact {sha256[:12]}… not found")
        return p

    def get_bytes(self, sha256: str) -> bytes:
        return self.get_path(sha256).read_bytes()

    def get_json(self, sha256: str) -> Any:
        return json.loads(self.get_bytes(sha256).decode("utf-8"))

    def get_meta(self, sha256: str) -> Optional[dict]:
        idx = self._load_index()
        entry = idx.get(sha256)
        return dict(entry) if entry else None

    def exists(self, sha256: str) -> bool:
        try:
            self.get_path(sha256)
            return True
        except KeyError:
            return False

    # -- integrity --------------------------------------------------------

    def verify_artifacts(self, refs: list[ArtifactRef]) -> dict:
        """Re-hash every referenced artifact AND check the ref's declared
        metadata against the store's index. Used before sealing and after
        any restore; a single mismatch means tampering or corruption.

        A18: re-hashing bytes alone vouches for bytes only. A seal covers
        the CLAIM too — "this artifact is a png produced by code X". A ref
        whose declared kind or code_sha256 contradicts the stored index
        entry fails verification even when its bytes are intact.
        """
        report = {"verified": 0, "missing": [], "corrupt": [], "lying": []}
        idx = self._load_index()
        for ref in refs:
            if not self.exists(ref.sha256):
                report["missing"].append(ref.sha256)
                continue
            actual = sha256_bytes(self.get_bytes(ref.sha256))
            if actual != ref.sha256:
                report["corrupt"].append(ref.sha256)
                continue
            # Bind declared metadata to what the store recorded at put time.
            entry = idx.get(ref.sha256) or {}
            if ref.kind and entry.get("kind") and ref.kind != entry["kind"]:
                report["lying"].append(ref.sha256)
                continue
            if ref.code_sha256 and entry.get("code_sha256") \
                    and ref.code_sha256 != entry["code_sha256"]:
                report["lying"].append(ref.sha256)
                continue
            report["verified"] += 1
        report["ok"] = not (
            report["missing"] or report["corrupt"] or report["lying"]
        )
        return report

    def gc(self, *, allow_rebuild: bool = False) -> list[str]:
        """Remove objects absent from the index (orphans only).

        Safety direction: garbage may be kept; evidence must never be
        destroyed. If the index file is unreadable/corrupt, gc REFUSES and
        raises ArtifactIndexCorrupt — an empty index is indistinguishable
        from "everything is an orphan", so deleting on its authority could
        wipe the store.

        allow_rebuild=True instead recovers: it rebuilds the index from the
        objects on disk (the actual evidence) and proceeds. Provenance lost
        in a rebuild is marked meta.reconstructed by rebuild_index().
        """
        idx = self._load_index(strict=True)
        if idx is None:
            if allow_rebuild:
                self.rebuild_index()
                idx = self._load_index(strict=True) or {}
            else:
                # Refuse: an unreadable index proves nothing about which
                # objects are orphans. Say so loudly, delete nothing.
                warnings.warn(
                    f"gc() refused: index at {self.index_path} is corrupt or "
                    "unreadable; nothing was deleted. Repair the index "
                    "(rebuild_index()) and retry.",
                    ArtifactIndexCorrupt,
                    stacklevel=2,
                )
                return []
        removed = []
        for obj in self.objects.rglob("*"):
            if obj.is_file() and obj.name not in idx:
                obj.unlink()
                removed.append(obj.name)
        return removed

    # -- persistence of refs ----------------------------------------------

    def export_ref(self, ref: ArtifactRef, dest_dir: Path) -> Path:
        """Copy an artifact to a human-accessible path (delivery surface).

        A4/A17: ref.name is attacker/model-writable text, and this is a WRITE
        surface. Two rules, fail-closed:
          - the resolved destination must stay inside dest_dir (no `..`, no
            absolute names, no symlink escape) — anything else is arbitrary
            file write;
          - never overwrite: an existing file at the destination is a
            collision with something we did not write, so refuse rather
            than silently replace it.
        """
        import re as _re

        dest_dir = Path(dest_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{ref.kind}" if ref.kind else ""
        base = ref.name or ref.sha256[:12]
        # Allow only plain path-safe characters; reject separators, dots
        # that form traversal segments, and control characters outright.
        if not _re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._ -]{0,128}", base) \
                or ".." in base or base.startswith("."):
            raise ValueError(
                f"refusing unsafe artifact name for export: {base!r}"
            )
        dest = dest_dir / f"{base}{suffix}"
        if not dest.resolve().parent == dest_dir:
            raise ValueError(f"export escaped dest_dir: {dest}")
        if dest.exists() or dest.is_symlink():
            raise FileExistsError(
                f"refusing to overwrite existing file at delivery path: {dest}"
            )
        shutil.copyfile(self.get_path(ref.sha256), dest)
        return dest

    # -- index ------------------------------------------------------------

    def _load_index(self, *, strict: bool = False) -> Optional[dict]:
        """Read the index. Non-strict (legacy callers): tolerate corruption
        by backing the file up and returning {} — objects survive. Strict:
        return None on corruption so destructive callers (gc) can refuse."""
        if not self.index_path.exists():
            return {}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            backup = self.index_path.with_suffix(".corrupt")
            if not backup.exists():
                shutil.copyfile(self.index_path, backup)
            if strict:
                return None
            # Corrupt index must never take the store down; objects survive.
            return {}

    def _write_index(self, idx: dict) -> None:
        """Atomic write under a lock, with a UNIQUE temp name so concurrent
        writers can never collide on a shared tmp path (A14)."""
        self.root.mkdir(parents=True, exist_ok=True)
        with _index_lock(self.root):
            fd, tmp = tempfile.mkstemp(
                dir=str(self.root), prefix=".index.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(idx, indent=1, sort_keys=True))
                os.replace(tmp, self.index_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def _index_add(self, ref: ArtifactRef) -> None:
        with _index_lock(self.root):
            idx = self._load_index()
            existing = idx.get(ref.sha256, {})
            if existing:
                # A3/A9: the bytes are immutable and so are the claims about
                # them. First-seen provenance wins in EVERY field; a later
                # put of identical bytes contributes nothing but a timestamp
                # check. This closes both directions of the takeover:
                #  - A3: later put overwriting non-empty data_refs / meta
                #  - A9: later put with a code argument adopting an entry
                #    that was stored (or index-rebuilt) without provenance —
                #    empty stays empty; absence is not an invitation.
                idx[ref.sha256] = dict(existing)
                self._write_index(idx)
                return
            merged = ref.to_dict()
            merged["created_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            idx[ref.sha256] = merged
            self._write_index(idx)

    def rebuild_index(self) -> int:
        """Scan all objects and rebuild metadata entries (kind inferred from
        sniffing, everything else unknown). Returns count.

        The objects are the evidence; the index is derived state. A rebuild
        cannot invent provenance it has no way to know, so entries are marked
        meta.reconstructed rather than silently presented as original.
        """
        idx = {}
        for obj in sorted(self.objects.rglob("*")):
            if not obj.is_file() or obj.name.startswith(".index."):
                continue
            data = obj.read_bytes()
            digest = sha256_bytes(data)
            if digest != obj.name:
                continue  # corrupt object: never index bytes that lie about their id
            idx[digest] = {
                "sha256": digest,
                "kind": _sniff_kind(data),
                "name": "",
                "code_sha256": "",
                "data_refs": [],
                "meta": {"reconstructed": True},
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(obj.stat().st_mtime)),
            }
        self._write_index(idx)
        return len(idx)


def _sniff_kind(data: bytes) -> str:
    """A12: kind from magic-prefix + structural checks, not substrings.

    A zip is not an xlsx (xlsx requires [Content_Types].xml); `<svg` inside
    HTML does not make the bytes SVG (an SVG document STARTS with the svg
    tag after optional whitespace/XML declaration). Misclassification feeds
    delivery suffixes and downstream interpretation, so ambiguity resolves
    to the more conservative kind."""
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.lstrip()[:1] == b"{" and data.rstrip().endswith(b"}"):
        return "json"
    if data.startswith(b"PK\x03\x04"):
        # xlsx IS a zip; a bare zip that lacks the OOXML content-types part
        # must not be labelled xlsx.
        try:
            import io
            import zipfile

            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
            if "[Content_Types].xml" in names:
                return "xlsx"
        except Exception:
            pass
        return "txt"
    head = data[:256].lstrip()
    if head.startswith(b"<?xml") and b"<svg" in data[:512]:
        return "svg"
    if head.startswith(b"<svg") or head.startswith(b"<!DOCTYPE svg"):
        return "svg"
    return "txt"


_default_store: Optional[ArtifactStore] = None


def default_store() -> ArtifactStore:
    global _default_store
    if _default_store is None:
        _default_store = ArtifactStore()
    return _default_store


def store_sandbox_outputs(
    result,  # tools.sandbox.SandboxResult
    store: Optional[ArtifactStore] = None,
    workspace: Optional[Path] = None,
) -> list[ArtifactRef]:
    """Seal every file produced by a sandbox run into the store.

    Each artifact records code_sha256 = hash of the producing code and
    stdout as a sibling 'txt' artifact, so a claim can cite
    (code, stdout, outputs) as one reproducible unit.
    """
    store = store or default_store()
    code_hash = sha256_bytes(result.code.encode("utf-8"))
    refs: list[ArtifactRef] = []
    stdout_ref = store.put_text(
        result.stdout,
        "txt",
        name="stdout",
        code_sha256=code_hash,
        meta={"status": result.status},
    )
    refs.append(stdout_ref)

    if workspace is not None:
        for f in result.files:
            p = Path(workspace) / f["name"]
            if p.exists():
                refs.append(store.put(
                    p.read_bytes(),
                    _sniff_kind(p.read_bytes()),
                    name=f["name"],
                    code_sha256=code_hash,
                    data_refs=[stdout_ref.sha256],
                ))
    elif result.status == "ok" and result.files:
        # RED TEAM A6: without workspace access the child's hashes are
        # CLAIMS, not evidence. We used to mint citable ArtifactRefs from
        # them (meta attested_by_child_only) — bytes nobody stored, cited by
        # sealed conclusions, verified by nothing. Policy: a ref may only
        # exist if its bytes are in the store. So the claim itself becomes
        # the stored artifact: one verifiable JSON record documenting what
        # the child reported, explicitly marked unfit for citation as
        # quantitative evidence.
        claim = {
            "type": "sandbox_attestation_claim",
            "status": result.status,
            "code_sha256": code_hash,
            "stdout_ref": stdout_ref.sha256,
            "files_reported_by_child": list(result.files),
            "citable_as_evidence": False,
            "note": ("hashes below were reported by the sandbox child; the "
                     "parent never observed these bytes. Kept as a record "
                     "of the claim, not as evidence."),
        }
        claim_ref = store.put_json(
            claim,
            name="attestation_claim",
            code_sha256=code_hash,
            data_refs=[stdout_ref.sha256],
        )
        # Self-check on the new invariant: every returned ref has stored bytes.
        for r in (*refs, claim_ref):
            if not store.exists(r.sha256):
                raise RuntimeError(
                    f"invariant violated: ref {r.name} has no bytes in store")
        refs.append(claim_ref)
    return refs
