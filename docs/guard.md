# Codex command guard

`carpool codex` installs `bin/carpool-guard-hook` as a trusted Codex
`PreToolUse` hook for shell commands. The hook is intentionally small and
portable: it protects only operations whose risk comes from the local machine
or Git repository, with no organization-, service-, account-, or project-
specific rules.

The launcher refuses to start a guarded Codex session unless the installed
Codex app-server reports the hook as enabled and trusted. Set
`CARPOOL_CODEX_GUARD=off` only when you have reviewed the command risk and need
an explicit one-run bypass.

## Policies

The hook emits one of four stable reason tags:

- `[unscoped-search]` denies unbounded `find`, `rg`, and recursive `grep`
  searches over system roots, user-home roots, or temporary-directory roots.
  A precise subtree is allowed. `find -maxdepth ...` and `rg --max-depth ...`
  are treated as bounded.
- `[keychain-dump]` denies `security dump-keychain -d`, which exports decrypted
  keychain data. Narrow item lookups such as `security find-generic-password`
  are allowed.
- `[stash-shared]` denies stash operations when `git worktree list` shows more
  than one worktree. A repository has one shared stash stack, so a pop or apply
  can mix work from concurrent sessions. `git stash list` remains available
  for recovery.
- `[local-main]` denies `git checkout -b` and `git switch -c/--create` from
  local `main` or `master` when the corresponding `origin` ref exists. Fetch
  first and branch from `origin/main` or `origin/master`.

The command inspection is conservative but does not evaluate shell text. It
handles ordinary quoting, command separators, wrappers such as `sudo`, and
redirections. It deliberately does not attempt to interpret dynamically built
commands, variable-expanded paths other than the home-root spellings, or an
arbitrary script passed to another shell. Those forms require human review.

## Hook protocol

The hook reads one Codex event object from stdin. A supported event has
`hook_event_name: "PreToolUse"`, `tool_name: "Bash"`, a `cwd`, and a string or
array at `tool_input.command`.

Allowed commands produce no stdout and exit zero. A denial also exits zero and
prints a Codex hook decision:

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"[rule-tag] ..."}}
```

Malformed JSON, missing fields, unrelated hook events, other tools, and a
missing JSON parser all fail open without a decision. This keeps a broken
optional hook from corrupting tool execution. `carpool-guard preflight` is the
separate fail-closed boundary: a session is not launched if Codex cannot load
and trust the hook.

Denial logging is off by default. Set `CARPOOL_GUARD_LOG` to a file path to
append timestamp, working directory, and a shortened one-line command. A log
write failure never changes the decision.

## Helper CLI

Inspect decisions without launching Codex:

```bash
carpool-guard check 'find / -name result'
carpool-guard check 'find / -maxdepth 2 -name result'
carpool-guard check 'git stash pop' /path/to/a/linked-worktree
printf '%s\n' 'rg needle $HOME' | carpool-guard check -
```

`check` prints `allow` and exits zero, or prints `deny: ...` and exits one. It
does not write the denial log. `--hook /absolute/path` checks a candidate hook,
and `--tool` can verify that irrelevant tool names pass through.

The trust helpers are:

```bash
carpool-guard hash
carpool-guard override
carpool-guard key
carpool-guard preflight -H "$CODEX_HOME" -C "$PWD" --codex /path/to/codex
```

`hash` reproduces Codex's normalized hook-identity SHA-256. `override` prints
the single TOML value passed through `codex exec -c`; it contains the same hash
and enables that hook-state key. Paths requiring shell quoting use the exact
same quoted command in both values.

`preflight` starts `codex app-server` locally with plugins disabled, sends a
`hooks/list` request, and requires the expected entry to be enabled, trusted,
and (when returned) at the expected hash. It uses a disposable `CODEX_HOME`
containing copies of only `config.toml` and `hooks.json`. It never copies or
reads `auth.json`, session stores, or lane databases, and it never lets the
probe write to the supplied home.

Successful preflights are cached using the Codex binary and version, supplied
home path, full hook override, and seed-file fingerprint. A configuration edit
or upgrade therefore triggers a fresh check. `--no-cache` bypasses both reads
and writes. Override the cache with `CARPOOL_GUARD_CACHE`; stale markers are
pruned after 30 days when a new result is written. The app-server deadline is
controlled by `CARPOOL_GUARD_PREFLIGHT_TIMEOUT`.

## After a Codex upgrade

The first guarded run on a new Codex version rechecks the live hook identity.
If Codex changes the hook schema, matcher behavior, state key, or fingerprint
algorithm, preflight fails with the listed trust state and expected hash. Keep
the guard disabled only for the shortest reviewed interval needed to update
the override and identity calculation, then rerun a no-cache preflight.
