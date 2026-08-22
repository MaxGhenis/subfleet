"""Periodic watchdog: snapshot, persist, detect transitions, alert the operator.

Alerting contract (incident 2026-07-11 postmortem):
- Alert EARLY on definitive auth failures (token-revoked, duplicate bindings)
  — these are silent until a lane dies mid-program.
- Alert on capacity cliffs (≤1 codex lane left, none left) with the earliest
  reset time, and on a Claude session limit with its reset time.
- Never auto-login. The only automatic auth repair is a one-shot vendor-CLI
  refresh probe for an expired access token. Definitive token revocations are
  latched until a re-login changes the lane's auth timestamp.
- Dedup: a condition alerts on transition, then at most every REALERT_HOURS
  while it persists; recovery of auth conditions sends one all-clear.
- A run where every codex probe is a network error is treated as "machine
  offline" — no alerts, snapshot marked accordingly (silence must never look
  like success, but offline must not cry wolf either).
"""

import fcntl
import json
import secrets
import shlex
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from . import codex, config, notify, paths, snapshot
from .util import atomic_write_json, fmt_clock, iso, load_json, now_local, parse_iso

REALERT_HOURS = 6
REFRESH_PROBE_SPACING_MIN = 20


def _notify(subject: str, body: str, dry_run: bool) -> bool:
    return notify.send(subject, body, dry_run=dry_run)


def _short(home: str) -> str:
    import pathlib

    return home.replace(str(pathlib.Path.home()), "~")


def _login_command(home: str) -> str:
    name = __import__("pathlib").Path(home).name
    target = name.removeprefix(".codex-") if name.startswith(".codex-") else "<lane>"
    return f"carpool login codex {target}"


def _reprobe_home(home: str) -> dict:
    """Read fresh auth and usage after the vendor CLI may have refreshed it."""
    path = Path(home)
    auth = codex.read_auth(path)
    probe = codex.probe_wham(auth)
    observed = None
    if probe.get("status") != "ok":
        observed = codex.latest_rollout_rate_limits(path)
    return {"auth": auth, "probe": probe, "observed": observed}


def _recount_codex_fleet(entries: list[dict], now) -> dict:
    """Rebuild the fleet roll-up after an in-cycle refresh changes a lane."""
    dispatchable = []
    resets = []
    for entry in entries:
        windows = entry.get("windows") or {}
        window = windows.get("five_hour") or windows.get("primary") or windows.get("weekly")
        used = window.get("used_percent") if isinstance(window, dict) else None
        if (
            entry.get("verdict") == "ok"
            and not entry.get("duplicate_of")
            and used is not None
            and 100.0 - float(used) >= snapshot.DEFAULT_MIN_HEADROOM
        ):
            dispatchable.append(entry)
        if entry.get("verdict") in ("ok", "limited") and isinstance(window, dict):
            reset = parse_iso(window.get("reset_at"))
            if reset and reset > now:
                resets.append(reset)
    return {
        "total_homes": len(entries),
        "dispatchable_now": len(dispatchable),
        "best_home": snapshot.dispatchable_best(entries),
        "earliest_reset": iso(min(resets)) if resets else None,
    }


@contextmanager
def _refresh_state_guard():
    """Serialize refresh claims without holding a lock during provider work."""
    state_path = paths.refresh_probes_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _refresh_state() -> dict:
    value = load_json(paths.refresh_probes_path(), {}) or {}
    return value if isinstance(value, dict) else {}


def _refresh_claim_owned(home: str, lease_id: str) -> bool:
    with _refresh_state_guard():
        state = _refresh_state()
        current = state.get(home) or {}
        owned = (
            isinstance(current, dict)
            and current.get("result") == "in-progress"
            and secrets.compare_digest(str(current.get("lease_id") or ""), lease_id)
        )
        if owned:
            state[home] = {**current, "attempted_at": iso(now_local())}
            atomic_write_json(paths.refresh_probes_path(), state)
        return owned


def _store_refresh_state(
    home: str,
    value: dict,
    *,
    lease_id: str | None = None,
) -> bool:
    """Merge one result if this process still owns the refresh claim."""
    with _refresh_state_guard():
        state = _refresh_state()
        if lease_id is not None:
            current = state.get(home) or {}
            if not isinstance(current, dict) or not secrets.compare_digest(
                str(current.get("lease_id") or ""), lease_id
            ):
                return False
        state[home] = value
        atomic_write_json(paths.refresh_probes_path(), state)
        return True


