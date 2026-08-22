# carpool

Formerly `ai-lanes`.

carpool runs agentic work across several Claude Code and Codex subscriptions
without repeatedly replacing one login. Each subscription gets a lane, the
router selects a lane with usable capacity, and the hardened runners can move
to another lane when a provider reports a limit.

The Python package has no runtime dependencies outside the standard library.
The shell runners expect Python 3.12 or newer; the Claude runner also uses
`jq` and `uuidgen`. The default secret store is the macOS keychain, but a
portable secret-store command can be configured.

## Quick start

```bash
uv sync
mkdir -p ~/.config/carpool
cp accounts.example.json ~/.config/carpool/accounts.json
export PATH="$PWD/bin:$PATH"
carpool status
```

Edit `~/.config/carpool/accounts.json` before enrolling or dispatching. The
committed [accounts.example.json](accounts.example.json) contains only reserved
example addresses and documents the supported public settings.

## Lanes

Codex keeps one login per home directory:

- `~/.codex` is the desktop app's home. carpool observes its account and live
  capacity, but never treats it as a dispatch lane.
- `~/.codex-1`, `~/.codex-2`, and so on are dispatch lanes. They are discovered
  automatically unless `codex_homes` or `CARPOOL_CODEX_HOMES` supplies an
  explicit list.
- A numbered lane bound to the desktop app's current account is *shadowed*.
  It remains visible but receives a dispatch handicap, and the watchdog warns
  only when the shadow state changes.
- A Codex account reporting the free plan is excluded from dispatch.

Log into a numbered lane or the separately observed app home with:

```bash
carpool login codex 1
carpool login codex app
```

The command starts the vendor login server in that home, opens its authorization
URL, and arms a detached watcher. On completion, the watcher checks that
numbered lanes contain distinct accounts, reports app shadows, refreshes the
snapshot, and uses the configured notification hook. `--no-open` prints the
URL; `--no-watch` skips the completion watcher.

Claude lanes are the addresses in `accounts`, mapped to secret-store items by
`enrolled`. Create and store a setup token without placing it in argv or an
environment variable:

```bash
claude setup-token | carpool enroll user1@example.com
```

Enrollment makes a one-time validation request and accepts the authorization
responses expected from an inference-only setup token. Routine status and
dispatch never use stored setup tokens as quota-observation credentials.
Instead, the Claude runner records transcript token totals in a private local
ledger and learns cooldowns from actual hard-limit responses.

## Commands

| Command | Purpose |
| --- | --- |
| `carpool status [--json] [--cached]` | Show account identity, quota, authentication, shadow, and health state. With no subcommand, `status` is the default. |
| `carpool capacity [--json]` | Normalize five-hour and weekly headroom across both providers. |
| `carpool pick codex` | Print the best dispatchable Codex home. |
| `carpool pick claude` | Print the best enrolled Claude address. |
| `carpool run` | Classify a task, select a provider and lane, and invoke a hardened runner. |
| `carpool codex` | Run hardened `codex exec`; `-H` is optional and omitted lanes are selected automatically. |
| `carpool claude` | Run hardened headless Claude Code with `-A` auto-selection or `-a EMAIL` pinning. |
| `carpool login codex N\|app` | Perform the Codex re-login ritual for one numbered lane or the app home. |
| `carpool enroll EMAIL` | Read a Claude setup token from stdin and store it through the secret-store abstraction. |
| `carpool mirror` | Run one Claude desktop-session mirror pass; accepts `--list`, `--dry-run`, `--prune`, and the mirror's other options. |
| `carpool errors` | Show recently observed provider limit and authentication errors. |
| `carpool watch [--dry-run]` | Take one snapshot, evaluate health transitions, and send or print alerts. |
| `carpool brief` | Print a compact Markdown capacity section. |
| `carpool runs` | Inspect the durable prompt/output/error ledger; `runs show ID` displays one run. |

Use `carpool COMMAND --help` for monitor and router options. The pass-through
runners intentionally retain their concise shell usage strings.

## Dispatch

The router accepts prompt text or `-p PROMPTFILE` and can explain its decision:

```bash
carpool run --why -C /path/to/project -o result.md "Fix the failing retry test"
carpool run --dry-run -p task.md
```

Its transparent pattern rules classify prose and final-judgment work, reviews,
mechanical sweeps, and general build work. It chooses a model family, consults
the shared capacity view, records the decision, and can cross from Codex to
Claude when the default family has no dispatchable lane. Explicit `-H` and
`-a` options pin a resource. `-m`, `-t`, `-s`, `-d`, and `-b` provide model,
task-class, sandbox, detached-run, and salvage-branch overrides.

The provider runners are also available directly:

