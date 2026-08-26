# What would actually make Callisto production-ready

**Date:** 2026-08-26
**Author:** orchestrator (not OX). Evidence from the 2026-08-26 brutal audit,
re-read against `master` @ `245d9f6` / last reviewed code `4c79807`.
**Operator note:** the 29/100 score was generous. This file treats the product
as **below 29** until it has a shipping model and fail-closed invariants.
**Disposition:** direction document. Code fixes are routed to OX Alpha
(wave 1 live, wave 2 queued). Do not treat this file as a license to rewrite
the kernel in one PR.

---

## 1. The honest diagnosis

Callisto is not 29% of a product. It is a **laboratory** that accumulated
three incompatible identities and never chose one:

| Identity | Where it lives | What it assumes |
| --- | --- | --- |
| Local research harness | `READme.md`, AGP, `callisto.py`, `ProviderRouter` | One operator, local models, sealed sessions, electricity as cost |
| Unattended sportsbook | `start.bat`, Telegram `/resume_all`, `BetExecutor`, `status='live'` | Network-facing API, money switches, 24/7 loop |
| Self-improving loop | `tools/autonomous.py` (8148 lines) | The process may rewrite its own evidence to keep moving |

Those three fight. A harness wants loopback, paper, and fail-closed seals.
A sportsbook wants bind-all, executor-on, and promotion to live. A
self-improving loop wants to lower `edge_threshold` and flip
`signal_generated` so the next cycle has something to promote.

The bones are real: hypothesis → backtest → gate → paper, confidence tiers
in the schema, a research loop that actually cycles, a provider ladder.
The direction is not. That is why the audit felt accidentally useful — it
described a system that does not know what it is for.

Sports betting is the **proving ground**, not the product (`AUDIT_MANDATE.md`,
`BUILD_MANDATE.md`). Ground truth arrives in hours, so calibration can be
measured. The intended product is: sit at a workstation, ask anything, get
back models, data, graphs, and math that can be re-run. Own the means of
production. A public sportsbook SaaS is the wrong destination.

---

## 2. How you ship this (harness, not website)

### v1 is a downloadable local harness

Ship a **single-operator appliance**: git clone / `uv` / a future installer,
then a CLI and an optional loopback UI talking to a process on the same
machine.

```text
callisto init          # local sqlite, generate CALLISTO_SEAL_KEY, bind 127.0.0.1
callisto research "…"  # AGP pipeline, sealed session, artifacts on disk
callisto loop          # paper-only ResearchLoop (hypothesis → backtest → gate)
callisto serve         # FastAPI on 127.0.0.1:8420, no 0.0.0.0
```

The trust boundary is the operator's machine. That matches the security
model that already exists (loopback-or-token, sqlite single-writer, Hermes
on the same host). It also matches the compute model: local models do
volume, a frontier model (Nous/OX, Claude, OpenRouter) does hard judgments.

### Do not ship a website first

A public website or multi-tenant SaaS would require a different kernel:

- real authn/authz, not "token if set, else allow on loopback"
- per-user isolation (the ResearchLoop is process-global)
- a database that is not one sqlite writer
- no live-adjacent betting code on the same process
- seals that fail closed (today keyed `verify_seal` still accepts public SHA-256)
- no FastAPI event loop blocked by 5000×365 Monte Carlo

Putting a login page in front of this API would not make it a product. It
would put other people's questions on a process that can rewrite evidence
and, if anyone "fixes" `generate_paper_trade_signal`, place bets.

A website can exist **later**, as a thin viewer: pull sealed artifacts from
a local kernel, or a "bring your own kernel" dashboard. It must not become
the kernel.

### What "downloadable" means in practice

Not an Electron wrapper around 135k lines. A **harness**:

1. One entrypoint (`callisto.py` / a console script) that does not require
   `start.bat` lore.
2. Paper-only money path. Live executor, Telegram arming, `OrderManager`
   default-on — attic or explicit `callisto arm --i-understand`.
3. One inference control plane (`ProviderRouter` + `providers.yaml`). The
   research loop's private `MODEL_LADDER` is a second product hiding inside
   the first.
4. A research loop that can run unattended **without mutating history**.
5. Sealed sessions on disk the operator can copy, hash, and re-verify.

Hermes + Nous Portal is a valid inference backend for that harness. It is
not the product.

---

## 3. Production-ready means fail-closed, not feature-complete

A production harness is one that can run overnight without lying and
without spending money. Feature breadth (Kalshi plugins, pace models,
Telegram) is optional. These are not:

### Invariants (ship blockers)

| # | Invariant | Current state (verified) | Owner |
| --- | --- | --- | --- |
| 1 | Launchers bind loopback unless the operator overrides | `api.py` defaults `127.0.0.1`; `start.bat` and `overnight_setup.py` pass `0.0.0.0` | OX wave 2 bind |
| 2 | Sensitive GETs are gated (`/system/full-status`, `/hypothesis/{id}`, `/bets`, `/executor/status`) | Ungated | later wave; do not expand API surface until then |
| 3 | When `CALLISTO_SEAL_KEY` is set, unkeyed SHA-256 must not verify | `verify_seal` still appends public SHA-256; test **pins the hole** | OX wave 2 seal |
| 4 | Automated actors must not lower `edge_threshold` or rewrite `signal_generated` | `auto_promote` writes both; `_phase_refresh_signals` upgrades 0→1 every cycle | OX wave 1 (live) |
| 5 | Chat `/resume_all` must not enable `bet_executor`; order manager defaults disabled | Opposite today | OX wave 2 telegram |
| 6 | Sync Monte Carlo / sqlite regime / health-file IO must not run on the event loop | No `asyncio.to_thread` in `api.py` | OX wave 1 (live) |
| 7 | Phase failures are recorded, not only logged | `except Exception: continue` | OX wave 1 (live) |
| 8 | `generate_paper_trade_signal` stays `paper_trading`-only | Accidental safety; **do not "fix"** | freeze |
| 9 | One Kelly, one router | `kelly.py` vs `sizing.py`; ladder vs `providers.yaml` | later, after safety |
| 10 | Live collection remains opt-in and unarmed | Telegram + `OrderManager._enabled=True` + executor default on | wave 2 + attic |

