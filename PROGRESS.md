# Port progress

## State

Porting the unified private `carpool` reference implementation into this public repository on `main`. The target worktree has been inventoried; an existing untracked `uv.lock` is being preserved until dependency reconciliation determines whether it belongs in the port.

## Done

- Confirmed the target branch is `main` and recorded the starting commit.
- Inventoried the public package, command wrappers, tests, and private reference surface.
- Established the staged implementation order: public rename first, then behavioral slices, reference tests, documentation, and final scrub.

## Next

- Rename `ai_lanes` to `carpool`, update packaging and environment names, and consolidate the legacy wrappers behind the unified `carpool` command.
