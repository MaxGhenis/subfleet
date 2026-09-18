# Native workflows and subfleet

Measured 2026-09-17 on Claude Code 2.1.274, codex-cli 0.153.3, and subfleet v1
(HEAD `519c9fc7` plus the working tree; line numbers refer to that tree).

How to read the evidence:

- A bare `path:line` is source code or a test in this repo, read that day.
- `observed` and `computed` come from this machine's state that day: the run
  ledger, Claude Code session directories, tool results.
- `doc` is Anthropic's documentation, fetched that day:
  [workflows](https://code.claude.com/docs/en/workflows.md) unless another
  page is named.
- Standing mechanisms are in the present tense. The experiment and the gauge
  readings are dated observations and stay in the past tense.
- Sizes follow their sources: GB for the native store, GiB for the ledger cap.

The README is not used as evidence. After drafting, four adversarial verifiers
checked 63 statements against the code: 50 confirmed, 13 imprecise, 0 refuted.
The 13 corrections are applied below. A completeness critic then listed 25
overreaching sentences in the draft; those are fixed too. Raw verdicts and
measurements are in
[native-workflows-vs-subfleet.evidence.json](native-workflows-vs-subfleet.evidence.json).

## Verdict

Native workflows orchestrate. Subfleet dispatches. Each lacks what the other
has.

- A **native workflow** is a JavaScript script that owns control flow: fan-out,
  joins, loops, schema-validated returns, and a journal that replays completed
  agents (`doc`). Its workers are sidechains of the launching session
  (`observed`). Runs count toward that login's plan limits (`doc`), and
  session-limit errors are 85% of all recorded worker errors (`computed`). No
  model id from another provider appears in 707 state files (`computed`). A
  run stops with its process unless the session is moved to the background
  (`doc`).
- **`subfleet run`** launches one worker process per call, on a model set by
  the task-by-tier grid and an account chosen by capacity rules, detached from
  the session, ledgered, with a completion notice pushed back. It has no
  steps, no dependencies, no fan-out helper, no output schema, no result cache
  and no concurrency cap. Orchestration across dispatches belongs to the
  caller: a session or a script.

| The work | Use |
|---|---|
| Needs script control flow (joins, dedup, verify loops, schema returns), and the agents are few and light. One Fable reading agent cost about 236,000 tokens here, and 11 of them moved the 5-hour gauge by up to 54 points. | Native workflow |
| One job with no join: anything over 10 minutes, anything that writes code, anything that must outlive the session, anything that should spend another account or a GPT model. Agreement on an exact plan or PR revision. | `subfleet run --task T --tier R`, `subfleet gate` |
| Script control flow and heavy agents. | Native workflow whose agents are thin dispatchers calling `subfleet run` ([measured below](#composition-a-workflow-that-dispatches-through-subfleet): 40,600 Haiku tokens per dispatcher on the login; the reading ran on another account) |

Login gauges, from the desktop app's usage card for this session's account
(account-wide, so each window is an upper bound for the run inside it, and
each window also contains the main session's own turns):

| Reading | 5-hour | Weekly, all models | Weekly, Fable |
|---|---|---|---|
| About 15:19 EDT, before the native mapping run | 9% | 27% | 45% |
| About 15:41, after it (11 agents, 2,410,406 subagent tokens) | 63% | 40% | 70% |
| About 16:04, after the composition run (4 Haiku dispatchers at 162,663 tokens, 1 Fable critic at 307,837) | 81% | 44% | 77% |

In the second window four Opus verifiers read 8.85M tokens (subfleet's lane
accounting, which includes cache reads) on a different account.

## What was run

| | Native workflow | Subfleet |
|---|---|---|
| Id | `wf_11aabbdb-9e5` | `20260917-152230-h2h-gates-reader` |
| Shape | 11 agents in one `parallel()` barrier: 8 subsystem readers over this repo and `~/subfleet-v2`, 1 official-docs reader, 1 on-disk-evidence reader, 1 run-ledger statistician | 1 dispatch: the agreement-gates reader prompt, verbatim, plus a prose JSON contract |
| Model | `claude-fable-5-1` for 10 agents, inherited from the session. The docs reader used `agentType: claude-code-guide` and ran on `claude-haiku-4-5-20251001` (state file). | `claude-opus-5`, routed by `--task research --tier standard` |
| Account | The session's login | An auto-picked lane; its row in the routing decision has `active: false` (`observed`) |
| Wall clock | 929.7 s for the run; 294 s for the gates reader | About 10 s to route, pre-create the ledger entry and detach; 411 s to finish |
| Result | 11 of 11 schema-valid objects, 0 errors, 489 tool calls | rc 0; the output parsed as JSON on the first try with no enforcement |
| Gates reader output | 40 claims; 4 README contradictions; every cited range resolves | 57 claims; 4 README contradictions; every cited range resolves |
| Completion signal | `task-notification` injected into the session | Inbox push; the ledger confirmed it in the transcript in the same second (`surfaced_by: transcript`) |

Confounds in the head-to-head, which measures mechanics and says nothing about
model quality:

1. Different models (Fable and Opus), n = 1.
2. Different tools. The subfleet reader ran on a read-only lane: Read, Glob,
   Grep and web tools, no Bash, no CLAUDE.md, no hooks. The native reader had
   the session's full tool set and hooks.
3. Different contracts: a JSON Schema against a prose description.
4. Different load: the native reader shared one login with 10 siblings.
5. The prompt asked for 15 to 40 claims. The subfleet reader returned 57, and
   4 of the 10 native maps also exceeded 40, because the schema carried the
   bound only in a description. Schema-valid does not mean bound-respecting.

Both readers reported that served-model attestation is enforced for Fable
peers only (`consensus.py:1164-1169`).

Two unplanned observations:

- The session's project folder was changed while the workflow was in flight.
  Claude Code moved the whole session directory, including the live run
  directory and journal, to the new project slug, and the run completed. Paths
  captured at launch went stale within minutes; the completion notification
  still cited the old ones.
- The account behind this session's usage card is not the account subfleet
  flags as the active login. At 15:22:20 subfleet's capacity snapshot showed
  its active row at 100% of the 5-hour window, 25% weekly, with the weekly
  reset on 2026-09-24; the usage card showed 9%, 27%, and a weekly reset on
  2026-09-23. The cause is not determined. While it holds, subfleet's active-login ordering
  protects a different account from the one a desktop session spends.

## Mechanism comparison

| | Native workflow | Subfleet v1 |
|---|---|---|
| **Control flow** | The script decides what runs next; intermediate results stay in script variables (`doc`). | One call is one dispatch. The parser exposes no step, dependency, fan-out or schema flag, and `main()` is one dispatch with a lane-retry loop (`delegate.py:328-370`, `841-1442`). `gate` is the only multi-round construct built on it (`consensus.py:1142`); `handoff` is a second caller that dispatches one run (`handoff.py:612`). |
| **Worker** | A sidechain of the parent session: every transcript record has `isSidechain: true` and the parent's `sessionId`; `agentType` is `workflow-subagent` (`observed`). OS-process identity was not checked. | A separate OS process: `claude -p --output-format json` or `codex exec -o` (`bin/subfleet-claude:811-831`, `bin/subfleet-codex:559-570`). Inside a session it starts with `nohup` and `start_new_session=True` after the ledger entry exists (`delegate.py:765-789`, `1142-1193`). |
| **Concurrency** | Up to 16 concurrent agents, 1,000 per run, 4,096 items per call (`doc`). Across 707 recorded runs none exceeds 16 in flight; of the 164 runs with at least 30 timed agents, 156 peak at exactly 16 (`computed`). | No cap and no queue in dispatch. `in_flight` is a sort tiebreak for Claude lanes and does not order Codex dispatch (`capacity.py:1306-1324`, `delegate.py:555-608`). Observed maximum: 35 overlapping runs; each of the two 34-wide batches used 2 lanes (`computed`). |
| **Models** | Per-agent `model` override; the default is the session model (`doc`). Only Anthropic model ids appear in the persisted data (`computed`). | Task-by-tier grid: lookup, research, review, build → Luna, Luna, Opus, Astra; sweep → Terra, Terra, Terra, Astra; authored prose, strategy, adjudication → Fable (`delegate.py:86-106`). Chains move upward only (`delegate.py:107-112`, `735-746`). Two moves sit outside the chains: Terra work overflows to Claude Opus when the whole Codex fleet is limited (`delegate.py:941-967`), and the Fable reserve can replace even a pinned `-m opus` with Fable (`delegate.py:1289-1334`). Fable work refuses to leave Claude (`delegate.py:1074-1106`). |
| **Whose quota** | Runs count toward the plan's usage and rate limits (`doc`). Of 5,103 worker errors on this machine, 4,339 are the launching account's "You've hit your session limit" (`computed`). The lane-usage ledger, cooldowns and run ledger are written only by subfleet's runners, and the reserve is consulted only by `subfleet run` (`bin/subfleet-claude:596-610`, `delegate.py:611-620`), so in-session fan-out leaves no record there and meets no reserve check. | The lane's setup token is fetched from the keychain and injected as `CLAUDE_CODE_OAUTH_TOKEN` with API keys unset (`bin/subfleet-claude:500-508`, `813-814`, `826`). Codex lanes are a `CODEX_HOME` each; API-key homes are refused with rc 7 (`bin/subfleet-codex:291-313`). In `subfleet run`'s ranking the active login sorts after every lane except Fable-stranded ones and carries a 10-point handicap (`delegate.py:568-607`). The Fable reserve withholds non-Fable Claude models unless `slack = (1 − shared) − 2.0 × (1 − fable) ≥ 0.05`; an unmeasured account is withheld, an account with no Fable window passes, and `reserve-policy.json` can override the defaults (`reserve.py:58-86`, `175-188`, `380-382`). |
| **Durability** | A run stops with the session unless the session is moved to the background; a relaunch replays completed agents, and the first changed or failed agent reruns along with every agent started after it (`doc`). 74 `stopped` notifications since 2026-08-01 carry a note that cannot distinguish a deliberate stop from a process exit (`observed`). | The worker outlives the session. The ledger entry is finalized by the runner's EXIT trap. A runner killed without its trap stays unfinished until someone runs `subfleet runs reap`, which has no automatic caller in `subfleet/` or `bin/` (`run_ledger.py:849-875`, `cli.py:811`); an entry with no recorded pid is never finalized by anything (`run_ledger.py:862-863`). There is no result cache: every dispatch allocates a new run and runs the provider again (`run_ledger.py:231-308`). "Resume" means continuing a Codex thread, synchronously, or briefing a fresh agent from a transcript (`resume_codex.py:204`, `handoff.py`). |
| **Completion signal** | A `task-notification` is enqueued into the owning session (`observed`). Of 868 launches since 2026-08-01, 154 have no notification record in the same transcript: 67 map to killed runs and 87 have no state file (`computed` by the mapping run; the join on task id is approximate). | A socket push into the session's inbox, addressed by session id at finish time so it survives a restart under a new pid (`notify.py:183-217`, `357-440`). The sender reads no acknowledgement (`notify.py:366-383`), so a pushed notice is also written to `notices/<session>.jsonl` and counts as confirmed only on transcript evidence or a hook render. A run whose inline `--attach` waiter is still alive is reported by that waiter and gets no push (`notify.py:978-988`). A follow-up re-pushes once after 300 s, then marks the notice lost and leaves it to the hooks; a dead bypass-mode session gets a one-shot `claude -p --resume` host; a Telegram alert covers dead sessions with pending work (`tickle.py:1388-1628`, `liveness.py:159-247`). |
| **Output contract** | A JSON Schema on `agent()`; up to five attempts, configurable with `MAX_STRUCTURED_OUTPUT_RETRIES` (`doc`), delivered through a StructuredOutput tool call (`observed`). | Free text in a file. Claude success is rc 0 plus a JSON envelope with `is_error == false` and a non-empty `.result` (`bin/subfleet-claude:833-843`); Codex success is rc 0 plus a non-empty `-o` file (`bin/subfleet-codex:571`). Content is not checked. Both installed CLIs offer schema flags (`claude --json-schema`, `codex exec --output-schema`) that the runners never pass (`observed` from `--help`). |
| **Worker context** | Workers inherit the session's hooks, CLAUDE.md and MCP instructions: 215 PreToolUse `hook_success` attachments across the 11 workers, and two of the user's guard rules blocked a worker's Bash calls (`observed`). 41 agent records carry `spawnedWithWorktree` (`computed`). | Read-only Claude lane: the runner passes flags and environment that request plan mode, a tool list of Read, Glob, Grep, WebSearch and WebFetch, empty setting sources, safe mode, an empty strict MCP config, and disabled CLAUDE.md and memory. No Bash (`bin/subfleet-claude:789-825`; the tests use a fake `claude`, so the effect inside the CLI is untested). Workspace-write Claude lane: `--dangerously-skip-permissions` and no restricting flags (`bin/subfleet-claude:787-788`). Codex lane: `--sandbox` plus a never-rules guard hook that must pass a preflight or the launch is refused, except for isolated reviews and `SUBFLEET_CODEX_GUARD=off` (`bin/subfleet-codex:484-499`). No runner creates a worktree; concurrent runs pointed at one directory share it (`bin/subfleet-claude:812`, `bin/subfleet-codex:562-567`). |
| **Agreement** | Patterns are written in the script: N skeptics, judge panels, loop-until-dry (workflow-authoring reference, `observed` in session). All voters share one login and one provider. | `subfleet gate`: the main approves an exact fingerprint (plan SHA-256, or PR head and base). One pinned peer reviews in a separate OS process on a lane chosen by `delegate`; nothing in `consensus.py` checks that the account or model differs from the main's. The peer is read-only with its cwd in a neutral temp directory; for PR gates the caller's live checkout is exposed with `--add-dir` and re-verified before and after the round. Verdict parsing fails closed; PR merge rechecks state and uses `--match-head-commit` (`consensus.py:1112-1132`, `1160-1181`). |
| **Audit trail** | `/workflows` progress view (`doc`). On disk: a journal whose result records hold each agent's full return value under a content key, with no timestamps and no error text; and a state file, written only at a terminal state, with per-agent model, tokens, tool calls, timings, attempt and error plus the full script. 94 runs have a journal and no state file (`computed`). | Per-run `meta.json`: routing decision, lane, rc, git HEAD before and after, salvage refs, caller session, notify state; one `decisions.jsonl` line per dispatch attempt, refusals included (`run_ledger.py:271-304`, `509-572`; `delegate.py:1021-1063`). No per-run token count in `meta.json`. Pruning keeps at most 500 runs or 2 GiB and removes finished entries only (`run_ledger.py:21-22`, `614-617`); finished-run history reached back about six days that week (`computed`). |
| **Guardrails** | Worker tool calls pass through the same PreToolUse hooks and permission rules as the main agent (`doc`, `observed`). | The session-side hook parses the command text only and reads no agent identity, so it treats main agent and subagent alike (`bin/subfleet-hook:40-43`). It blocks attached `subfleet codex`, `subfleet claude` and bare `codex exec` in command position, and never matches `subfleet run` (`bin/subfleet-hook:45-68`; `tests/test_hooks.py:100-145`). |
| **Budget** | The script API exposes a `budget` object (workflow-authoring reference, `observed` in session). An advisory "Large workflow" warning appears above 25 agents or 1.5M projected tokens, and the size guideline is advice (`doc`). Both are off on this machine: `ultracode` is `true` and `skipWorkflowUsageWarning` is `true` in `~/.claude/settings.json` (`observed`). | No max-turns, wall-clock or token cap on a worker (`bin/subfleet-claude:826-830`, `bin/subfleet-codex:559-569`). The longest finished run in the ledger is 5.1 h; builds have a p90 of 150 minutes (`computed`). Budget control is about which account pays: headroom floors, cooldowns, the Fable reserve. |
| **Failure handling** | `agent()` resolves to `null` on a terminal API error. From 2.1.271 a run pauses at a usage limit and continues after the reset, only in an interactive subscription session with automatic continue on, a reset within 24 hours and at most two waits; background sessions, `-p`, the Agent SDK and Remote Control are excluded (`doc`). Automatic continue is on by default (`doc`, interactive-mode page) and unset here. 644 session-limit worker errors are recorded in 77 runs on 2.1.271 or later, all from desktop-app sessions, so the pause does not cover them; which condition fails is not determined (`computed`). 234 of the 242 runs with a limit-related worker error still report `completed` (`computed`). | The runner classifies failure text. A hard limit re-picks a lane only when launched with `-A` (unpinned detached dispatches), up to 2 times, and restarts the task in a fresh session; a pinned lane exits 4. Transient errors back off on the same lane. An auth-failure signature exits 5 unless the token still authenticates (`bin/subfleet-claude:852-916`; `delegate.py:1358-1359`). Each trapped exit of a run whose workdir is a dirty git repo writes a salvage ref without touching HEAD or the index, unless the owned prompt copy cannot be removed; with `-b` the same trap force-pushes that commit (`bin/subfleet-claude:337-372`, `437-446`). |
| **Outside a session** | Available in `claude -p` and the Agent SDK; the `ultracode` keyword does not trigger there (`doc`). The CLI has no workflow subcommand or flag (`observed`). | Plain bash and Python. `subfleet run` is synchronous outside a session and detached inside one; `-d` and `SUBFLEET_RUN_DETACH` override either way (`delegate.py:765-789`). `resume-codex` is synchronous everywhere. launchd runs the watchdog, keepalive and revive passes. |

## How each is used on this machine

Native workflows, all of `~/.claude/projects`, recomputed at about 15:55 EDT
(`computed`):

- 798 run directories from 2026-05-30 to 2026-09-17, about 21,000 worker
  transcripts, 8.1 GB on disk (`cleanupPeriodDays` is 3650).
- 707 runs have a state file: 621 completed, 71 killed, 15 failed; 1.357
  billion tokens. Per run: median 12 agents; p90 63; maximum 340.
- No saved workflow exists in `~/.claude/workflows`, dotfiles, or within
  depth 4 of `chief-of-staff`, `PolicyEngine` and `TheAxiomFoundation`.
  13 runs launched a script file kept outside the session directory.
- 95 of 104 resume calls launched. 2 were refused while the run was live, and
  6 failed with "No such tool available: Workflow".

Subfleet run ledger as of about 15:30 EDT (`computed`). The ledger is live;
a re-run 30 minutes later moved each count by under 1%. The 500-run cap makes
the finished entries a sample of 2026-09-11 to 09-17.

- 429 Claude-lane runs and 71 Codex runs: Opus 230, Fable 199, Astra 38,
  Luna 23, Sol 9 (unfinished entries from August), Spark 1.
- Of the 484 runs with a full routing decision, 320 were routed by `--task`
  and `--tier` alone and 158 pinned a model with `-m`.
- rc 0 on 383 of 500 (76.6%). One cancelled 34-wide batch accounts for 34 of
  the 45 SIGTERM exits; without it, 82.2%.
- Finished, non-orphaned runs (n = 482): p50 7.0 min, p90 42.7 min, maximum
  5.1 h. By model: Opus p50 10.8 min, Fable 5.0 min, Astra 9.3 min, Luna
  2.0 min.
- 16 caller sessions dispatched 261 runs. The median session peaks at 1
  concurrent run; the maximum is 34. 141 of the 261 runs started in a burst
  of 3 or more within 60 s.
- Notices for those 261 runs: 52 confirmed (25 in the transcript, 27 by an
  inline waiter), 152 pushed and unconfirmed, 28 parked, 5 lost, 24 none or
  pending.
- 14 runs were promoted, all with Opus blocked by the Fable reserve: 10 to
  Fable and 4 to Astra. 11 of the 14 were caused by lanes that could not be
  measured because a login token had expired.
- Gates: 198 PR and 47 plan; 115 completed, 88 blocked, 26 at
  changes-requested. Median 2 rounds across all gates; 39 of the 115
  completed gates were approved on the first verdict. Fable peers return no
  verdict in 213 of 535 rounds (39.8%); Astra peers in 15 of 325 (4.6%).
  43 of 256 errored rounds are "output outside verdict sentinel".

## Where each one breaks

Native workflows:

- **Workers spend only the launching login.** Session-limit errors are 85% of
  all worker errors, and a run that loses most of its workers to them still
  reports `completed`: one recorded run finished `completed` with 10 of 17
  agents errored (`computed`). The standing configuration makes this the
  default path: `ultracode: true` with `CLAUDE_CODE_EFFORT_LEVEL=xhigh` has
  Claude plan a workflow for each substantive task (`doc`, model-config page;
  `observed` settings).
- **The run belongs to a process.** Unless the session is moved to the
  background, a process exit stops the run, and recovery is a relaunch by the
  model. A background session does not pause at a usage limit (`doc`), so the
  two native mitigations do not combine. Subfleet's own revive code treats a
  launched workflow as armed work that is orphaned when its session dies
  (`tickle.py:151-164`, `liveness.py:194-212`).
- **A failure in the middle of a fan-out reruns finished work.** If A, B, C
  and D start in that order and B fails, a relaunch returns A from cache and
  runs B, C and D again (`doc`).
- **Subfleet cannot see it.** In-session fan-out bypasses lane selection,
  cooldowns, the reserve and the usage ledger; subfleet sees only the active
  login's live percentages.

Subfleet:

- **Fan-out has no support in v1.** The widest batch in the ledger was
  dispatched twice by one session: 34 Opus runs, all SIGTERMed after about
  five minutes, then 34 again, all rc 0. All 10 launch failures (rc 127) came
  from one 21-wide burst (`computed`). Nothing caps, queues or retries a
  batch.
- **The completion push has no receipt.** Delivery means the socket accepted
  bytes (`notify.py:403-404`). Confirm, re-push and lost compensate for that;
  revive and the Telegram alert cover sessions with no live process. All of
  it rests on undocumented Claude Code internals: the session registry, the
  peer-token key file, the socket protocol, the message envelope and
  transcript field names (`notify.py:8-18`, `258-274`). A Claude Code
  behaviour change, measured on 2.1.263, already broke lane detection once
  (`lanes.py:36-47`).
- **A detached run never changes model at runtime.** On the Codex path,
  promotion after a quota exit is guarded by `mode != "detached"`
  (`delegate.py:1255`); on the Claude path, cooldown, lane retry and
  promotion all sit inside the synchronous branch (`delegate.py:1375-1434`).
  An in-session dispatch, `--attach` included, relies on the runner's own
  lane re-pick.
- **Reaping is manual and slow.** The ledger's 7 orphaned entries were
  finalized 9 to 48 hours after they started and account for about 251 of
  its 395 recorded hours, which is reap latency (`computed`). 9 entries from
  August have no pid, show RUNNING, and inflate `in_flight` for three Codex
  homes (`run_ledger.py:991-1010`, `computed`).

## Composition: a workflow that dispatches through subfleet

The verification pass for this document ran as the test. Workflow
`wf_78f6f13c-21d` started four Haiku agents with one job each: run
`subfleet run --attach --task review --tier standard -C <repo> -p <prompt> -o
<out>` in one Bash call with a 600 s timeout, then report the run id, rc and
output size. A fifth agent, the critic, ran in-session on Fable.

| | Result (`observed`) |
|---|---|
| Hooks | No Bash call was blocked (4 of 4). The attached-runner guard never matches `subfleet run` (`tests/test_hooks.py:124-125`). |
| Routing | 4 of 4 dispatched to Opus, all on one lane: nothing spreads a batch across lanes. |
| Duration | 221 s, 446 s, 446 s and 496 s, all under the 600 s Bash cap, so no `subfleet wait` loop ran. |
| Output | 4 of 4 parsed as JSON on the first try with no schema; 63 verdicts. Counting the head-to-head, 5 of 5 subfleet outputs that day parsed without enforcement. |
| Login cost | 40,627 to 40,752 Haiku tokens and 3 tool calls per dispatcher. A native Fable reader averaged 236,484 tokens. |
| Lane cost | 1.17M to 4.03M tokens per verifier by subfleet's lane accounting (cache reads included), 8.85M in total, on another account. |
| Caller | Each run's `caller.session_id` was the parent session: workers share the session id. The live waiter suppressed the push (`surfaced_by: inline-waiter`). |

What this does not show:

- **Runs over 600 s.** The Bash tool caps one call at 600 s. Opus runs have a
  p50 of 648 s in the ledger and research tasks 806 s, so more than half of
  Opus-tier dispatches outlast one call. When the waiter dies the run
  continues and the notice is pushed into the main session, one per dispatch.
  The shape for long work is a plain `subfleet run`, then
  `subfleet wait <id> --timeout 540` in a loop until the exit code is not
  124. That loop is untested here.
- **Replay.** Subfleet has no result cache. A workflow relaunch that reruns a
  dispatcher agent dispatches again unless the agent first checks for a
  non-empty output file.

## Subfleet v2 adds caps and recovery and stays a dispatcher

`~/subfleet-v2` (last commit 2026-09-06) is a daemon that owns a SQLite store
of jobs, attempts, leases and notices. Around a single job it adds what v1
lacks: request ids that are idempotent on a payload digest; retry and
re-route within `max_attempts` for unpinned jobs; a fleet cap of 4 active
attempts, 2 per lane, and 1 for a lane with no fresh provider reading; a
wall-clock bound (`max_wall_s`, 6 h by default); a git worktree per writable
job; crash recovery that re-adopts a running attempt (`daemon.py`,
`scheduler.py:207-214`, `default_policy.json:96-109`; verifier V4).

It adds no orchestration. The schema has no dependency table and no
result-schema column (`store_schema.sql`). `dependency` and `approval` are
enum values that no code writes (`contracts.py:37-41`). Sibling jobs under
one parent run one at a time by default, from a cap key that the default
policy does not list (`scheduler.py:71-95`). The deliverable is free text,
except a gate verdict, which is validated by hand-written code
(`gate/verdict.py`). The plan of record rejects adopting a workflow engine
(`docs/plan.md:29`, a plan document).

As of 2026-09-17 it is not running: `~/bin/subfleet` resolves to v1, no
daemon process or launchd job exists, and `~/.subfleet` holds only `logins/`
and `tmp/` (`observed`).

## Defects and drift found along the way

Each item was confirmed against the code by a verifier, or by a command run
that day where noted. Ordered by what to fix first.

1. **`bin/subfleet-claude -b main` force-pushes.** The salvage trap runs
   `git push -qf <remote> <sha>:refs/heads/<branch>` on every trapped exit,
   success included, and nothing refuses `main` or `master`.
   `bin/subfleet-codex:148-152` refuses both (`bin/subfleet-claude:367-371`).
2. **An unflagged prompt can run with permissions skipped.** With no `--task`
   or `-t`, a prompt with no review, Fable or sweep signal is classed
   `build`, which means workspace-write, which on a Claude lane is
   `--dangerously-skip-permissions` in a shared directory
   (`delegate.py:152`, `890`; `bin/subfleet-claude:787-788`). The same
   classifier sends a prompt to read-only Fable on words such as send,
   launch, design, email or voice (`delegate.py:22-39`). 124 of 484 ledger
   runs had no `--task`.
3. **Ledger lifecycle.** `reap_orphans` has one caller, the `runs reap`
   command, and a reaped run sends no completion notice
   (`run_ledger.py:849-875`, `cli.py:810-815`). An entry with no pid shows
   RUNNING indefinitely, is skipped by reap, is never pruned, and counts as
   in flight. `subfleet wait` exits 0 for a run reaped with rc −9
   (`cli.py:864-870`). `kill --grace 0` sends SIGKILL at once although its
   help says "signal only" (`run_ledger.py:897-916`).
4. **The Fable reserve has a side door, and most of its promotions come from
   expired tokens.** `reserve.filter_lanes` has one production caller. The
   runner's mid-run re-pick shells out to `subfleet pick claude --model`,
   which never consults the reserve and gives the active login only a
   removable 10-point handicap (`bin/subfleet-claude:472-498`,
   `cli.py:229-368`). 11 of 14 promotions in the ledger week moved Opus work
   to Fable or Astra because a lane was unmeasured, which spends the
   resource the reserve protects.
5. **Learned capacities never update.** `learned_capacities` skips hard-limit
   events with a model scope, and the runner always passes `--model`
   (`capacity.py:474-498`, `bin/subfleet-claude:599-605`). An rc 4 cooldown
   is stored at the scope of the running model even when the limit text is
   account-wide (`capacity.py:659-687`).
6. **Gates.** Served-model attestation and the downgrade marker are enforced
   for Fable peers only (`consensus.py:1164-1169`). There is no `gate list`
   or `gate abort`, and a live lease has no timeout;
   `gate continue <id> --dry-run` is the only read surface. A default of
   unlimited rounds produced one PR gate with 188 rounds (`computed`).
   `DEFAULT_MAX_ROUNDS` is unreferenced.
7. **Guards.** `SUBFLEET_ATTACHED_OK=1` passes anywhere in the command text,
   a trailing comment included; `-d` anywhere in a compound command passes;
   `bash -c "codex exec …"` is not matched (`bin/subfleet-hook:45-67`). The
   block message still advertises `-m sol` (`bin/subfleet-hook:68`). The
   drift pin `test_prefilter_literal_identical` fails today: the Claude-side
   hook gained a pattern-kill rule on 2026-09-15 with no port (`pytest` run
   that day).
8. **Runners.** A served-model downgrade never fails a Claude run; the only
   signals are a `.DOWNGRADED` file and a stderr warning. With `-A`, an auth
   failure still exits 5 at once, though the inline comment describes
   rotation (`bin/subfleet-claude:857-878`). `bin/codex` picks a home once
   and execs, so a bare `codex exec` cannot re-pick mid-run.
   `resume-codex` runs synchronously, so a resume started from a session
   dies with it (`resume_codex.py:204-220`).
9. **Signaling.** `cmd_session_hook` marks notices surfaced before it prints
   the hook JSON (`cli.py:948-994`). `notify.prune_notices` has no production
   caller, and `decisions.jsonl` has no retention: 359 MB, 68% of it from a
   two-day retry storm in August (`computed`).
10. **README drift.** Kill is documented as SIGTERM only; the code escalates
    to SIGKILL after a 10 s grace. Handoff targets include `luna`. The
    resume-nudge cooldown is 90 s where the README says 10 minutes. The
    ledger's notify field has eight states where README line 396 lists
    three. A hard-limit observation does not upgrade a window to `observed`
    confidence. The README says a bare `codex exec` re-picks mid-run.
11. **Dead code.** `delegate.pick_fable_lane` has no caller outside tests.
12. **v2.** `max_tokens_observed` is stored and never read. A `resume` job
    gets a fresh session, because `_launch` calls `resume_launch` only for
    `revive`. `notify_push.py` is imported by nothing. `lanes probe` lists
    lanes and probes nothing. The default policy maps the trivial and easy
    tiers to Haiku and Sonnet, which predates the 2026-09-11 Luna change.

## Recommendations

Each carries the observation that would reverse it.

1. **Write the three-way routing rule into the model-routing section of
   `~/.claude/CLAUDE.md`, and resolve its conflict with `ultracode: true`.**
   Today the settings make every substantive task a native workflow on the
   login, and the instructions say delegation-scale work goes to lanes.
   Either turn ultracode off or make the composition shape the default for
   ultracode-planned workflows. Effort: minutes. Reverse if runs on 2.1.271
   or later show near-zero session-limit worker errors (644 today) and a
   10-agent run moves the 5-hour gauge by under 20 points.
2. **Save the dispatcher workflow once the wait loop is tested.** The script
   owns control flow, schema and the journal; each agent dispatches, waits,
   checks for an existing output before dispatching, and returns the parsed
   object. Effort: hours. Drop it if a dispatcher's login tokens exceed about
   20% of a native reader's (17% today), or if per-dispatch notices in the
   main session cannot be suppressed.
3. **Add `--schema FILE` to `subfleet run`**, forwarded as
   `claude -p --json-schema` and `codex exec --output-schema`, with gate
   verdicts as the first consumer: 43 of 256 errored gate rounds are
   output-format failures. Effort: hours. Abandon if the flag conflicts with
   plan mode or the envelope handling.
4. **Refuse `-b main|master` in `bin/subfleet-claude`.** Effort: minutes.
5. **Make the ledger lifecycle automatic.** Call `reap_orphans` from the
   120 s revive pass, send the completion notice from the reap path,
   finalize pid-less entries older than 24 hours, make `wait` exit non-zero
   for rc < 0, and raise `MAX_RUNS` (500 runs use 65.5 MB of the 2 GiB cap;
   only 107 of 968 gate round run ids still resolve). Effort: hours. Skip
   the launchd hook if a job outside this repo already reaps.
6. **Add a per-lane in-flight ceiling after the zombies are reaped**, with
   v2's defaults (4 fleet, 2 per lane, 1 unmeasured) as the starting point.
   Let native workflows be the batch engine. Effort: hours. Build a batch
   verb only if composition does not absorb fan-out after two weeks.
7. **Hold the reserve everywhere and keep it measured.** Apply
   `filter_lanes` inside `pick claude --model <non-fable>`, and alert when
   lanes sit unmeasured on an expired token. Effort: hours. The re-pick
   exposure is small in this sample (19 rc 4 exits: 16 Fable, 3 Opus); if a
   month shows under one non-Fable re-pick a week, document it and leave it.
8. **Two questions to test.** Does the usage-limit pause apply to desktop-app
   sessions? Which account does a desktop session spend, given that the
   usage card and subfleet's active row disagreed today?

Briefs for the first three fixes are in
[native-workflows-vs-subfleet.followups.md](native-workflows-vs-subfleet.followups.md).

## Reproduce

- Native mapping run: script, journal and per-agent transcripts are under
  `~/.claude/projects/-Users-<user>-chief-of-staff-subfleet/4983a034-b9c8-4c7a-a935-3f29bb464ad2/`
  (`workflows/scripts/map-subfleet-vs-native-workflows-wf_11aabbdb-9e5.js`,
  `subagents/workflows/wf_11aabbdb-9e5/journal.jsonl`). The composition run
  is `wf_78f6f13c-21d` in the same directory.
- Subfleet runs: `subfleet runs show 20260917-152230-h2h-gates-reader`, and
  `…-verify-v1` to `-v4`. At that week's volume the ledger keeps finished
  runs for about six days, so the measurements and all 63 verdicts are
  copied into
  [native-workflows-vs-subfleet.evidence.json](native-workflows-vs-subfleet.evidence.json).
- Ledger statistics:
  [native-workflows-vs-subfleet.ledger-stats.py](native-workflows-vs-subfleet.ledger-stats.py)
  reads `meta.json` and `gate.json` metadata only, never prompts or outputs,
  and strips account emails before it categorizes.
