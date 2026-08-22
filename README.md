# carpool

> Renamed from `ai-lanes` on 2026-08-22 (the private stack it mirrors is now one package, `carpool`). This public snapshot still carries the `ai_lanes` package name and the pre-August tooling; a full port of the unified `carpool` CLI (lanes auto-assigned, app-home/lane split, free-plan guard, session-mirror health, `carpool login`) is in progress.

Run agentic AI work across multiple subscription accounts without rotating logins.

If you use Claude Code and Codex on consumer subscriptions, capacity comes in
per-account windows. When one account hits a limit, the usual move is logging
out and back in as another — which kills running agents and eats your time.
carpool replaces login rotation with *lanes*: each account's identity lives in
a token, workers pin an identity per process, and a router picks lanes for you.

## What's in the box

| tool | what it does |
|------|--------------|
| `delegate` | One entry point for dispatching agent work. Classifies the prompt (fable / review / build / sweep), picks a model family and an account with headroom, injects standing orders, and shells to the hardened runners below. `--why` explains routing and capacity decisions; `--dry-run` shows the command without running it. |
| `ai-lanes` | Quota/auth monitor across all lanes, including `capacity [--json]` for a cross-family 5h + weekly view, observed limit events, and alerting via a configurable `notify_cmd`. |
| `codex-pick` | Prints the best `CODEX_HOME` right now (distinct-account and rate-window aware). |
| `claude-pick` | Prints the best enrolled Claude lane for direct wrapper use. |
| `codex-run` | Hardened `codex exec`: retries transient deaths, fixes the worktree sandbox-git failure, snapshots uncommitted work to `refs/codex-salvage/*` on any exit, supports `-e <effort>`. |
| `claude-lane` | Hardened headless `claude` pinned to a lane via `CLAUDE_CODE_OAUTH_TOKEN`: retries transients, records transcript token usage, fails fast on hard limits (rc 4) and dead tokens (rc 5), salvages to `refs/claude-salvage/*`, and checks the transcript afterward for silent model substitution. |

Runtime is stdlib-only Python. macOS keychain is the default secret store; any
command that speaks `get`/`set`/`del` can replace it in config.

## Quick start

```bash
git clone https://github.com/MaxGhenis/ai-lanes && cd ai-lanes
mkdir -p ~/.config/ai-lanes
cp accounts.example.json ~/.config/ai-lanes/accounts.json  # edit with your accounts
export PATH="$PWD/bin:$PATH"
```

Enroll a Claude lane (one browser approval per account, once):

```bash
claude setup-token   # sign into the TARGET account in the browser, approve
# store the printed token under the configured secret prefix, e.g.:
security add-generic-password -a agent -s claude-quota-you@example.com -w 'sk-ant-oat01-...'
# then verify the lane actually serves inference:
CLAUDE_CODE_OAUTH_TOKEN=... claude -p "Reply with exactly: LANE-OK" --model haiku
```

Codex lanes are `CODEX_HOME` directories, one per ChatGPT account
(`codex login` inside each; never bind one account to two homes — token
refresh in one revokes the other).

Dispatch:

```bash
delegate "Fix the failing retry test"                    # → build → codex lane, ultra effort
delegate "Review and assess this diff"                   # → review → Sol, read-only audit
delegate "Final review and launch verdict"               # → fable floor → Claude, read-only
delegate "For each of the 40 files, verify the header"   # → sweep → cheap codex model
delegate -m fable -a you@example.com -C ~/proj -p task.md -o out.md   # full override
ai-lanes capacity                                        # human cross-family table
ai-lanes capacity --json                                 # machine-readable view
```

## Routing rules

Classification is deliberate, transparent regex — not a model call — so every
route is explainable and testable:

- **fable floor** (`as max`; voice/email/blog/essay/prose; adjudication,
  verdict, final review, merge gate, launch/send; design/strategy/wdyt) → Claude,
  read-only by default. Any fable signal wins over every other class.
- **review** (review, assess, critique, audit, evaluate, referee) → Sol,
  read-only, with a defensive correctness-and-completeness audit preamble.
- **sweep** (per-item mechanical verification at scale) → the cheap fast model.
- **build** (everything else) → the strong codex model at max reasoning
  effort, workspace-write, behind whatever tests and gates your prompt sets.

Precedence is `fable > review > sweep > build`: review beats build, while the
fable floor cannot be diluted by mixed signals. Build, review, and sweep are
elastic: if Codex is exhausted or limited and a Claude lane has headroom,
`delegate` crosses families automatically and calls it out in `--why`. Fable
work instead fails fast with the earliest reset when every Claude lane is
limited; it never silently downgrades to Sol.

## Capacity model

`ai-lanes capacity [--json]` reports one row per account across both families,
including 5h and weekly use, any learned token capacity, cooldown, and a
confidence label. Live Codex usage comes from each `CODEX_HOME`; the active
Claude desktop login is also probed live through its keychain app token. Live
readings have `live` confidence and are cached for 120 seconds so delegation
stays fast.

Claude setup-token lanes are different: their inference-only scope cannot read
usage. After each run, `claude-lane` instead appends transcript token totals to
`lane-usage.jsonl`, and the monitor computes rolling 5h and 7d estimates per
email. Before calibration, `estimated` readings report raw token sums with no
claimed capacity. A hard-limit observation records the window totals and
reset, learns the largest observed capacity for each window, and gives the
calibrated reading `observed` confidence. Existing rc 4/5 cooldowns supply
`limited_until`.

## Findings

Empirical constraints this design is built around (all reproduced, July 2026):

- **`claude setup-token` tokens are inference-only.** They carry scope
  `user:inference`: the OAuth usage and profile endpoints return 403, so
  per-lane quota probing and token↔account identity checks are impossible.
  The only validation that means anything is a live inference call, and the
  only identity gate is reading the account name on the OAuth approve page.
- **The env var wins completely.** With `CLAUDE_CODE_OAUTH_TOKEN` set, the
  keychain login is ignored — a lane process is only ever the account whose
  token it holds. The app's own session token is *rejected* for CLI inference,
  so lanes and the desktop login coexist without interference.
- **Serving model ≠ configured model.** A session can be silently served by a
  different model after a safety-classifier fallback while its environment
  still reports the configured one. The transcript's per-message `model` field
  is ground truth; `claude-lane` checks it after every run.
- **The setup-token TUI wraps the token it prints** (~79 columns) even in a
  raw `pipe-pane` stream. Capture in a terminal ≥130 columns wide, or rejoin
  the fragments and validate by inference.

## License

Apache-2.0.
