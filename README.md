# subfleet

A fleet of subs. Formerly `carpool` (renamed 2026-08-23; the old command and
`CARPOOL_*` variables stay aliased for a transition week).

Max's multi-subscription AI capacity stack, one package, one front door: jobs
ride together in one subscription until it is full, then the next lane opens,
and each job takes whichever lane is moving. The subs operate under the
surface — dispatched work outlives the session that launched it and surfaces
only to report back. Identity = lane, never login: orchestrators name a model
and a prompt; lane choice, rotation, shadowing, quota, and health are
machinery.

**Public tree (2026-09-18).** This repository is the live source tree of Max's
deployment, cut over from the private `chief-of-staff` monorepo with history
squashed. Defaults still assume that layout: state under
`~/chief-of-staff/state/subfleet/` (override `SUBFLEET_STATE_DIR`), and the
account rosters `claude-accounts.json` / `codex-accounts.json` next to this
file (gitignored; copy the `*.example.json` templates or point
`DELEGATE_ACCOUNTS_FILE` / `SUBFLEET_CODEX_ACCOUNTS` at yours). The fixtures in
`tests/test_guard.py` are the guard's regression corpus from that machine and
expect a `/Users/<user>` home.

    subfleet                       live table: codex lanes + app home, Claude lanes, mirror
    subfleet run ...               dispatch front door — routes by content class + capacity;
                                  DETACHED by default inside a Claude session (survives
                                  account switches) and the session is told when it finishes
    subfleet wait <id>|--mine      block until runs finish (re-runnable; fine in the background)
    subfleet codex ...             hardened `codex exec` on an auto-assigned codex lane
    subfleet claude ...            hardened headless `claude -p` on an auto-picked Claude lane
    subfleet pick codex|claude     best lane on stdout (for scripts that want just that)
    subfleet runs ...              durable prompts, outputs, errors, logs, and run metadata
    subfleet resume-codex <run-id> continue a Codex thread on the CODEX_HOME that owns it
    subfleet handoff ... --to astra  continue a Claude session through a fresh target agent
    subfleet gate ...              main/peer agreement loop for an exact PR or plan revision
    subfleet kill <id>             SIGTERM a running dispatch (its trap salvages + finalizes)
    subfleet sessions / notify     live Claude sessions with an inbox; push a message to one
    subfleet hooks install         Claude Code hooks: completion catch-up + attached-runner guard
    subfleet login codex <N|app>   stage a lane (re)login — Max clicks, machinery does the rest
    subfleet reset codex <N|all>   consume a gifted reset after live safety gates
    subfleet reset codex --policy  evaluate the automatic one-at-a-time policy
    subfleet enroll <email>        store a Claude setup-token (lane)
    subfleet keepalive             open idle Claude 5h windows (launchd every 5h05m)
    subfleet mirror                desktop session mirror pass (launchd every 60s)
    subfleet watch / brief / errors / capacity

Components: the `subfleet` Python package (snapshot, capacity model, watchdog,
renderers, dispatch router, login ritual), `bin/subfleet-codex` and
`bin/subfleet-claude` (the hardened runners), `bin/codex` (PATH shim: bare
`codex exec` follows the weekly-reset waterfall), `bin/subfleet-guard` (never-rules hook for
codex lanes, see docs/guard.md), `bin/subfleet-mirror` (desktop session
mirror), `bin/subfleet-statusline` (Claude Code statusline tap),
`bin/subfleet-watch` and `bin/subfleet-keepalive` (launchd entries), and `app/`
(Subfleet.app menu bar).
Outside the package but part of the same doctrine: the `/gpt-pro` skill
(GPT-5.6 Pro via Chrome — no quota API, so untracked here).

How this stack compares with Claude Code's native workflows, measured:
[docs/native-workflows-vs-subfleet.md](docs/native-workflows-vs-subfleet.md).

