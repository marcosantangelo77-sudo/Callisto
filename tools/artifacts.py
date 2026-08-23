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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ALLOWED_KINDS = {"csv", "json", "xlsx", "png", "svg", "txt", "ipynb", "pkl", "md"}


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

    def gc(self) -> list[str]:
        """Remove objects absent from the index (orphans only — the index is
        the reachability root alongside live refs)."""
        removed = []
        idx = self._load_index()
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

    def _load_index(self) -> dict:
        if not self.index_path.exists():
            return {}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Corrupt index must never take the store down; objects survive.
            backup = self.index_path.with_suffix(".corrupt")
            shutil.copyfile(self.index_path, backup)
            return {}

    def _index_add(self, ref: ArtifactRef) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
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
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def rebuild_index(self) -> int:
        """Scan all objects and rebuild metadata entries (kind inferred from
        sniffing, everything else unknown). Returns count."""
        idx = {}
        for obj in sorted(self.objects.rglob("*")):
            if not obj.is_file():
                continue
            data = obj.read_bytes()
            digest = sha256_bytes(data)
            idx[digest] = {
                "sha256": digest,
                "kind": _sniff_kind(data),
                "name": "",
                "code_sha256": "",
                "data_refs": [],
                "meta": {},
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(obj.stat().st_mtime)),
            }
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.index_path)
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
