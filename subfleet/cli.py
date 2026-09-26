"""subfleet CLI — Max's multi-account AI capacity stack, one front door.

  subfleet                       # live table: codex lanes, app home, Claude lanes, mirror
  subfleet status --json|--cached
  subfleet capacity [--json]     # fast cached cross-family headroom
  subfleet pick codex            # best CODEX_HOME on stdout (rc=1 if none)
  subfleet pick claude           # best enrolled Claude lane (email) on stdout
  subfleet run --task build --tier standard ... -p prompt.md  # semantic dispatch + auto-lanes
  subfleet gate pr|plan ...                                   # durable main/peer agreement loop
  subfleet codex  -m <model> -C <dir> -p <prompt> -o <out>     # hardened codex exec, lane auto-assigned
  subfleet claude -A|-a <email> -C <dir> -p <prompt> -o <out>  # hardened headless claude -p
  subfleet runs [--last N] [--json] [--mine] [--running]       # durable run ledger, newest first
  subfleet runs show <id> [--err]                              # metadata + saved output/error
  subfleet resume-codex <run-id> [PROMPT]                      # continue a Codex thread on its original lane
  subfleet handoff <claude-session-id>|--last --to sol|terra|astra|opus  # cross-provider continuation
  subfleet runs reap [--dry-run]                               # finalize RUNNING entries whose runner died
  subfleet wait <id>... | --mine | --last [--timeout S] [--cat]  # block until runs finish (background-safe)
  subfleet kill <id>             # SIGTERM a running dispatch (its trap salvages + finalizes)
  subfleet sessions              # live Claude Code sessions with an inbox (notification targets)
  subfleet notify [--session ID] TEXT   # push a message into a session inbox (default: this session)
  subfleet notices [--session ID] [--all] [--json]   # completion notices: pushed / landed / lost / revived
  subfleet hooks install|uninstall|status   # Claude Code hooks: completion catch-up + attached-runner guard
  subfleet revive [--dry-run]        # revive cold interrupted sessions into a persistent tmux host
  subfleet liveness [--dry-run]      # dead sessions with pending work → Telegram Max (no session needed)
  subfleet tickle [--all|--session ID] [--dry-run]   # resume nudge for sessions whose last turn was cut off
  subfleet revive [--model M] [--no-fallback] [--max N] [--dry-run]  # resume cold interrupted sessions
  subfleet login codex <N|app>   # stage a lane (re)login: server, OAuth tab, watcher — Max clicks
  subfleet reset codex <N|all>   # safely consume gifted reset credits on LIMITED lanes
  subfleet enroll <email>        # store a Claude setup-token
  subfleet mirror [--quiet]      # desktop session mirror pass (launchd every 60s)
  subfleet errors --hours 48     # observed limit/auth errors, both providers
  subfleet watch [--dry-run]     # watchdog cycle (launchd every 30 min)
  subfleet keepalive [--dry-run] # keep idle Claude 5h windows rolling
  subfleet brief                 # morning-brief markdown section
"""

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from . import (
    capacity,
    claude,
    codex,
    consensus,
    handoff,
    hooks,
    keepalive,
    notify,
    paths,
    render,
    reserve,
    reset_policy,
    resume_codex,
    run_ledger,
    snapshot,
    tickle,
    watchdog,
)
from . import desktop_app, liveness
from .util import iso, load_json, now_local, strip_private


def _load_snapshot(cached: bool, live_timeout: float = 15.0) -> dict:
    if cached:
        snap = load_json(paths.snapshot_path())
        if snap:
            keepalive_state = load_json(paths.keepalive_state_path())
            if isinstance(keepalive_state, dict):
                snap.setdefault("claude", {})["keepalive"] = keepalive_state
            return snap
        print("subfleet: no cached snapshot yet; probing live", file=sys.stderr)
    # Small error window interactively; the watchdog covers the long window.
    return snapshot.build(live=True, timeout=live_timeout, errors_hours=6)


def cmd_status(args) -> int:
    snap = _load_snapshot(args.cached)
    # A staged desktop-app update is a scheduled kill of every app-hosted
    # session; read the app's log fresh even from a cached snapshot (cheap).
    snap["desktop_app"] = desktop_app.status()
    if args.json:
        print(json.dumps(snap, indent=1))
    else:
        print(render.table(snap))
        if args.cached:
            print(f"\n(cached snapshot from {snap.get('generated_at')}; use without --cached for live)")
    return 0


def cmd_capacity(args) -> int:
    data = capacity.report()
    print(json.dumps(data, indent=1) if args.json else capacity.human_table(data))
    return 0


def cmd_reserve(args) -> int:
    """Fable reserve per Claude account: shared vs Fable weekly use, slack, verdict.

    Reads the v2 login dirs (full-scope logins can read the usage endpoint;
    setup tokens cannot). Percentages only; no token is printed or stored.
    """
    enrolled = claude.roster_config().get("enrolled") or {}
    emails = {email for email in enrolled if isinstance(email, str) and "@" in email}
    logins = paths.claude_logins_dir()
    if logins.is_dir():
        emails |= {p.name for p in logins.iterdir() if p.is_dir() and "@" in p.name}
    pol = reserve.policy()
    rows = reserve.table(sorted(emails), pol=pol)
    if args.json:
        print(json.dumps({"policy": pol, "accounts": rows}, indent=1))
    else:
        print(reserve.human_table(rows, pol))
    return 0


def cmd_pick(args, opener=None, sleep_fn=None, policy_fn=None) -> int:
    snap = _load_snapshot(args.cached)
    handicap = 0.0 if args.no_handicap else args.handicap
    ranked = snapshot.rank_for_dispatch(
        snap["codex"]["homes"], handicap=handicap, min_headroom=args.min_headroom
    )
    if not ranked:
        policy_runner = policy_fn or reset_policy.run
        policy_kwargs = {
            "opener": opener,
            "dispatchable_homes": set(),
        }
        if sleep_fn is not None:
            policy_kwargs["sleep_fn"] = sleep_fn
        decision = policy_runner(snap["codex"]["homes"], **policy_kwargs)
        redeemed = decision.get("redeemed") or {}
        if redeemed:
            target = next(
                (
                    row for row in snap["codex"]["homes"]
                    if row.get("home") == redeemed.get("home")
                ),
                None,
            )
            if target is not None:
                reset_policy.apply_redemption_to_snapshot_row(
                    target, redeemed
                )
                ranked = snapshot.rank_for_dispatch(
                    snap["codex"]["homes"],
                    handicap=handicap,
                    min_headroom=args.min_headroom,
                )
            if not ranked:
                # Consume success is authoritative even if wham remained stale
                # for the full propagation poll.
                ranked = [{
                    "home": redeemed["home"],
                    "account_id": redeemed.get("account_id"),
                    "email": redeemed.get("email"),
                    "five_hour_used_percent": 0.0,
                    "weekly_used_percent": 0.0,
                    "weekly_reset_at": redeemed.get("weekly_reset_at"),
                    "stale": False,
                    "protected": False,
                    "as_of": (redeemed.get("probe") or {}).get("checked_at"),
                    "score": None,
                    "headroom_score": 100.0,
                    "in_flight": redeemed.get("in_flight", 0),
                }]
            print(
                f"subfleet: redeemed reset on {redeemed['email']} "
                f"({redeemed['remaining'] if redeemed.get('remaining') is not None else '?'} "
                "credits remain fleet-wide); "
                f"weekly reset now {redeemed['weekly_reset_at']}",
                file=sys.stderr,
            )
            if decision.get("audit_error"):
                print(
                    f"subfleet: reset audit recording failed: {decision['audit_error']}",
                    file=sys.stderr,
                )
    if args.json:
        out = {
            "generated_at": snap["generated_at"],
            "best": ranked[0]["home"] if ranked else None,
            "ranked": ranked if args.all else ranked[:1],
            "excluded": [
                {
                    "home": e["home"],
                    "verdict": e["verdict"],
                    "duplicate_of": e.get("duplicate_of"),
                    "five_hour_used_percent": (e["windows"].get("primary") or {}).get("used_percent"),
                }
                for e in snap["codex"]["homes"]
                if e["home"] not in {r["home"] for r in ranked}
            ],
        }
        print(json.dumps(out, indent=1))
        return 0 if ranked else 1
    if not ranked:
        earliest = snap["codex"]["fleet"].get("earliest_reset")
        print(
            "subfleet pick: no dispatchable codex home"
            + (f" (earliest 5h reset {earliest})" if earliest else ""),
            file=sys.stderr,
        )
        return 1
    best = ranked[0]
    stale = " [stale data]" if best.get("stale") else ""
    print(best["home"])
    print(
        f"  {best.get('email') or best.get('account_id', '?')} · 5h {best['five_hour_used_percent']:.0f}%"
        f" used · week {best['weekly_used_percent']:.0f}%{stale}",
        file=sys.stderr,
    )
    if args.all:
        for r in ranked[1:]:
            print(
                f"  next: {r['home']} ({r.get('email') or '?'} · 5h {r['five_hour_used_percent']:.0f}%)",
                file=sys.stderr,
            )
    return 0


def _numeric(value):
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)