Until 1–8 hold, "production-ready" is marketing. The loop can look busy
while manufacturing signals and blocking `/health`.

### What production-ready does **not** mean

- Multi-tenant SaaS
- A public odds website
- Splitting `autonomous.py` in one PR (land incrementally; quarantine, never delete)
- Arming live betting so the proving ground "works"
- Merging unreviewed candidates (`dbcc751`, `1ec9778`) because a worker said tests passed

---

## 4. The product, named in one paragraph

**Callisto v1** is a local deep-research harness with a paper-trading
calibration loop. You install it on a machine you own. You ask it
questions. It returns sealed sessions: claims, evidence bytes, charts,
code. In parallel, a research loop proposes hypotheses, backtests them
on a stratified window, and promotes only what survives gates — into
**paper**, never into live, until a human arms a separate, default-off
executor. Sports markets are the gym. The sport is not the company.

That is shippable. Everything else is a later skin.

---

## 5. Staged path (propose freely, land incrementally)

`AUDIT_MANDATE.md` rule 3 still applies. No big-bang rewrite.

### Stage A — stop the bleeding (current OX waves)

Disjoint file ownership, host cap 3 Hermes processes:

1. Gate `_phase_refresh_signals`; record phase failures (`autonomous.py`)
2. `auto_promote` diagnose-only (`hypothesis.py`)
3. Event-loop offload (`api.py`)
4. Loopback launchers (`start.bat`, `overnight_setup.py`)
5. Telegram / order-manager money switch
6. Keyed seal fail-closed + invert the pinning test

Independent adversarial review before any squash to master. Never merge
because OX said pytest passed.

### Stage B — choose the harness identity

- CLI as the front door; FastAPI as a local control plane, not the product.
- Attic: live execute path, Telegram arming, `start.bat` bind-all lore.
  Restore notes, not `rm`.
- Paper-only default in `ResearchLoop`. Keep the accidental
  `generate_paper_trade_signal` status check as a hard gate; add a test that
  fails if anyone widens it to `live`.
- Generate `CALLISTO_SEAL_KEY` at `callisto init`. Refuse to seal as if
  keyed when the key is missing (operator-visible warning, not silent
  unkeyed).

### Stage C — one brain

- Point the research loop at `ProviderRouter` / `providers.yaml`. Delete
  the duplicate `MODEL_LADDER` once Hermes latency is acceptable (or keep
  a local proxy). Do not freeze the loop on 14s `hermes -z` forks without
  measuring.
- One Kelly implementation. The other becomes a wrapper or attic.

### Stage D — extract the loop without a rewrite

Carve `ResearchLoop` into importable packages **behind the same class**:
phases as modules, `_loop` as a sequencer, evidence writes going through
one journal that cannot UPDATE historical `signal_generated`.
`autonomous.py` shrinks by move-to-package, not by a 8k-line PR.

### Stage E — optional local UI

A loopback viewer for sealed sessions, hypothesis status, and paper PnL.
No cloud account. No 0.0.0.0. If a website ever exists, it reads exported
sealed artifacts.

---

## 6. Why the architecture audit was the right move

The structural flaws are not style nits. They are the reason a harness
cannot be shipped:

- **Evidence is not append-only.** Threshold saws and signal refresh mean
  a backtest is not a frozen experiment. Calibration against that is
  self-dealing.
- **The API is a workstation tool pretending to be a server.** Bind-all
  plus ungated GETs plus a blocking event loop is how a local app becomes
  an incident.
- **Seals are documentation unless keyed-and-exclusive.** An unkeyed
  SHA-256 the verifier still accepts is a checksum, not a seal. The test
  that requires the hole is worse than no test.
- **Money switches are scattered.** HTTP admin, Telegram chat, constructor
  defaults. Production software has one arming surface.
- **God modules hide the product.** The important subsystem is
  `ResearchLoop`. It is buried in 8148 lines next to scanners, Telegram
  implications, and live execute that does not execute.

Fix those, and the bones become a harness. Skip them and add a website,
and you will ship a confused sportsbook with a research aesthetic.

---

## 7. Scoring, restated

| Dimension | Why it is low |
| --- | --- |
| Direction | Three products in one tree; README says local AGP, launchers say bind-all betting server |
| Safety | Accidental fail-safe on live signals; intentional fail-open on seals, bind, Telegram, order manager |
| Evidence | Loop and auto_promote may rewrite the series they are judged on |
| Operability | Event loop can freeze on `/simulate/portfolio` and `/health` |
| Ship shape | No installer identity, no paper-only default product, no single router |

A fair score today is **teens**, not 29. The path to something you could
hand a second operator is Stage A + Stage B: fail-closed local harness,
paper loop, sealed research. That is a product. The rest is inventory.
