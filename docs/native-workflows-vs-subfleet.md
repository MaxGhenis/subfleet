# Native workflows and subfleet

Measured 2026-09-27 to 2026-09-28 (UTC 00:10 to 01:00 on the 28th) on Claude
Code 2.1.280 (CLI; the desktop session that ran the measurement was 2.1.281),
codex-cli 0.153.3, and subfleet v2 release `20260927T213553Z`. That release is
built from `MaxGhenis/subfleet-v2` at `b053e3de` (branch `release/217`), and
its installed package is byte-identical to that commit's `subfleet/` tree
(`diff -r`, `observed`). The v2 repository is private; line numbers refer to
that commit. The 2026-09-17 edition of this document compared native workflows
with subfleet v1 and is in this file's git history.

How to read the evidence:

- A bare `path:line` is v2 source or a test at `b053e3de`, read this session.
  v1 behaviour is stated only as "the 2026-09-17 edition found", because the v1
  tree was not re-read.
- `observed` and `computed` come from this machine's state on those dates: the
  v2 store `~/.subfleet/state.sqlite3` (opened read-only), `subfleet status`,
  `subfleet lanes list`, `subfleet why`, Claude Code session directories, and
  tool results. The `computed` figures are reproduced by
  [native-workflows-vs-subfleet.ledger-stats.py](native-workflows-vs-subfleet.ledger-stats.py).
