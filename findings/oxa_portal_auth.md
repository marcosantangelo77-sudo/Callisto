# Why Ox Alpha was not working on the cloud runner

**Date:** 2026-08-26
**Tag:** VERIFIED against this VM and current source.

## What ChatGPT had

On the workstation (`/Users/marcosantangelo/Documents/ChatGPT/callisto`):

1. Hermes was already installed at `~/.hermes/bin/hermes`.
2. `hermes portal login` had already stored a Nous OAuth refresh token in
   `~/.hermes/auth.json` (macOS keychain / Hermes auth store).
3. Workers were launched with `--provider nous -m stealth/ox-alpha` via
   `~/callisto-wt/nous-supervisor.sh` (not in this git tree at the time).
4. Rate limits were not the problem. Auth was already done.

`HANDOFF.md` even says the check: `hermes portal info` must show **logged in**,
model `stealth/ox-alpha`, API `inference-api.nousresearch.com`.

## What this cloud VM had

| Check | Result |
|---|---|
| Hermes CLI installed | yes (`~/.local/bin/hermes`) |
| `hermes portal info` | **Auth: not logged in** |
| `~/.hermes/auth.json` | Copilot `gh auth token` only. `providers.nous` absent |
| `NOUS_API_KEY` / Portal env | unset |
| Callisto API | not running |
| `ProviderRouter.check_health("ox_alpha")` | **false green** — treated "binary exists" as healthy |

So `hermes -z --provider nous -m stealth/ox-alpha` failed immediately:

```
hermes -z: agent failed: Hermes is not logged into Nous Portal.
```

Ox Alpha is not broken. Nous is not rate-limiting us. **This process has no Portal session.** ChatGPT never had to log in because the workstation already was.

## The health-check lie (fixed)

`inference.ProviderRouter.check_health` for `backend=hermes_cli` used to return
`ok` iff `hermes_available()` — binary on PATH. A fresh clone + CLI install
looked healthy and then every completion died at auth.

Now it also requires `hermes_logged_in()` (Nous credential present in the
auth store; no token is logged). Tests in `tests/test_oxa_portal_auth.py`.

## How to log in (headless / cloud)

Device-code OAuth — no SSH tunnel required:

```bash
hermes auth add nous --type oauth --no-browser
```

Hermes prints a `portal.nousresearch.com/manage-subscription?user_code=…` URL.
Approve it in any browser on the same Nous account that has the Ox Alpha
free-week grant. Then:

```bash
hermes config set model.provider nous
python3 scripts/oxa_status.py          # exit 0
hermes -z PONG --provider nous -m stealth/ox-alpha --in /tmp
```

Do not commit `auth.json`, refresh tokens, or API keys.

## Live verification (2026-08-26, this cloud VM)

After the operator approved device-code `C49Q-7VGP`:

| Check | Result |
|---|---|
| `hermes portal info` | Auth: ✓ logged in, API `inference-api.nousresearch.com/v1` |
| `python3 scripts/oxa_status.py` | exit 0, `nous_logged_in=True` |
| `ProviderRouter.check_health("ox_alpha")` | `{"status": "ok"}` |
| `hermes -z PONG --provider nous -m stealth/ox-alpha` | `PONG` in ~9s |
| `hermes -z` file-write probe | wrote `/tmp/oxa_write_probe.txt` (`OK`) — `-z` **does** have tools |
| `scripts/nous-supervisor.sh` | launches, exit 0; first probe wrote `$HOME/OX_OK.txt` instead of the worktree — supervisor now prefixes a cwd mandate and passes `--no-restore-cwd --yolo` |

## How to launch a worker after login

```bash
bash scripts/nous-supervisor.sh <task-name> <worktree> <prompt-file> 180
```

Foreground PTY. Cap 3 concurrent Hermes processes. Refuses `master`. Refuses
to start when Portal is logged out.

## What is still blocked until login succeeds

Dispatching OX implementation workers. The loop refactor and the handoff
candidate reviews wait on a green `scripts/oxa_status.py`.
