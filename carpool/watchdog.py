"""Periodic watchdog: snapshot, persist, detect transitions, alert the operator.

Alerting contract (incident 2026-07-11 postmortem):
- Alert EARLY on definitive auth failures (token-revoked, duplicate bindings)
  — these are silent until a lane dies mid-program.
- Alert on capacity cliffs (≤1 codex lane left, none left) with the earliest
  reset time, and on a Claude session limit with its reset time.
- Never auto-heal: every alert names the exact command for the operator.
- Dedup: a condition alerts on transition, then at most every REALERT_HOURS
  while it persists; recovery of auth conditions sends one all-clear.
- A run where every codex probe is a network error is treated as "machine
  offline" — no alerts, snapshot marked accordingly (silence must never look
  like success, but offline must not cry wolf either).
"""

import json
from . import notify, paths, snapshot
from .util import atomic_write_json, fmt_clock, load_json, now_local, parse_iso

REALERT_HOURS = 6


def _notify(subject: str, body: str, dry_run: bool) -> bool:
    return notify.send(subject, body, dry_run=dry_run)


def _short(home: str) -> str:
    import pathlib

    return home.replace(str(pathlib.Path.home()), "~")


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
            conditions.append(
                {
                    "key": f"codex-revoked:{e['home']}",
                    "severity": "warn",
                    "subject": f"codex auth: {_short(e['home'])} token revoked",
                    "body": (
                        f"{_short(e['home'])} ({e.get('email') or e.get('account_id', '?')}) "
                        "returned 401 token_revoked on the usage endpoint.\n"
                        "Next codex run there will try a token refresh; if it fails with "
                        "'refresh token was revoked', the home is dead until re-login.\n"
                        f"Heal: CODEX_HOME={_short(e['home'])} codex login\n"
                        "One login at a time (port 1455); pick a DISTINCT account per home."
                    ),
                }
            )
        elif e["verdict"] == "auth-suspect":
            conditions.append(
                {
                    "key": f"codex-suspect:{e['home']}",
                    "severity": "warn",
                    "subject": f"codex auth: {_short(e['home'])} probe failing",
                    "body": (
                        f"{_short(e['home'])} usage probe: {e['probe'].get('status')} "
                        f"{e['probe'].get('error', '')}\nLocal auth.json looks fine — "
                        "watch for 401s; if persistent, re-login that home."
                    ),
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
                    f"failing.{lane_reset_txt}\nDetails: carpool claude-pick --json --all"
                ),
            }
        )
    return conditions


def run(dry_run: bool = False, live: bool = True, snap: dict | None = None) -> dict:
    """One watchdog cycle. Returns a summary dict (also printed by the CLI)."""
    now = now_local()
    snap = snap or snapshot.build(live=live)
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
             "codex-fleet-empty", "claude-lane-auth:", "claude-lanes-empty")
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
    }
