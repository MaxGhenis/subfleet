# Port progress

## State

The public rename, unified CLI, topology/health behavior, durable ledger, login ritual, and hardened auto-picking runners are complete. The next slice commits the safe expired-token watchdog repair, followed by documentation and the final privacy audit.

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
- Made `carpool codex -H` optional, added exclusion-aware selection and mid-run re-picking, resolved the real vendor binary without relying on a scheduler's PATH, and recorded every provider attempt.
- Added the `bin/codex` auto-pick shim with `CARPOOL_NO_AUTOPICK`, while preserving explicit `CODEX_HOME` and interactive commands.
- Ported the Claude auto-pick/re-pick runner through the public secret store, including lane usage/run ledgers, rc 4/5 classification, API-key scrubbing, safe detach/salvage handling, and served-model downgrade markers.
- Added a generic Codex guard and live trust preflight for four portable high-risk command patterns; organization-specific policies were deliberately excluded.
- Preserved caller working directories in the repository launcher and kept the combined suite green (`317 passed`).
- Added `carpool login codex <N|app>` with configurable browser launching, private state permissions, a detached completion watcher, distinct numbered-lane validation, app-shadow reporting, public notifications, and a configurable status refresh.
- Completed unified `mirror`, `brief`, and app-inclusive `errors` routing and added compact Markdown capacity rendering.
- Unified Claude picking and status snapshots on the durable capacity ledger; inference-only setup tokens are no longer sent to the usage endpoint, expected enrollment responses are accepted, and successful re-enrollment clears stale cooldowns.
- Added free-plan and app-shadow handling to the normalized cross-provider capacity surface, classified Codex windows by duration, and filtered the app home and duplicate paths from explicit lane configuration.
- Kept the expanded suite green (`339 passed in 24.08s`).

## Next

- Commit the one-shot, rate-limited Codex expired-access-token repair and its revocation latch.
- Rewrite the README, scrub identifiers and legacy names, run the exact final suite, and prepare the local review report without pushing.
