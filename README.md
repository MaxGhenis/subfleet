# subfleet

A fleet of subs. Formerly `ai-lanes`, then `carpool` (renamed 2026-08-23;
`CARPOOL_*` environment variables are still honoured).

subfleet runs agentic work across several Claude Code and Codex subscriptions
without repeatedly replacing one login — the subs operate under the surface. Each subscription gets a lane, the
router selects a lane with usable capacity, and the hardened runners can move
to another lane when a provider reports a limit.

The Python package has no runtime dependencies outside the standard library.
The shell runners expect Python 3.12 or newer; the Claude runner also uses
`jq` and `uuidgen`. The default secret store is the macOS keychain, but a
portable secret-store command can be configured.

## Quick start

```bash
uv sync
mkdir -p ~/.config/subfleet
cp accounts.example.json ~/.config/subfleet/accounts.json
export PATH="$PWD/bin:$PATH"
subfleet status
```

Edit `~/.config/subfleet/accounts.json` before enrolling or dispatching. The
committed [accounts.example.json](accounts.example.json) contains only reserved
example addresses and documents the supported public settings.

## Lanes

Codex keeps one login per home directory:

- `~/.codex` is the desktop app's home. subfleet observes its account and live
  capacity, but never treats it as a dispatch lane.
- `~/.codex-1`, `~/.codex-2`, and so on are dispatch lanes. They are discovered
  automatically unless `codex_homes` or `SUBFLEET_CODEX_HOMES` supplies an
  explicit list.
- A numbered lane bound to the desktop app's current account is *shadowed*.
  It remains visible but receives a dispatch handicap, and the watchdog warns
  only when the shadow state changes.
- A Codex account reporting the free plan is excluded from dispatch.

Log into a numbered lane or the separately observed app home with:

```bash
subfleet login codex 1
subfleet login codex app
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
claude setup-token | subfleet enroll user1@example.com
```

Enrollment makes a one-time validation request and accepts the authorization
responses expected from an inference-only setup token. Routine status and
dispatch never use stored setup tokens as quota-observation credentials.
Instead, the Claude runner records transcript token totals in a private local
ledger and learns cooldowns from actual hard-limit responses.

## Commands

| Command | Purpose |
| --- | --- |
| `subfleet status [--json] [--cached]` | Show account identity, quota, authentication, shadow, and health state. With no subcommand, `status` is the default. |
| `subfleet capacity [--json]` | Normalize five-hour and weekly headroom across both providers. |
| `subfleet pick codex` | Print the best dispatchable Codex home. |
| `subfleet pick claude` | Print the best enrolled Claude address. |
| `subfleet run` | Classify a task, select a provider and lane, and invoke a hardened runner. |
| `subfleet codex` | Run hardened `codex exec`; `-H` is optional and omitted lanes are selected automatically. |
| `subfleet claude` | Run hardened headless Claude Code with `-A` auto-selection or `-a EMAIL` pinning. |
| `subfleet login codex N\|app` | Perform the Codex re-login ritual for one numbered lane or the app home. |
| `subfleet enroll EMAIL` | Read a Claude setup token from stdin and store it through the secret-store abstraction. |
| `subfleet mirror` | Run one Claude desktop-session mirror pass; accepts `--list`, `--dry-run`, `--prune`, and the mirror's other options. |
| `subfleet errors` | Show recently observed provider limit and authentication errors. |
| `subfleet watch [--dry-run]` | Take one snapshot, evaluate health transitions, and send or print alerts. |
| `subfleet brief` | Print a compact Markdown capacity section. |
| `subfleet runs [--mine] [--running]` | Inspect the durable prompt/output/error ledger; `runs show ID` displays one run, `runs reap` finalizes entries whose runner died. |
| `subfleet wait [ID ...] [--mine] [--last]` | Block until dispatched runs finish; the exit code reflects the worst result (124 timeout, 125 orphaned, 2 unknown id). |
| `subfleet kill ID ...` | Signal a running dispatch; the runner's exit path salvages and finalizes its ledger entry. |
| `subfleet sessions` | List live Claude Code sessions that can receive messages. |
| `subfleet notify [--session ID] TEXT` | Push a message into a Claude Code session inbox (default: the current session). |
| `subfleet tickle [--session ID \| --all]` | Resume-nudge sessions whose last turn was cut off by a restart. |
| `subfleet muster [--dry-run]` | Roll-call recently active sessions after a restart where nothing was cut off. |
| `subfleet hooks install\|uninstall\|status` | Manage the Claude Code hook entries: completion catch-up, resume nudges, and the attached-runner guard. |

