"""Periodic watchdog: snapshot, persist, detect transitions, alert Max.

Alerting contract (incident 2026-07-11 postmortem):
- Alert EARLY on definitive auth failures (token-revoked, duplicate bindings)
  — these are silent until a lane dies mid-program.
- Alert on capacity cliffs (≤1 codex lane left, none left) with the earliest
  reset time, and on Claude session or model-scoped limits with reset times.
- Never auto-login: logins are Max-only. Every alert names the exact command.
  ONE automatic heal exists (added 2026-08-12 after ~/.codex-2 sat
  AUTH-SUSPECT for 14h over a merely expired access token): a home whose
  usage probe 401s token-expired gets a one-shot `codex exec` refresh probe —
  the codex CLI refreshes and atomically persists its own token, so subfleet
  still never writes auth.json. 'refresh token was revoked' stays definitive
  death: it downgrades the home to auth-revoked and latches until re-login.
- Dedup: a condition alerts on transition, then at most every REALERT_HOURS
  while it persists; recovery of auth conditions sends one all-clear.
- A run where every codex probe is a network error is treated as "machine
  offline" — no alerts, snapshot marked accordingly (silence must never look
  like success, but offline must not cry wolf either).
"""

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from . import capacity, capacity_expiry, codex, paths, render, reset_policy, snapshot
from .util import atomic_write_json, fmt_clock, iso, load_json, now_local, parse_iso

REALERT_HOURS = 6
# Expired-token heal: one `codex exec` probe per home per watchdog cycle
# (cycles run every 30 min; the spacing also keeps manual back-to-back
# `subfleet watch` runs from hammering a lane that will not heal).
REFRESH_PROBE_SPACING_MIN = 20


def _notify(subject: str, body: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"[dry-run] ALERT: {subject}\n{body}\n", file=sys.stderr)
        return True
    notify = paths.notify_bin()
    if not notify.exists():
        print(f"subfleet: notify transport missing at {notify}; alert lost: {subject}", file=sys.stderr)
        return False
    try:
        r = subprocess.run([str(notify), subject, body], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"subfleet: notify failed rc={r.returncode}: {r.stderr[:200]}", file=sys.stderr)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"subfleet: notify error: {e}", file=sys.stderr)
        return False


def _short(home: str) -> str:
    import pathlib

    return home.replace(str(pathlib.Path.home()), "~")


def _reprobe_home(home: str) -> dict:
    """Fresh auth read + live usage probe (+ rollout fallback) for one home,
    after a refresh attempt may have rewritten its auth.json."""
    auth = codex.read_auth(Path(home))
    probe = codex.probe_wham(auth)
    observed = None
    if probe.get("status") != "ok":
        observed = codex.latest_rollout_rate_limits(Path(home))
    return {"auth": auth, "probe": probe, "observed": observed}


def _apply_reset_redemption(snap: dict, redemption: dict, now) -> dict | None:
    """Apply a policy re-probe to its lane and return the refreshed row."""
    home = redemption.get("home") or redemption.get("lane")
    probe = redemption.get("probe")
    if not home or not isinstance(probe, dict):
        return None
    row = next(
        (entry for entry in snap["codex"]["homes"] if entry.get("home") == home),
        None,
    )
    if row is None:
        return None

    reset_policy.apply_redemption_to_snapshot_row(row, redemption, now=now)
    row["email"] = row.get("email") or redemption.get("email")

    # A successful full reset supersedes every older rollout short-window
    # error. Leaving it on the row would make this cycle's picker recount skip
    # the freshly reset lane before the policy state reaches later processes.
    recent = dict(row.get("recent_errors") or {})
    recent["usage_limit"] = []
    row["recent_errors"] = recent
    snap["codex"]["fleet"] = snapshot.codex_fleet(snap["codex"]["homes"], now)
    return row


