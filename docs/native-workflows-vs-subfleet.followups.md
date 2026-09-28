# Follow-ups from the native workflows comparison

Self-contained briefs from
[native-workflows-vs-subfleet.md](native-workflows-vs-subfleet.md), written
2026-09-28 against subfleet v2. Each can be handed to a fresh session or
dispatched:

```bash
subfleet run --task build --tier hard -C ~/subfleet-v2-lanes/<lane> -p <brief-file> -n <name>
```

## Shared ground rules

- The code is `MaxGhenis/subfleet-v2` (private), cloned at `~/subfleet-v2`.
  Branch from `origin/main` into a lane checkout:
  `git -C ~/subfleet-v2 fetch origin && git -C ~/subfleet-v2 worktree add ~/subfleet-v2-lanes/<lane> -b <branch> origin/main`.
- Line numbers below refer to `b053e3de` (branch `release/217`), the commit
  the installed release was built from on 2026-09-27. `origin/main` was
  `75b718a2` on 2026-09-28 and has moved since; find each site by the symbol
  named, not the number.
- Do not touch the live state root `~/.subfleet/`, the launchd agent
  `com.subfleet.daemon`, or `~/.claude/settings.json`. Tests use the fake
  providers and scratch daemons the suite already has.
- Run the tests from a checkout outside `/tmp`: the CLI refuses a workdir under
  `/tmp` (`subfleet/cli.py:611-615`), which fails the batch and outcome tests.
  The full suite is `uv run pytest -q`; it must pass before the PR is opened.
- Scheduler, admission and classification are load-bearing. State each
  change's invariants in the PR and add property tests (Hypothesis) for them
  beside the example tests.
- The Claude classifier bug in brief 0 applies to any Claude lane that reviews
  this work: a reviewer that writes plan-limit wording in plain words gets its
  review classed `limited`. Until brief 0 lands, route reviews of these
  changes to Astra (`--tier hard`) or tell the reviewer to hyphenate.

## 0. Already in flight: land or rebase

These open pull requests address items in the comparison and were
`CONFLICTING` against `main` on 2026-09-28. Rebase them or close them with a
reason; do not start duplicate work.

- `MaxGhenis/subfleet-v2#39` (`fix/claude-prose-limit`): classify Claude lane
  faults from error text only, never from the answer. On `origin/main`, the
  limit pattern check still precedes the success check
  (`subfleet/adapters/claude.py`, steps 3 and 5 of `classify`).
- `MaxGhenis/subfleet-v2#27` (`fix/prereservation-worktree-leak`): collect the
  worktrees of jobs that end before any attempt.
- `MaxGhenis/subfleet-v2#28` and `#58`: an Opus gate peer; Fable retired from
  the shipped policy and gates.
- `MaxGhenis/subfleet-v2#38` (`fix/admission-cost`): name admission events for
  what happened.

## 1. Keep busy lanes measured (hours)

Observed 2026-09-28T00:20Z: 35 jobs pending, no open lane, 14 detached attempts
against a fleet cap of 16. Every lane was unmeasured, so each could take one
attempt.

Mechanism at `b053e3de`:

- A reading is fresh only if labelled `provider`, numeric, at most
  `reading_ttl_s` (120 s) old, and before its reset
  (`subfleet/capacity.py:236-248`).
- `judge_lane` gives a lane `max_in_flight_per_lane` (2) with any fresh reading
  and 1 otherwise (`subfleet/scheduler.py:452-455`).
- The usage timer skips any lane with a reserved, starting, running or
  finalizing attempt or a `lane:<id>:` lease (`subfleet/timers.py:326-338`),
  so a busy lane is measured only by its own attempts' `rate_limit_event`
  readings, which go stale 120 s later.
- The Claude usage read is an HTTP call, not a model turn; a 429 yields
  `rate-limited` with no reading (`subfleet/timers.py:426-433`;
  `subfleet/adapters/claude.py:1069-1070`). Nine Claude lanes returned 429 on
  2026-09-28.

Do:

1. Let the timer's HTTP usage probe run on a lane with live attempts, taking
   no slot lease and adding no attempt. Keep model-turn probes and keepalive
   off busy lanes.
2. Back off per lane on a 429 (record it, and do not retry every 60 s), and
   surface a lane whose reads keep failing in `status`.
3. Tests: a busy lane with a fresh HTTP reading gets its second slot; a 429
   leaves the lane at one slot and schedules a later read; property: across
   random interleavings of attempts, readings and probes, no lane ever exceeds
   its cap at placement, and the fleet cap still counts probe reservations.

Reverse if second attempts placed on lanes measured more than 120 s earlier
turn out to hit limits more often than first attempts.

## 2. Accept a finished deliverable before judging containment (hours)