def _capacity_lane_ranking(data: dict, *, handicap: float,
                           min_headroom: float,
                           model: str | None = None) -> tuple[list[dict], list[dict]]:
    """Rank enrolled Claude lanes from the unified capacity rows.

    Calibrated/live rows rank by their worst-window utilization. Uncalibrated
    rows have no honest percentage, so they follow known rows and rank by raw
    weekly/5h tokens (with the active desktop account last on ties).
    Historical ratios affect ranking only; measured headroom gates availability.
    """
    model = capacity.normalize_claude_model(model)
    ranked = []
    excluded = []
    for row in data.get("accounts") or []:
        if row.get("family") != "claude" or not row.get("enrolled"):
            continue
        five_hour = row.get("five_hour") or {}
        weekly = row.get("weekly") or {}
        fh_used = _numeric(five_hour.get("used_percent"))
        wk_used = _numeric(weekly.get("used_percent"))
        model_window = capacity.model_window_for(row, model) if model else None
        model_limit = capacity.scoped_limit_for(row, model) if model else None
        model_state = capacity.model_state_for(row, model) if model else None
        model_window_used = _numeric((model_window or {}).get("used_percent"))
        model_scoped_used = _numeric((model_limit or {}).get("percent"))
        model_used = _numeric((model_state or {}).get("used_percent"))
        account_used = [value for value in (fh_used, wk_used) if value is not None]
        account_effective = max(account_used) if account_used else None
        account_headroom = (
            max(0.0, min(100.0, 100.0 - account_effective))
            if account_effective is not None else None
        )
        used = [*account_used, *([model_used] if model_used is not None else [])]
        effective = max(used) if used else None
        headroom = max(0.0, min(100.0, 100.0 - effective)) if effective is not None else None
        measured_account_headroom = _numeric(
            row.get("measured_headroom_score", account_headroom)
        )
        measured_headroom = (
            capacity.model_headroom_score(
                dict(row, measured_headroom_score=measured_account_headroom),
                model, measured_only=True,
            ) if model else measured_account_headroom
        )
        hard_status = row.get("status") not in {"ok", "exhausted"}
        below_floor = measured_headroom is not None and measured_headroom < min_headroom
        model_states = capacity.claude_model_states(row)
        scoped_cooldowns = row.get("model_cooldowns") or {}
        default_scoped_gate = bool(scoped_cooldowns) if isinstance(scoped_cooldowns, dict) else False
        model_gate = bool(
            model and model_state
            and model_state["state"] not in {"ok", "exhausted"}
        )
        if (
            hard_status or below_floor
            or (row.get("status") == "exhausted" and measured_account_headroom is None)
            or model_gate or (model is None and default_scoped_gate)
        ):
            if hard_status:
                verdict = row.get("status")
                reset_at = row.get("limited_until")
            elif model_gate:
                verdict = model_state["state"]
                reset_at = model_state.get("until")
            elif model is None and default_scoped_gate:
                verdict = "cooled"
                reset_at = max(
                    (value for value in scoped_cooldowns.values() if isinstance(value, str)),
                    default=None,
                )
            else:
                verdict = "exhausted"
                reset_at = row.get("limited_until")
            if verdict == "exhausted" and reset_at is None:
                governing = []
                for value, window in (
                    (fh_used, five_hour), (wk_used, weekly),
                    (model_window_used, model_window or {}),
                    (model_scoped_used, model_limit or {}),
                ):
                    if (
                        "measured_headroom_score" in row
                        and (window is five_hour or window is weekly)
                        and window.get("confidence") != "live"
                    ):
                        continue
                    reset_at = window.get("reset_at") or window.get("resets_at")
                    if value is not None and value >= 100.0 - min_headroom and reset_at:
                        governing.append(reset_at)
                reset_at = max(governing) if governing else None
            entry = {"email": row.get("email"), "verdict": verdict,
                     "reset_at": reset_at}
            if model:
                entry.update({"model_state": model_state, "model_states": model_states})
            excluded.append(entry)
            continue

        score = effective + (handicap if row.get("active") else 0.0) if effective is not None else None
        ranked_row = {
            "email": row.get("email") or row.get("id"),
            "active": bool(row.get("active")),
            "five_hour_used_percent": fh_used,
            "weekly_used_percent": wk_used,
            "five_hour_tokens": five_hour.get("tokens"),
            "weekly_tokens": weekly.get("tokens"),
            "effective_used_percent": effective,
            "headroom_score": headroom,
            "measured_headroom_score": measured_headroom,
            "five_hour_reset_at": five_hour.get("reset_at"),
            "weekly_reset_at": weekly.get("reset_at"),
            "learned_capacity": row.get("learned_capacity"),
            "confidence": row.get("confidence"),
            "score": round(score, 2) if score is not None else None,
            "in_flight": int(row.get("in_flight") or 0),
        }
        if model:
            ranked_row.update({
                "model_used_percent": model_used,
                "model_reset_at": (model_window or {}).get("reset_at"),
                "model_state": model_state,
                "model_states": model_states,
            })
        ranked.append(ranked_row)

    def _tokens(value):
        number = _numeric(value)
        return float("inf") if number is None else number

    ranked.sort(
        key=lambda row: (
            row["score"] is None,
            row["score"] if row["score"] is not None else 0.0,
            row["in_flight"],
            bool(row["active"]) if row["score"] is None else False,
            _tokens(row["weekly_tokens"]),
            _tokens(row["five_hour_tokens"]),
            str(row["email"]),
        )
    )
    return ranked, excluded


def cmd_claude_pick(args) -> int:
    data = capacity.report()
    model = capacity.normalize_claude_model(getattr(args, "model", None))
    handicap = 0.0 if args.no_handicap else args.handicap
    ranked, excluded = _capacity_lane_ranking(
        data, handicap=handicap, min_headroom=args.min_headroom, model=model
    )
    requested_exclusions = {str(email) for email in args.exclude if email}
    if requested_exclusions:
        kept = []
        for row in ranked:
            if row["email"] in requested_exclusions:
                entry = {"email": row["email"], "verdict": "excluded", "reset_at": None}
                if model:
                    entry.update({
                        "model_state": row.get("model_state"),
                        "model_states": row.get("model_states"),
                    })
                excluded.append(entry)
            else:
                kept.append(row)
        ranked = kept
    enrolled = sum(
        bool(row.get("enrolled"))
        for row in data.get("accounts") or []
        if row.get("family") == "claude"
    )
    resets = [row["reset_at"] for row in excluded if row.get("reset_at")]
    earliest_reset = min(resets) if resets else None
    if args.json:
        out = {
            "generated_at": data.get("generated_at"),
            "best": ranked[0]["email"] if ranked else None,
            "ranked": ranked if args.all else ranked[:1],
            "excluded": excluded,
            "enrolled": enrolled,
            "earliest_reset": earliest_reset,
        }
        if model:
            out["model"] = model
        print(json.dumps(out, indent=1))
        return 0 if ranked else 1
    if not ranked:
        if enrolled == 0:
            print(
                "subfleet pick claude: no lanes enrolled — enroll (Max-only): "
                "claude setup-token, then subfleet enroll <email>",
                file=sys.stderr,
            )
        else:
            blocked = "; ".join(
                f"{row['email']} {row['verdict']}" for row in excluded
            )
            print(
                "subfleet pick claude: no dispatchable claude lane"
                + (f" for {model}" if model else "")
                + (f" (earliest reset {earliest_reset})" if earliest_reset else "")
                + (f" — {blocked}" if blocked else ""),
                file=sys.stderr,
            )
        return 1

    def _detail(r):
        fh = r.get("five_hour_used_percent")
        wk = r.get("weekly_used_percent")
        fh_tokens = r.get("five_hour_tokens")
        wk_tokens = r.get("weekly_tokens")

        def _reading(label, percent, tokens):
            if percent is not None:
                return f"{label} {percent:.0f}%"
            if _numeric(tokens) is not None:
                return f"{label} {int(tokens)} tok"
            return f"{label} ?"

        return _reading("5h", fh, fh_tokens) + " used · " + _reading(
            "week", wk, wk_tokens
        ) + (f" [{r.get('confidence')}]" if r.get("confidence") else "") + (
            " [active login]" if r.get("active") else ""
        )

    best = ranked[0]
    print(best["email"])
    print(f"  {best['email']} · {_detail(best)}", file=sys.stderr)
    if args.all:
        for r in ranked[1:]:
            print(f"  next: {r['email']} ({_detail(r)})", file=sys.stderr)
    return 0


def cmd_errors(args) -> int:
    out = {
        "codex": {
            str(h): codex.recent_limit_errors(h, hours=args.hours)
            for h in [*paths.codex_homes(), paths.app_codex_home()]
            if h.is_dir()
        },
        "claude": claude.transcript_limit_events(hours=args.hours),
    }
    if args.json:
        print(json.dumps(strip_private(out), indent=1))
        return 0
    for home, errs in out["codex"].items():
        for e in errs["usage_limit"]:
            print(f"codex {home}: usage limit at {e['observed_at']} (retry {e['try_again']})")
        for e in errs["auth_revoked"]:
            print(f"codex {home}: REFRESH TOKEN REVOKED at {e['observed_at']}")
    for e in out["claude"]:
        reset = f", resets {e['reset_at']}" if e.get("reset_at") else ""
        print(f"claude: {e['kind']} at {e['observed_at']} x{e['count']}{reset}")
    if not any(v["usage_limit"] or v["auth_revoked"] for v in out["codex"].values()) and not out["claude"]:
        print(f"no limit/auth errors observed in the last {args.hours}h")
    return 0


def cmd_watch(args) -> int:
    summary = watchdog.run(dry_run=args.dry_run)
    print(json.dumps(summary))
    return 0


def cmd_keepalive(args) -> int:
    report = keepalive.run(family=args.family, dry_run=args.dry_run)
    for result in report["results"]:
        status = result["status"]
        if status == "would-open":
            status = "opened (dry-run)"
        elif status == "skipped-auth" and result.get("auth_log_due"):
            code = result.get("auth_code") or "auth"
            status += f" ({code} at {result.get('auth_failed_at') or '?'})"
        elif status == "failed" and result.get("reason"):
            status += f" ({result['reason']})"
        print(f"{result['email']}: {status}")
    return 1 if any(result["status"] == "failed" for result in report["results"]) else 0