def heal_expired_codex_homes(snap: dict, dry_run: bool = False,
                             refresh_fn=None, reprobe_fn=None) -> list[dict]:
    """Auto-heal homes whose usage probe 401'd over an EXPIRED access token.

    That 401 is usually a false negative: the stored token aged out, and any
    real CLI call refreshes it (the CLI persists the rotation atomically in
    its own home — subfleet itself still never writes auth.json). One tiny
    `codex exec` turn per home, then a usage re-probe for a fresh verdict.
    'refresh token was revoked' (from the probe or the re-probe) downgrades
    the home to auth-revoked and LATCHES: no retry until auth.json changes
    (re-login rewrites last_refresh). Non-revoked failures retry next cycle,
    spaced by REFRESH_PROBE_SPACING_MIN.

    Mutates snap entries and the fleet counts in place; returns
    [{"home", "result": "healed"|"revoked"|"failed", "detail"}] for attempts
    actually made this cycle (latch re-application is not an attempt)."""
    candidates = [
        e for e in snap["codex"]["homes"]
        if e.get("verdict") == "auth-suspect"
        and codex.probe_looks_token_expired(e.get("probe") or {})
    ]
    if not candidates:
        return []
    now = now_local()
    refresh_fn = refresh_fn or codex.refresh_via_cli
    reprobe_fn = reprobe_fn or _reprobe_home
    state = load_json(paths.refresh_probes_path(), {}) or {}
    events: list[dict] = []
    state_changed = verdicts_changed = False
    for e in candidates:
        home = e["home"]
        prev = state.get(home) or {}
        if prev.get("result") == "revoked" \
                and prev.get("auth_last_refresh") == e.get("auth_last_refresh"):
            # Definitive death observed earlier and auth.json is unchanged:
            # carry the knowledge forward instead of reporting suspect again.
            e["verdict"] = "auth-revoked"
            e["refresh_probe"] = {**prev, "latched": True}
            verdicts_changed = True
            continue
        last = parse_iso(prev.get("attempted_at"))
        if last and now - last < timedelta(minutes=REFRESH_PROBE_SPACING_MIN):
            continue
        if dry_run:
            print(f"[dry-run] would refresh-probe {_short(home)} (expired access token)",
                  file=sys.stderr)
            continue
        attempt = refresh_fn(home)
        detail = (attempt.get("detail") or "").strip()
        if attempt.get("status") == "revoked":
            result = "revoked"
            e["verdict"] = "auth-revoked"
        else:
            # Even a failed exec may have refreshed the token at CLI startup
            # (e.g. the turn itself hit a usage limit) — re-probe regardless.
            fresh = reprobe_fn(home)
            auth, probe = fresh.get("auth") or {}, fresh.get("probe") or {}
            observed = fresh.get("observed")
            if probe.get("status") in ("ok", "token-revoked"):
                verdict = snapshot.codex_verdict(auth, probe, observed)
                e["probe"] = {k: v for k, v in probe.items() if not str(k).startswith("_")}
                e["reset_credits"] = codex.reset_credits_from_probe(probe)
                e["windows"] = snapshot.effective_windows(probe, observed)
                e["rollout_observed"] = observed
                e["verdict"] = verdict
                e["email"] = auth.get("email") or probe.get("email") or e.get("email")
                e["plan"] = probe.get("plan_type") or auth.get("plan") or e.get("plan")
                e["auth_last_refresh"] = auth.get("last_refresh") or e.get("auth_last_refresh")
                result = "healed" if verdict in ("ok", "limited") else "revoked"
                if result == "revoked":
                    detail = probe.get("error") or detail
            else:
                result = "failed"
                exec_note = f"exec rc={attempt.get('rc')}" + (f" ({detail})" if detail else "")
                reprobe_note = (f"usage re-probe {probe.get('status') or '?'} "
                                f"{probe.get('error') or ''}").strip()
                detail = f"{exec_note}; {reprobe_note}"
        e["refresh_probe"] = {"attempted_at": iso(now), "result": result,
                              "rc": attempt.get("rc"), "detail": detail}
        state[home] = {"attempted_at": iso(now), "result": result,
                       "auth_last_refresh": e.get("auth_last_refresh"),
                       "detail": detail[:200]}
        state_changed = True
        verdicts_changed = verdicts_changed or result in ("healed", "revoked")
        events.append({"home": home, "result": result, "detail": detail})
        print(f"subfleet: refresh probe {_short(home)}: {result}"
              + (f" ({detail})" if detail else ""), file=sys.stderr)
    if state_changed:
        atomic_write_json(paths.refresh_probes_path(), state)
    if verdicts_changed:
        fleet_now = parse_iso(snap.get("generated_at")) or now
        snap["codex"]["fleet"] = snapshot.codex_fleet(snap["codex"]["homes"], fleet_now)
    return events


