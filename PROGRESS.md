# Port progress

## State

The requested public port is complete on local `main`. The unified CLI, topology and health behavior, durable ledgers, login ritual, hardened runners, safe expired-token repair, documentation, and privacy scrub are committed; nothing has been pushed.

## Done

- Confirmed the target branch is `main` and recorded the starting commit.
- Inventoried the public package, command wrappers, tests, and private reference surface.
- Established a green pre-port baseline: `183 passed in 6.48s`.
- Renamed the Python package and legacy environment namespace to `carpool` and `CARPOOL_*`.
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
- Unified Claude picking and status snapshots on the durable capacity ledger; stored inference-only setup tokens are not used for routine quota probes, expected one-time enrollment responses are accepted, and successful re-enrollment clears stale cooldowns.
- Hardened the Claude ledger to count normal and cached input tokens, ignore torn/non-finite records, retain legacy totals, and keep cooldown updates locked and monotonic.
- Excluded malformed enrollments and missing secret-store items before dispatch, and made direct runner authentication failures persist the same 30-day cooldown as delegated runs.
- Added free-plan and app-shadow handling to the normalized cross-provider capacity surface, classified Codex windows by duration, and filtered the app home and duplicate paths from explicit lane configuration.
- Added a strictly gated vendor-CLI refresh probe for expired Codex access tokens, with locked renewable leases, pre-command auth revalidation, a 20-minute retry interval, post-refresh re-probe, dry-run safety, and definitive revocation latching until re-login changes the auth timestamp.
- Recomputed and persisted the healed snapshot before alert evaluation so auth recovery and fleet capacity are visible in the same watchdog cycle.
- Closed the legacy account-report probe path so stored Claude setup tokens remain inference-only outside the explicit one-time enrollment validation.
- Made Codex binary resolution and the PATH shim fail closed when no real vendor binary or dispatchable numbered lane exists, preventing accidental desktop-app dispatch and shim recursion.
- Kept the expanded suite green (`352 passed in 22.28s`).
- Rewrote the README and configuration example for the complete public command surface and abstractions, retaining only the one-line former-name note.
- Verified the vendored mirror is byte-identical to its source (`ec9bfc1bcad6fc708b1bb7e0c3b2d2fe12d977c4186bdbd5ae3cfa41dbdd04b2`).
- Scrubbed the tracked tree and lockfile: all example emails use the reserved `example.com` domain, the named private identifiers and UUID-shaped values are absent, and no obsolete executable or environment namespace remains.
- Completed the final suite at the committed tree: `358 passed in 23.48s`.

## Next

- Review the local commits and only push after the gated review approves them.
