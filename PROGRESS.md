# Port progress

## State

The public package, environment namespace, launcher, and hardened runners now use `carpool`. The unified command owns Codex/Claude picking, routing, and runner dispatch; the later private behavioral additions are next.

## Done

- Confirmed the target branch is `main` and recorded the starting commit.
- Inventoried the public package, command wrappers, tests, and private reference surface.
- Established a green pre-port baseline: `183 passed in 6.48s`.
- Renamed the Python package and `AI_LANES_*` environment namespace to `carpool` and `CARPOOL_*`.
- Consolidated the legacy picker/delegate/runner entry points behind `carpool pick`, `carpool run`, `carpool codex`, and `carpool claude`; removed the superseded scripts.
- Kept the renamed public suite green (`180 passed in 5.28s`; three deleted-entry-point checks were intentionally removed).

## Next

- Port numbered-lane/app-home topology, protected-account shadowing, free-plan exclusion, mirror health, and watchdog transitions through public configuration and notification abstractions.