def _scoped_limit_conditions(snap: dict, now) -> list[dict]:
    """Critical model buckets from normalized account rows, with probe fallback."""
    claude_section = snap.get("claude") or {}
    entries = []
    for account in claude_section.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        account_capacity = account.get("capacity")
        if not isinstance(account_capacity, dict):
            continue
        account_id = (
            account.get("email")
            or account_capacity.get("email")
            or account_capacity.get("id")
            or "active-account"
        )
        limits = account_capacity.get("scoped_limits")
        if not isinstance(limits, list):
            continue
        entries.extend((str(account_id), limit) for limit in limits)

    # Compatibility fallback for snapshots made before capacity rows carried
    # limits, or callers that supply only the normalized OAuth probe.
    if not entries:
        probe = claude_section.get("oauth_probe") or {}
        limits = probe.get("limits") if isinstance(probe, dict) else None
        identity = claude_section.get("account") or {}
        account_id = (
            identity.get("email")
            or identity.get("account_uuid")
            or "active-account"
        ) if isinstance(identity, dict) else "active-account"
        if isinstance(limits, list):
            entries.extend((str(account_id), limit) for limit in limits)

    conditions = []
    seen = set()
    for account_id, limit in entries:
        if not isinstance(limit, dict) or not capacity.scoped_limit_exhausted(limit):
            continue
        model = limit.get("scope_model")
        if not isinstance(model, str) or not model.strip():
            continue
        kind = str(limit.get("kind") or limit.get("group") or "scoped")
        surface = str(limit.get("scope_surface") or "all")
        key = (
            f"claude-scoped-limit:{account_id}:{model.casefold()}:"
            f"{kind.casefold()}:{surface.casefold()}"
        )
        if key in seen:
            continue
        seen.add(key)
        percent = limit.get("percent")
        percent_text = (
            f"{float(percent):g}%"
            if isinstance(percent, (int, float)) and not isinstance(percent, bool)
            else "?%"
        )
        severity = str(limit.get("severity") or "unknown")
        reset = parse_iso(limit.get("resets_at"))
        conditions.append(
            {
                "key": key,
                "severity": "critical",
                "subject": f"claude {model} scoped limit critical",
                "body": (
                    f"{account_id}: {model} {kind} is {percent_text} {severity}. "
                    f"Resets {fmt_clock(reset, now)}."
                ),
            }
        )
    return conditions


