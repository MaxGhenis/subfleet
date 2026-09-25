# Follow-ups from the native workflows comparison

Three self-contained briefs from
[native-workflows-vs-subfleet.md](native-workflows-vs-subfleet.md), written
2026-09-17. Each can be handed to a fresh session as is, or dispatched:

```bash
subfleet run --task build --tier standard -C ~/chief-of-staff/subfleet -p <brief-file> -n <name>
```

Shared ground rules for all three: the repo is `~/chief-of-staff` (local only,
branch `master`, no remote). Other sessions have uncommitted work in this
tree. Read the working-tree version of any file before editing it, preserve
those modifications, and stage and commit only the files you change, by
explicit path. Never `git add -A`.

## 1. Refuse `-b main|master` in `bin/subfleet-claude` (minutes)

`bin/subfleet-claude`'s EXIT-trap salvage function runs
`git push -qf <remote> <salvage_sha or HEAD>:refs/heads/<branch>` on every
trapped exit, success included, when `-b <branch>` is given (around
`bin/subfleet-claude:367-371`). Its argument validation has no refusal of
`main` or `master`. `bin/subfleet-codex` refuses `-b main|master` up front
with exit 2 (around `bin/subfleet-codex:148-152`).

Add the same up-front refusal to `bin/subfleet-claude` (exit 2, same message
style as the Codex runner). Add a test in `tests/test_claude_lane_script.py`
that pins it, mirroring how `tests/test_run_ledger_codex_runner.py` pins the
Codex refusal. Run `uv run pytest tests/test_claude_lane_script.py -q` and
report the result.

## 2. Automate run-ledger reaping and fix `wait`/`kill` exits (hours)

**Status 2026-09-22: superseded by the v2 cutover.** The brief below is the
2026-09-17 record; the resolution and the one remaining v1 caller follow it.

Verified defects (comparison doc, "Defects and drift" item 3):

1. `run_ledger.reap_orphans` (around `subfleet/run_ledger.py:849-875`) has one
   caller, the `subfleet runs reap` CLI branch (around `subfleet/cli.py:810-815`).
   Nothing reaps automatically, and the reap path does not call
   `cli._notify_finished`, so the dispatching session gets no completion
   notice for a reaped run.
2. An entry with no recorded pid shows RUNNING forever, is skipped by
   `reap_orphans` (around 862-863), is never pruned (`_prunable` requires
   `finished_at`, around 614-617), and is counted by `in_flight_counts`
   (around 991-1010). The live ledger holds 9 such entries from
   2026-08-22 to 08-30.
3. `subfleet wait` exits 0 for a run reaped with rc −9, because only rc > 0
   raises the exit code (around `cli.py:864-870`).
4. `kill --grace 0` help says "0 = signal only", but `kill_run` skips
   escalation only when the grace is `None`, so 0 sends SIGKILL at once
   (around `run_ledger.py:897-916`, `cli.py:1540-1542`).

Do: (a) call `reap_orphans` from the launchd-driven `subfleet revive` pass in
`cli.py` (the pass that already runs notice follow-up and liveness every
120 s); (b) send the completion notice from the reap path through the same
path finish uses; (c) finalize pid-less unfinished entries older than 24 h as
rc −9 orphaned, and stop `in_flight_counts` counting them; (d) make
`subfleet wait` exit non-zero for rc < 0; (e) make `--grace 0` mean signal
only. Add or extend tests in `tests/test_wait_kill.py` and
`tests/test_run_ledger.py` for each change, run
`uv run pytest tests/test_wait_kill.py tests/test_run_ledger.py -q`, and
update the README's run-ledger section. Do not run `subfleet runs reap`
against the live ledger without `--dry-run` until the tests pass; show the
dry-run output before the real pass.

### Resolution (2026-09-22)

The `subfleet` on PATH has been v2 since 2026-09-19
(`~/.subfleet/cutovers/20260919T135742Z/CUTOVER.md`): `subfleet 2.0.0a0`,
daemon `subfleetd`, store under `~/.subfleet`, installed from
`~/.local/share/subfleet/current` (source github.com/MaxGhenis/subfleet-v2).
Checked against that installed code, each item above already has a v2
counterpart:

| v1 defect | v2 |
|---|---|
| (a) nothing reaps automatically | the daemon tick checks guardian liveness per attempt (`daemon.py`, `procs.liveness`); a dead guardian with no receipt is contained and finalized `lost`. `runs reap` reports; "the daemon owns finalization" (`cli.py`, `cmd_runs_reap`). |
| (b) a reaped run sends no notice | `_lost → _finalize(lost=True) → _notice`; `_unlaunched` emits one too. |
| (c) pid-less rows RUNNING forever | `starting` with no `start.json` after `start_grace_s` → `_unlaunched("starting-no-receipt")`, failed or retried, with a notice; `reserved` with no pending launch → `_unlaunched("reserved-no-launch")`. The nine v1 rows were finalized rc −9 at the cutover (`orphan-reconciliation.json`, "predates current boot"). |
| (d) `wait` exits 0 on rc −9 | `exit_for_job`: `lost` → 125; a provider rc outside the table → 1 with the raw rc in the message (C-17.3). |
| (e) `kill --grace 0` sends SIGKILL at once | `--grace` is dropped by the compat layer with a note; containment owns the TERM→KILL escalation (C-5). |

