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
import os
import shutil
import tempfile
import fcntl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ALLOWED_KINDS = {"csv", "json", "xlsx", "png", "svg", "txt", "ipynb", "pkl", "md"}

# Per-store-root index lock. In-process (threads) plus a lock file so
# independent processes sharing a store root also serialize (A14). The
# lockfile lives outside objects/ so gc/rebuild never see it as an object.
_locks_guard = threading.Lock()
_locks: dict[str, "threading.Lock"] = {}


class ArtifactIndexCorrupt(RuntimeError):
    """Raised by destructive operations (gc) when the index is unreadable."""


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
        """Re-hash every referenced artifact. Used before sealing and after
        any restore; a single mismatch means tampering or corruption."""
        report = {"verified": 0, "missing": [], "corrupt": []}
        for ref in refs:
            if not self.exists(ref.sha256):
                report["missing"].append(ref.sha256)
                continue
            actual = sha256_bytes(self.get_bytes(ref.sha256))
            if actual != ref.sha256:
                report["corrupt"].append(ref.sha256)
            else:
                report["verified"] += 1
        report["ok"] = not report["missing"] and not report["corrupt"]
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
            if not allow_rebuild:
                raise ArtifactIndexCorrupt(
                    f"index at {self.index_path} is corrupt or unreadable; "
                    "gc() refused to delete anything. Repair the index "
                    "(rebuild_index()) and retry."
                )
            self.rebuild_index()
            idx = self._load_index(strict=True) or {}
        removed = []
        for obj in self.objects.rglob("*"):
            if obj.is_file() and obj.name not in idx:
                obj.unlink()
                removed.append(obj.name)
        return removed

    # -- persistence of refs ----------------------------------------------

    def export_ref(self, ref: ArtifactRef, dest_dir: Path) -> Path:
        """Copy an artifact to a human-accessible path (delivery surface)."""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{ref.kind}" if ref.kind else ""
        base = ref.name or ref.sha256[:12]
        dest = dest_dir / f"{base}{suffix}"
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
            merged = ref.to_dict()
            merged["created_at"] = existing.get("created_at") or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            # First-seen provenance wins: an artifact's origin does not change
            # because someone later re-put identical bytes.
            if existing:
                for key in ("code_sha256", "name"):
                    if not (existing.get(key) or ""):
                        continue
                    merged[key] = existing[key]
                if existing.get("data_refs") and not merged["data_refs"]:
                    merged["data_refs"] = existing["data_refs"]
                merged["created_at"] = existing.get("created_at")
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
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.lstrip()[:1] == b"{" and data.rstrip().endswith(b"}"):
        return "json"
    if data.startswith(b"PK\x03\x04"):
        return "xlsx"
    if b"<svg" in data[:512]:
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
    elif result.status == "ok":
        # Without workspace access we can only attest what the child hashed.
        for f in result.files:
            ext = Path(f["name"]).suffix.lstrip(".").lower()
            kind = ext if ext in ALLOWED_KINDS else "txt"
            refs.append(ArtifactRef(
                sha256=f["sha256"],
                kind=kind,
                name=f["name"],
                code_sha256=code_hash,
                data_refs=[stdout_ref.sha256],
                meta={"attested_by_child_only": True},
            ))
    return refs