def evaluate_conditions(snap: dict) -> list[dict]:
    """Pure function: snapshot -> list of active alert conditions."""
    now = parse_iso(snap["generated_at"]) or now_local()
    conditions = _scoped_limit_conditions(snap, now)
    homes = snap["codex"]["homes"]

    probes = [e["probe"].get("status") for e in homes]
    all_network_failed = probes and all(s == "network-error" for s in probes)
    if all_network_failed:
        return [
            {"key": "offline", "severity": "info", "silent": True,
             "subject": "offline", "body": "all codex probes were network errors"},
            *conditions,
        ]

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
                        f"Heal (Max-only): CODEX_HOME={_short(e['home'])} codex login\n"
                        "Pick the account that home is supposed to hold; verify distinctness "
                        "afterward with: subfleet status"
                    ),
                }
            )
        if e["verdict"] == "auth-revoked":
            via_probe = (e.get("refresh_probe") or {}).get("result") == "revoked"
            if via_probe:
                severity = "critical"
                cause = (
                    "hit 'refresh token was revoked' on the watchdog's `codex exec` "
                    "refresh probe — definitive: the home is dead until re-login "
                    "(no further auto-probes until auth.json changes).\n"
                )
            else:
                severity = "warn"
                cause = (
                    "returned 401 token_revoked on the usage endpoint.\n"
                    "Next codex run there will try a token refresh; if it fails with "
                    "'refresh token was revoked', the home is dead until re-login.\n"
                )
            conditions.append(
                {
                    "key": f"codex-revoked:{e['home']}",
                    "severity": severity,
                    "subject": f"codex auth: {_short(e['home'])} token revoked",
                    "body": (
                        f"{_short(e['home'])} ({e.get('email') or e.get('account_id', '?')}) "
                        f"{cause}"
                        f"Heal (Max-only): CODEX_HOME={_short(e['home'])} codex login\n"
                        "One login at a time (port 1455); pick a DISTINCT account per home."
                    ),
                }
            )
        elif e["verdict"] == "auth-suspect":
            probe_line = (
                f"{_short(e['home'])} usage probe: {e['probe'].get('status')} "
                f"{e['probe'].get('error', '')}".rstrip()
            )
            attempt = e.get("refresh_probe") or {}
            if attempt.get("result") == "failed":
                body = (
                    f"{probe_line}\n"
                    "Auto-heal `codex exec` refresh probe FAILED: "
                    f"{attempt.get('detail') or 'no output'}.\n"
                    "It retries next cycle; only 'refresh token was revoked' is "
                    "definitive death.\n"
                    "If this persists, re-login (Max-only): "
                    f"CODEX_HOME={_short(e['home'])} codex login"
                )
            elif codex.probe_looks_token_expired(e.get("probe") or {}):
                body = (
                    f"{probe_line}\n"
                    "Expired ACCESS token — usually a false negative (the stored "
                    "token aged out). The watchdog auto-heals this with a one-shot "
                    "`codex exec` refresh probe on the next live cycle; the CLI "
                    "persists the refreshed token itself. Escalate only if this "
                    "alert persists."
                )
            else:
                body = (
                    f"{probe_line}\nLocal auth.json looks fine — "
                    "watch for 401s; if persistent, re-login that home."
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
                        f"Heal (Max-only): CODEX_HOME={_short(e['home'])} codex login\n"
                        "Likely cause: the same account bound in two homes (one refresh revokes "
                        "the sibling). Verify all homes hold distinct accounts afterward: subfleet status"
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
                    "to a distinct account (Max-only): CODEX_HOME=<home> codex login"
                ),
            }
        )

    for e in homes:
        if e["verdict"] == "free-plan":
            conditions.append(
                {
                    "key": f"codex-free-plan:{e['home']}",
                    "severity": "warn",
                    "subject": f"codex: {_short(e['home'])} is on the FREE plan",
                    "body": (
                        f"{_short(e['home'])} is bound to {e.get('email') or e.get('account_id', '?')}, "
                        "which has no paid ChatGPT plan — no Pro models or quota, so it is "
                        "excluded from dispatch.\n"
                        f"Heal (Max-only): buy Pro on {e.get('email') or 'that account'}, then "
                        f"re-login so the token carries the entitlement: "
                        f"CODEX_HOME={_short(e['home'])} codex login"
                    ),
                }
            )

    # App-shadowed lanes: the ChatGPT/Codex desktop app (~/.codex) is signed
    # into a lane's account. Two token copies of one account — whichever
    # refreshes first revokes the other — so the lane is fragile while
    # shadowed. This is the steady state under Max's sign-in/out habit, not
    # an anomaly: alert ONCE per (app account -> lane) transition (no 6h
    # re-alert, no recovery notice), so each app switch costs one message.
    app_home = snap["codex"].get("app_home") or {}
    for lane_home in app_home.get("shadows") or []:
        conditions.append(
            {
                "key": f"codex-app-shadow:{lane_home}",
                "severity": "warn",
                "once": True,
                "subject": f"codex: app is signed into {_short(lane_home)}'s account",
                "body": (
                    f"The ChatGPT/Codex app ({_short(app_home.get('home', '~/.codex'))}) is signed "
                    f"into {app_home.get('email') or app_home.get('account_id', '?')} — the same "
                    f"account as lane {_short(lane_home)}. Two token copies of one account: "
                    "whichever refreshes first revokes the other, so that lane may die with "
                    "'refresh token was revoked' while the app stays there. Dispatch order "
                    "still follows the earliest weekly reset; automatic reset redemption "
                    "avoids this lane while an unshadowed candidate exists.\n"
                    f"If it dies: CODEX_HOME={_short(lane_home)} codex login (Max-only). "
                    "Checking for a reset? `subfleet` shows every account's windows without "
                    "signing in anywhere."
                ),
            }
        )

    fleet = snap["codex"]["fleet"]
    expiry = snap["codex"].get("capacity_expiry") or {}
    projected_unused = expiry.get(
        "projected_unused_windows_raw",
        expiry.get("projected_unused_windows"),
    )
    if not isinstance(projected_unused, (int, float)) or isinstance(projected_unused, bool):
        projected_unused = expiry.get(
            "known_projected_unused_windows_raw",
            expiry.get("known_projected_unused_windows"),
        )
    earliest_weekly = parse_iso(expiry.get("earliest_reset_at"))
    if (
        isinstance(projected_unused, (int, float))
        and not isinstance(projected_unused, bool)
        and projected_unused > 1.0
        and earliest_weekly is not None
        and timedelta(0) < earliest_weekly - now < timedelta(days=3)
    ):
        by_home = {
            entry.get("home"): entry for entry in homes if isinstance(entry, dict)
        }
        lane_ids = expiry.get("at_risk_lanes") or [
            home for home, projection in (expiry.get("lanes") or {}).items()
            if isinstance(projection, dict)
            and isinstance(projection.get("projected_unused_percent"), (int, float))
            and projection["projected_unused_percent"] > 0
        ]
        labels = [
            (by_home.get(home) or {}).get("email") or _short(home)
            for home in lane_ids
        ]
        conditions.append({
            "key": "codex-capacity-expiring",
            "severity": "warn",
            "min_interval_hours": 24,
            "subject": "codex: capacity expiring unused",
            "body": (
                "queue more sol work — "
                f"{', '.join(labels) or 'Codex lanes'} will expire "
                f"~{round(float(projected_unused) * 100)}% unused on "
                f"{fmt_clock(earliest_weekly, now)}"
            ),
        })
    reset_holders = snapshot.limited_reset_credit_homes(homes)
    auto_reset = snap["codex"].get("auto_reset")
    if not isinstance(auto_reset, dict):
        auto_reset = reset_policy.load_config()
    if (
        not auto_reset.get("enabled", True)
        and fleet["dispatchable_now"] <= 1
        and len(reset_holders) >= 2
    ):
        accounts = ", ".join(
            entry.get("email")
            or entry.get("account_id")
            or _short(entry["home"])
            for entry in reset_holders
        )
        conditions.append(
            {
                "key": "codex-resets-idle",
                "severity": "warn",
                "once": True,
                "subject": "codex: reset credits idle while fleet is low",
                "body": (
                    f"Limited accounts holding unused reset credits: {accounts}.\n"
                    "Heal: `subfleet reset codex all`"
                ),
            }
        )
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
                        "Details: subfleet status",
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
                        f"Heal (Max-only): claude setup-token   # sign into {lane['email']}\n"
                        f"                 subfleet enroll {lane['email']}"
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
                    f"failing.{lane_reset_txt}\nDetails: subfleet pick claude --json --all"
                ),
            }
        )

    # Session mirror (subfleet mirror): the interactive-side sibling of the
    # lanes. A dead mirror job fails silently — sessions stop syncing across
    # accounts and Max only notices at the next login switch.
    mirror = snap["claude"].get("session_mirror") or {}
    if mirror.get("status") == "stalled":
        if mirror.get("job_loaded") is False:
            detail = "launchd job not loaded"
        elif mirror.get("run_min") is not None:
            detail = f"run hung for {mirror['run_min']} min"
        else:
            detail = f"heartbeat idle {mirror.get('age_min', '?')} min, no run in flight"
        conditions.append(
            {
                "key": "cc-mirror-stalled",
                "severity": "warn",
                "subject": "subfleet mirror: stalled",
                "body": (
                    f"Desktop session mirroring has stopped ({detail}) — account "
                    "switches will hide sessions until it runs again.\n"
                    "Heal: launchctl kickstart -k gui/$UID/com.maxghenis.cos.subfleet-mirror\n"
                    f"Heartbeat: {mirror.get('log', '~/.claude/cc-mirror-state.json')} · log: ~/chief-of-staff/state/logs/subfleet-mirror.log"
                ),
            }
        )
    return conditions