```bash
carpool codex -m MODEL_NAME -C "$PWD" -p task.md -o result.md
carpool claude -A -m MODEL_NAME -C "$PWD" -p task.md -o result.md
carpool claude -a user1@example.com -m MODEL_NAME -C "$PWD" -p task.md -o result.md
```

An unpinned Codex run auto-picks a home and re-picks after a mid-run usage
limit. An auto-selected Claude run does the same after a hard limit; lanes with
a missing stored token are excluded before launch. Both runners retry transient
failures, record each attempt, and can
snapshot dirty Git state to dedicated salvage refs without moving `HEAD` or
the real index. Claude additionally records lane usage and writes a
`MODEL-DOWNGRADE` marker when the transcript shows silent model substitution.

Adding this repository's `bin` directory before the vendor Codex binary also
enables the `bin/codex` shim. It selects a lane for headless `exec`, `e`, and
`review` calls when `CODEX_HOME` is unset. Set `CARPOOL_NO_AUTOPICK=1` to opt
out; interactive commands and explicit homes pass through unchanged. carpool
resolves the real Codex executable without depending on scheduler `PATH`.

## Monitoring and repair

`carpool watch` is a single pass suitable for cron or another scheduler. It
persists snapshots and transition state under `~/.local/state/carpool` by
default. Alerts cover exhausted or unauthenticated lanes, free-plan accounts,
new app shadows, fleet exhaustion, and mirror health without repeating an
unchanged condition every pass.

The session mirror writes its per-pass result to the configured heartbeat
sidecar. The watchdog tolerates a currently running pass, distinguishes a
stale heartbeat from an in-flight run, and treats a run older than 30 minutes
as hung. When configured, `mirror_restart_cmd` is included in the alert's
recovery guidance.

For a Codex lane whose live quota request returns the narrow expired-token
signature, the watchdog may ask the vendor CLI to refresh its own credentials
and then re-probe. Repair is guarded by a renewable lock lease, bounded by a
timeout, disabled during dry runs, and latched after definitive revocation
until a new login changes the authentication file. carpool never writes a
Codex access token itself.

## Configuration

The default configuration file is `~/.config/carpool/accounts.json`. Important
keys are:

- `accounts` and `enrolled`: the Claude roster and its secret item names.
- `codex_homes`: `null` for numeric-home discovery, or an explicit list.
- `codex_app_home`: the observed desktop-app home.
- `protected_account`: an optional identity fallback used only when the app
  home cannot provide its current account.
- `secret_store_cmd`: an argv prefix implementing `get NAME`, `set NAME` with
  the value on stdin, and `del NAME`. When unset, carpool uses macOS `security`.
- `notify_cmd`: an argv prefix receiving `SUBJECT BODY`; the complete message
  is also sent on stdin. When unset, alerts go to stderr.
- `mirror_heartbeat`, `mirror_job_label`, and
  `mirror_restart_cmd`: optional mirror and scheduler integration.
- `login_browser_cmd`: a browser argv prefix. A `{url}` placeholder is
  replaced; otherwise the URL is appended.
- `login_refresh_cmd`: a snapshot-refresh argv command run after login.

Command settings may be JSON argv arrays or shell-like strings parsed into
argv; they are never executed as shell expressions.

Common environment overrides are `CARPOOL_CONFIG_DIR`, `CARPOOL_STATE_DIR`,
`CARPOOL_CODEX_HOMES` (colon-separated on macOS/Linux),
`CARPOOL_CODEX_APP_HOME`, `CARPOOL_CODEX_BIN`, `CARPOOL_CLAUDE_DIR`,
`CARPOOL_CLAUDE_JSON`, `CARPOOL_SECRET_NAME_PREFIX`,
`CARPOOL_SECRET_STORE_CMD`, `CARPOOL_NOTIFY_CMD`,
`CARPOOL_MIRROR_HEARTBEAT`,
`CARPOOL_MIRROR_JOB_LABEL`, `CARPOOL_MIRROR_RESTART_CMD`,
`CARPOOL_LOGIN_BROWSER_CMD`, `CARPOOL_LOGIN_REFRESH_CMD`, and
`CARPOOL_CODEX_GUARD=off` for an explicit one-run guard bypass.

carpool does not embed secrets or local account identifiers in repository
files. Runtime ledgers are created in the configured state directory with
private permissions; runner output is written only to the path you request.

## Codex command guard

`carpool codex` preflights a portable Codex `PreToolUse` hook before launch.
The hook covers four generic local-machine and Git hazards: unscoped root
searches, decrypted keychain dumps, stash mutation in shared worktrees, and
new local branches made from a stale local main branch. See
[docs/guard.md](docs/guard.md) for the exact policies, trust checks, and the
explicit one-run bypass.

## Development

```bash
uv run pytest -q
```

## License

Apache-2.0. See [LICENSE](LICENSE).
