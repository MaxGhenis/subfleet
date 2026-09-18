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