Cross-account usage/quota + auth-stability monitor for Max's Claude and GPT
accounts. Born from the 2026-07-11 incident: a Claude subagent lane died on the
session limit with no warning, codex lanes then failed across CODEX_HOME dirs
(revoked refresh token from the same-account-in-two-homes trap, then the
healthy account's 5h window exhausted mid-program), and the ChatGPT app's
gauge showed headroom the whole time because it does not refresh from CLI usage.

Principles: server responses are ground truth; never fabricate a number
(unreachable ⇒ "unknown" + last observed error, labeled with observation
time); read-only against auth stores (never writes auth.json, never refreshes
tokens itself — an UNPERSISTED refresh rotation is the revocation trap; the
watchdog's expired-token heal shells out to a one-shot `codex exec` so the
CLI refreshes and persists its own token); logins are Max-only actions and
every alert names the exact command.

## Commands

```bash
subfleet                         # live table: per-lane 5h/weekly %, resets, auth verdicts, app home, mirror
subfleet status --json|--cached  # machine-readable / instant from the last watchdog snapshot
subfleet capacity [--json]       # fast 5h + weekly view across codex and Claude
subfleet runs [--last N] [--json] [--mine] [--running]  # newest durable runs; RUNNING / ORPHANED marked
subfleet runs show <id> [--err]       # saved metadata + output, optionally err.log
subfleet runs reap [--dry-run]        # finalize RUNNING entries whose runner pid is gone
subfleet resume-codex <run-id> [PROMPT] [-o OUT]  # native Codex resume, pinned to the owning home
subfleet handoff <session-id>|--last --to sol|terra|astra|opus [-C DIR]  # Claude transcript → new agent
subfleet wait <id>... | --mine | --last [--timeout S] [--cat]   # block until done; rc = run rc, 124 timeout
subfleet kill <id>                    # SIGTERM the runner; salvage trap + ledger finish run as usual
subfleet sessions                     # live Claude Code sessions (name, id, pid, inbox)
subfleet notify [--session ID] TEXT   # push a message into a session inbox (default: this session)
subfleet hooks install|uninstall|status   # ~/.claude/settings.json entries for bin/subfleet-hook
subfleet pick codex [--json --all]    # best CODEX_HOME on stdout (rc=1 if none); details on stderr
subfleet pick claude [--json --all]   # best enrolled Claude lane (email) on stdout
subfleet run --task build --tier standard -C <dir> -p prompt.md -o out.md  # semantic dispatch
subfleet run -m astra -C <dir> -p prompt.md -o out.md             # exact expert override (GPT-6 Astra)
subfleet gate pr 42 --peer fable --dry-run                      # capture the revision to review
subfleet gate plan plan.md --peer astra --main-approve --expect-sha256 <reviewed-hash>
subfleet codex  -m gpt-6-astra -C <dir> -p prompt.md -o out.md    # lane auto-assigned (-H pins)
subfleet claude -A -C <dir> -p prompt.md -o out.md               # lane auto-picked (-a pins)
subfleet login codex 3 | app     # stage a (re)login: server + OAuth tab + watcher; Max clicks
subfleet reset codex 3 [--dry-run]  # reset one LIMITED lane; use `all` for each eligible lane
subfleet reset codex --policy [--dry-run]  # run or inspect the automatic one-at-a-time policy
subfleet enroll <email>          # store a Claude setup-token for lane dispatch
subfleet keepalive [--dry-run] [--family claude]  # open each idle enrolled Claude lane's 5h window
subfleet mirror [--list|--dry-run|--quiet]   # desktop session mirror (launchd runs --quiet every 60s)
subfleet errors --hours 12       # observed limit/auth errors (codex rollouts + Claude transcripts)
subfleet watch [--dry-run]       # one watchdog cycle (launchd every 30 min)
subfleet brief                   # morning-brief markdown section
```

Dispatch never hand-picks lanes: `subfleet run`, `subfleet codex` (no `-H`),
and bare `codex exec` (PATH shim) all auto-assign via the picker and re-pick on
a mid-run hard limit; `-H` / `-a` exist only to PIN identity (attestation,
provenance, forensics). Codex requires minimum headroom in both reported
windows, then waterfalls by weekly `reset_at` ascending: all work goes to the
soonest-expiring weekly window until that lane is exhausted. A live wham
`limit_reached`, a rollout usage-limit retry clock in the future, or the
15-minute cooldown written immediately after a dispatch limit skips that lane;
it re-enters automatically when the clock passes. Codex in-flight counts and
app-shadow metadata remain visible but do not affect order. Claude keeps its
worst-window headroom score, active-app protection, and lower-in-flight
tiebreak.

## Data sources

codex (lanes = ~/.codex-1..N, one distinct PAID ChatGPT account each — a free-plan
binding is verdict FREE-PLAN, excluded from dispatch, alerted; ~/.codex is the
ChatGPT/Codex desktop app's home — observed for identity, never dispatched to,
since 2026-08-19: the app rewrites it on every sign-in/out, which used to
evaporate a lane's binding and create same-account duplicates. The lane bound
to the app's current account is "shadowed": named by the watchdog because two
token copies of one account revoke each other on refresh. Shadowing does not
change dispatch order, but it does make the lane a last-resort automatic-reset
candidate):
- Live: `GET https://chatgpt.com/backend-api/wham/usage` with the home's
  access token — quota (5h primary + weekly secondary windows, resets, plan)
  and auth health (401 `token_revoked` ⇒ home degraded) in one cheap call.
- Observed fallback: `$CODEX_HOME/sessions/**/rollout-*.jsonl` rate_limits
  snapshots + usage-limit/"refresh token was revoked" error events, scanned
  incrementally (per-file size/mtime/append cache in
  state/subfleet/rollout-scan-cache.json — cold sweep is seconds, steady
  state near-zero). More complete than subfleet-codex err.logs, which are just
  stderr captures of the same events scattered across dispatch workdirs.
- `auth.json` is read (never written) for account_id/email/plan identity.

claude (active Claude Code login) — the snapshot picks the FRESHEST of these
per read (`claude.pick_live_source`), stamps its age, and demotes anything
older than 3h to a labeled "last reading … stale" line instead of headlining
it; with only stale readings the table leads with the activity-derived window:
- Active-login keychain OAuth probe (`api.anthropic.com/api/oauth/usage`): the
  app token supplies live 5h/weekly readings. 401, 403, and 429 results are
  categorized rather than raised.
- Last-good probe read-back: every 200 payload is persisted to
  state/subfleet/claude-oauth-raw.json and re-parsed when the current probe
  fails (the keychain token routinely goes stale between desktop sessions
  while the last good reading is minutes old).
- Capacity live cache: the active-account row capacity.collect callers
  (subfleet run, the picks, subfleet claude) refresh; only live-confidence windows
  qualify — ledger-estimated lane percentages are not desktop usage.
- Statusline tap: `bin/subfleet-statusline` (installed as the statusLine
  command in ~/.claude/settings.json) tees rate_limits to
  state/subfleet/claude-statusline.json. TERMINAL-TUI ONLY (diagnosed
  2026-08-12): the desktop app, SDK, and `claude -p` never invoke statusLine
  commands, so with app-based workflows this can be weeks stale (last real
  capture 2026-07-22). claude-statusline-invoked.json breadcrumbs every
  invocation so tap liveness is checkable.
- Transcript scan: isApiErrorMessage events across ~/.claude/projects
  (session/weekly/usage/rate limits with reset clocks, e.g. "resets 6:40pm
  (America/New_York)").
- Identity from ~/.claude.json oauthAccount; other Claude accounts (from
  cc-mirror-accounts.json) are listed as not-probeable (no local tokens).

capacity / run / monitor lanes:
- Live Codex-home and active-Claude readings are sanitized and cached in
  `capacity-live-cache.json` for 120 seconds. Ledger windows and cooldowns are
  merged on every read, so completed runs and hard limits are immediate. The
  same cache records only whether each enrolled keychain service exists (never
  its token value), so a missing setup token cannot be selected as a lane.
- Claude setup tokens are inference-only (the usage endpoint returns 403), so
  `bin/subfleet-claude` records per-message transcript usage in `lane-usage.jsonl`.
  Those 5h/7d token sums are `estimated`; a hard-limit observation learns the
  maximum seen capacity for each window and upgrades calibrated percentages to
  `observed` confidence. A successful keepalive writes a compact
  `kind="keepalive"` marker to the same ledger; until that five-hour window
  closes, its exact `ts + 5h` reset is also `observed` rather than estimated.
- Status snapshots, watchdog lane alerts, `subfleet pick claude`, and `subfleet run` all reuse
  those normalized rows; enrolled setup tokens are never treated as quota
  probes. Cooldown updates are locked, and successful re-enrollment clears a
  prior auth hold without clearing genuine hard-limit ledger events.
- The 30-minute `history.jsonl` samples drive Codex weekly burn rates over the
  last 6 and 24 hours. The table shows both rates, marks a lane `→ ~NN% unused
  at reset` when more than 20% is projected to expire within three days, and
  reports fleet windows left plus projected unused windows. The brief carries
  the same fleet projection when every lane has enough data.

## Resume nudges (tickle)

A Claude account switch restarts every open session; one that was mid-turn
sits idle until someone types "." into it. The SessionStart hook classifies
the session's own transcript and, when the last turn was cut off (a tool call
with no recorded result, a tool result the model never continued from, an
unanswered prompt — but not an Esc-interrupt and not a completed turn), a
detached worker pushes a "continue where you left off" message into the
session's inbox a few seconds later — the same wake path as completion
notices, so the session resumes exactly as if "." had been typed.

Guards: SessionStart sources `startup`/`resume` only (never `compact`/
`clear`); an age cap on the interruption (`SUBFLEET_TICKLE_MAX_AGE_S`,
default 8h); one nudge per interruption point plus a 10-minute per-session
cooldown; and a liveness check — the transcript must stay byte-identical
across the wait, so a session that already continued on its own (the CLI's
`--resume` replays interrupted work natively; or the user typed) is never
nudged on top. `SUBFLEET_TICKLE=off` disables. `subfleet tickle --all
[--dry-run]` is the manual sweep — `/tickle` in any Claude session runs it
(skill at ~/.claude/skills/tickle) (it additionally requires two minutes of
transcript quiet, since outside SessionStart an "interrupted" tail can just
be a long-running tool call); `subfleet tickle --session <id> --force`
overrides every guard. Outcomes and skip reasons land in
`state/subfleet/tickles/<session>.json`.

## Reset credits

The Codex usage payload may include
`rate_limit_reset_credits.available_count` (all unused gifted credits) and
`applicable_available_count` (credits usable against the current limit).
Subfleet exposes both as `reset_credits.available` / `.applicable` in status
snapshots and capacity rows. Upsell CTAs are ignored: these are gifted,
non-purchase entitlements, and subfleet has no purchase or add-credit path.

The two authenticated endpoints are:

- `GET https://chatgpt.com/backend-api/wham/rate-limit-reset-credits` for
  concrete credit IDs, statuses, and types.
- `POST https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume`
  with a fresh UUID4 `redeem_request_id` plus an optional `credit_id`.

They follow the upstream Codex implementation in
[`codex-rs/backend-client/src/client/rate_limit_resets.rs`](https://github.com/openai/codex/blob/main/codex-rs/backend-client/src/client/rate_limit_resets.rs).

`subfleet reset codex <N|all> [--dry-run]` is gated twice: a fresh usage probe
must report `limit_reached: true`, and the list endpoint must return a concrete
`status: available` credit whose `reset_type` is exactly `codex_rate_limits`.
`--dry-run` lists eligible credits without posting. `all` handles eligible
lanes sequentially, re-probes after every POST, prints weekly usage before and
after with the response code, and stops on the first non-success. Every
successful redemption is appended to `history.jsonl` as a `reset` event and
recorded as a completed, meta-only `reset` event in the run ledger.

### Auto-reset policy

Max's rule (2026-08-22):

> "i want default behavior then to use available resets one by one when im close to exhausting all codex lanes, starting with the account that has the furthest out weekly reset"

The policy is on by default. Its configuration lives in
`codex-accounts.json`:

```json
"auto_reset": {
  "enabled": true,
  "headroom_floor_pct": 15,
  "min_interval_min": 30
}
```

It triggers when no Codex lane is dispatchable, or when the sum of weekly
headroom across dispatchable lanes is below 15 percentage points of one weekly
window. Candidates must be server-confirmed LIMITED lanes and hold an
available concrete `codex_rate_limits` credit. App-shadowed lanes are excluded
when any unshadowed candidate exists. The winner is the lane with the
furthest-out natural weekly reset, then the lowest in-flight count, then the
lowest lane number.

Only one credit is consumed per evaluation, and the locked
`state/subfleet/reset-policy.json` record prevents another redemption inside
`min_interval_min`. This preserves an imminent natural reset, opens one fresh
seven-day window from the current moment, and staggers future reset clocks
instead of synchronizing the fleet.

The watchdog evaluates the policy every cycle before fleet alerts. An empty
`subfleet pick codex` evaluation also runs it, so `subfleet run`, `subfleet codex`,
and the bare shim inherit redemption. After a successful consume, subfleet
polls the usage endpoint for up to about 90 seconds for propagation, clears
the lane's cooldown, returns it to the picker, and reports the account,
fleet-wide credits remaining, and new weekly reset. The watchdog sends the
same facts as an informational Telegram notice. A confirmed consume remains
authoritative if the endpoint is still stale at timeout; the reset clock is
NOW+7 and the lane is dispatchable for that cycle. If any account's credit
count is unreadable, the fleet remaining field is `?`/`null` instead of a
false undercount. `subfleet reset codex --policy` runs one evaluation
interactively; add `--dry-run` to print the decision and ordered concrete
candidates without consuming.

### Run ledger

Every `subfleet run`, `subfleet codex`, and `subfleet claude` dispatch gets a private
directory under `~/chief-of-staff/state/subfleet/runs/` (or
`$SUBFLEET_STATE_DIR/runs/`). The directory is created at agent start (or by
`subfleet run` before the runner exists — the runner then ADOPTS the id it is
handed in `SUBFLEET_RUN_ID`), so `subfleet runs` shows an active lane as
`RUNNING`; the runner's EXIT path finalizes it on success or failure. Each
entry records the runner `pid` (an entry whose pid is gone without a finish
record shows `ORPHANED`; `subfleet runs reap` finalizes those as rc=-9), the
dispatching Claude session (`caller`), and how that session was told
(`notify`: pushed / parked / surfaced). Each completed entry contains the exact sent prompt,
`out.md`, `err.log`, a detached `lane.log` when applicable, and `meta.json` with
provider/model/lane, workdir, before/after git HEAD, return code and timing, the
caller's original `-o`, Claude session/transcript context, Codex thread/home/rollout
identity, resume provenance, new salvage refs, and
the `subfleet run` routing decision. `subfleet capacity --json` exposes each lane's
current count as `in_flight`.

The ledger is an extra copy: caller-facing `-o` behavior and temporary-prompt
cleanup are unchanged. Prompt-bearing directories and files are created private
to the user. Retention prunes the oldest completed entries whenever a run is
recorded, keeping at most the newest 500 and at most 2 GiB; active RUNNING entries
are not deleted mid-dispatch.

### Codex resume and Claude handoff

Completed Codex ledger entries record the thread UUID, the exact `CODEX_HOME`
that owns it, and its rollout path. `subfleet resume-codex <run-id> [PROMPT]`
uses Codex's native `exec resume` command through the hardened runner, creating
a new ledger entry whose `resumed_from` points to the source run. A Codex thread
is home-local: resume never invokes the picker or `-A`, and it never substitutes
another account. If the recorded home is cooled or limited, the command parks
with the reset time and returns 3. Historical ledger entries are resolved lazily
from their saved `err.log`, so runs recorded before these metadata fields were
added remain resumable when their rollout still exists.

`subfleet handoff <claude-session-id> --to sol|terra|astra|opus` (or `--last`)
dispatches a fresh agent with continuity from a local Claude Code transcript.
The deterministic brief names the source transcript, carries the original task,
a bounded recent main-chain excerpt (including useful bounded textual tool
results), and the target worktree's `PROGRESS.md`, git status/log, and salvage
refs. The source transcript remains available by absolute path, so the recipient
can inspect context beyond the excerpt instead of relying on a lossy session
database rewrite. Only raw credential values and binary/base64 payloads are
removed from the copied prompt; ordinary code, commands, paths, tool output, and
technical discussion are preserved. Handoffs run detached through `subfleet run`
and therefore inherit its capacity routing, guard, salvage, ledger, and completion
behavior.

For sidebar-level discovery, Codex also has a supported one-way import from
Claude Code: use **Settings → Import** in the desktop app and enable automatic
updates, or `/import` in an idle Codex CLI session. The official import is the
right owner of Codex's project/chat indexes; Subfleet deliberately never writes
Claude or Codex application databases. The explicit handoff above remains useful
for reproducible dispatch, full local transcript access, workspace evidence, and
run-ledger provenance. See [Import from another agent](https://learn.chatgpt.com/docs/import).

## Watchdog + alerting

launchd job `com.maxghenis.cos.subfleet` (plist in ../launchd, installed in
~/Library/LaunchAgents) runs `subfleet watch` every 30 min + at load. Each
cycle: live-probe all accounts → auto-heal expired-token homes → evaluate the
auto-reset policy → write state/subfleet/snapshot.json + history.jsonl +
brief.md → alert via
../bin/notify (Telegram outbound-only, email fallback) on:

- token revoked on a home (warn) / "refresh token was revoked" seen in a
  rollout or hit by the refresh probe (critical) — with the exact
  `CODEX_HOME=… codex login` heal
- duplicate account_id across homes (critical — revocation trap)
- fleet ≤1 dispatchable lane (warn) / 0 lanes (critical) with earliest reset
- two or more limited lanes holding applicable reset credits while the fleet
  has at most one dispatchable lane (warn, once per transition, only when
  automatic redemption is disabled)
- more than one full Codex window projected to expire unused before the
  earliest reset within three days (warn, at most once per day)
- Claude session/weekly limit with future reset (deduped per reset clock)
- app shadows a lane (warn, once per app-account change, no re-alert): the
  desktop app is signed into a lane's account — that lane may get revoked
  while the app stays there; heal named if it dies
- session mirror stalled (warn) — desktop session mirroring stopped, so
  account switches would hide sessions; heartbeat = the mirror's per-pass
  state sidecar mtime (the log is silent on no-op runs),
  with an in-flight-run allowance (long passes under app churn are normal)
  and a 30-min hang cutoff; the alert carries the `launchctl kickstart` heal

The auto-heal: a usage probe that 401s with an EXPIRED access token (verdict
auth-suspect) is usually a false negative — the stored token aged out and any
real CLI call refreshes it. The watchdog runs one tiny
`codex exec -m gpt-5.6-terra … "Reply with exactly: ok"` in that home (~8k
tokens; the CLI persists the refreshed token atomically), then re-probes
usage to restore the verdict. Gated to one attempt per home per cycle
(state/subfleet/refresh-probes.json); `refresh token was revoked` downgrades
the home to auth-revoked (critical) and latches until a re-login rewrites
auth.json. Only this expired-token signature is auto-probed — token_revoked
and other 4xx are alerted as before.

Alert on transition, re-alert at most every 6h while persisting (once per day
for expiring capacity), one recovery notice when auth/fleet conditions clear.
A run where every probe is a network error is treated as offline: no alerts.
The morning brief includes state/subfleet/brief.md only when something is wrong.

### Keepalive

Claude starts a subscription's five-hour window with its first request; an idle
lane has no open window. `subfleet keepalive [--family claude] [--dry-run]`
checks every lane in `claude-accounts.json`'s `enrolled` map. If the latest
request in `lane-usage.jsonl` or the run ledger is unknown or at least five
hours old, it sends exactly one minimal request through that lane's setup token:
`claude -p "ok" --model claude-haiku-4-5-20251001 --output-format json`. It
bypasses the full runner, so it creates no salvage artifacts or run directory;
success only appends the `kind="keepalive"` usage marker described above. A
lane with a request in the last five hours is `skipped-open`, because another
ping would only consume usage. `--dry-run` reports what would open without
sending requests or changing state.

Keeping that window rolling means a burst that starts at phase φ reaches a
fresh window in `5h - φ`, instead of opening a new window at the burst and then
waiting roughly three hours after a two-hour exhaustion. Across random phases,
the expected post-exhaustion wait falls from about half a window to about a
quarter. The marker also gives the capacity model a known reset timestamp;
setup tokens cannot probe Claude usage (the usage endpoint returns 403).

Idle lanes run concurrently in a thread pool (at most four workers), with a
60-second timeout per lane. A 401/403 keepalive failure (or a recorded
lane-run auth failure) marks that lane auth-dead; later passes return
`skipped-auth` without sending a request or an alert, and log the auth detail
at most once per day. Re-enrolling the lane
clears that hold. Sanitized outcomes live in
`~/chief-of-staff/state/subfleet/keepalive.json` (under
`SUBFLEET_STATE_DIR` when overridden), while successful window-open timestamps
live in `lane-usage.jsonl`. When this state file exists, the CLAUDE table adds
`keepalive: last HH:MM, N opened`. `CLAUDE_LANE_CLAUDE` may override the
`claude` executable; the launchd entry otherwise supplies the standard
Homebrew, system, `~/bin`, and `~/.local/bin` paths.

The launchd job `com.maxghenis.cos.subfleet-keepalive` uses
`../launchd/com.maxghenis.cos.subfleet-keepalive.plist` to run
`bin/subfleet-keepalive` at load and every 18,300 seconds (5h05m). Install or
replace it, then verify the loaded job, with:

```bash
mkdir -p ~/Library/LaunchAgents
cp ~/chief-of-staff/launchd/com.maxghenis.cos.subfleet-keepalive.plist ~/Library/LaunchAgents/
launchctl bootout gui/$UID/com.maxghenis.cos.subfleet-keepalive 2>/dev/null || true
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.maxghenis.cos.subfleet-keepalive.plist
launchctl print gui/$UID/com.maxghenis.cos.subfleet-keepalive
```

Scheduled-task registry: `com.maxghenis.cos.subfleet` is the 30-minute
watchdog, `com.maxghenis.cos.subfleet-keepalive` is the 5h05m Claude keepalive,
and `com.maxghenis.cos.subfleet-mirror` is the 60-second session mirror.

## Menu bar app

`app/` holds a single-file SwiftUI MenuBarExtra (`AI Quota.app`, built by
`app/build.sh` into /Applications). Bar shows dispatchable lanes ("4/4", bolt
gains a warning badge on any problem); the popover lists codex lanes with
usage bars + reset times, a "copy best" dispatch button, Claude accounts
(active + enrolled probed, others marked not enrolled), and active limits.
It only reads the state files — "Refresh" kickstarts the launchd watchdog so
probing/alerting/app all share one pipeline. "Start at login" uses
SMAppService.

## Claude multi-account enrollment

`subfleet enroll <email>` stores a per-account OAuth token (from
`claude setup-token`, pasted via stdin) in the agent keychain as
`claude-quota-<email>` and records it in claude-accounts.json. A usage-endpoint
403 is accepted as the expected inference-only scope (401 is still rejected).
The full roster (10 accounts) lives in claude-accounts.json.

Enrolled accounts are dispatch **lanes**. `subfleet claude` runs headless work
pinned via `CLAUDE_CODE_OAUTH_TOKEN`, while the capacity ledger estimates each
lane's rolling usage without rotating the desktop login. A hard limit is
recorded before auto-repick; auth failures retain the exact re-enrollment
ritual.

After a successful lane, the runner locates the UUID-named session transcript
under Claude's configured project store and requires exactly one match before
checking the served model. This avoids relying on Claude's lossy cwd-to-project
encoding. Missing or ambiguous transcripts, checker failures, and model
mismatches never produce a positive `.MODEL_ATTESTED` marker; transient
transcript persistence is retried four times with a one-second backoff.

## Dev

```bash
uv sync && uv run pytest    # stdlib-only runtime; pytest via uv
```

State lives in ~/chief-of-staff/state/subfleet/ (Subfleet never writes auth tokens
there; `runs/` intentionally contains private prompt/output copies). Paths are
env-overridable (SUBFLEET_STATE_DIR, SUBFLEET_CODEX_HOMES,
SUBFLEET_CLAUDE_DIR, SUBFLEET_CLAUDE_JSON, SUBFLEET_NOTIFY, and
DELEGATE_STATE_DIR; keepalive's Claude executable is separately overridable
with CLAUDE_LANE_CLAUDE) — tests isolate via these. To uninstall the statusline:
remove the statusLine key from ~/.claude/settings.json. To unload the watchdog:
`launchctl bootout gui/$UID/com.maxghenis.cos.subfleet` (keepalive:
`com.maxghenis.cos.subfleet-keepalive`; mirror:
`com.maxghenis.cos.subfleet-mirror`).

## subfleet run (the dispatch front door)

`subfleet run [--task TASK --tier TIER] [-m fable|opus|sonnet|sol|terra|astra|haiku] [-C DIR]
[-o OUT] [-n NAME] [-d | --attach] [--json] [--overflow] [--dry-run]
(-p PROMPTFILE | PROMPT_TEXT)` dispatches through the hardened Claude and
Codex runners. `-t fable|review|build|sweep` remains as a legacy coarse-class
override. `-m` is the expert escape hatch: it pins one exact model and disables
capability fallback.

Model aliases: `fable` = claude-fable-5-1, `opus` = claude-opus-5, `sonnet`,
`haiku` = claude-haiku-4-5-20251001 (Claude lanes); `terra` = gpt-5.6-terra,
`astra` = gpt-6-astra, `luna` = gpt-5.6-luna (Codex lanes). `sol` (gpt-5.6-sol) is retired from
dispatch as of 2026-09-04 (Max: Opus for standard work, Astra for hard work):
`-m sol`, `--peer sol`, and `--to sol` still parse but dispatch Astra and say
so on stderr, so older scripts and in-flight gate states keep working. GPT-6 Astra is
served to ChatGPT-account Codex lanes from codex CLI 0.153.0 (its catalog entry
is hidden from the model picker but dispatchable; an older CLI gets "requires a
newer version of Codex"); it draws on the same `codex` weekly window as sol and
terra, so the capacity model and picker need nothing new, and like sol it is
dispatched at `ultra` reasoning effort. Verified on the subscription 2026-09-04.

GPT-5.6 Luna (2026-09-11): `luna` = gpt-5.6-luna, the catalog's "fast and
affordable agentic coding model" (visibility `list`, default reasoning `medium`,
272k context), on the same Codex lanes and `codex` weekly window. Max: "use
gpt-5.6 luna for some of the simple subagent work" — so the trivial and easy
tiers of lookup, research, review and build now route to Luna instead of Haiku
and Sonnet, which the Fable reserve had been rerouting upward to Fable anyway
(a Claude account's small models draw its shared weekly bucket; Luna draws
nothing from Claude). Luna runs at the catalog default effort, not `ultra`.
Haiku and Sonnet stay reachable only by exact `-m` pin. `--to luna` is a
handoff target; gate peers stay Fable/Astra.

Subscription only: lanes never run on the metered OpenAI API. The Codex runner
refuses a home whose `auth.json` is an API-key login (rc 7, before any codex
call) and drops an exported `CODEX_API_KEY` (which would silently override a
ChatGPT login — verified with a bogus key); `subfleet run -H` refuses such a
home up front, and the `bin/codex` shim refuses an exported API-keyed
`CODEX_HOME` for `exec`. `SUBFLEET_ALLOW_API_LANE=1` is the deliberate
operator override.

### Fable reserve (2026-09-08)

Every Claude Max account has two weekly buckets: the shared all-models window
(`weekly_all`) and a Fable-scoped window (`weekly_scoped`, display name
"Fable"). Fable draws on both; Opus, Sonnet and Haiku draw only on the shared
one. So an Opus run on an account that still has Fable left shrinks the Fable
that account can deliver this week — on 2026-09-06 farness and policybench sat
at 94% shared / 49% Fable, and on 2026-09-08 axiom.org (100/27),
maxghenis.com (100/63), rulesfoundation.org (100/50) and axiom-foundation.org
(100/85) were exhausted for the week with Fable unused. Max's rule (9/6): "we
can use opus before fable is exhausted but not to the degree where it'd cost us
fable."

`subfleet run` enforces it for every non-Fable Claude model (`subfleet/reserve.py`,
mirroring subfleet-v2's `scheduler.reserve_verdict`). Per candidate lane:

    slack = (1 − shared_used) − cap_ratio × (1 − fable_used)

and the lane is open for Opus/Sonnet/Haiku only when `slack ≥ min_slack`
(`cap_ratio` 2.0, `min_slack` 0.05). An **unmeasured** lane is reserved — a
missing reading is not evidence of slack. A lane whose usage payload has no
Fable window (a Team seat) has nothing to protect and is open. When every
eligible lane is reserved the work moves **upward**, never down: to Astra when
the task/tier chain allows it and a Codex home has room, otherwise to Fable on
those same lanes (an explicit `-m opus -a <lane>` on a reserved lane runs Fable
on that lane and says so). The decision record carries `reserve` (policy,
blocked model, per-lane drops, action); stderr prints `FABLE RESERVE: …`.

Readings come from the subfleet-v2 login dirs (`~/.subfleet/logins/<email>/`,
full-scope `claude auth login` per account): their OAuth access tokens can read
the usage endpoint, which setup tokens cannot (standing 429 — the reason v1
lanes were blind). Readings are cached 300 s in `reserve-usage-cache.json`
(percentages only, never a token) and GETs are paced 3 s apart. Access tokens
expire after hours; a lane whose token has expired is healed by a detached
one-word Fable request in that config dir (`claude -p … --model fable`, which
refreshes the login on use), at most 3 per dispatch and once per 20 min per
lane, so the next dispatch can measure it.

`subfleet reserve [--json]` prints the table (state, slack, shared %, Fable %,
reading status) for every enrolled lane and login dir. Policy overrides live in
`<state>/reserve-policy.json` — `{"enabled": false}` turns the guard off; that
is an operator action, deliberately not an environment variable an agent could
prepend to a command.


For ordinary dispatches, name the work and its minimum capability instead of a
provider model:

| Task | Trivial | Easy | Standard | Hard |
|---|---|---|---|---|
| Lookup | Luna | Luna | Opus | Astra |
| Research | Luna | Luna | Opus | Astra |
| Review | Luna | Luna | Opus | Astra |
| Build | Luna | Luna | Opus | Astra |
| Sweep | Terra | Terra | Terra | Astra |
| Authored prose | Fable | Fable | Fable | Fable |
| Strategy | Fable | Fable | Fable | Fable |
| Adjudication | Fable | Fable | Fable | Fable |

Fable rows dispatch `claude-fable-5-1` (Claude Fable 5.1). The retired pin
`claude-fable-5` is still accepted and normalizes onto the current id
everywhere — cooldowns, hard-limit records, revive targets, `pick --model` —
because both draw on the same account-scoped Fable limit; a session last
served by the retired model revives on the current one. Lanes need Claude
Code 2.1.251 or newer: an older CLI gets a 400 for this model and the runner
fails fast (rc 6, naming `claude update`) instead of rotating or cooling lanes;
the revive/probe path raises the same fault once per model instead of parking
sessions as "no lane serves". Every lane, probe, revive, and keepalive runs
the binary `paths.claude_bin()` resolves: `CLAUDE_LANE_CLAUDE`, else
`~/.local/bin/claude` (the native launcher `claude update` maintains), else
`claude` on PATH — launchd PATHs put `/opt/homebrew/bin` first, where a cask
frozen at 2.1.87 shadowed a current install. The runner resolves its `-m`
through `subfleet _canonical-model` before dispatch, so the served-model check
is exact and independent of the CLI's alias table; a `[1m]` suffix survives on
the dispatch id and is dropped for scope keys and comparisons.

Example: `subfleet run --task research --tier easy -C <dir> -p brief.md`.
When a tier is conclusively exhausted, routing may move only upward
(Luna → Opus → Astra); it never silently moves downward. Fable-role
rows remain on Fable, hard work remains Astra, and non-hard sweeps retain the
legacy Opus overflow only if the whole Codex fleet is exhausted. Exact `-m`
pins never fall back. The task controls permissions: build defaults to
`workspace-write`; every other semantic task defaults to `read-only`.
Capacity-based promotion happens before launch. Synchronous runs can also
promote after runtime exhaustion; an already-detached run keeps its selected
model, rotates eligible lanes, and reports exhaustion if none succeeds.

### Detached by default inside a Claude session; the session is told when it finishes

Three `subfleet run` dispatches died on 2026-08-23 when the Claude Code session
that launched them restarted (an account switch; the desktop app also
SIGTERMs a session's process group after 15 idle minutes). Inside a session —
detected by the `CLAUDECODE=1` / `CLAUDE_CODE_SESSION_ID` the tool shell
exports — `subfleet run` therefore:

1. pre-creates the ledger entry so the run id is known up front;
2. launches the runner with `nohup` in a NEW process session
   (`start_new_session=True`), immune to the session's death; a lane that was
   auto-picked gets `-A`, so a usage limit re-picks inside the runner;
3. returns immediately with the run id, output path, lane log, and the
   follow-up commands;
4. when the runner's EXIT path finalizes the ledger, it **pushes a completion
   notice into the dispatching session's inbox** — the same cross-session
   channel `SendMessage` uses (`~/.claude/sessions/<pid>.json` registry +
   unix socket + published peer token). The recipient is resolved by SESSION
   ID at finish time, so it still arrives after the session restarted under a
   new pid (verified live 2026-08-23: dispatched from pid 78753, delivered to
   pid 76529). An idle session wakes into a turn; a busy one sees it at its
   next tool boundary.
5. if the session is not running at that moment, the notice is parked in
   `state/subfleet/notices/<session>.jsonl`; `bin/subfleet-hook`
   (SessionStart + UserPromptSubmit, registered by `subfleet hooks install`)
   hands it to the session as context the next time it is up.

Flags: `-d` forces detached anywhere; `--attach` keeps the detached launch
but waits inline (rc = the run's rc; output echoed when `-o` was omitted,
as in synchronous mode) — if the session dies mid-wait the run continues and
`subfleet wait <id>` re-joins it. Outside a session (launchd lane scripts, a
terminal) the synchronous runner path is unchanged; `SUBFLEET_RUN_DETACH=0|1`
overrides the detection either way. Without `-o`, a detached run's output
lives at `<runs>/<id>/out.md` (`-n` names the id).

Waiting: `subfleet wait <id>` polls the ledger every 2 s and exits with the
run's rc (124 on `--timeout`, 125 for an orphaned runner, 2 for an unknown
id). It is safe to background and re-run — the common shape from an agent is
`subfleet wait <id>` with `run_in_background`, which makes the harness notify
the agent on completion while the completion message covers the case where
that waiter died. `subfleet wait --mine` waits for everything this session
dispatched; `subfleet runs --mine` lists it.

Attestation: the inbox holds a message at a recipient that runs without
permission prompts unless the sender declares the same permission class
(`from-mode="bypass"|"prompting"` on the envelope). subfleet is not a session;
its notice is metadata about the recipient's own dispatch (paths, rc, first
line of the output — never the prompt or output body), so it declares the
RECIPIENT's current class, read from its transcript. `SUBFLEET_NOTIFY_MODE`
(`bypass`/`prompting`/`none`) overrides; `none` leaves the message undeclared
(delivered at prompting sessions, held for the user at bypass sessions).

The PreToolUse guard (`subfleet hooks install`) blocks `subfleet codex`,
`subfleet claude`, bare `codex exec`, and the deprecated `codex-run` /
`claude-lane` when a session's Bash tool launches them directly — they die
with the session — and names the `subfleet run` replacement; the runners' own
`-d` (now a real `setsid`) passes, as does an explicit
`SUBFLEET_ATTACHED_OK=1` prefix. Without semantic task/tier flags, the legacy
classifier applies: authored prose, final
adjudication, and design/strategy signals form the fable floor and beat every
other signal. Review/assess/critique/audit/evaluate/referee routes to Opus in a
read-only defensive-audit frame; mechanical sweeps route to Terra; builds use
Opus (legacy classes carry no tier — use `--tier hard` or `-m astra` for hard
work, which runs Astra at ultra effort). Explicit resource/model pins remain
pins. Sweeps overflow to Claude Opus only when Codex is confirmed
exhausted/limited; fable-floor work fails fast with the earliest overall or
Fable-scoped reset instead of downgrading. A Fable-only limit does not block
Opus or Haiku.
`--why` and `decisions.jsonl` include the normalized capacity inputs, family
scores, and any loud cross-family routing note. `--overflow` remains accepted
for command-line compatibility; overflow is now automatic for elastic classes.

## Main/peer agreement gates

`subfleet gate` gives a running main agent a durable review/fix/review loop with
one complementary peer. A Fable main normally picks an Astra peer, and an
Astra main picks a Fable peer (`--peer sol` is remapped to Astra). The main must explicitly approve the fingerprint of the
revision it actually reviewed; the peer is pinned, runs read-only, and returns a
strict revision-bound verdict. If it requests changes, the command returns the
gate id and a nonzero review status. The main fixes the artifact (or records a
reasoned response) and starts the next round:

```bash
subfleet gate plan plan.md --peer fable --dry-run  # inspect the fingerprint
subfleet gate plan plan.md --peer fable --main-approve \
  --expect-sha256 <reviewed-sha256>
# ...main fixes the plan after peer findings...
subfleet gate continue <gate-id> --main-approve \
  --expect-sha256 <new-reviewed-sha256>

subfleet gate pr 42 --peer astra --main-approve \
  --expect-head <reviewed-head-oid> --expect-base <reviewed-base-oid> \
  --on-agreement merge --merge-method squash
# ...main fixes and pushes after peer findings...
subfleet gate continue <gate-id> --main-approve \
  --expect-head <new-reviewed-head-oid> --expect-base <reviewed-base-oid> \
  --response response.md
```

`--dry-run` only reads the current artifact and prints its fingerprint; it
never dispatches, changes gate state, or retries an action. Review that exact
revision before supplying its fingerprint with `--main-approve`. The gate
does not run an autonomous editor: the driving agent handles findings and
repeats `continue` until agreement or a reported blocker. Agreement with one
peer is sufficient; a third agent is not required.

Plan agreement writes a consensus certificate and authorizes the main agent to
proceed; it never runs arbitrary commands. PR merge is the sole built-in
external action. It requires a clean checkout at the exact PR head, binds both
approvals to the PR's head and base commits, rechecks that the PR is open,
non-draft, conflict-free, unchanged, and has terminal green CI, then invokes
`gh pr merge` with `--match-head-commit`. Merge and squash are supported; rebase
is not, because this gate does not yet verify a rebased landing. After merging,
it checks the immutable merge commit's parents, not the moving base branch tip.
GitHub's head guard is atomic; the base and CI preflight checks are not. Keep
server-side branch protection enabled. A race detected after a merge is
reported as a merged revision mismatch, not success, and is never retried or
automatically reverted. Queue/auto-merge membership is checked before reporting
that an open PR is queued. A removed queue entry can be retried with fresh main
approval; a closed PR or unknown queue state does not trigger another merge.

A changed artifact, malformed peer verdict, approval with nonempty findings
or notes, failed dispatch, missing positive Fable model attestation, a downgrade,
pending/failed/absent CI, or
mergeability uncertainty fails closed. Gates stop after four peer rounds by
default (`--max-rounds` changes the bound).

Peers run from temporary directories outside the repository, with an immutable
artifact copy and source access for PRs. Automatic project instructions,
user hooks, plugins, and external integrations are disabled for isolated
reviews. Isolated Codex reviews reject managed requirements or nonempty system/
project settings that could override isolation; Claude retains genuine machine
policy. Claude gate reviewers get only Read/Glob/Grep, and Codex reviewers use
read-only sandboxing. Ordinary read-only Claude research calls also retain
WebSearch/WebFetch. Prompt and verdict
validation supplements these restrictions; it is not a sandbox by itself.

Private gate state lives under `state/subfleet/gates/<gate-id>/`, including the
exact plan snapshot or PR diff/revision, prompts, peer outputs, verdicts, main
responses, action state, and final certificate. Each round reserves a unique
attempt directory under a lock. Live gate/peer leases prevent duplicate
reviews; abandoned output never counts as a new approval. Exit codes are 0 for
agreement/completion, 1 for an operational error, 2 for invalid input, 3 for
changes requested, 4 for a blocked/invalid review, and 5 for a failed, unverified,
or queued merge action.