Observed: 23 attempts whose provider exited 0 ended with their job `lost`
(rc 125), and their `-o` was never written. For 19 of them the only census
error was `marker enumeration unavailable`, a failed `ps -axEww` read.

Mechanism at `b053e3de`:

- `_finalize` re-runs the containment census after the exit receipt. If it is
  not verified empty for more than `EXIT_SETTLE_S` (3 s), it quarantines the
  attempt before extracting the deliverable (`subfleet/daemon.py:4634-4645`,
  `4688`).
- An inspection error makes the census not verified empty
  (`subfleet/procs.py:338-340`, `426-445`, `462-463`).
- `_quarantine` ends the job `lost` (or `cancelled`), keeps its `out:` and
  `worktree:` leases, and waits for `kill --confirm-dead` or `--force-release`
  (`subfleet/daemon.py:4469-4501`). Resolving it never accepts or exports.

Do:

1. Retry a failed marker read a bounded number of times before deciding.
2. When the provider exited 0 and the adapter classifies `ok`, extract, accept
   and export the deliverable, then quarantine only the remaining processes:
   keep the lane slot fenced until containment is verified, but let the job
   succeed.
3. When the census finds live processes, record their command names in
   `quarantine_reason` so the cause can be found.
4. Tests: rc 0 plus an unverifiable census exports the deliverable; rc 0 plus
   a live writer still quarantines and keeps the `worktree:` lease; property:
   no attempt with rc 0 and an `ok` class ends `lost` because of an
   inspection error alone.

Reverse if a lingering process is ever shown to change a deliverable after its
exit receipt.

## 3. Fix notice plumbing (hours)

Observed: all 1,388 service notices were `pending`; 1,046 of them were timer
alerts addressed to the literal session `operator`; one session was shown the
same nudge 13 times in an hour; 112 of 120 sessionless job notices were gate
rounds.

Mechanism at `b053e3de`:

- `notice.pending` returns service notices with negated ids; the session hook
  marks printed rows with those ids; `notice.mark` updates only the `notices`
  table, so a service notice never leaves `pending`
  (`subfleet/daemon.py:1959-1986`; `subfleet/hooks.py:436-453`). Only
  `notice.ack` maps negative ids.
- Timer alerts `ping` `alerts.operator_session`, falling back to `operator`;
  neither policy sets it, and nothing reads `operator`
  (`subfleet/daemon.py:1989`, `2498-2501`).
- Gate rounds are submitted with no `caller_session`
  (`subfleet/gate/round.py:66-72`), so their notices name no session and the
  gate advances only when a client polls it (`subfleet/gate/cli.py:126-128`).

Do:

1. Make `notice.mark` handle negated ids the way `notice.ack` does.
2. Deliver timer alerts somewhere a person reads: set
   `alerts.operator_session` from policy, or route alerts to the operator's
   decision queue; stop writing alerts nobody can read.
3. Carry the gate caller's session onto each round job, so a finished round
   wakes the session that started the gate.
4. Tests against the real daemon op (the hook tests use a fake daemon): a
   surfaced service notice is not returned again; property: after any sequence
   of `ping`, `notice.pending` and `notice.mark`, a row marked surfaced is
   never returned by `notice.pending`.

Reverse if re-printing service notices at every prompt is intended; then say so
in the header the hooks print.

## 4. Close routing drift (hours)

Mechanism at `b053e3de`:

- The capacity view recomputes the desktop lane from `~/.claude.json`
  (`subfleet/capacity.py:131-176`, `347-363`), but timers read the stored
  `lanes.desktop` column (`subfleet/timers.py:330`, `690`), which only
  enrolment, seeding and the importer write (`subfleet/store.py:526-565`). On
  2026-09-28 the view marked claude-4 and the column marked claude-1, so the
  live desktop account received keepalive turns and claude-1 received no
  probes.
- `-m`, `why -m`, `--batch` entries and `sessions handoff --to` accept a
  hard-coded list without `luna` (`subfleet/cli.py:73`;
  `subfleet/sessions/cli.py:46`), while the live policy's chains start at
  `luna`.
- `competes()` compares model sets and lane pins only
  (`subfleet/scheduler.py:176-223`), so an older job whose `-x` exclusions or
  missing `--allow-desktop` rule out the only open lane holds back newer jobs
  that lane would take.

Do:

1. Timers take the desktop flag from the view, or the view's flag is written
   back to the store when it changes.
2. Build the `-m` and `--to` choices from the policy's model registry plus the
   retired map.
3. Make competition account for exclusions and `allow_desktop`.
4. Tests: a timer never selects the view's desktop lane; `-m luna` is accepted
   when the policy defines it; an older job excluded from lane L never holds a
   newer job that L admits. Property: `competes(a, b)` is false whenever the
   lanes `a` can use and the lanes `b` can use are disjoint.

Reverse if the stored desktop column is meant to be authoritative; then the
view should not recompute it.
