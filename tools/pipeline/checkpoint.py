"""W3 — step-level checkpointing and resumability for the research pipeline.

Stolen deliberately from LangGraph: persist enough state after each pipeline
stage (decompose, per-leaf select/fetch/compute/answer, adversary, seal) that
a crashed run resumes from the last good step instead of throwing away the
decomposition, every fetch, and every synthesis that already succeeded.

Design contract (so engine.py can adopt it with a minimal diff — see
findings/instance_w3.md):

  1. CONTENT-ADDRESSED STEPS. A step key is
         sha256(run_key | stage | input_hash)
     where run_key binds the root question/domain/date and input_hash binds
     everything the stage reads. An unchanged step is a cache hit and is NOT
     re-executed — no duplicate fetches, ledger entries, or artifacts.

  2. RESUME SEMANTICS THAT DO NOT LIE. Every checkpoint stores the UTC time
     its payload was produced. A cache hit carries the ORIGINAL produced_at
     forward — evidence fetched an hour ago is labeled with that hour, never
     with the resume time. A RunTrace reports which steps were resumed and
     the oldest evidence timestamp, so the caller decides what staleness
     means.

  3. IDEMPOTENCE. Because a completed step short-circuits before execution,
     re-running cannot duplicate evidence or ledger entries. The one place a
     resumed run MUST touch the ledger again is replaying prior fetch bytes
     into a NEW ProvenanceLedger instance (the old process is gone);
     replay_ledger() does that exactly once per distinct content hash.

  4. SEALING ACROSS THE RESUME BOUNDARY. A seal covers a conclusion and its
     evidence, wherever each was produced. replay_ledger() restores the
     provenance facts (content hash -> primary bytes, urls) so
     ProvenanceLedger.assign_source_class keeps working for checkpointed
     evidence. Integrity is checked, not assumed: if a checkpointed fetch's
     body no longer matches its recorded hash, provenance_is_intact()
     reports False and seal_guard() says REFUSE. Resumption must never become
     a way to launder evidence whose provenance was lost — when we cannot
     guarantee provenance, we refuse to seal rather than seal something
     unverifiable.

  5. GARBAGE COLLECTION. gc(now, max_age_days) deletes stale checkpoints,
     but NEVER one whose claim_ids are still open. Openness is delegated to
     an injected callable so this module stays domain-general (any
     falsifiable claim, not just betting).

Storage is one JSON file per step under root/<run_key[:16]>/<key>.json —
human-inspectable, crash-safe (write-temp-rename), and trivially prunable.
No network, no database, no provider coupling.
"""
from __future__ import annotations

import hashlib
import json
from tools.retrodiction.cutoff import harness_key as _harness_key
import dataclasses
import hmac
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("callisto.pipeline.checkpoint")

UTC = timezone.utc


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


# ── Keys ───────────────────────────────────────────────────────────────────

def run_key(root_query: str, domain: str = "", today: str = "") -> str:
    """Identity of the RUN: same question, domain, and date -> same run."""
    return _sha("\x1f".join([root_query.strip(), domain.strip(), today]))


def step_key(rk: str, stage: str, input_hash: str) -> str:
    """Identity of one STEP within a run. Content-addressed: change any
    input and you get a different key (a miss), repeat it exactly and you
    get the same key (a hit)."""
    return _sha("\x1f".join([rk, stage.strip(), input_hash]))


def hash_inputs(inputs: Optional[dict]) -> str:
    """Canonical hash of a stage's inputs. Values must be JSON-serializable;
    anything else should be pre-hashed by the caller (e.g. pass a body
    digest, not the object)."""
    return _sha(json.dumps(inputs or {}, sort_keys=True,
                           separators=(",", ":"), default=str))


# ── Checkpoint record ──────────────────────────────────────────────────────

