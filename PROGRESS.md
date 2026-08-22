# Port progress

## State

The public rename, topology/health behavior, and durable run-ledger core are complete. The next slice ports the re-login ritual through configured state and notifications, then the hardened auto-picking runners.

## Done

- Confirmed the target branch is `main` and recorded the starting commit.
- Inventoried the public package, command wrappers, tests, and private reference surface.
- Established a green pre-port baseline: `183 passed in 6.48s`.
- Renamed the Python package and `AI_LANES_*` environment namespace to `carpool` and `CARPOOL_*`.
- Consolidated the legacy picker/delegate/runner entry points behind `carpool pick`, `carpool run`, `carpool codex`, and `carpool claude`; removed the superseded scripts.
- Kept the renamed public suite green (`180 passed in 5.28s`; three deleted-entry-point checks were intentionally removed).
- Split the observed desktop app home from numbered dispatch lanes and made the app identity authoritative over the configured protected-account fallback.
- Added app-shadow dispatch handicapping and transition-only watchdog warnings, with no periodic re-alert or recovery noise.
- Excluded `plan_type=free` lanes and added watchdog/recovery/render coverage.
- Added mirror sidecar heartbeat health, in-flight tolerance, the 30-minute hang cutoff, generic scheduler/recovery configuration, and watchdog/render coverage.
- Vendored `carpool-mirror` byte-identically from the reference (`ec9bfc1…`) and pinned its public name/version strings.
- Kept the topology/health suite green (`215 passed`).
- Added a private-permission durable run ledger with immutable prompts, provider artifacts, Git/salvage metadata, transcript discovery, collision-safe IDs, and bounded retention.
- Added live in-flight counts as a same-capacity dispatch tie-break; better headroom always wins.
- Exposed `carpool runs`, `carpool runs show`, and the private runner recording hook.
- Kept the expanded suite green (`225 passed`).

## Next

- Port `carpool login codex <N|app>` and its completion watcher through configured state, `notify.py`, and the unified CLI.
- Port runner auto-pick/re-pick, the Codex PATH shim, and the generic guard.