def _auth_still_matches(entry: dict, auth_reader) -> bool:
    """Fail closed if a re-login changed the home after snapshot probing."""
    try:
        current = auth_reader(Path(entry["home"]))
    except Exception:
        return False
    if not isinstance(current, dict) or current.get("status") != "ok":
        return False
    comparisons = []
    for snapshot_key, auth_key in (
        ("auth_last_refresh", "last_refresh"),
        ("account_id", "account_id"),
        ("email", "email"),
    ):
        expected = entry.get(snapshot_key)
        if expected is None:
            continue
        actual = current.get(auth_key)
        comparisons.append(
            isinstance(actual, str)
            and str(expected).casefold() == actual.casefold()
        )
    return bool(comparisons) and all(comparisons)


def heal_expired_codex_homes(
    snap: dict,
    dry_run: bool = False,
    refresh_fn=None,
    reprobe_fn=None,
    auth_reader=None,
) -> list[dict]:
    """Refresh only lanes with a clearly expired access token.

    The vendor CLI owns token rotation and persistence; carpool never writes a
    lane's ``auth.json``. A refresh-token revocation is definitive and is
    latched until ``auth_last_refresh`` changes. Other failures may retry after
    ``REFRESH_PROBE_SPACING_MIN``. The snapshot is updated in place.
    """
    candidates = [
        entry
        for entry in (snap.get("codex") or {}).get("homes") or []
        if entry.get("verdict") == "auth-suspect"
        and codex.probe_looks_token_expired(entry.get("probe") or {})
    ]
    if not candidates:
        return []

    now = now_local()
    refresh_fn = refresh_fn or codex.refresh_via_cli
    reprobe_fn = reprobe_fn or _reprobe_home
    auth_reader = auth_reader or codex.read_auth
    events: list[dict] = []
    verdicts_changed = False
    claimed: list[dict] = []

    if dry_run:
        for entry in candidates:
            print(
                f"[dry-run] would refresh-probe {_short(entry['home'])} "
                "(expired access token)",
                file=sys.stderr,
            )
        return []

    try:
        with _refresh_state_guard():
            state = _refresh_state()
            state_changed = False
            for entry in candidates:
                home = entry["home"]
                previous = state.get(home) or {}
                if not isinstance(previous, dict):
                    previous = {}

                if (
                    previous.get("result") == "revoked"
                    and previous.get("auth_last_refresh") == entry.get("auth_last_refresh")
                ):
                    entry["verdict"] = "auth-revoked"
                    entry["refresh_probe"] = {**previous, "latched": True}
                    verdicts_changed = True
                    continue

                last_attempt = parse_iso(previous.get("attempted_at"))
                if last_attempt and now - last_attempt < timedelta(
                    minutes=REFRESH_PROBE_SPACING_MIN
                ):
                    entry["refresh_probe"] = previous
                    continue

                if not _auth_still_matches(entry, auth_reader):
                    entry["refresh_probe"] = {
                        "attempted_at": iso(now),
                        "result": "skipped-auth-changed",
                        "detail": "auth changed after snapshot; refresh suppressed",
                    }
                    continue

                claim = {
                    "attempted_at": iso(now),
                    "result": "in-progress",
                    "lease_id": secrets.token_hex(16),
                    "auth_last_refresh": entry.get("auth_last_refresh"),
                    "detail": "vendor refresh probe claimed",
                }
                state[home] = claim
                entry["refresh_probe"] = claim
                claimed.append(entry)
                state_changed = True
            if state_changed:
                atomic_write_json(paths.refresh_probes_path(), state)
    except OSError as exc:
        print(f"carpool: refresh probe suppressed: cannot claim state ({exc})", file=sys.stderr)
        return []

    for entry in claimed:
        home = entry["home"]
        lease_id = str((entry.get("refresh_probe") or {}).get("lease_id") or "")
        if not lease_id or not _refresh_claim_owned(home, lease_id):
            continue
        if not _auth_still_matches(entry, auth_reader):
            skipped = {
                "attempted_at": iso(now),
                "result": "skipped-auth-changed",
                "auth_last_refresh": entry.get("auth_last_refresh"),
                "detail": "auth changed after refresh claim; command suppressed",
            }
            entry["refresh_probe"] = skipped
            try:
                _store_refresh_state(home, skipped, lease_id=lease_id)
            except OSError:
                pass
            continue
        attempt = refresh_fn(home)
        detail = str(attempt.get("detail") or "").strip()
        if attempt.get("status") == "revoked":
            result = "revoked"
            entry["verdict"] = "auth-revoked"
        else:
            # CLI startup may refresh successfully even if the tiny turn later
            # fails (for example because the account is at its usage limit).
            fresh = reprobe_fn(home)
            auth = fresh.get("auth") or {}
            probe = fresh.get("probe") or {}
            observed = fresh.get("observed")
            if probe.get("status") in ("ok", "token-revoked"):
                verdict = snapshot.codex_verdict(auth, probe, observed)
                entry["probe"] = {
                    key: value
                    for key, value in probe.items()
                    if not str(key).startswith("_")
                }
                entry["windows"] = snapshot.effective_windows(probe, observed)
                entry["rollout_observed"] = observed
                entry["verdict"] = verdict
                entry["email"] = auth.get("email") or probe.get("email") or entry.get("email")
                entry["plan"] = probe.get("plan_type") or auth.get("plan") or entry.get("plan")
                entry["auth_last_refresh"] = (
                    auth.get("last_refresh") or entry.get("auth_last_refresh")
                )
                # A successful auth refresh can legitimately reveal a
                # non-dispatchable free plan. Only the provider's explicit
                # revocation verdict is a revoked credential.
                if verdict == "auth-revoked":
                    result = "revoked"
                    detail = str(probe.get("error") or detail)
                elif verdict in ("ok", "limited", "free-plan"):
                    result = "healed"
                else:
                    result = "failed"
                    detail = (
                        f"exec rc={attempt.get('rc')}; usage re-probe returned "
                        f"{verdict}"
                    )
            else:
                result = "failed"
                exec_note = f"exec rc={attempt.get('rc')}"
                if detail:
                    exec_note += f" ({detail})"
                reprobe_note = (
                    f"usage re-probe {probe.get('status') or '?'} "
                    f"{probe.get('error') or ''}"
                ).strip()
                detail = f"{exec_note}; {reprobe_note}"

        attempted_at = iso(now)
        entry["refresh_probe"] = {
            "attempted_at": attempted_at,
            "result": result,
            "rc": attempt.get("rc"),
            "detail": detail,
        }
        persisted = {
            "attempted_at": attempted_at,
            "result": result,
            "auth_last_refresh": entry.get("auth_last_refresh"),
            "detail": detail[:200],
        }
        try:
            persisted_result = _store_refresh_state(
                home, persisted, lease_id=lease_id
            )
        except OSError as exc:
            print(
                f"carpool: warning: could not persist refresh result for {_short(home)}: {exc}",
                file=sys.stderr,
            )
            persisted_result = False
        if not persisted_result:
            print(
                f"carpool: refresh result for {_short(home)} not persisted "
                "because this process no longer owns the claim",
                file=sys.stderr,
            )
        verdicts_changed = verdicts_changed or result in ("healed", "revoked")
        events.append({"home": home, "result": result, "detail": detail})
        print(
            f"carpool: refresh probe {_short(home)}: {result}"
            + (f" ({detail})" if detail else ""),
            file=sys.stderr,
        )

    if verdicts_changed:
        codex_section = snap.get("codex") or {}
        codex_section["fleet"] = _recount_codex_fleet(
            codex_section.get("homes") or [],
            parse_iso(snap.get("generated_at")) or now,
        )
    return events