Use `subfleet COMMAND --help` for monitor and router options. The pass-through
runners intentionally retain their concise shell usage strings.

## Dispatch

The router accepts prompt text or `-p PROMPTFILE` and can explain its decision:

```bash
subfleet run --why -C /path/to/project -o result.md "Fix the failing retry test"
subfleet run --dry-run -p task.md
```

Its transparent pattern rules classify prose and final-judgment work, reviews,
mechanical sweeps, and general build work. It chooses a model family, consults
the shared capacity view, records the decision, and can cross from Codex to
Claude when the default family has no dispatchable lane. Explicit `-H` and
`-a` options pin a resource. `-m`, `-t`, `-s`, `-d`, and `-b` provide model,
task-class, sandbox, detached-run, and salvage-branch overrides.

The provider runners are also available directly:

```bash
subfleet codex -m MODEL_NAME -C "$PWD" -p task.md -o result.md
subfleet claude -A -m MODEL_NAME -C "$PWD" -p task.md -o result.md
subfleet claude -a user1@example.com -m MODEL_NAME -C "$PWD" -p task.md -o result.md
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
`review` calls when `CODEX_HOME` is unset. Set `SUBFLEET_NO_AUTOPICK=1` to opt
out; interactive commands and explicit homes pass through unchanged. subfleet
resolves the real Codex executable without depending on scheduler `PATH`.

## Detached runs and completion notices

A run dispatched from inside a Claude Code session must outlive that session:
the desktop app restarts every session on an account switch and stops an idle
session's process group. Inside a session — detected through the environment
the session's tool shell exports — `subfleet run` therefore pre-creates the
run-ledger entry, launches the runner in its own process session, and returns
immediately with the run id and paths. Outside a session, dispatch stays
synchronous. `-d` forces a detached launch anywhere, `--attach` keeps the
detached launch but waits inline (if the waiting process dies, the run
continues and `subfleet wait` re-joins it), and `SUBFLEET_RUN_DETACH=0|1`
overrides the detection in either direction. A detached run without `-o`
writes to `<state>/runs/<id>/out.md`; `-n NAME` labels the run id.

When the runner finishes, its exit path pushes a completion notice into the
dispatching session's inbox over the harness's local cross-session messaging
socket. The recipient is resolved by session id at finish time, not by the
process or socket captured at dispatch, so the notice still arrives after the
session restarted under a new process. The notice carries a status summary —
run id, state, lane, duration, output path and size, the output's first line,
and a short error tail on failure — never the prompt or the full output. The
notice's permission attestation defaults to the recipient's own recorded
permission class; `SUBFLEET_NOTIFY_MODE` (`bypass`, `prompting`, `none`)
overrides it.

If the session is not running at finish time, the notice is parked under the
state directory and surfaced as context the next time that session starts or
prompts, through the hooks below.

```bash
subfleet run -C /path/to/project -p task.md -o result.md   # returns a run id
subfleet wait RUN_ID
subfleet runs --mine --running
subfleet kill RUN_ID
```

Both runners adopt a pre-created ledger entry through `SUBFLEET_RUN_ID`
instead of starting a second record, and never pass run identity on to
dispatches nested inside the lane. `subfleet codex` also accepts `-A` (start
on the given lane but re-pick on a usage limit) and `-d` (re-exec in a new
process session and return at once, with progress in the lane log).

## Resume nudges (tickle)

An account switch or app relaunch restarts every open Claude Code session; a
session that was mid-turn sits idle until someone types into it. On
SessionStart (sources `startup` and `resume` only), the hook classifies the
session's own transcript. When the last real turn was cut off — a tool call
with no recorded result, a tool result the model never continued from, or an
unanswered prompt, but not a deliberate interrupt and not a completed turn —
a detached worker pushes a "continue where you left off" message into the
session's inbox a few seconds later. The desktop app's own resume stub pair
is recognized and skipped, and provider limit banners are noted in the nudge.

Guards: `SUBFLEET_TICKLE=off` disables; interruptions older than
`SUBFLEET_TICKLE_MAX_AGE_S` (default 8 hours) are left alone; one nudge per
interruption point plus a per-session cooldown; and the transcript must be
unchanged across the delivery delay, so a session that already continued is
never nudged on top. `subfleet tickle --all [--dry-run]` is the manual sweep;
it additionally requires two minutes of transcript quiet, since outside
SessionStart an interrupted-looking tail can just be a long tool call.
`subfleet tickle --session ID --force` overrides every guard. Outcomes and
skip reasons land in `<state>/tickles/`.

`subfleet muster [--dry-run]` is the roll call for a restart where nothing
was cut off (for example, the account moved to usage credits and every turn
ended normally). Each session whose last turn — completed or interrupted —
falls inside the roll-call window (`SUBFLEET_MUSTER_MAX_AGE_S`, default
2 hours) gets a message asking it to pick its standing work back up, with
the same transcript-quiet discipline as the manual sweep and one call per
interruption point.

## Claude Code hooks

`subfleet hooks install` adds three entries to `~/.claude/settings.json`
(override the path with `SUBFLEET_CLAUDE_SETTINGS`), all routed through
`bin/subfleet-hook`:

- `SessionStart` / `UserPromptSubmit` — surface parked completion notices;
  SessionStart also runs the resume-nudge classifier.
- `PreToolUse` (Bash) — block `subfleet codex`, `subfleet claude`, and bare
  `codex exec` launched directly from a session, since they die with it; the
  message names the `subfleet run` replacement. The runners' own `-d` passes,
  and prefixing a command with `SUBFLEET_ATTACHED_OK=1` is the explicit
  one-off override.

Install is idempotent, preserves the rest of the settings file, and writes a
timestamped backup beside it before any change. `subfleet hooks uninstall`
removes exactly the subfleet entries. The hook script uses `jq` when present
and stays silent when it is not.

## Monitoring and repair

`subfleet watch` is a single pass suitable for cron or another scheduler. It
persists snapshots and transition state under `~/.local/state/subfleet` by
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
until a new login changes the authentication file. subfleet never writes a
Codex access token itself.

## Configuration

The default configuration file is `~/.config/subfleet/accounts.json`. Important
keys are:

- `accounts` and `enrolled`: the Claude roster and its secret item names.
- `codex_homes`: `null` for numeric-home discovery, or an explicit list.
- `codex_app_home`: the observed desktop-app home.
- `protected_account`: an optional identity fallback used only when the app
  home cannot provide its current account.
- `secret_store_cmd`: an argv prefix implementing `get NAME`, `set NAME` with
  the value on stdin, and `del NAME`. When unset, subfleet uses macOS `security`.
- `notify_cmd`: an argv prefix receiving `SUBJECT BODY`; the complete message
  is also sent on stdin. When unset, alerts go to stderr.
- `mirror_heartbeat`, `mirror_job_label`, and
  `mirror_restart_cmd`: optional mirror and scheduler integration.
- `login_browser_cmd`: a browser argv prefix. A `{url}` placeholder is
  replaced; otherwise the URL is appended.
- `login_refresh_cmd`: a snapshot-refresh argv command run after login.

Command settings may be JSON argv arrays or shell-like strings parsed into
argv; they are never executed as shell expressions.

Common environment overrides are `SUBFLEET_CONFIG_DIR`, `SUBFLEET_STATE_DIR`,
`SUBFLEET_CODEX_HOMES` (colon-separated on macOS/Linux),
`SUBFLEET_CODEX_APP_HOME`, `SUBFLEET_CODEX_BIN`, `SUBFLEET_CLAUDE_DIR`,
`SUBFLEET_CLAUDE_JSON`, `SUBFLEET_SECRET_NAME_PREFIX`,
`SUBFLEET_SECRET_STORE_CMD`, `SUBFLEET_NOTIFY_CMD`,
`SUBFLEET_MIRROR_HEARTBEAT`,
`SUBFLEET_MIRROR_JOB_LABEL`, `SUBFLEET_MIRROR_RESTART_CMD`,
`SUBFLEET_LOGIN_BROWSER_CMD`, `SUBFLEET_LOGIN_REFRESH_CMD`,
`SUBFLEET_CODEX_GUARD=off` for an explicit one-run guard bypass,
`SUBFLEET_RUN_DETACH`, `SUBFLEET_NOTIFY_MODE`, `SUBFLEET_TICKLE`,
`SUBFLEET_TICKLE_MAX_AGE_S`, `SUBFLEET_MUSTER_MAX_AGE_S`, and
`SUBFLEET_CLAUDE_SETTINGS`.

subfleet does not embed secrets or local account identifiers in repository
files. Runtime ledgers are created in the configured state directory with
private permissions; runner output is written only to the path you request.

## Codex command guard

`subfleet codex` preflights a portable Codex `PreToolUse` hook before launch.
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