@dataclass
class Checkpoint:
    """One persisted stage output. payload is plain JSON-able data."""
    key: str
    run: str
    stage: str
    input_hash: str
    payload: dict = field(default_factory=dict)
    #: when the payload was PRODUCED (never updated on cache hits — this is
    #: the field that keeps resumed runs honest about evidence age).
    produced_at: str = ""
    #: ids of claims this checkpoint contributes evidence to (GC protection).
    claim_ids: list[str] = field(default_factory=list)
    #: HMAC over the record under the harness key. produced_at was a plain
    #: JSON field, so rewriting it to now() made 40-day-old evidence report
    #: age ~0 AND kept it permanently immune to gc() (red-team C4).
    sig: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key, "run": self.run, "stage": self.stage,
            "input_hash": self.input_hash, "payload": self.payload,
            "produced_at": self.produced_at, "claim_ids": self.claim_ids,
            "sig": self.sig,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        return cls(
            key=d["key"], run=d["run"], stage=d["stage"],
            input_hash=d["input_hash"], payload=d.get("payload") or {},
            produced_at=d.get("produced_at", ""),
            claim_ids=list(d.get("claim_ids") or []),
            sig=d.get("sig", ""))

    @property
    def signing_payload(self) -> str:
        """Canonical bytes the signature covers — INCLUDING produced_at."""
        body = json.dumps(self.payload, sort_keys=True, default=str)
        return "|".join([
            self.key, self.run, self.stage, self.input_hash,
            self.produced_at, ",".join(sorted(self.claim_ids)),
            hashlib.sha256(body.encode()).hexdigest()])

    def signed(self, key: str) -> "Checkpoint":
        return dataclasses.replace(self, sig=hmac.new(
            key.encode(), self.signing_payload.encode(),
            hashlib.sha256).hexdigest())

    def verify_signature(self, key: str) -> bool:
        if not self.sig or not key:
            return False
        expected = hmac.new(key.encode(), self.signing_payload.encode(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.sig, expected)

    def trusted_age_seconds(self, now: Optional[datetime] = None,
                            key: Optional[str] = None) -> float:
        """Age, but only when produced_at is AUTHENTIC.

        Under a keyed regime an unsigned or re-dated record has untrusted age,
        and untrusted age is treated as maximally old. Forged freshness must
        buy neither trust nor gc immunity — the whole point of the attack was
        that a rewritten produced_at did both. With no key configured this
        falls back to the raw claim, which is all an unkeyed regime can offer.
        """
        k = key if key is not None else _harness_key()
        if k and not self.verify_signature(k):
            return float("inf")
        return self.age_seconds(now)

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        if not self.produced_at:
            return float("inf")
        ref = (now or _now())
        return (ref - datetime.fromisoformat(self.produced_at)).total_seconds()


# ── Store ──────────────────────────────────────────────────────────────────

class FileCheckpointer:
    """JSON-file checkpoint store with GC that spares open claims."""

    def __init__(self, root: Optional[Path] = None,
                 is_claim_open: Optional[Callable[[str], bool]] = None):
        self.root = Path(root) if root else Path(
            os.environ.get("CALLISTO_STATE_DIR",
                           str(Path.home() / ".local" / "state"))) \
            / "callisto" / "checkpoints"
        # is_claim_open(claim_id) -> bool. Default: nothing is ever open
        # (pure-age GC). Production passes agp.claims' liveness check.
        self.is_claim_open = is_claim_open or (lambda cid: False)

    # -- paths --

    def _dir_for(self, rk: str) -> Path:
        d = self.root / rk[:16]
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, ckpt: Checkpoint) -> Path:
        return self._dir_for(ckpt.run) / f"{ckpt.stage}.{ckpt.key[:24]}.json"

    # -- core ops --

    def save(self, rk: str, stage: str, input_hash: str,
             payload: dict, *, claim_ids: Optional[list[str]] = None,
             produced_at: Optional[datetime] = None) -> Checkpoint:
        ck = Checkpoint(
            key=step_key(rk, stage, input_hash), run=rk, stage=stage,
            input_hash=input_hash, payload=payload,
            produced_at=(produced_at or _now()).isoformat(),
            claim_ids=list(claim_ids or []))
        _k = _harness_key()
        if _k:
            ck = ck.signed(_k)
        path = self._path(ck)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, suffix=".tmp")
        try:
            json.dump(ck.to_dict(), tmp, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, path)  # atomic; crash leaves no half file
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)
        return ck

    def load(self, rk: str, stage: str, input_hash: str) -> Optional[Checkpoint]:
        key = step_key(rk, stage, input_hash)
        return self.load_by_key(rk, key)

    def load_by_key(self, rk: str, key: str) -> Optional[Checkpoint]:
        d = self.root / rk[:16]
        if not d.is_dir():
            return None
        for p in sorted(d.glob(f"*.{key[:24]}.json")):
            try:
                return Checkpoint.from_dict(json.loads(p.read_text()))
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warning("unreadable checkpoint %s: %s", p, e)
                return None
        return None

    def list_all(self) -> list[Checkpoint]:
        out: list[Checkpoint] = []
        if not self.root.is_dir():
            return out
        for p in self.root.glob("*/*.json"):
            try:
                out.append(Checkpoint.from_dict(json.loads(p.read_text())))
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        return out

    # -- garbage collection --

    def gc(self, *, now: Optional[datetime] = None,
           max_age_days: float = 30.0) -> list[str]:
        """Delete checkpoints older than max_age_days — EXCEPT those whose
        claim_ids contain an open claim, which survive at any age."""
        cutoff = (now or _now()) - timedelta(days=max_age_days)
        removed: list[str] = []
        for ck in self.list_all():
            if any(self.is_claim_open(c) for c in ck.claim_ids):
                continue
            # Use the AUTHENTICATED age. Reading produced_at raw meant a
            # rewritten date reset the clock on every touch, so forged-fresh
            # evidence was immune to collection forever (red-team C4).
            # Untrusted age is infinite, so a forged record is collected, not
            # protected — the failure mode points at deletion, not retention.
            age_s = ck.trusted_age_seconds(now=(now or _now()))
            age_old = age_s > (max_age_days * 86400.0)
            if age_old:
                path = self._path(ck)
                try:
                    path.unlink(missing_ok=True)
                    removed.append(ck.key)
                except OSError as e:
                    logger.warning("gc could not remove %s: %s", path, e)
        # prune empty run dirs
        if self.root.is_dir():
            for d in self.root.iterdir():
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        return removed