def cmd_brief(args) -> int:
    snap = _load_snapshot(cached=True)
    print(render.brief_md(snap))
    return 0


def _probe_with_retry(token: str, attempts: int = 3, pause_s: float = 2.0) -> dict:
    """The usage probe is an auth sanity check; a transient socket error must
    not reject a freshly minted token. Retry only on network-error."""
    import time as _time
    probe = {}
    for i in range(attempts):
        probe = claude.probe_oauth_usage(token)
        if probe.get("status") != "network-error":
            return probe
        if i + 1 < attempts:
            _time.sleep(pause_s)
    return probe


def cmd_enroll(args) -> int:
    """Store a per-account inference token for pinned Claude lane dispatch.

    The token is read from stdin, never argv. The usage request is an auth
    sanity check only: inference-scoped setup tokens normally return 403.
    """
    import getpass
    import subprocess as sp

    email = args.email
    roster = claude.known_accounts()
    if email not in roster:
        print(f"subfleet enroll: {email} is not in the roster ({len(roster)} accounts); "
              f"add it to {claude.roster_config_path()} first", file=sys.stderr)
        return 2
    if getattr(args, "mint", False):
        from . import enroll_mint

        paste = bool(getattr(args, "paste", False))

        def _on_url(url: str) -> None:
            how = ("then paste the code it shows below" if paste
                   else "the CLI receives the code on its own local callback; nothing to paste")
            print(f"subfleet enroll: approve as {email} in the browser ({how}). "
                  f"If it did not open, use this URL:\n  {url}", file=sys.stderr)

        def _code_prompt():
            if not sys.stdin.isatty():
                return None
            return input("subfleet enroll: paste the code the browser shows: ").strip()

        print(f"subfleet enroll: minting a setup-token for {email} via `claude setup-token` "
              "in a subfleet-owned pty (the token is captured, never displayed)", file=sys.stderr)
        minted = enroll_mint.mint(paths.claude_bin(), on_url=_on_url, code_prompt=_code_prompt,
                                  paste=paste)
        diag_path = paths.state_dir() / "enroll-mint-last.json"
        if not minted.token or not enroll_mint.well_formed(minted.token):
            enroll_mint.write_diagnostic(diag_path, enroll_mint.diagnostic(minted))
            why = minted.error or f"captured string has an unexpected shape (len {len(minted.token)})"
            tail = "\n".join(minted.transcript.strip().splitlines()[-6:])
            print(f"subfleet enroll: mint failed: {why}\n{tail}\n(diagnostic, token masked: {diag_path})",
                  file=sys.stderr)
            return 1
        token = minted.token
    elif sys.stdin.isatty():
        token = getpass.getpass(f"Paste setup-token for {email} (input hidden): ").strip()
    else:
        token = sys.stdin.read().strip()
    if not token:
        print("subfleet enroll: empty token", file=sys.stderr)
        return 2
    probe = _probe_with_retry(token)
    probe_status = probe.get("status")
    if getattr(args, "mint", False):
        enroll_mint.write_diagnostic(diag_path, enroll_mint.diagnostic(minted, probe))
    # setup-token credentials are inference-scoped and normally receive 403
    # from the usage endpoint. A 403 (or authenticated 429) is therefore not
    # grounds to reject the lane token; a 401 remains a hard rejection.
    if probe_status not in {"ok", "http-403", "rate-limited"}:
        detail = f"{probe_status}: {probe.get('error')}" if probe.get("error") else str(probe_status)
        print(f"subfleet enroll: token REJECTED by usage endpoint ({detail}) — "
              "not storing. Is it fresh, and for the right account?", file=sys.stderr)
        return 1
    secret = f"claude-quota-{email}"
    r = sp.run([str(paths.HOME / ".claude" / "manage-secret.sh"), "set", secret, token],
               capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"subfleet enroll: secret store failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    cfg_path = claude.roster_config_path()
    cfg = load_json(cfg_path, {}) or {}
    cfg.setdefault("enrolled", {})[email] = secret
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    if not capacity.clear_lane_cooldown(email):
        print(
            f"subfleet enroll: warning: could not clear prior cooldown for {email}",
            file=sys.stderr,
        )
    if not keepalive.clear_auth_dead(email):
        print(
            f"subfleet enroll: warning: could not clear keepalive auth state for {email}",
            file=sys.stderr,
        )
    extracted = {k: probe.get(k) for k in ("five_hour", "seven_day") if probe.get(k)}
    if probe_status == "ok":
        detail = f"usage probe ok {json.dumps(extracted)}" if extracted else "usage probe ok"
    else:
        detail = f"usage probe {probe_status} (expected for inference-only setup token)"
    print(f"enrolled {email} -> keychain {secret}; {detail}; capacity comes from lane transcripts")
    return 0


def cmd_api_lane_check(args) -> int:
    """Private: rc 7 with a message on stderr when HOME is an OpenAI API-key
    login that lanes must not dispatch to (bin/subfleet-codex, bin/codex);
    rc 0 when the home may run. SUBFLEET_ALLOW_API_LANE=1 overrides."""
    refusal = codex.api_lane_refusal(args.home)
    if refusal:
        print(f"subfleet: {refusal}", file=sys.stderr)
        return codex.API_LANE_REFUSED_RC
    return 0


def cmd_canonical_model(args) -> int:
    """Private: canonical full Claude model id for an alias or retired pin,
    with any [1m]-style suffix preserved (used by bin/subfleet-claude)."""
    print(capacity.normalize_claude_model(args.model) or args.model)
    return 0


def cmd_record_lane_run(args) -> int:
    """Private, best-effort hook used by bin/subfleet-claude after each attempt."""
    error_parts = []
    for value in (args.err_file, args.raw_file):
        if not value:
            continue
        try:
            path = Path(value)
            with path.open("rb") as stream:
                size = path.stat().st_size
                stream.seek(max(0, size - 128 * 1024))
                error_parts.append(stream.read().decode(errors="replace"))
        except OSError:
            pass
    capacity.record_lane_run(
        args.email,
        args.session_id,
        args.rc,
        workdir=args.workdir,
        model=args.model,
        error="\n".join(error_parts),
    )
    return 0


def cmd_record_run(args) -> int:
    """Private, best-effort start/update/finalize hook used by both runners."""
    try:
        if args.phase == "start":
            required = {
                "family": args.family,
                "model": args.model,
                "workdir": args.workdir,
                "prompt": args.prompt,
                "out": args.out,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError("start requires " + ", ".join(f"--{name}" for name in missing))
            caller = None
            raw_caller = (
                args.caller_json if args.caller_json is not None
                else os.environ.get("SUBFLEET_RUN_CALLER_JSON")
            )
            if raw_caller:
                try:
                    parsed = json.loads(raw_caller)
                    caller = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    caller = None
            if caller is None:
                # A runner invoked straight from a Claude session: record the
                # session so its completion notice still finds the way home.
                caller = notify.caller_context(cwd=args.workdir)
            print(
                run_ledger.start_run(
                    family=args.family,
                    model=args.model,
                    lane=args.lane,
                    workdir=args.workdir,
                    prompt=args.prompt,
                    out=args.out,
                    err=args.err,
                    lane_log=args.lane_log,
                    original_out=args.original_out,
                    decision_json=(
                        args.decision_json if args.decision_json is not None
                        else os.environ.get("SUBFLEET_RUN_DECISION_JSON")
                    ),
                    started=args.started,
                    caller=caller,
                    pid=args.pid,
                    launcher=args.launcher,
                    resumed_from=args.resumed_from,
                )
            )
        elif args.phase == "adopt":
            if not args.run_id:
                raise ValueError("adopt requires --run-id")
            print(
                run_ledger.adopt_run(
                    args.run_id,
                    lane=args.lane,
                    pid=args.pid,
                    out=args.out,
                    err=args.err,
                    lane_log=args.lane_log,
                    resumed_from=args.resumed_from,
                )
            )
        elif args.phase == "update":
            if not args.run_id:
                raise ValueError("update requires --run-id")
            run_ledger.update_run(
                args.run_id, lane=args.lane, session_id=args.session_id, pid=args.pid
            )
        else:
            if not args.run_id or args.rc is None:
                raise ValueError("finish requires --run-id and --rc")
            run_ledger.finish_run(
                args.run_id,
                rc=args.rc,
                lane=args.lane,
                session_id=args.session_id,
                transcript_path=args.transcript_path,
                finished=args.finished,
            )
            _notify_finished(args.run_id)
    except (OSError, TypeError, ValueError) as exc:
        print(f"subfleet _record-run: {exc}", file=sys.stderr)
        return 1
    return 0


def _notify_finished(run_id: str) -> None:
    """Tell the dispatching session. Best-effort: never affects the runner.

    A pushed notice is not confirmed by the push (notify.py: the 2026-09-06
    21:40 push); the detached follow-up worker checks the transcript after a
    grace and re-pushes or revives (tickle.notice_followup)."""
    try:
        run_dir, meta = run_ledger.load_run(run_id)
        info = notify.on_finish(run_id, run_dir, meta)
        if info is not None:
            run_ledger.set_notify(
                run_id,
                {
                    "pushed": info.get("pushed"),
                    "surfaced": info.get("surfaced"),
                    "surfaced_by": info.get("surfaced_by"),
                    "at": info.get("ts"),
                    "push": info.get("push"),
                },
            )
            if not info.get("surfaced") and isinstance(info.get("session_id"), str):
                tickle.spawn_followup(info["session_id"])
    except Exception as exc:  # noqa: BLE001 - accounting must never fail a run
        print(f"subfleet _record-run: notify skipped: {exc}", file=sys.stderr)


def _this_session_id() -> str | None:
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None


def _session_names() -> dict[str, str]:
    return {
        row["session_id"]: row["name"]
        for row in notify.live_sessions()
        if isinstance(row.get("name"), str) and row.get("name")
    }


def cmd_runs(args) -> int:
    if args.runs_command == "show":
        try:
            run_dir, meta = run_ledger.load_run(args.id)
        except (OSError, ValueError) as exc:
            print(f"subfleet runs show: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(strip_private(meta), indent=1))
        artifacts = [("out.md", run_dir / "out.md")]
        if args.err:
            artifacts.append(("err.log", run_dir / "err.log"))
        for label, path in artifacts:
            print(f"\n--- {label} ---")
            try:
                text = path.read_text(errors="replace")
            except OSError:
                text = ""
            sys.stdout.write(text)
            if text and not text.endswith("\n"):
                print()
        return 0
    if args.runs_command == "reap":
        reaped = run_ledger.reap_orphans(dry_run=args.dry_run, grace_s=args.grace)
        verb = "would finalize" if args.dry_run else "finalized"
        print(f"subfleet runs reap: {verb} {len(reaped)} orphaned run(s)"
              + (": " + ", ".join(reaped) if reaped else ""))
        return 0
    if args.last < 0:
        print("subfleet runs: --last must be non-negative", file=sys.stderr)
        return 2
    session_id = None
    if args.mine:
        session_id = _this_session_id()
        if session_id is None:
            print("subfleet runs: --mine needs CLAUDE_CODE_SESSION_ID (run it from a Claude session)",
                  file=sys.stderr)
            return 2
    rows = run_ledger.list_runs(args.last, session_id=session_id, running_only=args.running)
    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        print(run_ledger.format_runs(rows, session_names=_session_names()))
    return 0


def _wait_summary(run_id: str, meta: dict) -> str:
    return run_ledger.summary_line(run_id, meta)


def cmd_wait(args) -> int:
    ids = list(args.ids)
    if args.mine:
        session_id = _this_session_id()
        if session_id is None:
            print("subfleet wait: --mine needs CLAUDE_CODE_SESSION_ID", file=sys.stderr)
            return 2
        ids += [row["id"] for row in run_ledger.list_runs(200, session_id=session_id, running_only=True)]
    if args.last:
        latest = run_ledger.latest_run_id(session_id=_this_session_id() if args.mine else None)
        if latest:
            ids.append(latest)
    ids = list(dict.fromkeys(ids))
    if not ids:
        print("subfleet wait: nothing to wait for", file=sys.stderr)
        return 0 if args.mine else 2
    print(f"subfleet wait: waiting for {len(ids)} run(s): {' '.join(ids)}", file=sys.stderr)
    done = run_ledger.wait_for_runs(ids, timeout=args.timeout, interval=args.interval)
    worst = 0
    for run_id in ids:
        meta = done.get(run_id)
        if meta is None:
            print(f"subfleet wait: {run_id} still RUNNING after {args.timeout:.0f}s (timeout)")
            worst = max(worst, 124)
            continue
        print(_wait_summary(run_id, meta))
        if meta.get("missing"):
            worst = max(worst, 2)
        elif meta.get("orphaned") and meta.get("finished_at") is None:
            worst = max(worst, 125)
        else:
            rc = meta.get("rc")
            worst = max(worst, int(rc) if isinstance(rc, int) and rc > 0 else 0)
            if args.cat and not meta.get("orphaned"):
                out_path = run_ledger.output_path(meta)
                try:
                    text = Path(out_path).read_text(errors="replace") if out_path else ""
                except OSError:
                    text = ""
                print(f"--- {run_id} out ---")
                sys.stdout.write(text)
                if text and not text.endswith("\n"):
                    print()
    return worst


def cmd_kill(args) -> int:
    worst = 0
    for run_id in args.ids:
        try:
            result = run_ledger.kill_run(run_id, escalate_after_s=getattr(args, "grace", None))
        except (OSError, ValueError) as exc:
            print(f"subfleet kill: {run_id}: {exc}", file=sys.stderr)
            worst = 1
            continue
        print(f"subfleet kill: {run_id}: {result.get('status')}"
              + (f" pid={result['pid']}" if result.get("pid") else ""))
        if result.get("status") not in {"signalled", "killed", "escalated", "already-finished"}:
            worst = 1
    return worst


def cmd_sessions(args) -> int:
    rows = notify.live_sessions(include_lanes=bool(getattr(args, "all", False)))
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("no live Claude Code sessions registered")
        return 0
    mine = _this_session_id()
    print(f"{'name':<24} {'session':<36} {'pid':>6} {'inbox':<6} cwd")
    for row in rows:
        marker = " (this)" if row["session_id"] == mine else ""
        if row.get("lane"):
            marker += " [lane: headless run — do not notify]"
        print(f"{str(row.get('name') or '-'):<24.24} {row['session_id']:<36} {row['pid']:>6} "
              f"{'yes' if row.get('socket_present') else 'no':<6} {row.get('cwd') or '-'}{marker}")
    return 0


def cmd_notify(args) -> int:
    session_id = args.session or _this_session_id()
    if not session_id:
        print("subfleet notify: --session ID required outside a Claude session", file=sys.stderr)
        return 2
    text = args.text if args.text is not None else sys.stdin.read()
    result = notify.push_to_session(session_id, text, mode_class=args.mode, force=bool(getattr(args, "force", False)))
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        target = result.get("name") or session_id
        if result.get("delivered"):
            print(f"subfleet notify: delivered to {target} (pid {result.get('pid')}, mode={result.get('mode_class')})")
        else:
            print(f"subfleet notify: NOT delivered to {target}: {result.get('reason')}")
    return 0 if result.get("delivered") else 1


def cmd_hooks(args) -> int:
    if args.hooks_command == "install":
        report = hooks.install(dry_run=args.dry_run)
    elif args.hooks_command == "uninstall":
        report = hooks.uninstall(dry_run=args.dry_run)
    else:
        report = hooks.status()
    print(hooks.format_report(report))
    return 0 if report.get("ok", True) else 1


def cmd_session_hook(args) -> int:
    """Backend for bin/subfleet-hook session-start|user-prompt (stdin = hook JSON)."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return 0
    event = {
        "session-start": "SessionStart",
        "user-prompt": "UserPromptSubmit",
    }.get(args.event, payload.get("hook_event_name") or "UserPromptSubmit")
    if args.event == "session-start":
        # A restarted session whose last turn was cut off gets a resume nudge
        # a few seconds from now, once its inbox is listening (tickle.py).
        verdict = tickle.decide(
            session_id, payload.get("transcript_path"), source=payload.get("source"),
        )
        reason = verdict.get("reason") or ""
        # The app writes this restart's stub ~0.7s AFTER the hook runs, so a
        # dedupe/cooldown verdict computed now can be keyed to the PREVIOUS
        # restart (observed 2026-08-24 morning: "already nudged" blocked a
        # fresh restart's nudge). Those two gates defer to the worker, which
        # re-decides after the delay against the fresh transcript.
        deferred = verdict["state"].get("state") == "interrupted" and (
            "already nudged" in reason or "cooldown" in reason
        )
        tickle.note(session_id, {
            "hook": payload.get("source"), "tickle": verdict["tickle"], "reason": reason,
            "state": verdict["state"].get("state"), "stubs": verdict["state"].get("restart_stubs"),
            **({"deferred_to_worker": True} if deferred else {}),
        })
        if verdict["tickle"] or deferred:
            tickle.spawn(session_id, payload.get("transcript_path"))
    # Parked notices, plus pushed ones the transcript never showed: on a
    # (re)start every unconfirmed push (the inbox died with the previous
    # process), on a prompt only pushes older than the follow-up grace.
    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None
    rows = notify.notices_for_hook(session_id, args.event, transcript or notify.transcript_path(session_id),
                                   source=payload.get("source") if isinstance(payload.get("source"), str) else None)
    if not rows:
        return 0
    context = notify.render_pending(session_id, rows)
    notify.mark_surfaced(session_id, [row["run_id"] for row in rows], how=f"hook:{args.event}")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))
    return 0


def cmd_notice_followup_worker(args) -> int:
    """Detached follow-up spawned when a completion notice is written
    (tickle.spawn_followup): after the grace, confirm / re-push / revive."""
    results = tickle.followup_worker(args.session, delay_s=args.delay, rounds=args.rounds)
    if args.json:
        print(json.dumps(results, indent=1))
    return 0


def _notice_followup_after_pass(*, dry_run: bool) -> None:
    """The backstop for the detached worker (a reboot kills it; a push can
    be dropped hours after the worker exited): every revive pass re-checks
    each unresolved notice."""
    try:
        results = tickle.notice_followup_pass(dry_run=dry_run)
    except Exception as exc:  # the follow-up must never take the revive pass down
        print(f"subfleet notices: follow-up failed: {exc}", file=sys.stderr)
        return
    text = tickle.format_followup(results)
    if text:
        print(text)


def cmd_notices(args) -> int:
    """Completion notices and where each one stands (unresolved by default)."""
    session_ids = [args.session] if args.session else sorted(
        path.stem for path in notify.notices_dir().glob("*.jsonl")
    ) if notify.notices_dir().is_dir() else []
    rows = []
    for session_id in session_ids:
        for row in notify._read_notices(notify.notices_path(session_id)):
            if row.get("surfaced") and not args.all:
                continue
            followup = notify.followup_of(row)
            if row.get("surfaced"):
                how = row.get("surfaced_by")
                if how == "transcript":
                    state = "landed"
                elif how:
                    state = f"surfaced ({how})"
                elif row.get("pushed"):
                    # rows written before 2026-09-07 were marked at push time
                    state = "surfaced (legacy: marked at push time, unverified)"
                else:
                    state = "surfaced (hook)"
            elif followup.get("revive"):
                state = f"revived pid={followup['revive'].get('pid')} lane={followup['revive'].get('lane')}"
            elif followup.get("lost"):
                state = "LOST (hooks render it)"
            elif row.get("pushed"):
                state = f"pushed ×{len(notify.delivery_attempts(row))}, unconfirmed"
            else:
                state = f"parked ({(row.get('push') or {}).get('reason') or 'not delivered'})"
            anchor = notify.delivery_anchor(row)
            rows.append({
                "session_id": row.get("session_id") or session_id, "run_id": row.get("run_id"),
                "rc": row.get("rc"), "state": state, "at": row.get("ts"),
                "anchor": iso(anchor) if anchor else None, "surfaced_at": row.get("surfaced_at"),
                "pushes": len(notify.delivery_attempts(row)), "followup": followup or None,
            })
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("subfleet notices: nothing unresolved" if not args.all else "subfleet notices: none recorded")
        return 0
    print(f"{'session':<10} {'run':<44} {'rc':>4} {'at':<26} state")
    for row in rows:
        print(f"{str(row['session_id'])[:8] + '…':<10} {str(row['run_id']):<44.44} {str(row['rc']):>4} "
              f"{str(row['at']):<26} {row['state']}")
    return 0


def cmd_tickle_worker(args) -> int:
    """Detached nudger spawned by the SessionStart hook, the revive launcher
    (with --await-inbox), or `subfleet tickle`."""
    verdict = tickle.deliver(args.session, args.transcript, delay_s=args.delay, force=args.force,
                             await_inbox_s=float(getattr(args, "await_inbox", 0.0) or 0.0))
    return 0 if verdict.get("delivered") else 1


def cmd_revive(args) -> int:
    """Revive cold interrupted sessions (their processes died) headlessly."""
    results = tickle.auto_revive(
        max_batch=args.max, dry_run=args.dry_run, model=args.model,
        allow_fallback=not args.no_fallback,
    )
    if not results:
        # Nothing cold — but the after-pass checks are not about cold
        # sessions: unresolved completion notices (a live seat that dropped
        # a push) and the liveness relay run on every pass regardless.
        print("subfleet revive: nothing cold to revive")
        _notice_followup_after_pass(dry_run=args.dry_run)
        _liveness_after_pass(dry_run=args.dry_run)
        return 0
    revived = 0
    for row in results:
        sid = (row.get("session_id") or "-")[:8]
        if row.get("stalled_host"):
            verb = "reaped" if row.get("reaped") else ("would reap" if args.dry_run else "could not reap")
            print(f"  {sid}… {verb} stalled tmux host {row.get('tmux_session')} "
                  f"({row['stalled_host']}); lane cache cleared" + (f" — {row['error']}" if row.get("error") else ""))
            continue
        if row.get("revived"):
            revived += 1
            host = row.get("host") or tickle.REVIVE_HOST_PRINT
            where = (f" tmux={row['tmux_session']} (attach: {tickle.tmux_attach_hint(row['session_id'])})"
                     if row.get("tmux_session") else "")
            print(f"  {sid}… revived pid={row['pid']} host={host}{where} lane={row['lane']} "
                  f"model={row.get('model') or '-'} — {row.get('detail','')[:70]}")
        elif row.get("would_revive"):
            models = ",".join(row.get("models") or [])
            suffix = f" models={models}" if models else ""
            print(f"  {sid}… would revive{suffix} — {row.get('detail','')[:70]}")
        else:
            print(f"  {sid}… skip — {row.get('skip')}")
    verb = "would revive" if args.dry_run else "revived"
    print(f"subfleet revive: {verb} {sum(1 for r in results if r.get('revived') or r.get('would_revive'))} session(s)"
          if args.dry_run else f"subfleet revive: revived {revived} session(s)")
    _notice_followup_after_pass(dry_run=args.dry_run)
    _liveness_after_pass(dry_run=args.dry_run)
    return 0


def _liveness_after_pass(*, dry_run: bool) -> None:
    """The session-independent relay to Max, run on the revive cadence (every
    two minutes under launchd) so a dead session with pending work is
    reported within the grace period, not at the watchdog's next half hour."""
    try:
        summary = liveness.run(dry_run=dry_run)
    except Exception as exc:  # the relay must never take the revive pass down
        print(f"subfleet liveness: check failed: {exc}", file=sys.stderr)
        return
    if summary.get("alerts_sent") or summary.get("recovered"):
        print(liveness.format_summary(summary))


def cmd_liveness(args) -> int:
    """Dead sessions waiting on a process; Telegram Max past the grace period."""
    summary = liveness.run(dry_run=args.dry_run, grace_minutes=args.grace)
    if args.json:
        print(json.dumps(summary, indent=1))
    else:
        print(liveness.format_summary(summary))
    return 0


def cmd_muster(args) -> int:
    """Roll-call every recently-active live session back to work."""
    rows = tickle.survey()
    me = _this_session_id()
    called = 0
    print(f"{'name':<24} {'state':<12} {'age':>7} outcome")
    for row in rows:
        age = "-" if row.get("age_s") is None else f"{int(row['age_s'])}s"
        if me is not None and row.get("session_id") == me:
            print(f"{str(row.get('name') or '-'):<24.24} {str(row.get('state')):<12} {age:>7} this session (excluded)")
            continue
        if not row.get("inbox"):
            print(f"{str(row.get('name') or '-'):<24.24} {str(row.get('state')):<12} {age:>7} no inbox (open it in the app)")
            continue
        if args.dry_run:
            verdict = tickle.muster_eligible(row["session_id"], row.get("transcript"))
            age_s = (verdict.get("state") or {}).get("age_s")
            if verdict["muster"] and age_s is not None and age_s < 120:
                outcome = f"skip — last activity {int(age_s)}s ago; a roll call waits 120s of quiet"
            else:
                outcome = ("would call — " if verdict["muster"] else "skip — ") + str(verdict.get("reason"))[:90]
        else:
            verdict = tickle.muster_deliver(row["session_id"], row.get("transcript"))
            outcome = ("called — " if verdict.get("delivered") else "skip — ") + str(verdict.get("reason"))[:90]
            called += int(bool(verdict.get("delivered")))
        print(f"{str(row.get('name') or '-'):<24.24} {str(row.get('state')):<12} {age:>7} {outcome}")
    cold = tickle.cold_sessions()
    if cold:
        print(f"cold — interrupted work but NO process; open these in the app ({len(cold)}):")
        for row in cold:
            age = "-" if row.get("age_s") is None else f"{int(row['age_s'])}s"
            print(f"  {row['session_id'][:8]}…  {age:>7}  {row['detail'][:80]}  [{row['project'][-40:]}]")
    if not args.dry_run:
        print(f"subfleet muster: called {called} session(s)"
              + (f"; {len(cold)} cold session(s) need opening" if cold else ""))
    return 0


def cmd_tickle(args) -> int:
    if args.session:
        transcript = args.transcript or notify.transcript_path(args.session)
        if args.dry_run:
            print(json.dumps(tickle.decide(args.session, transcript, source=None, force=args.force), indent=1))
            return 0
        verdict = tickle.deliver(args.session, transcript, delay_s=0.0, force=args.force)
        target = (verdict.get("push") or {}).get("name") or args.session
        if verdict.get("delivered"):
            print(f"subfleet tickle: nudged {target} — {verdict['reason']}")
            return 0
        print(f"subfleet tickle: not nudged ({target}): {verdict.get('reason')}"
              + (f" / {(verdict.get('push') or {}).get('reason')}" if verdict.get("push") else ""))
        return 1
    rows = tickle.survey()
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("no live Claude Code sessions registered")
        return 0
    nudged = 0
    me = _this_session_id()
    print(f"{'name':<24} {'state':<12} {'age':>7} detail")
    for row in rows:
        age = "-" if row.get("age_s") is None else f"{int(row['age_s'])}s"
        mine = me is not None and row.get("session_id") == me
        print(f"{str(row.get('name') or '-'):<24.24} {str(row.get('state')):<12} {age:>7} {row.get('detail')}"
              + (" — this session (excluded)" if mine else ""))
        if mine:
            # a long tool call writes no turns: the sweeping session can look
            # dead to itself. Never self-nudge.
            continue
        if args.all and not args.dry_run and row.get("state") == "interrupted" and row.get("inbox"):
            # a manual sweep cannot know the session just restarted: insist on
            # quiet (no transcript writes for 2 minutes, none during a 3s wait)
            verdict = tickle.deliver(row["session_id"], row.get("transcript"), delay_s=3.0,
                                     min_idle_s=120.0, force=args.force)
            flag = "nudged" if verdict.get("delivered") else f"skipped: {verdict.get('reason')}"
            print(f"{'':<24} → {flag}")
            nudged += int(bool(verdict.get("delivered")))
    if args.all and not args.dry_run:
        print(f"subfleet tickle: nudged {nudged} session(s)")
    return 0


def _bin(name: str) -> str:
    return str(Path(__file__).resolve().parent.parent / "bin" / name)


def _exec_tool(name: str, rest: list[str]) -> int:
    """Replace this process with a sibling tool script (subfleet-codex, ...)."""
    import os

    path = _bin(name)
    if not os.access(path, os.X_OK):
        print(f"subfleet: tool missing or not executable: {path}", file=sys.stderr)
        return 2
    os.execv(path, [path, *rest])


def cmd_run(args) -> int:
    from . import delegate

    return delegate.main(list(args.rest))


def cmd_resume_codex(args) -> int:
    """Resume a ledgered Codex thread without changing its CODEX_HOME."""
    return resume_codex.run(args.id, prompt=args.prompt, output=args.output)


def cmd_handoff(args) -> int:
    """Continue a Claude transcript through a freshly dispatched target agent."""
    return handoff.run(
        args.session_id,
        last=args.last,
        target=args.target,
        workdir=args.workdir,
    )


def cmd_gate(args) -> int:
    return consensus.run(args)


def cmd_login(args) -> int:
    """Stage a codex lane (re)login: Max clicks, the machinery does the rest."""
    from . import login

    return login.codex_login(args.target, watch=not args.no_watch, open_browser=not args.no_open)


def _reset_target_homes(target: str) -> list[Path]:
    homes = paths.codex_homes()
    if target == "all":
        return homes
    expected_name = f".codex-{target}"
    return [home for home in homes if home.name == expected_name]


def _available_codex_reset_credit(result: dict) -> dict | None:
    return next(iter(reset_policy.available_codex_credits(result)), None)


def _weekly_percent(probe: dict):
    return (probe.get("weekly") or {}).get("used_percent")


def _percent_label(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):g}%"
    return "?%"


def _print_policy_decision(decision: dict) -> None:
    print(
        "policy: "
        f"{decision.get('status')} · trigger={decision.get('trigger_reason')} · "
        f"dispatchable={decision.get('dispatchable')} · "
        f"weekly headroom={decision.get('weekly_headroom_pct')}%"
    )
    for index, candidate in enumerate(decision.get("candidates") or [], 1):
        shadow = " · app-shadowed fallback" if candidate.get("shadowed_by_app") else ""
        credit = (
            f" · credit {candidate['credit_id']}"
            if candidate.get("credit_id") else " · no concrete credit"
        )
        print(
            f"  {index}. {candidate.get('lane')} ({candidate.get('email')}) · "
            f"weekly reset {candidate.get('weekly_reset_at') or '?'} · "
            f"in-flight {candidate.get('in_flight', 0)}{credit}{shadow}"
        )
    redeemed = decision.get("redeemed") or {}
    if redeemed:
        remaining = (
            redeemed.get("remaining")
            if redeemed.get("remaining") is not None else "?"
        )
        print(
            f"redeemed {redeemed['credit_id']} on {redeemed['email']}; "
            f"{remaining} credits remain fleet-wide; "
            f"weekly reset now {redeemed['weekly_reset_at']}"
        )


def cmd_reset_codex(args, opener=None) -> int:
    """Consume gifted Codex resets only after both live safety gates pass."""
    if getattr(args, "policy", False):
        if getattr(args, "target", None):
            print(
                "subfleet reset codex: --policy cannot be combined with a lane target",
                file=sys.stderr,
            )
            return 2

        def policy_probe(auth):
            return codex.probe_wham(auth, opener=opener)

        snap = snapshot.build(live=True, probe_fn=policy_probe)
        ranked = snapshot.rank_for_dispatch(snap["codex"]["homes"])
        decision = reset_policy.run(
            snap["codex"]["homes"],
            dry_run=args.dry_run,
            opener=opener,
            dispatchable_homes={row["home"] for row in ranked},
        )
        _print_policy_decision(decision)
        return 1 if decision.get("status") in {"consume-failed", "no-concrete-credit"} else 0

    if not getattr(args, "target", None):
        print(
            "subfleet reset codex: provide a lane number, `all`, or --policy",
            file=sys.stderr,
        )
        return 2
    homes = _reset_target_homes(args.target)
    if not homes:
        print(
            f"subfleet reset codex: lane {args.target!r} is not in the configured fleet",
            file=sys.stderr,
        )
        return 1

    eligible = 0
    fleet_homes = {str(home) for home in paths.codex_homes()}
    fleet_available: dict[str, int | None] = {}
    for home in homes:
        auth = codex.read_auth(home)
        before = codex.probe_wham(auth, opener=opener)
        available_before = (before.get("reset_credits") or {}).get("available")
        fleet_available[str(home)] = (
            available_before
            if isinstance(available_before, int)
            and not isinstance(available_before, bool)
            else None
        )
        account = before.get("email") or auth.get("email") or auth.get("account_id") or "?"
        lane = str(home).replace(str(Path.home()), "~")
        if before.get("status") != "ok" or before.get("limit_reached") is not True:
            detail = before.get("status") or "unknown"
            print(
                f"subfleet reset codex: skip {lane} ({account}) — not LIMITED "
                f"(usage probe {detail}, limit_reached={before.get('limit_reached')!r})",
                file=sys.stderr,
            )
            continue

        listed = codex.list_reset_credits(auth, opener=opener)
        credit = _available_codex_reset_credit(listed)
        if credit is None:
            detail = (
                f"list endpoint {listed.get('status')}"
                if listed.get("status") != "ok"
                else "no available codex_rate_limits reset credit"
            )
            print(
                f"subfleet reset codex: skip {lane} ({account}) — {detail}",
                file=sys.stderr,
            )
            continue

        eligible += 1
        if args.dry_run:
            print(
                f"[dry-run] {lane} ({account}): would consume reset credit {credit['id']}"
            )
            continue

        consumed = codex.consume_reset_credit(auth, credit["id"], opener=opener)
        succeeded = codex.reset_consume_succeeded(consumed)
        before_weekly = _weekly_percent(before)
        response_code = consumed.get("code") or consumed.get("status") or "unknown"
        windows_reset = consumed.get("windows_reset")
        audit_run_id = None
        audit_error = None
        if succeeded:
            prior_available = fleet_available.get(str(home))
            if prior_available is not None:
                fleet_available[str(home)] = max(0, prior_available - 1)
            counts_complete = (
                fleet_homes == set(fleet_available)
                and all(value is not None for value in fleet_available.values())
            )
            remaining = (
                sum(fleet_available.values())
                if counts_complete else None
            )
            try:
                audit_run_id = reset_policy.record_redemption(
                    lane=str(home),
                    email=str(account),
                    credit_id=credit["id"],
                    remaining=remaining,
                    metadata={
                        "account": account,
                        "account_id": auth.get("account_id"),
                        "credit_id": credit["id"],
                        "redeem_request_id": consumed.get("redeem_request_id"),
                        "automatic": False,
                        "before": {
                            "probe_status": before.get("status"),
                            "weekly_used_percent": before_weekly,
                        },
                        "response": {
                            "status": consumed.get("status"),
                            "code": consumed.get("code"),
                            "windows_reset": windows_reset,
                            "error": consumed.get("error"),
                        },
                    },
                )
            except (OSError, TypeError, ValueError) as exc:
                audit_error = str(exc)

        # Keep the irreversible consume durable before this post-action probe.
        after = codex.probe_wham(auth, opener=opener)
        after_weekly = _weekly_percent(after)
        if audit_run_id:
            try:
                run_ledger.update_event_metadata(audit_run_id, {
                    "after": {
                        "probe_status": after.get("status"),
                        "weekly_used_percent": after_weekly,
                    }
                })
            except (OSError, TypeError, ValueError) as exc:
                audit_error = str(exc)
        print(
            f"{lane} ({account}): weekly {_percent_label(before_weekly)} -> "
            f"{_percent_label(after_weekly)} · code {response_code} · "
            f"windows_reset {windows_reset if windows_reset is not None else '?'}"
        )
        if not succeeded:
            print(
                f"subfleet reset codex: stopping after non-success code {response_code}",
                file=sys.stderr,
            )
            return 1
        if audit_error:
            print(
                f"subfleet reset codex: reset occurred but ledger recording failed: {audit_error}",
                file=sys.stderr,
            )
            return 1

    if eligible == 0:
        print("subfleet reset codex: no limited lane holds an available reset credit", file=sys.stderr)
        return 1
    return 0


def cmd_record_codex_cooldown(args) -> int:
    """Internal shell-runner hook for the rollout scan's propagation gap."""
    until = now_local() + timedelta(minutes=args.minutes)
    capacity.store_lane_cooldown(str(Path(args.home).expanduser()), until)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="subfleet", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="per-account quota + auth table (the default)")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--cached", action="store_true", help="use last watchdog snapshot (no network)")

    p_capacity = sub.add_parser("capacity", help="cached 5h + weekly capacity across both families")
    p_capacity.add_argument("--json", action="store_true")

    p_reserve = sub.add_parser("reserve", help="Fable reserve per Claude account: shared vs Fable weekly, slack, verdict")
    p_reserve.add_argument("--json", action="store_true")

    p_runs = sub.add_parser("runs", help="durable prompt/output/error ledger, newest first")
    p_runs.add_argument("--last", type=int, default=20, help="maximum runs to list (default 20)")
    p_runs.add_argument("--json", action="store_true")
    p_runs.add_argument("--mine", action="store_true",
                        help="only runs dispatched by this Claude session (CLAUDE_CODE_SESSION_ID)")
    p_runs.add_argument("--running", action="store_true", help="only unfinished runs")
    runs_sub = p_runs.add_subparsers(dest="runs_command")
    p_runs_show = runs_sub.add_parser("show", help="print one run's metadata and saved output")
    p_runs_show.add_argument("id")
    p_runs_show.add_argument("--err", action="store_true", help="also print the saved err.log")
    p_runs_reap = runs_sub.add_parser("reap", help="finalize RUNNING entries whose runner pid is gone")
    p_runs_reap.add_argument("--dry-run", action="store_true")
    p_runs_reap.add_argument("--grace", type=float, default=60.0,
                             help="seconds a run must have existed before a dead pid counts (default 60)")

    p_wait = sub.add_parser("wait", help="block until dispatched runs finish; safe to background and re-run")
    p_wait.add_argument("ids", nargs="*", help="run ids (from `subfleet run` / `subfleet runs`)")
    p_wait.add_argument("--mine", action="store_true", help="every unfinished run this session dispatched")
    p_wait.add_argument("--last", action="store_true", help="the most recent run")
    p_wait.add_argument("--timeout", type=float, default=None, help="seconds before giving up (rc 124)")
    p_wait.add_argument("--interval", type=float, default=2.0, help="poll interval seconds (default 2)")
    p_wait.add_argument("--cat", action="store_true", help="print each finished run's output")

    p_kill = sub.add_parser("kill", help="SIGTERM a running dispatch; its EXIT trap salvages and finalizes")
    p_kill.add_argument("--grace", type=float, default=10.0, metavar="SECONDS",
                        help="wait this long after SIGTERM, then SIGKILL the run's process tree and report killed/escalated/survived (0 = signal only)")
    p_kill.add_argument("ids", nargs="+")

    p_resume_codex = sub.add_parser(
        "resume-codex",
        help="continue a ledgered Codex thread on its original CODEX_HOME",
    )
    p_resume_codex.add_argument("id", help="source run id (see `subfleet runs`)")
    p_resume_codex.add_argument(
        "prompt",
        nargs="?",
        help="continuation prompt (default: reinspect the worktree and continue)",
    )
    p_resume_codex.add_argument("-o", "--output", help="also write the final response here")

    p_handoff = sub.add_parser(
        "handoff",
        help="continue a Claude Code session through a freshly dispatched agent",
    )
    p_handoff.add_argument("session_id", nargs="?", help="exact Claude Code session UUID")
    p_handoff.add_argument(
        "--last",
        action="store_true",
        help="use this Claude session, or the newest durable transcript outside one",
    )
    p_handoff.add_argument("--to", dest="target", required=True, choices=("sol", "terra", "astra", "luna", "opus"))
    p_handoff.add_argument("-C", dest="workdir", help="target worktree (default: transcript cwd)")

    p_gate = sub.add_parser(
        "gate",
        help="durable main/peer agreement gate for a PR or plan",
    )
    gate_sub = p_gate.add_subparsers(dest="gate_command")
    for kind, target_help in (
        ("pr", "PR number, URL, or branch"),
        ("plan", "plan file to review"),
    ):
        p_gate_kind = gate_sub.add_parser(kind, help=f"review an exact {kind} revision")
        p_gate_kind.add_argument("target", help=target_help)
        p_gate_kind.add_argument("--peer", required=True, choices=("fable", "sol", "astra"))
        p_gate_kind.add_argument("--peer-account", metavar="EMAIL",
                                 help="pin this review to an enrolled Claude account")
        p_gate_kind.add_argument("--exclude-account", action="append", default=[], metavar="EMAIL",
                                 help="exclude a Claude account from this review (repeatable)")
        p_gate_kind.add_argument(
            "--main-approve",
            action="store_true",
            help="explicitly attest that the invoking main agent approves this revision",
        )
        p_gate_kind.add_argument("--brief", help="bounded UTF-8 review brief")
        p_gate_kind.add_argument("-C", dest="workdir", help="review worktree (default: cwd)")
        p_gate_kind.add_argument(
            "--max-rounds", type=int, default=0,
            help="maximum peer review rounds (default: 0, unlimited)",
        )
        p_gate_kind.add_argument("--json", action="store_true")
        p_gate_kind.add_argument("--dry-run", action="store_true")
        if kind == "pr":
            p_gate_kind.add_argument("--expect-head", help="full PR head OID approved by the main agent")
            p_gate_kind.add_argument("--expect-base", help="full PR base OID approved by the main agent")
            p_gate_kind.add_argument(
                "--on-agreement", choices=("proceed", "merge"), default="proceed"
            )
            p_gate_kind.add_argument(
                "--merge-method", choices=("merge", "squash"), default="merge"
            )
        else:
            p_gate_kind.add_argument("--expect-sha256", help="full plan SHA-256 approved by the main agent")
            p_gate_kind.add_argument(
                "--on-agreement", choices=("proceed",), default="proceed"
            )
            p_gate_kind.set_defaults(merge_method=None)
    p_gate_continue = gate_sub.add_parser("continue", help="run the next peer round or reconcile an action")
    p_gate_continue.add_argument("gate_id")
    p_gate_continue.add_argument("--main-approve", action="store_true")
    p_gate_continue.add_argument("--expect-head", help="full PR head OID approved by the main agent")
    p_gate_continue.add_argument("--expect-base", help="full PR base OID approved by the main agent")
    p_gate_continue.add_argument("--expect-sha256", help="full plan SHA-256 approved by the main agent")
    p_gate_continue.add_argument("--response", help="main agent's bounded UTF-8 response/evidence")
    p_gate_continue.add_argument("--peer-account", metavar="EMAIL",
                                 help="pin this review to an enrolled Claude account")
    p_gate_continue.add_argument("--exclude-account", action="append", default=[], metavar="EMAIL",
                                 help="exclude a Claude account from this review (repeatable)")
    p_gate_continue.add_argument(
        "--max-rounds", type=int,
        help="change the limit for the next review; 0 removes it (omitted: keep current limit)",
    )
    p_gate_continue.add_argument("--json", action="store_true")
    p_gate_continue.add_argument("--dry-run", action="store_true")

    p_sessions = sub.add_parser("sessions", help="live Claude Code sessions with an inbox (notification targets)")
    p_sessions.add_argument("--all", action="store_true", help="include lane sessions (headless runs), hidden by default")
    p_sessions.add_argument("--json", action="store_true")

    p_notify = sub.add_parser("notify", help="push a message into a Claude session inbox (default: this session)")
    p_notify.add_argument("--force", action="store_true", help="deliver even to a lane session (overwrites its captured deliverable)")
    p_notify.add_argument("text", nargs="?", help="message text (stdin when omitted)")
    p_notify.add_argument("--session", help="target session id (see `subfleet sessions`)")
    p_notify.add_argument("--mode", choices=["bypass", "prompting", "none"], default=None,
                          help="permission class to declare (default: the recipient's own)")
    p_notify.add_argument("--json", action="store_true")

    p_hooks = sub.add_parser("hooks", help="Claude Code hooks: completion catch-up + attached-runner guard")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_command")
    for name, help_ in (("install", "add subfleet hooks to ~/.claude/settings.json"),
                        ("uninstall", "remove subfleet hooks from ~/.claude/settings.json")):
        p_h = hooks_sub.add_parser(name, help=help_)
        p_h.add_argument("--dry-run", action="store_true")
    hooks_sub.add_parser("status", help="show which subfleet hooks are installed")

    p_session_hook = sub.add_parser("_session-hook", help=argparse.SUPPRESS)
    p_session_hook.add_argument("event", choices=["session-start", "user-prompt"])

    p_tickle_worker = sub.add_parser("_tickle", help=argparse.SUPPRESS)
    p_tickle_worker.add_argument("--session", required=True)
    p_tickle_worker.add_argument("--transcript")
    p_tickle_worker.add_argument("--delay", type=float, default=0.0)
    p_tickle_worker.add_argument("--force", action="store_true")
    p_tickle_worker.add_argument("--await-inbox", dest="await_inbox", type=float, default=0.0,
                                 help="wait up to S seconds for the session's inbox before nudging")

    p_followup = sub.add_parser("_notice-followup", help=argparse.SUPPRESS)
    p_followup.add_argument("--session", required=True)
    p_followup.add_argument("--delay", type=float, default=None,
                            help="grace before each check (default SUBFLEET_NOTICE_FOLLOWUP_S or 300)")
    p_followup.add_argument("--rounds", type=int, default=tickle.NOTICE_WORKER_ROUNDS)
    p_followup.add_argument("--json", action="store_true")

    p_notices = sub.add_parser("notices", help="completion notices and where each stands (unresolved by default)")
    p_notices.add_argument("--session", help="one session id (see `subfleet sessions`)")
    p_notices.add_argument("--all", action="store_true", help="include surfaced notices")
    p_notices.add_argument("--json", action="store_true")

    p_liveness = sub.add_parser(
        "liveness",
        help="dead sessions with pending work (no live process); Telegram Max after the grace period",
    )
    p_liveness.add_argument("--dry-run", action="store_true", help="print the alert instead of sending")
    p_liveness.add_argument("--grace", type=float, default=None, metavar="MIN",
                            help="minutes a session must stay dead before alerting (default SUBFLEET_LIVENESS_GRACE_MIN or 10)")
    p_liveness.add_argument("--json", action="store_true")

    p_muster = sub.add_parser("muster", help="roll-call every recently-active session: resume the interrupted, ask the idle to continue standing work")
    p_muster.add_argument("--dry-run", action="store_true")

    p_revive = sub.add_parser(
        "revive",
        help="headlessly resume cold interrupted sessions whose process died; model/lane-probed",
        description=(
            "Headlessly resume cold interrupted sessions once per stuck point. "
            "Each session resumes on its own recorded model (its desktop store "
            "entry's `model`; unrecorded sessions use the first configured "
            "model) and parks when no lane serves that tier — cross-tier "
            "fallback happens only with an explicit --model, which tries M "
            "then the SUBFLEET_REVIVE_MODELS chain "
            "(default claude-fable-5-1,claude-opus-5)."
        ),
    )
    p_revive.add_argument(
        "--model", metavar="M",
        help="override every session's tier: try M first, then configured models unless --no-fallback",
    )
    p_revive.add_argument(
        "--no-fallback", action="store_true",
        help="probe only --model M, or only the first configured model",
    )
    p_revive.add_argument("--dry-run", action="store_true", help="show candidates; launch nothing")
    p_revive.add_argument(
        "--max", type=int, default=8,
        help="max concurrent detached revives (default 8)",
    )

    p_tickle = sub.add_parser("tickle", help="resume nudge for sessions whose last turn was cut off by a restart")
    p_tickle.add_argument("--session", help="one session id (see `subfleet sessions`)")
    p_tickle.add_argument("--transcript", help="transcript path override (with --session)")
    p_tickle.add_argument("--all", action="store_true", help="nudge every live interrupted session")
    p_tickle.add_argument("--dry-run", action="store_true", help="show states, send nothing")
    p_tickle.add_argument("--force", action="store_true", help="ignore the age cap, dedupe, and cooldown")
    p_tickle.add_argument("--json", action="store_true")

    p_pick = sub.add_parser("pick", help="best lane for dispatch: `pick codex` (CODEX_HOME) or `pick claude` (email)")
    p_pick.add_argument("family", nargs="?", choices=["codex", "claude"], default="codex")
    p_pick.add_argument(
        "--model", metavar="MODEL",
        help="Claude-only model scope (fable/opus/sonnet/haiku or a full model id)",
    )
    p_pick.add_argument("--json", action="store_true")
    p_pick.add_argument("--all", action="store_true", help="show full ranking")
    p_pick.add_argument("--cached", action="store_true",
                        help="codex: use the last watchdog snapshot; claude: no-op (capacity is cached)")
    p_pick.add_argument("--min-headroom", type=float, default=None,
                        help="minimum headroom %% to qualify (default 5; claude gates EVERY window)")
    p_pick.add_argument("--handicap", type=float, default=10.0,
                        help="Claude-only score penalty for the active app account; ignored for Codex")
    p_pick.add_argument("--no-handicap", action="store_true",
                        help="Claude-only: disable active-app protection; ignored for Codex")
    p_pick.add_argument("--exclude", action="append", default=[], help=argparse.SUPPRESS)

    p_run = sub.add_parser("run", help="dispatch front door: route a prompt to the best lane of the right family",
                           add_help=False)
    p_run.add_argument("rest", nargs=argparse.REMAINDER)

    for name, tool, help_ in (
        ("codex", "subfleet-codex", "hardened `codex exec` on an auto-assigned (or -H pinned) codex lane"),
        ("claude", "subfleet-claude", "hardened headless `claude -p` on an auto-picked (-A) or -a pinned Claude lane"),
        ("mirror", "subfleet-mirror", "desktop session mirror across accounts (launchd runs it every 60s)"),
    ):
        p_tool = sub.add_parser(name, help=help_, add_help=False)
        p_tool.add_argument("rest", nargs=argparse.REMAINDER)

    p_login = sub.add_parser("login", help="stage a codex lane (re)login: `login codex 3` or `login codex app`")
    p_login.add_argument("family", choices=["codex"])
    p_login.add_argument("target", help="lane number (1-9) or `app` for the desktop app home ~/.codex")
    p_login.add_argument("--no-watch", action="store_true", help="don't arm the completion watcher")
    p_login.add_argument("--no-open", action="store_true", help="print the OAuth URL instead of opening Chrome")

    p_reset = sub.add_parser(
        "reset", help="consume a gifted reset credit after live limit + entitlement gates"
    )
    reset_sub = p_reset.add_subparsers(dest="reset_family", required=True)
    p_reset_codex = reset_sub.add_parser("codex", help="reset a LIMITED Codex lane")
    p_reset_codex.add_argument(
        "target", nargs="?", choices=[*(str(i) for i in range(1, 10)), "all"]
    )
    p_reset_codex.add_argument(
        "--policy", action="store_true", help="evaluate the automatic one-at-a-time policy"
    )
    p_reset_codex.add_argument(
        "--dry-run", action="store_true", help="list eligible credits without consuming"
    )

    p_errors = sub.add_parser("errors", help="observed limit/auth errors")
    p_errors.add_argument("--hours", type=float, default=24)
    p_errors.add_argument("--json", action="store_true")

    p_watch = sub.add_parser("watch", help="watchdog cycle: snapshot + alerts (launchd)")
    p_watch.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")

    p_keepalive = sub.add_parser(
        "keepalive", help="keep enrolled Claude five-hour windows rolling"
    )
    p_keepalive.add_argument("--dry-run", action="store_true")
    p_keepalive.add_argument("--family", choices=["claude"], default="claude")

    sub.add_parser("brief", help="morning-brief markdown section")

    p_enroll = sub.add_parser("enroll", help="store a Claude setup-token for pinned lane dispatch")
    p_enroll.add_argument("email", help="account email (must be in claude-accounts.json roster)")
    p_enroll.add_argument("--mint", action="store_true",
                          help="run `claude setup-token` in a subfleet-owned pty and capture the "
                               "token: you click Approve in the browser, the CLI receives the code "
                               "on its own local callback, subfleet stores the token; nothing is "
                               "shown or pasted")
    p_enroll.add_argument("--paste", action="store_true",
                          help="with --mint: if the browser callback cannot reach the CLI, paste "
                               "the code from the hosted page at a prompt instead")

    p_canonical = sub.add_parser("_canonical-model", help=argparse.SUPPRESS)
    p_canonical.add_argument("model")
    p_api_lane = sub.add_parser("_api-lane-check", help=argparse.SUPPRESS)
    p_api_lane.add_argument("home")
    p_record = sub.add_parser("_record-lane-run", help=argparse.SUPPRESS)
    p_record.add_argument("--email", required=True)
    p_record.add_argument("--model")
    p_record.add_argument("--session-id", required=True)
    p_record.add_argument("--rc", required=True, type=int)
    p_record.add_argument("--workdir")
    p_record.add_argument("--err-file")
    p_record.add_argument("--raw-file")

    p_run_record = sub.add_parser("_record-run", help=argparse.SUPPRESS)
    p_run_record.add_argument("--phase", choices=("start", "adopt", "update", "finish"), required=True)
    p_run_record.add_argument("--pid", type=int)
    p_run_record.add_argument("--caller-json")
    p_run_record.add_argument("--launcher")
    p_run_record.add_argument("--run-id")
    p_run_record.add_argument("--family", choices=("codex", "claude"))
    p_run_record.add_argument("--model")
    p_run_record.add_argument("--lane")
    p_run_record.add_argument("--workdir")
    p_run_record.add_argument("--prompt")
    p_run_record.add_argument("--out")
    p_run_record.add_argument("--err")
    p_run_record.add_argument("--lane-log")
    p_run_record.add_argument("--original-out")
    p_run_record.add_argument("--rc", type=int)
    p_run_record.add_argument("--started")
    p_run_record.add_argument("--finished")
    p_run_record.add_argument("--session-id")
    p_run_record.add_argument("--transcript-path")
    p_run_record.add_argument("--decision-json")
    p_run_record.add_argument("--resumed-from")

    p_codex_cooldown = sub.add_parser("_record-codex-cooldown", help=argparse.SUPPRESS)
    p_codex_cooldown.add_argument("--home", required=True)
    p_codex_cooldown.add_argument("--minutes", type=float, default=15.0)

    argv = list(sys.argv[1:] if argv is None else argv)
    known = {
        "status", "capacity", "reserve", "runs", "pick", "run", "codex", "claude", "mirror", "login",
        "errors", "watch", "keepalive", "brief", "enroll", "reset", "_record-lane-run", "_record-run",
        "_canonical-model", "_api-lane-check",
        "_record-codex-cooldown", "wait", "kill", "sessions", "notify", "notices", "hooks", "_session-hook",
        "_tickle", "_notice-followup", "tickle", "muster", "revive", "resume-codex", "handoff", "gate",
        "liveness",
    }
    if not argv or (argv[0] not in known and argv[0] not in ("-h", "--help")):
        argv = ["status", *argv]
    # Pass-through subcommands own their argv entirely (their own -h, flags).
    if argv[0] in ("run", "codex", "claude", "mirror"):
        rest = argv[1:]
        if argv[0] == "run":
            from . import delegate

            return delegate.main(rest)
        return _exec_tool({"codex": "subfleet-codex", "claude": "subfleet-claude",
                           "mirror": "subfleet-mirror"}[argv[0]], rest)
    args = parser.parse_args(argv)
    if args.command == "pick":
        if args.family == "claude":
            if args.min_headroom is None:
                args.min_headroom = claude.DEFAULT_MIN_HEADROOM
            return cmd_claude_pick(args)
        if args.min_headroom is None:
            args.min_headroom = snapshot.DEFAULT_MIN_HEADROOM
        return cmd_pick(args)
    handlers = {
        "status": cmd_status,
        "capacity": cmd_capacity,
        "reserve": cmd_reserve,
        "runs": cmd_runs,
        "login": cmd_login,
        "reset": cmd_reset_codex,
        "errors": cmd_errors,
        "watch": cmd_watch,
        "keepalive": cmd_keepalive,
        "brief": cmd_brief,
        "enroll": cmd_enroll,
        "_canonical-model": cmd_canonical_model,
        "_api-lane-check": cmd_api_lane_check,
        "_record-lane-run": cmd_record_lane_run,
        "_record-run": cmd_record_run,
        "_record-codex-cooldown": cmd_record_codex_cooldown,
        "wait": cmd_wait,
        "kill": cmd_kill,
        "resume-codex": cmd_resume_codex,
        "handoff": cmd_handoff,
        "gate": cmd_gate,
        "sessions": cmd_sessions,
        "notify": cmd_notify,
        "notices": cmd_notices,
        "hooks": cmd_hooks,
        "_session-hook": cmd_session_hook,
        "_tickle": cmd_tickle_worker,
        "_notice-followup": cmd_notice_followup_worker,
        "tickle": cmd_tickle,
        "muster": cmd_muster,
        "revive": cmd_revive,
        "liveness": cmd_liveness,
    }
    if args.command == "hooks" and not args.hooks_command:
        args.hooks_command = "status"
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