def evaluate_conditions(snap: dict) -> list[dict]:
    """Pure function: snapshot -> list of active alert conditions."""
    now = parse_iso(snap["generated_at"]) or now_local()
    conditions = []
    homes = snap["codex"]["homes"]

    probes = [e["probe"].get("status") for e in homes]
    all_network_failed = probes and all(s == "network-error" for s in probes)
    if all_network_failed:
        return [{"key": "offline", "severity": "info", "silent": True,
                 "subject": "offline", "body": "all codex probes were network errors"}]

    for e in homes:
        if e["verdict"] == "no-auth":
            conditions.append(
                {
                    "key": f"codex-noauth:{e['home']}",
                    "severity": "warn",
                    "subject": f"codex auth: {_short(e['home'])} has NO credentials",
                    "body": (
                        f"{_short(e['home'])} has no auth.json — usually an aborted/incomplete "
                        "`codex login` (starting a login purges the old token immediately).\n"
                        f"Heal: CODEX_HOME={_short(e['home'])} codex login\n"
                        "Pick the account that home is supposed to hold; verify distinctness "
                        "afterward with: carpool status"
                    ),
                }
            )
        if e["verdict"] == "auth-revoked":
            via_refresh_probe = (e.get("refresh_probe") or {}).get("result") == "revoked"
            if via_refresh_probe:
                severity = "critical"
                cause = (
                    "hit 'refresh token was revoked' during the watchdog's vendor-CLI "
                    "refresh probe. The lane is dead until re-login, and no further "
                    "refresh probes will run until its auth timestamp changes.\n"
                )
            else:
                severity = "warn"
                cause = (
                    "returned 401 token_revoked on the usage endpoint.\n"
                    "A provider revocation is never auto-healed.\n"
                )
            conditions.append(
                {
                    "key": f"codex-revoked:{e['home']}",
                    "severity": severity,
                    "subject": f"codex auth: {_short(e['home'])} token revoked",
                    "body": (
                        f"{_short(e['home'])} ({e.get('email') or e.get('account_id', '?')}) "
                        f"{cause}"
                        f"Heal: {_login_command(e['home'])} (guided codex login)\n"
                        "Pick a distinct account for each lane."
                    ),
                }
            )
        elif e["verdict"] == "auth-suspect":
            probe_line = (
                f"{_short(e['home'])} usage probe: {e['probe'].get('status')} "
                f"{e['probe'].get('error', '')}"
            ).rstrip()
            attempt = e.get("refresh_probe") or {}
            if attempt.get("result") == "failed":
                body = (
                    f"{probe_line}\n"
                    "Automatic vendor-CLI refresh probe failed: "
                    f"{attempt.get('detail') or 'no output'}.\n"
                    "It may retry after the probe-spacing window; only a revoked "
                    "refresh token is definitive.\n"
                    f"If this persists, run `{_login_command(e['home'])}`."
                )
            elif codex.probe_looks_token_expired(e.get("probe") or {}):
                body = (
                    f"{probe_line}\n"
                    "The stored access token expired. On the next live cycle the "
                    "watchdog will let the vendor CLI perform one refresh and then "
                    "re-probe usage."
                )
            else:
                body = (
                    f"{probe_line}\nLocal auth.json looks fine — watch for 401s; "
                    "if persistent, re-login that lane."
                )
            conditions.append(
                {
                    "key": f"codex-suspect:{e['home']}",
                    "severity": "warn",
                    "subject": f"codex auth: {_short(e['home'])} probe failing",
                    "body": body,
                }
            )
        if e["recent_errors"]["auth_revoked"]:
            seen = e["recent_errors"]["auth_revoked"][0]["observed_at"]
            conditions.append(
                {
                    "key": f"codex-refresh-revoked:{e['home']}",
                    "severity": "critical",
                    "subject": f"codex auth: {_short(e['home'])} REFRESH token revoked",
                    "body": (
                        f"A codex session in {_short(e['home'])} hit 'refresh token was revoked' "
                        f"(seen {seen}). That home is dead until re-login.\n"
                        f"Heal: CODEX_HOME={_short(e['home'])} codex login\n"
                        "Likely cause: the same account bound in two homes (one refresh revokes "
                        "the sibling). Verify configured homes hold distinct accounts afterward: carpool status"
                    ),
                }
            )

        if e["verdict"] == "free-plan":
            account = e.get("email") or e.get("account_id") or "this account"
            conditions.append(
                {
                    "key": f"codex-free-plan:{e['home']}",
                    "severity": "warn",
                    "subject": f"codex: {_short(e['home'])} is on the FREE plan",
                    "body": (
                        f"{_short(e['home'])} is bound to {account}, which has no paid "
                        "Codex entitlement and is excluded from dispatch.\n"
                        "Heal: upgrade that account, then run "
                        f"`{_login_command(e['home'])}` so the lane receives "
                        "the updated entitlement."
                    ),
                }
            )

    for d in snap["codex"]["duplicates"]:
        conditions.append(
            {
                "key": f"codex-dup:{d['account_id']}",
                "severity": "critical",
                "subject": "codex: same account bound in two homes (revocation trap)",
                "body": (
                    f"Account {d['account_id'][:8]}… is bound in: "
                    + ", ".join(_short(h) for h in d["homes"])
                    + "\nWhichever refreshes first revokes the other. Re-login one of them "
                    "to a distinct account: CODEX_HOME=<home> codex login"
                ),
            }
        )

    app_home = snap["codex"].get("app_home") or {}
    for lane_home in app_home.get("shadows") or []:
        conditions.append(
            {
                "key": f"codex-app-shadow:{lane_home}",
                "severity": "warn",
                "once": True,
                "subject": f"codex: app and {_short(lane_home)} share an account",
                "body": (
                    f"The desktop app ({_short(app_home.get('home', '~/.codex'))}) and "
                    f"lane {_short(lane_home)} are both bound to "
                    f"{app_home.get('email') or app_home.get('account_id') or 'one account'}. "
                    "Concurrent token refreshes can revoke the lane, so dispatch is "
                    f"handicapping it. If the lane dies, run `{_login_command(lane_home)}`. "
                    "`carpool status` shows all bindings without switching logins."
                ),
            }
        )

    fleet = snap["codex"]["fleet"]
    reset_txt = (
        f" Earliest 5h reset: {fmt_clock(parse_iso(fleet['earliest_reset']), now)}."
        if fleet.get("earliest_reset")
        else ""
    )
    if fleet["dispatchable_now"] == 0:
        conditions.append(
            {
                "key": "codex-fleet-empty",
                "severity": "critical",
                "subject": "codex: NO dispatchable lanes",
                "body": f"All codex accounts are exhausted, dead, or unknown.{reset_txt}\n"
                        "Details: carpool status",
            }
        )
    elif fleet["dispatchable_now"] == 1:
        best = fleet.get("best_home")
        conditions.append(
            {
                "key": "codex-fleet-low",
                "severity": "warn",
                "subject": "codex: only 1 dispatchable lane left",
                "body": f"Only {_short(best) if best else '?'} has headroom.{reset_txt}",
            }
        )

    active = snap["claude"].get("active_limit")
    if active:
        reset = parse_iso(active.get("reset_at"))
        conditions.append(
            {
                "key": f"claude-limit:{active.get('reset_at') or active.get('observed_at')}",
                "severity": "warn",
                "subject": f"claude: {active['kind']} hit",
                "body": (
                    f"Claude Code reported '{active['text'][:100]}' "
                    f"(seen in {active.get('sessions', 1)} session(s)). "
                    f"Resets {fmt_clock(reset, now)}."
                ),
            }
        )

    lanes = snap["claude"].get("lanes") or {}
    for lane in lanes.get("lanes") or []:
        if lane["verdict"] in ("token-invalid", "secret-missing", "no-token"):
            conditions.append(
                {
                    "key": f"claude-lane-auth:{lane['email']}",
                    "severity": "warn",
                    "subject": f"claude lane {lane['email']}: {lane['verdict']}",
                    "body": (
                        f"Enrolled lane {lane['email']} failed its usage probe "
                        f"({lane['verdict']}) — headless dispatch to it will fail.\n"
                        f"Heal: claude setup-token   # sign into {lane['email']}\n"
                        f"                 carpool enroll {lane['email']}"
                    ),
                }
            )
    if lanes.get("enrolled", 0) >= 1 and lanes.get("dispatchable_now", 0) == 0:
        lane_reset_txt = (
            f" Earliest lane reset: {fmt_clock(parse_iso(lanes['earliest_reset']), now)}."
            if lanes.get("earliest_reset")
            else ""
        )
        conditions.append(
            {
                "key": "claude-lanes-empty",
                "severity": "warn",
                "subject": "claude: no dispatchable lanes",
                "body": (
                    f"All {lanes['enrolled']} enrolled Claude lane(s) are exhausted or "
                    f"failing.{lane_reset_txt}\nDetails: carpool pick claude --json --all"
                ),
            }
        )

    mirror = snap["claude"].get("session_mirror") or {}
    if mirror.get("status") == "stalled":
        if mirror.get("job_loaded") is False:
            detail = "configured scheduler job is not loaded"
        elif mirror.get("run_min") is not None:
            detail = f"run hung for {mirror['run_min']} min"
        else:
            detail = f"heartbeat idle {mirror.get('age_min', '?')} min, no run in flight"
        try:
            restart = config.command("mirror_restart_cmd")
        except config.ConfigError:
            restart = None
        recovery = shlex.join(restart) if restart else "carpool mirror --quiet"
        conditions.append(
            {
                "key": "cc-mirror-stalled",
                "severity": "warn",
                "subject": "carpool mirror: stalled",
                "body": (
                    f"Desktop session mirroring has stopped ({detail}); account switches "
                    "may hide sessions until it runs again.\n"
                    f"Heal: {recovery}\nHeartbeat: {mirror.get('log') or 'not reported'}"
                ),
            }
        )
    return conditions