# ── Checked stage execution (the thing engine.py wraps each stage with) ────

@dataclass
class StageOutcome:
    stage: str
    resumed: bool                       # True = served from checkpoint
    payload: dict
    produced_at: str                    # ORIGINAL production time even on hits


@dataclass
class RunTrace:
    """Honest record of which work was redone vs resumed."""
    run: str
    stages: list[StageOutcome] = field(default_factory=list)

    @property
    def resumed_stages(self) -> list[str]:
        return [s.stage for s in self.stages if s.resumed]

    @property
    def fresh_stages(self) -> list[str]:
        return [s.stage for s in self.stages if not s.resumed]

    @property
    def is_resume(self) -> bool:
        return bool(self.resumed_stages)

    def oldest_produced_at(self) -> Optional[str]:
        times = [s.produced_at for s in self.stages if s.produced_at]
        return min(times) if times else None


async def run_stage(
    cp: FileCheckpointer, trace: RunTrace, stage: str,
    inputs: Optional[dict],
    execute: Callable[[], Awaitable[dict]],
    *,
    claim_ids: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> StageOutcome:
    """Execute-or-reuse one pipeline stage.

    Cache hit  -> the stored payload is returned WITH ITS ORIGINAL
                  produced_at (stale evidence stays honestly stale) and the
                  execute callable is never invoked, so no ledger entry,
                  artifact, or fetch can be duplicated.
    Cache miss -> execute() runs, its dict payload is persisted, and the
                  checkpoint records THIS moment as produced_at.
    """
    ih = hash_inputs(inputs)
    hit = cp.load(trace.run, stage, ih)
    if hit is not None:
        oc = StageOutcome(stage=stage, resumed=True, payload=hit.payload,
                          produced_at=hit.produced_at)
    else:
        payload = await execute()
        saved = cp.save(trace.run, stage, ih, payload,
                        claim_ids=claim_ids, produced_at=now)
        oc = StageOutcome(stage=stage, resumed=False, payload=payload,
                          produced_at=saved.produced_at)
    trace.stages.append(oc)
    return oc


# ── Provenance across the resume boundary ──────────────────────────────────

def replay_ledger(ledger, checkpoints: list[Checkpoint]) -> dict:
    """Replay checkpointed FETCH records into *ledger* so source-class
    assignment works identically for resumed evidence.

    Each fetch checkpoint's payload is expected to carry:
      body           — the exact bytes/string returned by the source
      url            — where they came from
      content_sha256 — sha256 of body, recorded at fetch time

    Deduplication is intrinsic to the ledger's hash keying AND enforced here:
    a hash already replayed into this ledger instance is skipped, so calling
    replay twice (or resuming twice) cannot double-record.

    Returns {"replayed": n, "skipped_duplicates": n, "integrity_failures":
    [keys]} — a non-empty integrity_failures means a stored body no longer
    matches its recorded hash and the affected evidence must not be sealed.
    """
    # Dedup key lives ON THE LEDGER instance, so calling replay twice (or
    # resuming twice into the same ledger) cannot double-record observations.
    seen: set[str] = getattr(ledger, "_w3_replayed_hashes", None)
    if seen is None:
        seen = set()
        setattr(ledger, "_w3_replayed_hashes", seen)
    replayed = skipped = 0
    failures: list[str] = []
    for ck in checkpoints:
        for rec in ck.payload.get("fetches", []):
            body = rec.get("body", "")
            digest = rec.get("content_sha256") or ""
            # ABSENCE IS FAILURE. This used to read `if digest and ...`, so a
            # missing or empty content_sha256 skipped verification entirely and
            # the bytes were still replayed as primary=True — one absent JSON
            # field minted PRIMARY provenance for arbitrary fabricated bytes,
            # and seal_guard sealed over them (red-team C1). An unverifiable
            # record is exactly as untrustworthy as a mismatched one.
            # It also kept "" out of `seen`: with an empty digest the dedup key
            # was "", so a second DISTINCT record was dropped as a duplicate.
            if not digest:
                failures.append(ck.key)
                continue
            if _sha(body) != digest:
                failures.append(ck.key)
                continue
            if digest in seen:
                skipped += 1
                continue
            seen.add(digest)
            ledger.record_tool_result(
                rec.get("tool_name") or f"{rec.get('source_name', 'source')}_fetch",
                body, primary=bool(rec.get("primary", True)),
                urls=[rec["url"]] if rec.get("url") else None)
            replayed += 1
    return {"replayed": replayed, "skipped_duplicates": skipped,
            "integrity_failures": failures}


def _is_fetch_stage(stage: str) -> bool:
    """Stages whose checkpoints are REQUIRED to carry fetch records.

    Kept as one predicate so the rule cannot drift between call sites — the
    membership-rule bug in retrieval/why/base landed three separate times
    because the same test was reimplemented instead of shared.
    """
    return "fetch" in (stage or "")


def admissible_checkpoints(
    trace_run: str, checkpoints: list[Checkpoint],
    key: Optional[str] = None,
) -> list[Checkpoint]:
    """THE single definition of which checkpoints a run may consume.

    A checkpoint is ADMISSIBLE iff it belongs to THIS run AND its record is
    authenticated: under a keyed harness regime an unsigned or bad-HMAC
    checkpoint is inadmissible everywhere — it is never replayed into a
    ledger and never judged by seal_guard, because both consumers must see
    the SAME world for the guard's verdict to cover what the seal seals
    (red-team D3). With no key configured (unkeyed deployment) signature
    verification cannot run, so only the run-scope filter applies.

    This function is the ONE place the rule lives. engine.py's replay path
    and seal_guard() both call it; neither reimplements the predicate.
    """
    k = key if key is not None else _harness_key()
    out = []
    for ck in checkpoints:
        if ck.run != trace_run:
            continue
        if k and not ck.verify_signature(k):
            logger.warning("inadmissible checkpoint %s: signature fails", ck.key)
            continue
        out.append(ck)
    return out


def provenance_is_intact(ledger, checkpoints: list[Checkpoint]) -> bool:
    """True iff EVERY checkpointed fetch's bytes are provably in the ledger.

    This is the anti-laundering check: resumption may only contribute
    evidence whose provenance survived the boundary intact.
    """
    report = replay_ledger(ledger, checkpoints)
    if report["integrity_failures"]:
        return False
    for ck in checkpoints:
        # "NOTHING TO VERIFY" IS NOT "VERIFIED". A fetch-stage checkpoint
        # always writes a `fetches` key (see engine.py's fetch payload). Its
        # ABSENCE means the payload was restructured — by tampering or by a
        # schema change — and the guard used to read that as intact, so a
        # resumed run could seal with zero verified provenance (red-team C3).
        # Stages that never fetch (decompose, answer_leaf) are unaffected.
        if _is_fetch_stage(ck.stage) and "fetches" not in ck.payload:
            return False
        for rec in ck.payload.get("fetches", []):
            body = rec.get("body", "")
            if not ledger.has_observation(body):
                return False
    return True


class _ScratchLedger:
    """Write-only sink for provenance CHECKS.

    provenance_is_intact works by replaying records and seeing whether they
    verify. On a resumed run that replay belongs in the real ledger — that is
    how resumed evidence earns its source class. On a fresh run it does not:
    there the guard is only asking a question, and answering it must not
    change the answer.
    """

    def __init__(self):
        self._bodies: set[str] = set()

    def record_tool_result(self, tool, body, primary=True, urls=None):
        # It must really STORE: provenance_is_intact replays, then asks
        # has_observation() for each body. A sink that drops writes would make
        # the check answer False for everything and turn a purity fix into a
        # blanket REFUSE.
        self._bodies.add(body)

    def has_observation(self, body) -> bool:
        return body in self._bodies


def seal_guard(
    trace: RunTrace, checkpoints: list[Checkpoint], ledger,
) -> tuple[str, str]:
    """Decide whether a possibly-resumed run may seal.

    Returns ("SEAL", "") or ("REFUSE", reason). Fresh runs are unaffected.
    A resumed run may only seal when every checkpointed piece of evidence
    has verifiable provenance in (replayed) ledger. If we cannot guarantee
    that, we refuse — sealing something unverifiable is worse than redoing
    the work.
    """
    # SCOPE + AUTHENTICATE VIA THE SHARED PREDICATE. The engine passes
    # cp.list_all() — every checkpoint ever written, by every run. Both the
    # guard and the ledger-replay path must consume the SAME admissible set,
    # or the guard reasons over a world the seal does not cover (red-team
    # D3). admissible_checkpoints() is the one definition: run-scope filter
    # (red-team C2) plus signature verification (red-team D1) in ONE place.
    checkpoints = admissible_checkpoints(trace.run, checkpoints)

    if not trace.is_resume:
        # Even fresh runs must not seal over checkpointed evidence whose
        # integrity fails — the guard is about the EVIDENCE, not the label.
        # CHECK ONLY: verify against a scratch ledger. This branch used to
        # replay into the real one, so merely *checking* a fresh run laundered
        # the bytes in, and the guard then returned SEAL over evidence it had
        # itself just admitted. A check must not mutate what it checks.
        if checkpoints and not provenance_is_intact(_ScratchLedger(),
                                                    checkpoints):
            return "REFUSE", (
                "checkpointed evidence provenance could not be verified "
                "against the ledger; refusing to seal")
        return "SEAL", ""
    if not provenance_is_intact(ledger, checkpoints):
        failed = sum(len(ck.payload.get("fetches", [])) for ck in checkpoints)
        return "REFUSE", (
            "resumed run: checkpointed evidence provenance could not be "
            "verified against the ledger; refusing to seal rather than "
            "laundering evidence across the resume boundary "
            f"(checkpointed fetch records: {failed})")
    return "SEAL", ""


# ── Crash simulation helper (tests + ops drills) ──────────────────────────

class Crash(Exception):
    """Simulated mid-run death at a named stage."""


async def run_pipeline_checked(cp: FileCheckpointer, rk: str, stages: list,
                               *, claim_ids_for=None) -> tuple[RunTrace, dict]:
    """Generic checked runner mirroring the engine's stage sequence.

    stages: list of (name, inputs_dict_or_callable, async_execute_fn). A
    stage whose execute raises propagates — everything earlier is already
    checkpointed, and re-calling this with the same stages resumes from the
    failed one. Returns the trace plus a merged payload dict keyed by stage
    name.
    """
    trace = RunTrace(run=rk)
    merged: dict[str, dict] = {}
    for name, inputs, fn in stages:
        ins = inputs() if callable(inputs) else inputs
        oc = await run_stage(
            cp, trace, name, ins, fn,
            claim_ids=(claim_ids_for or (lambda s: []))(name))
        merged[name] = oc.payload
    return trace, merged