The hook point named in (a), the v1 `subfleet revive` launchd pass, is
unloaded with the other four v1 services (CUTOVER.md).

**What is still true.** v1's `consensus.py` gate has one live caller: a
long-running Codex session that drives a private study runs
`<its-worktree>/subfleet/bin/subfleet gate plan <dossier> --peer fable
--peer-account <email> --peer-native-login-dir ~/.subfleet/logins/<email>
--main-approve --expect-sha256 <sha>` by path, a v1 variant (branch
`native-claude-gate-20260910`, off `c769b39b`) that pins the Fable peer to a
native login directory. Its peer rounds are the only writes to this ledger
since the cutover (`~/chief-of-staff/state/subfleet/runs`; 26 rows,
2026-09-19 to 09-21), nothing reaps that ledger now, and v2's `lanes
transfer` refuses a lane while an unfinished v1 row names it
(`lanes_transfer.py`: "v1 must finish or reap the row first"). Zero
unfinished rows on 2026-09-22; if one appears, `~/subfleet/bin/subfleet runs
reap` (v1, by path) still finalizes it.

**Moving that caller.** v2 already does what the variant was built for: v2
Claude lanes run on `~/.subfleet/logins/<email>` as `CLAUDE_CONFIG_DIR`
(`credentials.py`), and `--peer-account <email>` pins the round to that lane.
The equivalent command is

```bash
subfleet gate plan <dossier> --peer fable --peer-account <email> --main-approve --expect-sha256 <sha256>
```

Probe gates on 2026-09-22 (`~/.subfleet/gates/20260922-144449-plan-83506e7d`,
`…-144654-plan-e46e61ef`, a 698-byte plan): the peer job ran on lane `claude-1`
(the pinned account, claude-fable-5-1) and `runs show` records the attempt, lane,
and deliverable. The first round was rejected for prose before the sentinel by
the same `before.strip() or after.strip()` check v1 has (`gate/verdict.py` =
`consensus.py`); the study's real Fable rounds all began with the sentinel
(14 of 15 v1 rounds since 2026-09-19 parsed). A second probe with the normal
round budget (`--max-rounds 3`) was submitted at 14:46:54Z and was still queued behind the daemon's four-attempt cap when
this note was committed; its outcome is the durable record at
`~/.subfleet/gates/20260922-144654-plan-e46e61ef/gate.json`.

What changes for the study if it switches: gate records move from
`~/chief-of-staff/state/subfleet/gates/<id>/` to `~/.subfleet/gates/<id>/`
with the same layout and the same `certificate.json` keys; exit codes keep
the v1 table; `--max-rounds 0` means the policy cap (`caps.gate_max_rounds`,
4 in `~/.subfleet/policy.json`) rather than v1's unlimited, and one study
gate ran five rounds (`20260919-093330-plan-bda0a327`), so raise the cap
first; and the caller pins its v1 worktree, byte for byte, as a trusted input of
the study it drives, so the switch is a design amendment for that session to
record. Nothing under that worktree or the study was edited for this note.

## 3. Build and test a saved dispatcher workflow (hours)

Measured 2026-09-17 (comparison doc, "Composition"): a native Workflow script
started 4 Haiku agents that each ran
`subfleet run --attach --task review --tier standard -C <dir> -p <prompt> -o <out> -n <name>`
in one Bash call with a 600,000 ms timeout. All 4 returned rc 0, no hook
blocked, and each dispatcher cost about 40,600 Haiku tokens on the login
while the Opus work ran on a subfleet lane. All four runs finished under
600 s, so the wait loop never ran.

Constraints: the Bash tool caps one call at 600 s while Opus-lane runs have a
p50 of 648 s. When an `--attach` waiter dies the run continues and subfleet
pushes a completion notice into the main session (workers share the parent's
`CLAUDE_CODE_SESSION_ID`). Subfleet has no result cache, so a workflow
relaunch that reruns a dispatcher agent dispatches again.

Do:

1. Load the `workflow-authoring` skill, then write `subfleet-fanout.js`. Its
   `args` is a list of `{task, tier, dir, promptPath, outPath, name}`. For
   each item one agent (`model: 'haiku'`) skips dispatch if `outPath` exists
   and is non-empty; otherwise runs plain
   `subfleet run --task T --tier R -C dir -p promptPath -o outPath -n name`
   (detached, no `--attach`), captures the run id, loops
   `subfleet wait <id> --timeout 540` (Bash timeout 600000) until the exit
   code is not 124 (cap the loop; report if exceeded), and returns
   `{run_id, rc, out_path, out_bytes, wait_loops, notes}` through a schema.
   Keep dispatcher prompts minimal and forbid any other subfleet subcommand.
2. Save it at `subfleet/.claude/workflows/subfleet-fanout.js` and note in the
   comparison doc how to copy it to `~/.claude/workflows/`.
3. Test with 2 items, one expected to exceed 600 s (for example a
   `--task research --tier standard` read of a large module). Record whether
   the wait loop worked, how many loops, dispatcher token cost, and whether
   completion notices landed in the main session transcript
   (`subfleet notices`, the runs' notify state). Check the login's 5-hour
   gauge first and keep the test small.
4. Replace the "untested" wording in the Composition section of
   `docs/native-workflows-vs-subfleet.md` with what was measured.