def run(dry_run: bool = False, live: bool = True, snap: dict | None = None) -> dict:
    """One watchdog cycle. Returns a summary dict (also printed by the CLI)."""
    now = now_local()
    snap = snap or snapshot.build(live=live)
    # Heal before persistence and alert evaluation so every downstream view
    # sees the post-refresh verdict and fleet count.
    refresh_events = heal_expired_codex_homes(snap, dry_run=dry_run)
    state_dir = paths.state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    atomic_write_json(paths.snapshot_path(), snap)

    # Compact history line for later trend analysis.
    active_row = next(
        (row for row in snap["claude"].get("accounts") or [] if row.get("active")),
        {},
    )
    hist = {
        "ts": snap["generated_at"],
        "codex": {
            e["home"]: {
                "v": e["verdict"],
                "p5h": (e["windows"].get("primary") or {}).get("used_percent"),
                "wk": (e["windows"].get("secondary") or {}).get("used_percent"),
            }
            for e in snap["codex"]["homes"]
        },
        "claude_5h": (active_row.get("live") or {}).get("five_hour_pct"),
        "claude_lanes": {
            "enrolled": (snap["claude"].get("lanes") or {}).get("enrolled"),
            "dispatchable": (snap["claude"].get("lanes") or {}).get("dispatchable_now"),
        },
    }
    with open(paths.history_path(), "a") as f:
        f.write(json.dumps(hist) + "\n")

    conditions = evaluate_conditions(snap)
    alerts_state = load_json(paths.alerts_path(), {}) or {}
    sent, recovered = [], []

    active_keys = {c["key"] for c in conditions}
    for c in conditions:
        if c.get("silent"):
            continue
        prev = alerts_state.get(c["key"]) or {}
        last_sent = parse_iso(prev.get("last_sent"))
        due = last_sent is None or (now - last_sent).total_seconds() > REALERT_HOURS * 3600
        if c.get("once") and prev.get("active"):
            due = False
        if not prev.get("active") or due:
            prefix = {"critical": "🚨", "warn": "⚠️"}.get(c["severity"], "ℹ️")
            if _notify(f"{prefix} {c['subject']}", c["body"], dry_run):
                alerts_state[c["key"]] = {"active": True, "last_sent": now.isoformat(timespec="seconds")}
                sent.append(c["key"])
        else:
            alerts_state[c["key"]] = {**prev, "active": True}

    # Recovery notices for auth/fleet conditions that cleared. A cleared key is
    # only "recovered" if the same home has no OTHER active auth condition —
    # revoked→no-auth is a state change, not a recovery.
    def _same_home_still_bad(key: str) -> bool:
        _, _, home = key.partition(":")
        return bool(home) and any(k.partition(":")[2] == home for k in active_keys)

    for key, st in list(alerts_state.items()):
        if not st.get("active") or key in active_keys:
            continue
        if key.startswith(
            ("codex-revoked:", "codex-refresh-revoked:", "codex-noauth:", "codex-dup:",
             "codex-suspect:", "codex-fleet-empty", "codex-free-plan:", "claude-lane-auth:",
             "claude-lanes-empty", "cc-mirror-stalled")
        ) and not _same_home_still_bad(key):
            _notify(f"✅ recovered: {key}", "Condition no longer present.", dry_run)
            recovered.append(key)
        alerts_state[key] = {**st, "active": False}

    if not dry_run:
        atomic_write_json(paths.alerts_path(), alerts_state)

    return {
        "generated_at": snap["generated_at"],
        "conditions": [c["key"] for c in conditions],
        "alerts_sent": sent,
        "recovered": recovered,
        "refresh_probes": refresh_events,
        "healed": [event["home"] for event in refresh_events if event["result"] == "healed"],
    }