- `doc` is Anthropic's documentation, fetched 2026-09-27:
  [workflows](https://code.claude.com/docs/en/workflows.md) unless another
  page is named.
- The v2 README, `docs/plan*.md`, `docs/acceptance-contract.md` and reports are
  plans and claims. They are not used as evidence.
- Standing mechanisms are in the present tense; dated readings are in the past
  tense.
- Failure text from the providers is paraphrased with hyphenated compounds
  ("session-limit error"). The reason is in
  [Where subfleet v2 breaks](#subfleet-v2), item 1.

Verification: ten Opus 5.5 readers mapped the v2 tree row by row, an
eleventh re-read Anthropic's workflow documentation, and one adversarial
Opus 5.5 verifier per v2 reader re-opened every cited range. Of 577
verdicts, 446 confirmed, 131 were imprecise and 0 were refuted. Every
correction is applied below, and the headline findings (the classifier
ordering, the disabled-lane verdicts, the desktop-flag drift, the capacity
snapshot) were re-checked by hand. Independent Opus reviewers dispatched
through `subfleet run --task review --tier standard` then read the draft; their
findings and the resulting changes are listed under
[Independent review](#independent-review). Raw verdicts and measurements are in
[native-workflows-vs-subfleet.evidence.json](native-workflows-vs-subfleet.evidence.json).

## Verdict

Native workflows still orchestrate and subfleet still dispatches. v2 closed
most of v1's mechanical gaps: it queues, caps, batches, retries, recovers and
isolates writers. What decides the choice now is capacity and correctness at
the edges, not missing features.

- A **native workflow** is a JavaScript script that owns control flow: fan-out,
  joins, loops, schema-validated returns, nested saved workflows, and a journal
  that replays completed agents (`doc`). Its workers spend the launching login
  (`doc`). Since 2026-09-17, 1,477 of its 1,829 worker errors were
  session-limit errors, all on Claude Code 2.1.271 or later, the first version
  with the documented plan-limit pause (`computed`, `doc`). 239 of the 242
  runs that lost workers to plan limits still report `completed`
  (`computed`). Across this session's own mapping run, 21 Opus 5.5 agents and
  5.26M tokens, the 5-hour gauge went from 13% to 60% (`observed`).
- **Subfleet v2** is a launchd daemon over a SQLite store. `subfleet run`
  queues one job; the daemon admits it through tier-ordered FIFO passes under
  fleet, lane, turn and parent caps, runs it under a guardian on a lane chosen
  from policy, retries it within `max_attempts`, recovers it across daemon
  restarts, writes changes to a per-job worktree, and reports completion
  through Claude Code hooks. It still has no steps, dependencies, joins,
  output schema or result cache; `run --batch` submits independent jobs
  (`subfleet/cli.py:892-1096`, `subfleet/protocol.py:88-124`,
  `subfleet/store_schema.sql:66-107`).
- **On the night of the measurement, subfleet could not place work.** At
  00:20Z it held 35 pending jobs with no open lane. The binding limit was the
  per-lane cap: every lane was unmeasured, which allows one attempt, because
  the usage timer never probes a busy lane and the Claude usage endpoint was
  answering HTTP 429 on nine lanes (`observed`; `subfleet/timers.py:326-335`,
  `subfleet/scheduler.py:452-455`). Since the 2026-09-19 cutover, time from
  submit to first start had a p50 of 2.3 min and a p90 of 1.97 h (`computed`).
- **Composition works when the daemon answers.** The saved `subfleet-fanout`
  workflow now submits a whole batch through one Haiku dispatcher for 43,000
  to 66,000 login tokens, whatever the batch size. Of its 10 batch-era runs, 5
  met a daemon that refused, crashed or stopped answering (`computed`).

| The work | Use |
|---|---|
| Script control flow (joins, dedup, verify loops, schema returns) with a few agents. Budget about 250,000 login tokens per Opus 5.5 reading agent; the 5-hour gauge rose up to 47 points across 21 of them here. | Native workflow |
| One job with no join: long, writes code, must outlive the session, should spend another account or a GPT model. Agreement on an exact plan or PR revision. Check `subfleet status` first: with no open lane the job waits in the queue, for hours at the p90. | `subfleet run --task T --tier R`, `subfleet gate` |
| Script control flow and heavy agents, when `subfleet status` shows open lanes. | Native workflow that calls the saved `subfleet-fanout` (one Haiku dispatcher per batch) |

Login gauges, from the desktop app's usage card for this session's account.
They are account-wide, so each window is an upper bound for the run inside it
and also contains the main session's own turns.

| Reading | 5-hour | Weekly, all models | Weekly, Fable |
|---|---|---|---|
| About 00:14Z, before the mapping run | 13% | 55% | 0% |
| About 00:52Z, after it (21 agents, 5,255,544 tokens, 36 min) | 60% | 68% | 0% |

## What changed since 2026-09-17

- Subfleet v2 replaced v1 on 2026-09-19. v1's per-call runners, JSON ledger and
  socket push are gone; the daemon, store, admission queue, guardians,
  worktrees, hooks and gate service replace them.
- The live `~/.subfleet/policy.json` overrides the shipped default whole
  (`subfleet/daemon.py:480-484`), although caps and sections it omits still
  take code defaults (`subfleet/policy.py:218-219`, `311-327`). It raises the
  fleet cap from 4 to 16, routes lookup, research, review and build through
  Luna, Luna, Opus, Astra, routes the three writing tasks to Opus, empties the
  Fable reserve and turns reset credits off (`observed`).
- The saved dispatcher workflow exists (`~/.claude/workflows/subfleet-fanout.js`,
  2026-09-22) and has 12 recorded runs; see
  [Composition](#composition-the-saved-subfleet-fanout-workflow).
- Native workflows gained configurable concurrency
  (`CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS`, 1 to 256, from 2.1.269), hook
  input that names the calling agent, nested `workflow()` calls, saved and
  plugin-distributed workflows, and a narrower `ultracode` keyword trigger
  (`doc`).

## Mechanism comparison

| | Native workflow | Subfleet v2 |
|---|---|---|
| **Control flow** | The script decides what runs next; intermediate results stay in script variables; `workflow()` runs a saved workflow inline, one level deep (`doc`; workflow-authoring reference). | One `run` sends one `submit`; the daemon inserts one `queued` job row and writes its prompt and manifest (`subfleet/daemon.py:1330-1340`, `1369-1433`). Admission later adds one attempt per placement (`daemon.py:3592-3598`). No flag, wire field or table expresses a step, dependency, join or output schema (`subfleet/cli.py:2690-2750`, `subfleet/protocol.py:88-124`, `subfleet/store_schema.sql:66-107`). The `dependency` and `approval` wait reasons are enum values no code writes; `approval` is read but never set (`subfleet/contracts.py:37-42`; `daemon.py:107-108`, `3303-3304`). `run --batch FILE` validates a TOML or JSON manifest of up to 256 entries, then sends one `submit` per entry; a refused entry does not stop the rest (`cli.py:859`, `892-1096`). `wait A B C` is a caller-side barrier that passes no output between jobs (`daemon.py:2317-2332`, `cli.py:1159-1200`). Gate rounds, conversation turns, `resume` and `--parent` children are the constructs that relate several jobs (`subfleet/gate/round.py:57-72`, `subfleet/conversations/service.py:1788-1795`, `cli.py:1831-1849`, `daemon.py:1752-1758`). |
| **Worker** | A sidechain of the parent session: every transcript record carries `isSidechain: true` and the parent's `sessionId` (`observed` 2026-09-17). | A process tree owned by the launchd daemon, not the caller (`subfleet/cli.py:2369-2392`). Per attempt the daemon starts `python -m subfleet.guardian` (`daemon.py:4053-4071`); the guardian calls `setsid`, ignores SIGTERM and SIGINT, and blocks on a launch-gate pipe until the daemon has committed its pid, pgid, boot id and start time (`subfleet/guardian.py:115-130`; `daemon.py:4069-4099`). It publishes `start.json`, runs the provider under `taskpolicy -c utility`, and writes `exit.json` (`guardian.py:131-204`, `25-46`). Claude runs as `claude -p --model <id> --session-id <uuid> --output-format stream-json --verbose` plus sandbox flags; Codex as `codex exec --json … --sandbox <mode> --output-last-message <attempt>/last.md`; both read the prompt on stdin (`subfleet/adapters/claude.py:1304-1334`, `subfleet/adapters/codex.py:433-480`). At 00:30Z, 13 guardians were live, each leading its own process group; one had outlived the daemon that started it (`observed`). |
| **Concurrency** | 16 concurrent agents by default, fewer on machines with fewer CPUs, settable from 1 to 256 with `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS`; 1,000 agents per run; 4,096 items per call (`doc`). In the 694 runs since 2026-09-17, no run exceeded 16 in flight; 49 of the 54 runs with at least 30 timed agents peaked at exactly 16 (`computed`). | Fleet cap `caps.max_active_attempts`, counting reserved, starting, running and finalizing attempts plus probe leases: 4 shipped, 16 live (`daemon.py:3484-3487`; `subfleet/scheduler.py:382-387`; `subfleet/default_policy.json:103-106`). Per lane: 2 while the lane has any fresh provider reading, otherwise 1, judged only when an attempt is placed (`scheduler.py:452-459`; `subfleet/capacity.py:236-248`). Conversation turns have their own pool, 3 fleet-wide and 1 per lane (`subfleet/policy.py:77-78`). All descendants of a parent share one active attempt, from a key no policy file lists (`scheduler.py:226-250`). Detached attempts peaked at 16 on 2026-09-27, never above 4 before 2026-09-24, and were at 4 or fewer 82% of the time since cutover (`computed`). |
| **Admission queue** | None: `agent()` calls wait for a free slot inside the run (`doc`). | Pending jobs live in the store. The control loop offers an admission pass every 50 ms (`daemon.py:379`, `2469-2496`). A pass walks tiers in policy order, trivial first, and FIFO by `created_at` within a tier (`scheduler.py:155-167`). A job is held `behind-older-job`, unevaluated, when an older waiter of its tier could use the same model and lane (`scheduler.py:176-223`; `daemon.py:3288-3302`); after one reservation finds the pool full, later jobs of that pool in the pass are held `fleet-full` (`daemon.py:3487`, `3298-3301`). A job that passes a waiter may take only cap minus 1 slots (`daemon.py:3488-3508`). Capacity waits recheck after 1 s, doubling to 30 s, or on the next pass after a lease frees (`scheduler.py:705-723`; `daemon.py:3216-3231`). `status` reports the hold reasons and open lanes; `why <job>` prints the hold and recorded decision (`daemon.py:2008-2076`; `subfleet/render.py:198-249`). |
| **Models** | A per-agent `model` in the script wins; then the agent definition, `CLAUDE_CODE_SUBAGENT_MODEL`, then the session model; `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` forces one model (`doc`, sub-agents page). The agents since 2026-09-17 ran on Anthropic models only: Opus 5.5 for 58%, Opus 5 for 12%, Fable for 11% (`computed`). | The task-by-tier grid is policy data, read once at daemon start (`daemon.py:480-484`). Shipped: lookup, research, review, build → Haiku, Sonnet, Opus, Astra; sweep → Terra ×3, Astra; the three writing tasks → Fable (`default_policy.json:8-57`). Live: Luna, Luna, Opus, Astra, and Opus for the writing tasks (`observed`). A job walks its chain from its own tier upward with repeats removed, and a model with no candidate lane promotes to the next, across providers (`scheduler.py:339-366`, `550-586`). `-m` or a lane pin evaluates one model and never promotes (`scheduler.py:339-340`, `361-364`). `-m sol` becomes Astra with a note (`cli.py:525-528`). `-m` accepts a hard-coded list that omits `luna`, the live chains' first model (`cli.py:73`). No dispatch attempt has requested Luna or Haiku since cutover (`computed`). |
| **Whose quota** | Runs count toward the plan's usage and rate limits (`doc`). Of 1,829 worker errors since 2026-09-17, 1,477 were session-limit and 163 weekly-limit errors of the launching account (`computed`). Subfleet records none of this spend; it reaches the scheduler only as account-wide usage readings for the desktop account's lane (`subfleet/capacity.py:371-375`, `387-393`). | The lane the job is placed on. The daemon adds that lane's credential to the child environment only: a keychain setup token as `CLAUDE_CODE_OAUTH_TOKEN`, or a Codex `CODEX_HOME` (`subfleet/credentials.py:13-50`). Every launch removes `CODEX_API_KEY`, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` (`daemon.py:4046-4049`); API-key Codex homes are refused at enrolment, on a pinned submit and before each probe and launch, by detectors that differ slightly (`codex.py:116-123`, `284-290`; `daemon.py:1789-1799`, `1289-1290`, `2839`, `3995`). The Claude lane matching the desktop app's account is rejected as `desktop` unless the job passes `--allow-desktop`, and the ranking has no login term (`scheduler.py:435-436`, `486-506`; `capacity.py:131-176`, `347-363`). Timers also spend lane quota, with Haiku keepalive turns every 18,300 s on idle Claude lanes (`daemon.py:2604`; `timers.py:655-691`). |
| **Durability** | A run stops with the session unless the session is moved to the background; a background session's workflows survive its process being restarted or updated. A relaunch in the same session replays completed agents; the first changed or failed agent reruns with every agent started after it (`doc`, workflows and agent-view pages). | Jobs survive the caller and the daemon: a submitting client's exit and a daemon SIGKILL mid-run both end in success (`tests/fake/test_daemon_contract.py:56`, `:79`). The store runs WAL with `synchronous=FULL` (`subfleet/store.py:149-151`). Each tick the daemon re-reads receipts, cancels and wall clocks for every live attempt, and at most once a second re-checks processes by pid, boot id and start time (`daemon.py:2469-2491`, `4194-4235`; `subfleet/procs.py:162-194`). A matching guardian is re-adopted; a dead one with `exit.json` is finalized; a dead one without it is finalized `lost` on an empty census or killed and possibly quarantined otherwise (`daemon.py:4243-4285`). `runs reap` only reports (`cli.py:1610-1658`). A quarantined attempt waits for `kill --confirm-dead` or `--force-release` (`daemon.py:2334-2341`, `4480-4501`). The same request id with the same payload returns the existing job while its row is retained; nothing caches results (`daemon.py:1317-1325`; `cli.py:651`). `subfleet resume` continues the source's native session on its lane (`daemon.py:1529-1537`, `4007-4028`). |
| **Completion signal** | A `task-notification` is enqueued into the owning session; the SDK returns `async_launched` and delivers the result later (`observed` 2026-09-17; `doc`, Agent SDK page). | A notice row written in the transaction that makes the job terminal, addressed to the submitting session; turns get none (`daemon.py:2360-2384`; `cli.py:112-114`, `679`). Hooks deliver it. PostToolUse on Bash, installed with `asyncRewake` and a 600 s timeout, long-polls one running job of the session for up to 595 s, marks its notice `offered` and exits 2, which Claude Code turns into a task notification that wakes an idle session (`subfleet/hooks.py:491-606`, `707-730`; 132 such wakes since 2026-09-25, `observed`). SessionStart and UserPromptSubmit print pending or offered notices as added context and mark job notices `surfaced` (`hooks.py:406-454`). A background `subfleet wait <id>` is the other wake path (`cli.py:832`). Only `runs show` from the caller acknowledges; `wait` acknowledges nothing (`cli.py:1511-1543`, `1159-1290`). No production code pushes into a session inbox; `notify_push.py` is imported only by its test (`observed` grep). Of 409 finished-job notices that named a session, 283 had been surfaced or acknowledged at 00:22Z (`computed`). |
| **Output contract** | A JSON Schema on `agent()`, up to five attempts (`MAX_STRUCTURED_OUTPUT_RETRIES`); a schema that contradicts itself fails before the agent starts (`doc`). | Free text, judged by class and non-emptiness. A Claude attempt is `ok` only if no CLI-gate, auth, rejected `rate_limit_event`, credit or limit pattern matched first, then rc 0, a non-error `result` and non-empty final text (`claude.py:1533-1681`). The patterns scan the worker's own assistant text, not only provider errors (`claude.py:1487-1488`; `subfleet/adapters/claude_stream.py:259-270`). A Codex attempt is `ok` on rc 0, no `turn.failed` and a non-empty `last.md` (`codex.py:503-545`). The daemon demotes `ok` to `unknown` when it signalled the attempt or the deliverable is empty, except for turns (`daemon.py:4698-4717`). `-o` is exported only after acceptance, from a digest-checked copy (`daemon.py:4803-4843`). No launch passes `--json-schema` or `--output-schema`, though both installed CLIs offer them (`claude.py:1307-1325`, `codex.py:433-470`; `observed` `--help`). The only checked schema is the gate verdict (`subfleet/gate/verdict.py:29-69`). |
| **Worker context** | Workers inherit the session's hooks, CLAUDE.md and MCP instructions (`observed` 2026-09-17); hook input now carries `agent_id` and `agent_type` (`doc`, hooks page). `isolation: 'worktree'` gives an agent its own worktree (workflow-authoring reference). | A writable job that is not `--in-place` runs in a detached worktree cut at admission from the caller's HEAD at submit, under `~/.subfleet/worktrees/<job id>`, so uncommitted files never reach it (`daemon.py:1383-1403`, `2651-2672`; `tests/fake/test_main_refusal_write_dir.py:13`). In-place writers on `main` or `master` are refused at submit, admission and launch (`daemon.py:1241-1246`, `2643-2650`, `3991-3994`). Read-only Claude gets plan mode, Read/Glob/Grep plus web tools, empty setting sources, `--safe-mode`, an empty strict MCP config and no slash commands; isolated reviews drop the web tools and add `--add-dir` (`claude.py:1200-1228`). Writable Claude gets only `--dangerously-skip-permissions`, and at 00:30Z all 115 writable Claude transcripts in the store loaded the global CLAUDE.md, hooks and skills, against none of 73 read-only ones (`claude.py:1212-1213`; `computed`). Writable Codex gets network access and a never-rules hook (`codex.py:453-467`). Changes come back as a local `refs/subfleet-salvage/` commit of the final tree; subfleet code never pushes, but a worker can (`subfleet/salvage.py:263-308`). |
| **Agreement** | Patterns in the script: N skeptics, judge panels, loop-until-dry (workflow-authoring reference). All voters share one login and one provider. | `subfleet gate pr\|plan\|continue`, a daemon service with no list or abort (`subfleet/gate/cli.py:24-54`; `protocol.py:38-44`). The main supplies `--main-approve` and the expected plan SHA-256 or PR head and base; the rest of the fingerprint comes from the captured subject (`subfleet/gate/revision.py:31-60`). The peer is `fable` or `astra` (`sol` maps to Astra); there is no Opus peer (`gate/service.py:182-184`). Each round is a read-only, isolated `gate-review` job pinned to the peer model, run from a neutral directory under the state root (`round.py:55-72`; `daemon.py:1221-1232`). Families are compared only with `--main-model`; accounts never (`service.py:47-56`, `208-219`). Verdicts parse fail-closed against the approved revision (`verdict.py:29-69`). Rounds stop at the policy cap of 4 (`service.py:59-67`). A merge runs `gh pr merge --match-head-commit` once per head, after state, CI and mergeability checks (`subfleet/gate/merge.py:122-139`, `281-351`). |
| **Audit trail** | `/workflows` shows runs, per-phase agent counts, tokens and elapsed time; the journal holds each agent's return value; the state file holds per-agent model, tokens, tool calls, timings and errors (`doc`; `observed`). | Every store transaction that changes rows appends an `events` row (`store.py:231-237`). Jobs keep request id, payload digest, workdir HEAD, pins, caller session and policy hash; attempts keep lane, requested and served model, attestation, process identity, native session id, rc, outcome class and evidence (`store_schema.sql:66-142`). Placement stores the full routing decision beside its attempt (`daemon.py:3592-3596`), and finalization records artifacts with sha256 and size (`daemon.py:4607-4613`, `4718-4768`). No token count is recorded; `max_tokens_observed` is stored and never read (`computed` grep). Retention deletes job, attempt, decision, notice, reading and artifact rows, the job directory and owned worktrees, never events (`subfleet/retention.py:145-219`, `336-387`). |
| **Guardrails** | Worker tool calls pass the same hooks and permission rules as the main agent (`doc`). | The session hook is a Bash PreToolUse entry that parses command text and reads no agent identity (`hooks.py:201-207`, `671-683`). Per shell segment, after leading assignments and `env`, `nohup`, `exec`, `time` or `command`, it denies `codex exec\|e\|review`, and `subfleet codex\|claude` or the v1 runner names unless `-d` follows (`hooks.py:643-667`). `SUBFLEET_ATTACHED_OK=1` exempts only its own segment (`hooks.py:643-657`). The CLI refuses `subfleet codex` and `subfleet claude` with exit 7 regardless (`subfleet/compat.py:182-191`). Every non-isolated Codex launch first passes a fail-closed preflight of the operator's never-rules overlay against pinned hashes (`daemon.py:3998-4006`; `subfleet/guard/preflight.py:173-220`, `526-651`). Claude launches rely on the operator's global hooks (`claude.py:1291-1300`). |
| **Budget** | The script API has a `budget` object that throws once spent; the "Large workflow" warning above 25 agents or 1.5M projected tokens is advisory and never shown with `ultracode` on; the size guideline (medium, under 10 agents, by default) is advice (`doc`; workflow-authoring reference). `skipWorkflowUsageWarning`, set in `~/.claude/settings.json`, is not a documented setting (`doc`, `observed`). | `max_wall_s` (21,600 s) and `max_attempts` (3) per job, capped by policy and not settable from `run` (`default_policy.json:103-116`; `daemon.py:1291-1297`). The wall clock starts at the first reservation and covers every attempt and the waits between them; a waiting job past it is cancelled through `kill`, and a running attempt is killed and its job cancelled with rc 130 (`daemon.py:3247-3249`, `3597`, `4163-4172`). No turn, token or dollar cap is passed (`claude.py:1307-1325`; `codex.py:433-470`). A lane is refused for a model while a fresh account or same-model reading is at 85% or more (`scheduler.py:419-422`, `460-461`). |
| **Failure handling** | `agent()` resolves to `null` on an unrecoverable API error. From 2.1.271 an interactive subscription session with automatic continue on waits for a plan-limit reset due within 24 hours, at most twice; background sessions, `-p`, the Agent SDK, Remote Control and teammate sessions are excluded (`doc`). All 1,477 session-limit worker errors since 2026-09-17 came from 2.1.271 or later: 1,263 from `claude-desktop` sessions, 214 from `sdk-cli` (`computed`). Which condition fails is not determined. | Outcomes are `ok`, `limited`, `auth-dead`, `cli-too-old`, `content-filter`, `transient` or `unknown` (`contracts.py:65-72`); a quarantined attempt gets none. A job retries while `seq < max_attempts` on `limited` without a lane pin, on `transient` (a pinned job only once), or on a lost read-only attempt (`daemon.py:4749-4754`); an attempt that never launched is requeued too (`daemon.py:4393-4403`). A limit writes a closure at account or model scope until the reported reset, or now + 3,600 s marked guessed (`claude.py:1573-1649`, `1937-1948`). A retry excludes the limited lane and re-routes from the job's tier (`daemon.py:1051-1096`; `scheduler.py:339-366`). `auth-dead` disables the lane and ends the job with rc 5 (`daemon.py:4769-4781`). A census that is not verified empty 3 s into finalization quarantines the attempt and ends the job `lost`, rc 125, with nothing exported (`daemon.py:4634-4645`, `4469-4478`). |
| **Outside a session** | Available in the CLI, desktop, IDEs, `claude -p` and the Agent SDK; `claude -p` waits for a background workflow up to 10 idle minutes; the `ultracode` keyword counts only in a prompt a person typed (`doc`). | `run` detaches when `CLAUDECODE` or `CLAUDE_CODE_SESSION_ID` is set and blocks otherwise; `-d` and `SUBFLEET_RUN_DETACH` choose the mode, and `--attach` waits inline in any mode (`cli.py:117-161`, `801-840`). A job submitted outside a session has no caller session, so its notice is never delivered (`cli.py:679`; `daemon.py:2383-2384`). The daemon runs under a KeepAlive launchd agent and schedules its own timers: lane probe every 60 s with alerts and reset credits inside it, keepalive every 18,300 s from each daemon start, the sidebar mirror every 60 s plus a 2 s hot pass, and retention an hour after each completed pass (`cli.py:2369-2392`; `timers.py:66-85`, `102-110`, `593-636`; `daemon.py:2491-2522`). No timer nudges or revives sessions (`timers.py:83-85`). |

## How each is used on this machine

Native workflows, all of `~/.claude/projects`, runs started after the previous
measurement (2026-09-17T19:55Z) through 2026-09-27T23:59Z (`computed`):

- 694 runs: 667 completed, 22 killed, 5 failed; 8,776 agents; 852,675,025
  tokens. Agents per run: p50 6, p90 24, maximum 829. 1,402 state files exist
  in all.
- 1,829 agents ended in error: 1,477 session-limit (80.8%), 163 weekly-limit
  (8.9%), 189 other. 242 runs had at least one plan-limit error; 239 of them
  report `completed`, one with 3 of 3 agents errored.
- This session's mapping run, `wf_6573a202-69f`: 11 readers averaging 265,000
  tokens (141,000 to 326,000) and 10 verifiers averaging 234,000, all Opus 5.5,
  2,156 s, 1,945 tool calls, 0 errors.
- Three saved workflows are named `subfleet-fanout`: the batch version in
  `~/.claude/workflows/` (2026-09-22), a byte-identical untracked copy in
  `~/subfleet-v2/.claude/workflows/`, and an untracked per-item version from
  2026-09-18 in `~/subfleet/.claude/workflows/`. A project copy wins a name tie
  (`doc`), and this session, started under `~/subfleet`, listed the per-item
  copy (`observed`).
- `ultracode` is `true` and `CLAUDE_CODE_EFFORT_LEVEL` is `xhigh` in
  `~/.claude/settings.json`, so Claude plans a workflow for each substantive
  task (`observed`; `doc`, model-config page).

Subfleet v2 store, from the cutover (2026-09-19T14:49Z). The store is live
and retention deletes job rows, so two snapshots are used, each labelled.

From the stats script at 00:59Z on 2026-09-28 (`computed`; the output is in
the evidence file). 1,482 jobs had been submitted; 608 rows remained, because
retention had pruned 874 of the submitted jobs and all 501 imported from v1.
Row-level figures cover those 608 jobs; event-level figures cover everything.

- Retained rows by kind: dispatch 468, gate-review 113, turn 24, resume 3.
- Dispatch routing: 417 by `--task` and `--tier` alone, 38 also pinned a
  model, 13 pinned a lane or had no task. Sandboxes: 314 workspace-write, 154
  read-only. Of the writable ones, 179 ran in a daemon worktree, 105 in place,
  and 30 had never been placed. 108 used a caller-chosen request id; 187
  submissions came through 45 `--batch` manifests.
- Attempts requested Astra 325 times, Opus 5.5 143, Fable 60, Opus 5 18,
  Luna 2, Terra 1, Sonnet 1. Outcomes: 435 `ok`, 42 quarantined, 19 unknown,
  14 transient, 12 limited, 1 auth-dead, 7 interrupted, 3 lost, 17 running.
  508 jobs needed one attempt, 18 two and 2 three.
- Succeeded attempt run time: Astra p50 12.5 min and p90 69.7 min; Opus 5.5
  p50 60.5 min and p90 3.3 h; Fable p50 3.4 min.
- Submit to first start, from events (n = 1,157, every kind): p50 2.3 min,
  p90 1.97 h, maximum 11.3 h. Jobs cancelled before any start (n = 234) had
  waited p50 9.5 min and p90 2.0 h.
- 124 caller sessions; the median session peaked at 1 running job, the
  maximum at 8. Per lane, at most 2 attempts ran at once, or 3 when a
  conversation turn's separate slot is counted.

From the verifiers' read-only queries between 00:20Z and 00:25Z (`computed`;
the queries are in the evidence file):

- Detached attempts peaked at 16 at once, 17 with conversation turns. On
  2026-09-27 the standard-tier dispatches that started waited a median of
  177 min, the hard-tier ones 56 min.
- Notices: every terminal non-turn job had one. 120 of 529 named no session
  (112 of them gate rounds, 8 dispatches from outside a session). Of the 409
  that named one, 283 were surfaced or acknowledged, 16 offered and 110 still
  pending. All 1,388 service notices (pings, nudges, alerts) were pending, 1,046
  of them addressed to the literal session `operator`.
- Gates: 112 peer rounds across 76 gates (52, 16, 4 and 4 gates at 1 to 4
  rounds). Fable peers ran 57 rounds, all on 2026-09-22 and 09-23; Astra 55.
  47 Fable rounds were attested; none of the 44 accepted Astra rounds was.
  25 merge actions: 6 merged, 16 blocked as drafts.
- 39 jobs had ended `lost`; every one began as a quarantine. 23 of their
  attempts had exited 0; for 19 of those the only census error was a failed
  `ps -axEww` read.
- Store: 811 MB; 781,739 events, 533,550 of them with an empty payload;
  1,531 of 1,642 retention passes had ended in a timeout, and none had
  completed since 2026-09-27T08:07Z. `~/.subfleet/worktrees` held about
  80 GiB.

### Capacity at 2026-09-28T00:20Z

`subfleet status`, `subfleet lanes list` and `subfleet why`, read-only
(`observed`):

| Lanes | State |
|---|---|
| claude-1, 6, 7, 9 | Enabled, no closure, 2 attempts each, unmeasured |
| claude-2, 3, 5, 8, 10 | Closed on provider limits at account scope, with reported clocks from 00:40Z on the 28th to 2026-10-02 |
| claude-11 to 14, 16, 17 | Disabled. Each disable was followed in the same second by a `timer.verdict` event of `auth-dead`, which `lanes list --json` shows as `probe_status` and the text view does not |
| claude-15 | Disabled since the v1 import; the reason exists only in the importer's one-time report |
| claude-4 | The desktop app's account, excluded unless `--allow-desktop` |
| codex-1 to 6 | Enabled, no closure, 1 attempt each, unmeasured |

Admission held 35 jobs, 34 `behind-older-job` and 1 `no-slot`, with no open
lane and nothing placed for 1,789 s. The head job, a standard-tier review,
walked Opus then Astra and found every enabled lane `no-slot`. The other 34
had no decision row at all; `why` evaluates them on demand. 14 detached
attempts ran against the live cap of 16, so the fleet cap was not binding. The
per-lane cap was: no lane had a provider reading under 120 s old, which limits
a lane to one attempt. Nine Claude lanes' usage reads had returned HTTP 429;
the timer never probes a lane with a live attempt; and busy Codex lanes carried
weekly readings 2.6 to 5.1 hours old (`subfleet/timers.py:326-335`;
`subfleet/adapters/claude.py:1069-1070`; `scheduler.py:452-455`). claude-1,
6, 7 and 9 were over that cap because their second attempts were placed while
they were measured (deduced). At 00:59Z claude-2 had reopened and taken two
jobs, 16 detached attempts ran, and admission held 36 jobs, 2 of them
`fleet-full`.

The desktop flag has drifted. The capacity view recomputes the desktop lane
from `~/.claude.json` and marks claude-4, while the stored `lanes.desktop`
column, which the timers read, still marks claude-1 from the 2026-09-19
import (`capacity.py:347-363`; `timers.py:330`, `690`; `observed`). claude-1
has had no timer probe or keepalive since cutover; claude-4, the live desktop
account, has had 287 timer verdicts and 22 keepalive events (`computed`).

## Where each one breaks

### Native workflows

- **Workers spend only the launching login, and runs hide it.** 80.8% of
  worker errors are session-limit errors, and 239 of 242 runs that hit plan
  limits report `completed`, including one where every agent errored
  (`computed`). The documented usage-limit pause did not prevent 1,263 of
  them in desktop-app sessions on 2.1.271 or later.
- **Heavy reading is expensive on the login.** The 5-hour gauge rose 47
  points across 21 Opus 5.5 agents; the gauge is account-wide, so this is an
  upper bound (`observed`).
- **The run belongs to a process, and a failure mid-fan-out reruns finished
  work.** Unchanged from the 2026-09-17 edition (`doc`).
- **Two scripts share a saved-workflow name.** The stale per-item
  `subfleet-fanout` in `~/subfleet/.claude/workflows/` shadows the batch
  version for sessions under `~/subfleet` (`doc`; `observed`).

### Subfleet v2

Ordered by what to fix first. Items marked with a pull request have an open fix
in `MaxGhenis/subfleet-v2` that did not merge into `origin/main` by
2026-09-28; each was checked against `origin/main` at `75b718a2`.

1. **A worker's own words can fail its job.** The Claude classifier runs the
   CLI-gate, org-block, credit and limit patterns over stderr and every
   assistant text block before it checks for success (`claude.py:1487-1488`,
   `1533-1649` before `1658`; `claude_stream.py:259-270`). A successful run
   whose answer mentions plan limits in the words the patterns match is
   classed `limited`: its lane is closed account-wide for a guessed
   hour, the job retries, and its `-o` is never written. One such case is in
   the store (`20260920-161631-handoff-microcosm-us-release/a1`: rc 0,
   `rate_limit_event` allowed, attested, classed `limited`; the job failed with
   rc 4), and the current code still classifies that stream the same way
   (`computed`). Organization-block wording is classed `auth-dead` and
   disables the lane, and version-gate wording `cli-too-old`. Codex is not
   affected: it checks success first (`codex.py:535-545`). This document is exactly the kind of text that
   triggers it. Open fix: `MaxGhenis/subfleet-v2#39`.
2. **Busy lanes are never re-measured, so they take one attempt.** The usage
   timer skips any lane with a live attempt (`timers.py:326-335`), a reading is
   fresh for 120 s (`capacity.py:236-248`), and an unmeasured lane gets one
   slot (`scheduler.py:452-455`). On 2026-09-28 this, together with HTTP 429
   from the Claude usage endpoint, left zero open lanes with 35 jobs waiting.
3. **Finished work can be recorded lost.** If the containment census is not
   verified empty 3 s into finalization, the attempt is quarantined before its
   deliverable is extracted (`daemon.py:4634-4645`, `4688`). A failed
   `ps -axEww` read counts as not empty (`procs.py:338-340`, `426-445`). 23
   attempts that exited 0 ended this way, 19 of them only because that read
   failed (`computed`). Quarantine is released only by an operator, and it
   keeps the job's `out:` and `worktree:` leases, which refuse later in-place
   writers into 8 callers' checkouts (`daemon.py:4474`, `1766-1771`;
   `computed`).
4. **Retention cannot converge.** The daemon never passes
   `salvage_referenced_elsewhere`, so every job with a salvage artifact is
   pinned with its worktree forever (`daemon.py:2507-2511`;
   `retention.py:113-115`). Jobs with a pending notice for any named session
   are pinned too (`retention.py:103-104`). 512 of 580 detached jobs were
   pinned against a 500-job budget, and pinned worktrees held about 82 GiB
   against 2 GiB. Each pass sizes everything before pruning within 60 s, and
   a walk of the worktrees took 80 to 93 s (`retention.py:271-283`;
   `computed`). A worktree cut before placement is never collected when its
   job is cancelled first (`daemon.py:2651-2670`, `3597`, `2346-2356`;
   open fix `#27`). Meanwhile every unpinned job is pruned, so request-id
   idempotence and the attempt history last only while rows are retained
   (`daemon.py:1317-1318`; `retention.py:386-387`).
5. **Notices outside the job path go nowhere.** Session hooks mark service
   notices with negated ids that `notice.mark` never matches, so every ping and
   nudge stays pending and is printed again at every prompt
   (`hooks.py:447-451`; `daemon.py:1959-1986`; reproduced in a scratch daemon).
   Timer alerts go to `alerts.operator_session`, which neither policy sets, so
   they land on the literal session `operator` that nothing reads
   (`daemon.py:1989`, `2498-2501`). Gate rounds carry no caller session, so a
   gate advances only when some client polls it; 8 gates sat in `reviewing`
   with finished peers (`round.py:66-72`; `gate/cli.py:126-128`; `computed`).
   Nudges are written about 8 s after the SessionStart hook has run and push
   nothing (`subfleet/sessions/nudge.py:363-405`).
6. **The desktop exclusion has a side door.** Timers choose lanes from the
   stored desktop column, not the view (`timers.py:330`, `690`), so the live
   desktop account gets keepalive turns and claude-1 gets no probes.
7. **Admission blocks on things competition ignores.** `competes()` compares
   models and lane pins only, so an older job whose own `-x` or
   `--allow-desktop` needs cannot be met holds back newer jobs an open lane
   would take (`scheduler.py:176-223`; an ad-hoc test reproduced the `-x`
   case, and the `--allow-desktop` case follows from the same code). Tiers
   are served strictly in order with no aging (`scheduler.py:155-167`).
8. **The live policy's first model cannot be pinned.** `-m`, `why -m`,
   `--batch` and `sessions handoff --to` accept a hard-coded list without
   `luna` (`cli.py:73`; `subfleet/sessions/cli.py:46`).
9. **The Codex guard has gaps and drift.** `bash -c`, `sh -c`, `eval`, `sudo`,
   `timeout`, `xargs`, `env -i`, shell keywords, a global option before the
   subcommand (`codex -m x exec`), a line ending in a comment, and any command
   `shlex` cannot parse all pass (`hooks.py:629-632`, `650-667`; `computed`).
   The `bin/codex` PATH shim is not in the release, and `~/bin/codex` dangles,
   so a bare `codex` runs with no lane pick (`pyproject.toml:27-33`;
   `observed`). The Codex never-rules overlay lacks the pattern-kill,
   axiom-brief and hand-authored-RuleSpec rules the Claude hook has, and a
   test pins the old bytes (`tests/unit/test_guard_overlay.py:149`;
   `computed`).
10. **Gates.** No Opus peer (open fixes `#28`, `#58`); Astra rounds are never
    attested (`codex.py:650-654`); an attestation failure reserves the next
    round with an approval stamped by the service, not the main
    (`service.py:244-246`, `373-378`); round leases have no expiry
    (`service.py:389-411`); there is no list or abort.
11. **Smaller drift.** `store.add_event` writes each event twice
    (`store.py:746-749`); `attempt.reserved` events are written for holds that
    reserve nothing (`daemon.py:3457`; open fix `#38`); a finalization that
    raises is retried every 60 s without limit (`daemon.py:2408-2425`);
    keepalive's clock restarts at each daemon start and had not run since
    2026-09-27T04:41Z (`timers.py:102-110`; `observed`); Claude conversation
    turns record false `<synthetic>` model mismatches (4 accepted); the schema
    header says version 4 while `store.py` sets 5; `max_child_jobs` counts
    retained child rows, including resumes.

## Composition: the saved subfleet-fanout workflow

`~/.claude/workflows/subfleet-fanout.js` (read this session) takes a list of
`{task, tier, dir, promptPath, outPath, name}`. One Haiku agent writes a
manifest, skips items whose output already exists, runs
`subfleet run --batch <manifest> --json [--request-id ID]` once, then loops
`subfleet wait <ids> --timeout 540 --json` in Bash calls capped at 600 s, up to
20 slices, and returns a schema-validated report. With `requestId`, a relaunch
that reruns the dispatcher gets the existing jobs back, because each entry's
request id is `<id>-<n>` and a repeat with an equal payload digest returns the
existing job (`cli.py:952-957`; `daemon.py:1317-1325`). The digest includes
the workdir's HEAD and the policy hash, so a relaunch after either changes is
refused instead (`subfleet/ids.py:54-92`). The
saved-workflow runs recorded on this machine, all 12 (`computed`):

| Run | Date | Shape | Status | Wall | Login tokens | Outcome |
|---|---|---|---|---|---|---|
| `wf_d4e04baf-9b2` | 09-17 | per item, 2 dispatchers (v1) | killed | 41 s | 122,558 | Stopped before reporting |
| `wf_06e602f8-308` | 09-18 | per item, 2 dispatchers (v1) | completed | 706 s | 79,719 | Both rc 0; the long item needed 2 wait slices |
| `wf_a6e32b6d-4d1` | 09-22 | batch of 5 | killed | 969 s | 50,373 | Submitted; one 540 s slice, then interrupted |
| `wf_8d956f8e-83f` | 09-22 | batch of 5 | killed | 170 s | 43,456 | Submitted; wait interrupted |
| `wf_a201784d-a1e` | 09-23 | batch of 4 | killed | 85 s | 46,438 | Submitted; wait interrupted |
| `wf_ec9748e1-acd` | 09-24 | batch of 3 | completed | 22 s | 43,461 | State directory missing; nothing submitted |
| `wf_1dcf8842-47d` | 09-24 | batch of 2 | completed | 2,967 s | 66,092 | One 540 s slice timed out, then 19 slices returned at once with rc 1 or 69 (daemon unavailable); gave up at 20 with neither job finished |
| `wf_e4cc3cea-3ca` | 09-24 | batch of 2 | completed | 453 s | 54,616 | Submit returned 69 after the first entry; that job was lost |
| `wf_0d0cb19b-603` | 09-25 | batch of 5 | completed | 101 s | 55,569 | All refused: no daemon response within 15 s |
| `wf_258b4fab-e2f` | 09-25 | batch of 13 | completed | 660 s | 53,598 | 3 slices; 8 succeeded, 1 lost, 4 cancelled |
| `wf_1da3f507-3e1` | 09-25 | batch of 7 | completed | 139 s | 47,517 | All refused: no daemon response within 15 s |
| `wf_68136f3d-04c` | 09-26 | batch of 4 | completed | 111 s | 51,452 | All refused: no daemon response within 15 s |

What this shows:

- **The slicing works.** Across 58 dispatcher Bash calls the longest took
  541 s; none reached the 600 s cap. The 2026-09-18 test, still on subfleet
  v1, carried a job past 600 s to rc 0 in two slices, which answers the
  previous edition's open question; the ten batch-era runs are on v2.
- **The 2,967 s run was not a long wait.** Its time went to 19 failed slices
  against an unavailable daemon. Of its two jobs, one started 3 h 06 min after
  submission, long after the dispatcher had given up, and succeeded in 6 min;
  the other's row has been pruned.
- **Login cost is flat per batch.** One dispatcher costs 43,000 to 66,000
  Haiku tokens whether it carries 2 jobs or 13; the per-item version cost
  about 40,000 per item. A native Opus 5.5 reader cost 265,000.
- **The daemon was the weak link.** 5 of the 10 batch-era runs met a daemon
  that refused, failed mid-submit or stopped answering. 3 more were killed
  mid-wait, and 1 had a caller error.
- **Replay is safe only with `requestId` and a retained row.** A relaunch
  without it submits again, and the idempotent answer lasts only until
  retention prunes the job (see item 4 above).

## Defects and drift: the 2026-09-17 list in v2

| # | 2026-09-17 finding (v1) | v2 status | Evidence |
|---|---|---|---|
| 1 | `bin/subfleet-claude -b main` force-pushed | Superseded | No v2 code runs `git push`. Salvage is a local ref created against an all-zero old value (`salvage.py:284-308`); in-place writers on `main`/`master` are refused three times (`daemon.py:1241-1246`, `2643-2650`, `3991-3994`). Workers can still push, and a gate merge runs `gh pr merge` (`merge.py:334`). |
| 2 | An unflagged prompt was classed `build` and ran with permissions skipped | Superseded, mitigated | `run` needs `--task`, `-m`, `-a` or `-H`; no prompt classifier exists (`cli.py:593-596`; `daemon.py:1270-1271`). Policy makes only `build` writable (`default_policy.json:59-62`), but `-s workspace-write` overrides it: 141 writable reviews and 52 writable research jobs are retained. Writers get a worktree unless `--in-place` (105 of 314 were). Writable Claude still skips permissions. |
| 3 | Manual reaping, pid-less RUNNING entries, `wait` exit 0 for rc −9, `kill --grace 0` | Fixed | The daemon re-adopts or finalizes every live attempt (`daemon.py:2469-2491`, `4243-4285`); `runs reap` only reports; `wait` exits 125 for lost and 130 for cancelled (`cli.py:173-193`); `kill` has no `--grace` and escalates to SIGKILL only if the census is not empty after 15 s (`daemon.py:4428-4441`). New: quarantine is manual (item 3 above). |
| 4 | The Fable reserve had a side door, and expired tokens drove promotions | Superseded | One evaluator serves submit, `why` and `pick` (`daemon.py:1027`, `1859-1864`; `picker.py:56-58`). The shipped reserve still promoted 29 standard Opus jobs to Astra from 2026-09-19 to 09-24 as `reserve:fable:unmeasured`; the live policy emptied the reserve (`computed`). Timers are the remaining side door (item 6 above). |
| 5 | Learned capacities never updated; cooldowns stored at the wrong scope | Superseded | Closures take their scope from the provider's rate-limit event, or account scope for limit text (`claude.py:89-93`, `1573-1649`). The only cooldown closures are 13 imported from v1. |
| 6 | Gate attestation for Fable only; no list or abort; leases without timeout; unlimited rounds | Partly fixed | Attestation is required for any Claude peer and a mismatch discards any round (`verdict.py:72-106`); rounds are capped at 4 (`service.py:59-67`). Still no list or abort, leases never expire, and Astra rounds are never attested. |
| 7 | Guard matched `SUBFLEET_ATTACHED_OK` and `-d` anywhere; missed `bash -c`; stale `-m sol`; drift test failing | Partly fixed | The override and `-d` are now per segment, and the message no longer mentions `-m sol` (`hooks.py:643-679`). `bash -c` and more still pass, the drift test is gone, and the drift widened (item 9 above). |
| 8 | Downgrade never failed a run; `-A` auth exit; `bin/codex` picked once; `resume-codex` synchronous | Mixed | Attestation is recorded per attempt and named in the notice, but dispatch acceptance ignores it (`daemon.py:4744`, `4764-4766`, `4789-4794`). `-A` is gone; `auth-dead` disables the lane and never re-routes (`daemon.py:4769-4781`). `bin/codex` still picks once, and is not installed. `resume` is a supervised daemon job (fixed). |
| 9 | Session hook marked notices before printing; no notice pruning; `decisions.jsonl` unbounded | Partly fixed | The hook prints first (`hooks.py:445-453`); notices and decisions are deleted with their job; repeated waits add no decision row (`daemon.py:3493-3513`). Service notices never leave `pending`, events are never pruned, and the store is never vacuumed. |
| 10 | README drift | Not assessed | The README is not evidence. The mechanisms behind the four v1 items: kill escalates after 15 s; handoff targets omit `luna`; the nudge cooldown is 90 s (`nudge.py:124`); notices have four states (`contracts.py:102-106`). |
| 11 | Dead `pick_fable_lane` | Superseded | No v2 counterpart. v2's own dead code: `HEADLESS_PREAMBLE` (tests only), `TRANSIENT_RETRY_DELAY_S`, `notify_push.py`, the `WaitReason` enum; `policy.pick` is used only by tests and tools. |
| 12 | The old doc's v2 notes | Mixed | Still present: `max_tokens_observed` never read; `notify_push.py` unused; `lanes probe` probes nothing (`daemon.py:1926-1945`); the shipped policy's trivial and easy tiers map to Haiku and Sonnet. Fixed: `resume` continues the native session (`daemon.py:4007-4028`; `tests/fake/test_resume_contract.py:33`). |

## Recommendations

Each carries the observation that would reverse it. Where the 2026-09-17
recommendation still stands, it says so.

1. **Classify Claude outcomes from provider signals, not the answer.** Move
   the success check ahead of the text patterns, or run the patterns only over
   stderr and `result.errors`; land or rebase `MaxGhenis/subfleet-v2#39`, which
   was open and conflicting on 2026-09-28. Effort: hours. Reverse if a real
   provider limit is ever delivered only inside a successful result's text.
2. **Keep busy lanes measured.** Let the HTTP usage read (not a model turn) run
   on busy lanes, or keep an attempt-sourced reading's slot count until its
   reported reset, and find out why the Claude usage endpoint returns 429.
   Effort: hours. Reverse if second attempts placed on lanes measured more
   than 120 s earlier turn out to hit limits more often than first attempts.
3. **Accept a finished deliverable before judging containment.** Retry a
   failed `ps` read, and when the provider exited 0 with an accepted result,
   export it and quarantine only the leftover processes. Effort: hours.
   Reverse if a lingering process is ever shown to change a deliverable after
   the exit receipt.
4. **Make retention converge.** Treat a salvage ref as the preserved record
   and pass `salvage_referenced_elsewhere`, collect pre-placement worktrees
   (`#27`), size incrementally instead of walking 80 GiB inside a 60 s
   deadline, and add event retention and `VACUUM`. Effort: a day. Reverse if
   salvage refs are ever garbage-collected before their worktrees.
5. **Fix notice plumbing.** Map negated ids in `notice.mark`, point
   `alerts.operator_session` at a channel someone reads, and give gate rounds
   the caller's session. Effort: hours. Reverse if the hooks' re-printing of
   service notices is intended; then say so in the header they print.
6. **Close routing drift.** Timers should use the view's desktop flag, the
   `-m` choices should come from the policy registry, `competes()` should
   account for exclusions and `--allow-desktop`, and tiers should age. Effort:
   hours.
7. **Resolve the standing conflict in `~/.claude/CLAUDE.md`.** Still open from
   2026-09-17: `ultracode` makes every substantive task a native workflow on
   the login, while the instructions send delegation-scale work to lanes. Add
   the three-way rule from the table above, with `subfleet status` as the
   tiebreak, and delete the per-item `subfleet-fanout` copy in
   `~/subfleet/.claude/workflows/`. Effort: minutes. Reverse if runs on 2.1.271
   or later stop producing session-limit worker errors.
8. **Add `--schema FILE` to `subfleet run`.** Still open from 2026-09-17: the
   run parser and `SubmitArgs` have no schema field (`cli.py:2690-2750`;
   `protocol.py:88-124`), while both CLIs accept one. Gate verdicts are the
   first consumer: output outside the verdict sentinel and invalid verdict
   values were the most frequent round errors. Effort: hours. Abandon if the
   flag conflicts with plan mode or stream-json.
9. **Gates.** Allow an Opus peer (`#28`, `#58`), give rounds the caller's
   session, add list and abort, expire round leases, and require a fresh
   `--main-approve` for retry rounds. Effort: hours.
10. **Codex guard.** Ship `bin/codex` or delete the dangling symlink, re-stage
    the Codex overlay with the three missing rules and re-pin it, and unwrap
    `bash -c` and common wrappers. Effort: hours.

The 2026-09-17 recommendations 2 (save the dispatcher workflow), 4 (refuse
`-b main`), 5 (automatic reaping) and 6 (in-flight caps) are done or
superseded by v2. Recommendation 7's reserve half is done; its measurement half
became recommendation 2 here. Of its two open questions, the desktop account is
now identified from `~/.claude.json` (`capacity.py:131-176`), and whether the
usage-limit pause covers desktop sessions is still undetermined.

Briefs for recommendations 2, 3, 5 and 6 are in
[native-workflows-vs-subfleet.followups.md](native-workflows-vs-subfleet.followups.md).

## Independent review

Pending: filled in after the subfleet reviewers report.

## Reproduce

- Stats: `python3 docs/native-workflows-vs-subfleet.ledger-stats.py --live`.
  It reads the workflow state files, the first line of one worker transcript
  per run, the dispatcher transcripts' Bash timings and exit markers, and the
  v2 store with `mode=ro`, then calls `subfleet status --json`. It prints no
  prompts, outputs or e-mail addresses. `--since` sets the native window.
- The mapping run: `wf_6573a202-69f` under this session's directory in
  `~/.claude/projects/-Users-<user>-subfleet--claude-worktrees-lucid-cohen-5b61b8/deb8fa52-5c47-457d-8b1a-fbf15f6f2671/`
  (`workflows/scripts/`, `subagents/workflows/wf_6573a202-69f/journal.jsonl`).
- Code: `git -C ~/subfleet-v2 archive b053e3de | tar -x -C <dir>`, then
  `diff -r <dir>/subfleet ~/.local/share/subfleet/current/venv/lib/python3.14/site-packages/subfleet`
  should print nothing apart from `__pycache__`.
- Retention prunes job rows, so row-level store figures shift from run to
  run; the event-level ones only grow. The measurements and all 577 verdicts
  are copied into
  [native-workflows-vs-subfleet.evidence.json](native-workflows-vs-subfleet.evidence.json).