def run(dry_run: bool = False, live: bool = True, snap: dict | None = None,
        policy_fn=None) -> dict:
    """One watchdog cycle. Returns a summary dict (also printed by the CLI)."""
    now = now_local()
    snap = snap or snapshot.build(live=live)
    # Heal before persisting so the snapshot/brief/history and the conditions
    # below all see post-heal verdicts and fleet counts.
    heal_events = heal_expired_codex_homes(snap, dry_run=dry_run)
    policy_now = parse_iso(snap.get("generated_at")) or now
    policy_config = reset_policy.load_config()
    snap["codex"]["auto_reset"] = policy_config
    ranked = snapshot.rank_for_dispatch(snap["codex"]["homes"], now=policy_now)
    policy_result = (policy_fn or reset_policy.run)(
        snap["codex"]["homes"],
        dry_run=dry_run,
        now=policy_now,
        config=policy_config,
        dispatchable_homes={row["home"] for row in ranked},
    )
    redemption = policy_result.get("redeemed") if isinstance(policy_result, dict) else None
    if isinstance(redemption, dict):
        refreshed = _apply_reset_redemption(snap, redemption, policy_now)
        email = redemption.get("email") or (refreshed or {}).get("email") or "?"
        remaining = redemption.get("remaining")
        remaining_text = str(remaining) if remaining is not None else "?"
        weekly_reset = (
            redemption.get("weekly_reset_at")
            or (((refreshed or {}).get("windows") or {}).get("weekly") or {}).get("reset_at")
        )
        _notify(
            f"ℹ️ codex: reset redeemed on {email}",
            f"Redeemed one Codex reset on {email}. "
            f"{remaining_text} credits remain fleet-wide. "
            f"Weekly reset now {fmt_clock(parse_iso(weekly_reset), policy_now)}.",
            dry_run,
        )
    history_records = capacity_expiry.read_history()
    snap["codex"]["capacity_expiry"] = capacity_expiry.analyze(
        snap["codex"]["homes"], history_records, now=policy_now
    )
    state_dir = paths.state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    atomic_write_json(paths.snapshot_path(), snap)

    # Compact history line for later trend analysis.
    hist = {
        "ts": snap["generated_at"],
        "codex": {
            e["home"]: {
                "v": e["verdict"],
                "p5h": (
                    e["windows"].get("five_hour")
                    or e["windows"].get("primary")
                    or {}
                ).get("used_percent"),
                "wk": (
                    e["windows"].get("weekly")
                    or e["windows"].get("secondary")
                    or {}
                ).get("used_percent"),
            }
            for e in snap["codex"]["homes"]
        },
        # Trend data must be actually-current: a stale reading repeated every
        # cycle would draw a flat line that looks like real usage.
        "claude_5h": (
            (snap["claude"].get("live") or {}).get("five_hour_pct")
            if not (snap["claude"].get("live") or {}).get("stale") else None
        ),
        "claude_lanes": {
            "enrolled": (snap["claude"].get("lanes") or {}).get("enrolled"),
            "dispatchable": (snap["claude"].get("lanes") or {}).get("dispatchable_now"),
        },
    }
    with open(paths.history_path(), "a") as f:
        f.write(json.dumps(hist) + "\n")

    paths.brief_path().write_text(render.brief_md(snap) + "\n")

    conditions = evaluate_conditions(snap)
    alerts_state = load_json(paths.alerts_path(), {}) or {}
    sent, recovered = [], []

    active_keys = {c["key"] for c in conditions}
    for c in conditions:
        if c.get("silent"):
            continue
        prev = alerts_state.get(c["key"]) or {}
        last_sent = parse_iso(prev.get("last_sent"))
        interval_hours = float(c.get("min_interval_hours", REALERT_HOURS))
        due = last_sent is None or (now - last_sent).total_seconds() >= interval_hours * 3600
        if c.get("once") and prev.get("active"):
            due = False  # transition-only conditions never re-alert while they persist
        strict_interval = c.get("min_interval_hours") is not None
        if (due if strict_interval else (not prev.get("active") or due)):
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
        "refresh_probes": heal_events,
        "healed": [ev["home"] for ev in heal_events if ev["result"] == "healed"],
        "reset_policy": policy_result,
        "reset_redemption": redemption,
    }
