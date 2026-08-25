"""subfleet CLI — multi-account AI capacity and dispatch, one front door.

  subfleet                       # human table (live probes)
  subfleet status --json|--cached
  subfleet capacity [--json]
  subfleet pick codex            # best dispatchable CODEX_HOME
  subfleet pick claude           # best enrolled Claude lane
  subfleet run ...                # capacity-aware delegate router
  subfleet codex ...              # hardened Codex runner
  subfleet claude ...             # hardened Claude runner
  subfleet runs [--last N] [--mine] [--running]   # durable run ledger, newest first
  subfleet runs show <id> [--err] # one run's metadata + saved output
  subfleet runs reap [--dry-run]  # finalize RUNNING entries whose runner died
  subfleet wait <id>... | --mine | --last [--timeout S] [--cat]   # block until runs finish
  subfleet kill <id>...           # SIGTERM a running dispatch (its trap salvages + finalizes)
  subfleet sessions               # live Claude Code sessions with an inbox
  subfleet notify [--session ID] TEXT       # push a message into a session inbox
  subfleet hooks install|uninstall|status   # Claude Code hooks: completion catch-up + guard
  subfleet tickle [--all|--session ID]      # resume nudge for sessions cut off by a restart
  subfleet muster [--dry-run]     # roll-call every recently-active session
  subfleet login codex <N|app>    # stage an interactive Codex re-login
  subfleet enroll <email>
  subfleet mirror [--quiet]       # desktop session mirror pass
  subfleet errors --hours 48
  subfleet watch [--dry-run]
  subfleet brief                  # compact Markdown status section
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import (
    capacity,
    claude,
    codex,
    config,
    hooks,
    inbox,
    paths,
    render,
    run_ledger,
    secret_store,
    snapshot,
    tickle,
    watchdog,
)
from .util import (
    from_epoch,
    iso,
    load_json,
    now_local,
    parse_iso,
    parse_reset_clock,
    strip_private,
)


def _load_snapshot(cached: bool, live_timeout: float = 15.0) -> dict:
    if cached:
        snap = load_json(paths.snapshot_path())
        if snap:
            return snap
        print("subfleet: no cached snapshot yet; probing live", file=sys.stderr)
    # Small error window interactively; the watchdog covers the long window.
    return snapshot.build(live=True, timeout=live_timeout, errors_hours=6)


def cmd_status(args) -> int:
    snap = _load_snapshot(args.cached)
    if args.json:
        print(json.dumps(snap, indent=1))
    else:
        print(render.table(snap))
        if args.cached:
            print(f"\n(cached snapshot from {snap.get('generated_at')}; use without --cached for live)")
    return 0


def _capacity_cell(reading: dict | None) -> str:
    if not isinstance(reading, dict):
        return "-"
    used = reading.get("used_percent", reading.get("used"))
    if reading.get("unit") == "percent" and used is not None:
        return f"{float(used):.0f}%"
    tokens = reading.get("tokens", reading.get("used"))
    if tokens is None:
        return "-"
    capacity_tokens = reading.get("capacity")
    if capacity_tokens:
        pct = reading.get("used_percent")
        suffix = f" ({float(pct):.0f}%)" if pct is not None else ""
        return f"{int(tokens):,}/{int(capacity_tokens):,} tok{suffix}"
    return f"{int(tokens):,} tok"


def _learned_capacity_cell(value: dict | None) -> str:
    if not isinstance(value, dict) or not any(v is not None for v in value.values()):
        return "-"
    five = value.get("five_hour")
    week = value.get("weekly")
    return "5h " + (f"{int(five):,}" if five is not None else "-") + \
        " / 7d " + (f"{int(week):,}" if week is not None else "-")


def cmd_capacity(args) -> int:
    report = capacity.build()
    if args.json:
        print(json.dumps(strip_private(report), indent=1))
        return 0

    columns = ["family", "account", "five_hour", "weekly", "learned_capacity",
               "limited_until", "confidence"]
    rendered = []
    for row in report.get("accounts") or []:
        rendered.append(
            [
                str(row.get("family") or "-"),
                str(row.get("email") or row.get("id") or row.get("resource") or "-"),
                _capacity_cell(row.get("five_hour")),
                _capacity_cell(row.get("weekly")),
                _learned_capacity_cell(row.get("learned_capacity")),
                str(row.get("limited_until") or "-"),
                str(row.get("confidence") or "-"),
            ]
        )
    widths = [len(name) for name in columns]
    for row in rendered:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]

    def line(row):
        return "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()

    print(line(columns))
    print(line(["-" * width for width in widths]))
    for row in rendered:
        print(line(row))
    return 0


def _reset_from_text(text: str) -> str | None:
    """Extract either an absolute reset value or a human reset clock."""
    now = now_local()
    for match in re.finditer(
        r'(?i)(?:reset_at|resets_at|reset)["\s:=]+["\']?([^"\'\s,}]+)', text
    ):
        raw = match.group(1)
        parsed = parse_iso(raw)
        if parsed is None:
            try:
                parsed = from_epoch(float(raw))
            except ValueError:
                parsed = None
        if parsed is not None:
            return iso(parsed)
    return iso(parse_reset_clock(text, now))


def cmd_lane_usage(args) -> int:
    """Internal, best-effort bridge used by ``subfleet claude``."""
    try:
        if args.action == "record":
            capacity.append_lane_usage(
                args.email,
                Path(args.transcript),
                session_id=args.session_id,
            )
            return 0
        if args.action == "auth-failure":
            capacity.record_auth_failure(args.email)
            return 0
        text = "\n".join(
            Path(path).read_text(errors="replace")
            for path in args.error_file
            if Path(path).is_file()
        )
        event = capacity.record_hard_limit(args.email, reset=_reset_from_text(text))
        if event.get("reset"):
            print(event["reset"])
        return 0
    except Exception as exc:
        # Accounting must never turn a completed agent run into a failure.
        print(f"subfleet lane-usage: accounting skipped: {exc}", file=sys.stderr)
        return 0


def cmd_pick(args) -> int:
    snap = _load_snapshot(args.cached)
    handicap = 0.0 if args.no_handicap else args.handicap
    ranked = snapshot.rank_for_dispatch(
        snap["codex"]["homes"], handicap=handicap, min_headroom=args.min_headroom
    )
    requested_exclusions = {str(home) for home in args.exclude if home}
    if requested_exclusions:
        ranked = [row for row in ranked if str(row["home"]) not in requested_exclusions]
    if args.json:
        out = {
            "generated_at": snap["generated_at"],
            "best": ranked[0]["home"] if ranked else None,
            "ranked": ranked if args.all else ranked[:1],
            "excluded": [
                {
                    "home": e["home"],
                    "verdict": (
                        "excluded" if str(e["home"]) in requested_exclusions else e["verdict"]
                    ),
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


def _capacity_lane_ranking(
    report: dict, *, handicap: float, min_headroom: float
) -> tuple[list[dict], list[dict]]:
    return capacity.rank_claude_lanes(
        report.get("accounts") or [],
        handicap=handicap,
        min_headroom=min_headroom,
    )


def cmd_claude_pick(args) -> int:
    report = capacity.build()
    handicap = 0.0 if args.no_handicap else args.handicap
    ranked, excluded = _capacity_lane_ranking(
        report, handicap=handicap, min_headroom=args.min_headroom
    )
    requested_exclusions = {str(email) for email in args.exclude if email}
    if requested_exclusions:
        excluded.extend(
            {"email": row["email"], "verdict": "excluded", "reset_at": None}
            for row in ranked
            if str(row["email"]) in requested_exclusions
        )
        ranked = [row for row in ranked if str(row["email"]) not in requested_exclusions]
    enrolled = sum(
        bool(row.get("enrolled"))
        for row in report.get("accounts") or []
        if row.get("family") == "claude"
    )
    resets = [row["reset_at"] for row in excluded if row.get("reset_at")]
    earliest_reset = min(resets) if resets else None
    if args.json:
        out = {
            "generated_at": report.get("generated_at"),
            "best": ranked[0]["email"] if ranked else None,
            "ranked": ranked if args.all else ranked[:1],
            "excluded": excluded,
            "enrolled": enrolled,
            "earliest_reset": earliest_reset,
        }
        print(json.dumps(out, indent=1))
        return 0 if ranked else 1
    if not ranked:
        if enrolled == 0:
            print(
                "subfleet pick claude: no lanes enrolled — enroll with: "
                "claude setup-token, then subfleet enroll <email>",
                file=sys.stderr,
            )
        else:
            blocked = "; ".join(f"{lane['email']} {lane['verdict']}" for lane in excluded)
            print(
                "subfleet pick claude: no dispatchable claude lane"
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

        def reading(label, percent, tokens):
            if percent is not None:
                return f"{label} {percent:.0f}%"
            if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
                return f"{label} {int(tokens)} tok"
            return f"{label} ?"

        return reading("5h", fh, fh_tokens) + " used · " + reading(
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
    homes = [*paths.codex_homes()]
    app_home = paths.app_codex_home()
    if app_home.is_dir() and app_home not in homes:
        homes.append(app_home)
    out = {
        "codex": {
            str(h): codex.recent_limit_errors(h, hours=args.hours)
            for h in homes
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


def cmd_brief(_args) -> int:
    print(render.brief_md(_load_snapshot(cached=True)))
    return 0


def cmd_login(args) -> int:
    from . import login

    return login.codex_login(
        args.target,
        watch=not args.no_watch,
        open_browser=not args.no_open,
    )


def cmd_enroll(args) -> int:
    """Store a Claude setup token for inference dispatch.

    The token is read from stdin, never argv or environment. Enrollment makes
    one validation request; routine status and dispatch use the local ledger.
    """
    import getpass
    email = args.email
    try:
        cfg = config.load(strict=True)
    except config.ConfigError as exc:
        print(f"subfleet enroll: {exc}", file=sys.stderr)
        return 2
    accounts = cfg.get("accounts", [])
    enrolled = cfg.get("enrolled")
    if not isinstance(accounts, list) or (enrolled is not None and not isinstance(enrolled, dict)):
        print(f"subfleet enroll: invalid roster schema in {config.accounts_path()}", file=sys.stderr)
        return 2
    roster = sorted(account for account in accounts if isinstance(account, str) and "@" in account)
    if email not in roster:
        print(f"subfleet enroll: {email} is not in the roster ({len(roster)} accounts); "
              f"add it to {claude.roster_config_path()} first", file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        token = getpass.getpass(f"Paste setup-token for {email} (input hidden): ").strip()
    else:
        token = sys.stdin.read().strip()
    if not token:
        print("subfleet enroll: empty token", file=sys.stderr)
        return 2
    probe = claude.probe_oauth_usage(token)
    probe_status = probe.get("status")
    if probe_status not in {"ok", "http-403", "rate-limited"}:
        print(f"subfleet enroll: token REJECTED by usage endpoint ({probe.get('status')}) — "
              "not storing. Is it fresh, and for the right account?", file=sys.stderr)
        return 1
    secret = config.secret_name_for(email)
    if not secret_store.set(secret, token):
        print("subfleet enroll: secret store failed", file=sys.stderr)
        return 1
    cfg.setdefault("enrolled", {})[email] = secret
    config.save(cfg)
    if not capacity.clear_lane_cooldown(email):
        print(
            f"subfleet enroll: warning: could not clear prior cooldown for {email}",
            file=sys.stderr,
        )
    extracted = {k: probe.get(k) for k in ("five_hour", "seven_day") if probe.get(k)}
    if probe_status == "ok":
        detail = f"usage probe ok {json.dumps(extracted)}" if extracted else "usage probe ok"
    else:
        detail = f"usage probe {probe_status} (expected for an inference-only setup token)"
    print(f"enrolled {email} -> secret store item {secret}; {detail}")
    return 0


def cmd_secret(args) -> int:
    """Internal bridge used by the hardened shell runner."""
    name = config.secret_name_for(args.email, require_enrolled=True)
    if name is None:
        print(
            f"subfleet secret: {args.email} is not enrolled in {config.accounts_path()}",
            file=sys.stderr,
        )
        return 1
    value = secret_store.get(name)
    if value is None:
        print(f"subfleet secret: item unavailable for {args.email}", file=sys.stderr)
        return 1
    print(value)
    return 0


def cmd_codex_binary(_args) -> int:
    """Internal bridge for shell entrypoints that cannot assume a rich PATH."""
    binary = codex._codex_binary()
    if not binary:
        print("subfleet: real Codex binary not found", file=sys.stderr)
        return 1
    print(binary)
    return 0


def cmd_record_run(args) -> int:
    """Best-effort start/adopt/update/finish hook used by the hardened runners."""
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
                raise ValueError(
                    "start requires " + ", ".join(f"--{name}" for name in missing)
                )
            caller = None
            raw_caller = (
                args.caller_json
                if args.caller_json is not None
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
                caller = inbox.caller_context(cwd=args.workdir)
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
                        args.decision_json
                        if args.decision_json is not None
                        else os.environ.get("SUBFLEET_RUN_DECISION_JSON")
                    ),
                    started=args.started,
                    caller=caller,
                    pid=args.pid,
                    launcher=args.launcher,
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
    """Tell the dispatching session. Best-effort: never affects the runner."""
    try:
        run_dir, meta = run_ledger.load_run(run_id)
        info = inbox.on_finish(run_id, run_dir, meta)
        if info is not None:
            run_ledger.set_notify(
                run_id,
                {
                    "pushed": info.get("pushed"),
                    "surfaced": info.get("surfaced"),
                    "at": info.get("ts"),
                    "push": info.get("push"),
                },
            )
    except Exception as exc:  # noqa: BLE001 - accounting must never fail a run
        print(f"subfleet _record-run: notify skipped: {exc}", file=sys.stderr)


def _this_session_id() -> str | None:
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None


def _session_names() -> dict[str, str]:
    return {
        row["session_id"]: row["name"]
        for row in inbox.live_sessions()
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
        for label, artifact in artifacts:
            print(f"\n--- {label} ---")
            try:
                text = artifact.read_text(errors="replace")
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
            print("subfleet runs: --mine needs CLAUDE_CODE_SESSION_ID "
                  "(run it from a Claude session)", file=sys.stderr)
            return 2
    rows = run_ledger.list_runs(args.last, session_id=session_id, running_only=args.running)
    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        print(run_ledger.format_runs(rows, session_names=_session_names()))
    return 0


def cmd_wait(args) -> int:
    ids = list(args.ids)
    if args.mine:
        session_id = _this_session_id()
        if session_id is None:
            print("subfleet wait: --mine needs CLAUDE_CODE_SESSION_ID", file=sys.stderr)
            return 2
        ids += [row["id"] for row in
                run_ledger.list_runs(200, session_id=session_id, running_only=True)]
    if args.last:
        latest = run_ledger.latest_run_id(
            session_id=_this_session_id() if args.mine else None
        )
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
        print(run_ledger.summary_line(run_id, meta))
        if meta.get("missing"):
            worst = max(worst, 2)
        elif meta.get("orphaned"):
            # includes entries `runs reap` finalized (rc=-9): the runner died
            # without a finish record, so waiting on it is never a success.
            worst = max(worst, 125)
        else:
            rc = meta.get("rc")
            if isinstance(rc, int) and rc != 0:
                worst = max(worst, rc if rc > 0 else 1)
            if args.cat:
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
            result = run_ledger.kill_run(run_id)
        except (OSError, ValueError) as exc:
            print(f"subfleet kill: {run_id}: {exc}", file=sys.stderr)
            worst = 1
            continue
        print(f"subfleet kill: {run_id}: {result.get('status')}"
              + (f" pid={result['pid']}" if result.get("pid") else ""))
        if result.get("status") not in {"signalled", "already-finished"}:
            worst = 1
    return worst


def cmd_sessions(args) -> int:
    rows = inbox.live_sessions()
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
        print(f"{str(row.get('name') or '-'):<24.24} {row['session_id']:<36} {row['pid']:>6} "
              f"{'yes' if row.get('socket_present') else 'no':<6} {row.get('cwd') or '-'}{marker}")
    return 0


def cmd_notify_session(args) -> int:
    session_id = args.session or _this_session_id()
    if not session_id:
        print("subfleet notify: --session ID required outside a Claude session", file=sys.stderr)
        return 2
    text = args.text if args.text is not None else sys.stdin.read()
    result = inbox.push_to_session(session_id, text, mode_class=args.mode)
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        target = result.get("name") or session_id
        if result.get("delivered"):
            print(f"subfleet notify: delivered to {target} "
                  f"(pid {result.get('pid')}, mode={result.get('mode_class')})")
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
        # The app writes this restart's resume stub shortly AFTER the hook
        # runs, so a dedupe/cooldown verdict computed now can be keyed to the
        # PREVIOUS restart. Those two gates defer to the worker, which
        # re-decides after the delay against the fresh transcript.
        deferred = verdict["state"].get("state") == "interrupted" and (
            "already nudged" in reason or "cooldown" in reason
        )
        tickle.note(session_id, {
            "hook": payload.get("source"), "tickle": verdict["tickle"], "reason": reason,
            "state": verdict["state"].get("state"),
            "stubs": verdict["state"].get("restart_stubs"),
            **({"deferred_to_worker": True} if deferred else {}),
        })
        if verdict["tickle"] or deferred:
            tickle.spawn(session_id, payload.get("transcript_path"))
    rows = inbox.pending_notices(session_id, include_pushed=(args.event == "session-start"))
    if not rows:
        return 0
    context = inbox.render_pending(session_id, rows)
    inbox.mark_surfaced(session_id, [row["run_id"] for row in rows])
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))
    return 0


def cmd_tickle_worker(args) -> int:
    """Detached nudger spawned by the SessionStart hook (or `subfleet tickle`)."""
    verdict = tickle.deliver(args.session, args.transcript, delay_s=args.delay, force=args.force)
    return 0 if verdict.get("delivered") else 1


def cmd_muster(args) -> int:
    """Roll-call every recently-active live session back to work."""
    rows = tickle.survey()
    me = _this_session_id()
    called = 0
    print(f"{'name':<24} {'state':<12} {'age':>7} outcome")
    for row in rows:
        age = "-" if row.get("age_s") is None else f"{int(row['age_s'])}s"
        if me is not None and row.get("session_id") == me:
            print(f"{str(row.get('name') or '-'):<24.24} {str(row.get('state')):<12} "
                  f"{age:>7} this session (excluded)")
            continue
        if not row.get("inbox"):
            print(f"{str(row.get('name') or '-'):<24.24} {str(row.get('state')):<12} "
                  f"{age:>7} no inbox (open it in the app)")
            continue
        if args.dry_run:
            verdict = tickle.muster_eligible(row["session_id"], row.get("transcript"))
            age_s = (verdict.get("state") or {}).get("age_s")
            if verdict["muster"] and age_s is not None and age_s < 120:
                outcome = f"skip — last activity {int(age_s)}s ago; a roll call waits 120s of quiet"
            else:
                outcome = ("would call — " if verdict["muster"] else "skip — ") \
                    + str(verdict.get("reason"))[:90]
        else:
            verdict = tickle.muster_deliver(row["session_id"], row.get("transcript"))
            outcome = ("called — " if verdict.get("delivered") else "skip — ") \
                + str(verdict.get("reason"))[:90]
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
        transcript = args.transcript or inbox.transcript_path(args.session)
        if args.dry_run:
            print(json.dumps(
                tickle.decide(args.session, transcript, source=None, force=args.force), indent=1
            ))
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
        print(f"{str(row.get('name') or '-'):<24.24} {str(row.get('state')):<12} "
              f"{age:>7} {row.get('detail')}"
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
    """Replace this process with a sibling runner script."""
    import os

    path = _bin(name)
    if not os.access(path, os.X_OK):
        print(f"subfleet: tool missing or not executable: {path}", file=sys.stderr)
        return 2
    os.execv(path, [path, *rest])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="subfleet", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="per-account quota + auth table")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--cached", action="store_true", help="use last watchdog snapshot (no network)")

    p_capacity = sub.add_parser("capacity", help="5h + weekly headroom across both families")
    p_capacity.add_argument("--json", action="store_true")

    p_runs = sub.add_parser("runs", help="durable prompt/output/error ledger")
    p_runs.add_argument("--last", type=int, default=20)
    p_runs.add_argument("--json", action="store_true")
    p_runs.add_argument("--mine", action="store_true",
                        help="only runs dispatched by this Claude session (CLAUDE_CODE_SESSION_ID)")
    p_runs.add_argument("--running", action="store_true", help="only unfinished runs")
    runs_sub = p_runs.add_subparsers(dest="runs_command")
    p_runs_show = runs_sub.add_parser("show", help="print one run and its output")
    p_runs_show.add_argument("id")
    p_runs_show.add_argument("--err", action="store_true")
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
    p_kill.add_argument("ids", nargs="+")

    p_sessions = sub.add_parser("sessions", help="live Claude Code sessions with an inbox (notification targets)")
    p_sessions.add_argument("--json", action="store_true")

    p_notify = sub.add_parser("notify", help="push a message into a Claude session inbox (default: this session)")
    p_notify.add_argument("text", nargs="?", help="message text (stdin when omitted)")
    p_notify.add_argument("--session", help="target session id (see `subfleet sessions`)")
    p_notify.add_argument("--mode", choices=["bypass", "prompting", "none"], default=None,
                          help="permission class to declare (default: the recipient's own)")
    p_notify.add_argument("--json", action="store_true")

    p_hooks = sub.add_parser("hooks", help="Claude Code hooks: completion catch-up + attached-runner guard")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_command")
    for name, help_ in (("install", "add subfleet hooks to the Claude settings file"),
                        ("uninstall", "remove subfleet hooks from the Claude settings file")):
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

    p_muster = sub.add_parser("muster", help="roll-call every recently-active session: resume the "
                                             "interrupted, ask the idle to continue standing work")
    p_muster.add_argument("--dry-run", action="store_true")

    p_tickle = sub.add_parser("tickle", help="resume nudge for sessions whose last turn was cut off by a restart")
    p_tickle.add_argument("--session", help="one session id (see `subfleet sessions`)")
    p_tickle.add_argument("--transcript", help="transcript path override (with --session)")
    p_tickle.add_argument("--all", action="store_true", help="nudge every live interrupted session")
    p_tickle.add_argument("--dry-run", action="store_true", help="show states, send nothing")
    p_tickle.add_argument("--force", action="store_true", help="ignore the age cap, dedupe, and cooldown")
    p_tickle.add_argument("--json", action="store_true")

    p_pick = sub.add_parser(
        "pick", help="best lane for dispatch: `pick codex` (home) or `pick claude` (email)"
    )
    p_pick.add_argument("family", nargs="?", choices=("codex", "claude"), default="codex")
    p_pick.add_argument("--json", action="store_true")
    p_pick.add_argument("--all", action="store_true", help="show full ranking")
    p_pick.add_argument("--cached", action="store_true")
    p_pick.add_argument("--min-headroom", type=float, default=None,
                        help="minimum headroom %% to qualify (default 5)")
    p_pick.add_argument("--handicap", type=float, default=10.0,
                        help="score penalty for the account active in the desktop app (default 10)")
    p_pick.add_argument("--no-handicap", action="store_true",
                        help="rank purely by usage (may burn the primary account's window)")
    p_pick.add_argument("--exclude", action="append", default=[], help=argparse.SUPPRESS)

    for name, help_ in (
        ("run", "capacity-aware delegate router"),
        ("codex", "hardened Codex runner"),
        ("claude", "hardened Claude runner"),
        ("mirror", "desktop session mirror pass"),
    ):
        p_tool = sub.add_parser(name, help=help_, add_help=False)
        p_tool.add_argument("rest", nargs=argparse.REMAINDER)

    p_login = sub.add_parser(
        "login", help="stage a Codex lane re-login: `login codex 3` or `login codex app`"
    )
    p_login.add_argument("family", choices=("codex",))
    p_login.add_argument("target", help="lane number 1-9, or `app` for the desktop app home")
    p_login.add_argument("--no-watch", action="store_true", help="do not arm the completion watcher")
    p_login.add_argument("--no-open", action="store_true", help="print the OAuth URL instead of opening a browser")

    p_errors = sub.add_parser("errors", help="observed limit/auth errors")
    p_errors.add_argument("--hours", type=float, default=24)
    p_errors.add_argument("--json", action="store_true")

    p_watch = sub.add_parser("watch", help="one snapshot + alert cycle (for cron or another scheduler)")
    p_watch.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")

    sub.add_parser("brief", help="compact Markdown section for a daily status brief")

    p_enroll = sub.add_parser("enroll", help="store a Claude setup token for lane dispatch")
    p_enroll.add_argument("email", help="account email (must be in accounts.json roster)")

    p_secret = sub.add_parser("secret", help=argparse.SUPPRESS)
    p_secret.add_argument("action", choices=("get-for-account",))
    p_secret.add_argument("email")

    sub.add_parser("_codex-binary", help=argparse.SUPPRESS)

    p_usage = sub.add_parser("lane-usage", help=argparse.SUPPRESS)
    usage_sub = p_usage.add_subparsers(dest="action", required=True)
    p_usage_record = usage_sub.add_parser("record", help=argparse.SUPPRESS)
    p_usage_record.add_argument("--email", required=True)
    p_usage_record.add_argument("--session-id", required=True)
    p_usage_record.add_argument("--transcript", required=True)
    p_usage_limit = usage_sub.add_parser("hard-limit", help=argparse.SUPPRESS)
    p_usage_limit.add_argument("--email", required=True)
    p_usage_limit.add_argument("--session-id", required=True)
    p_usage_limit.add_argument("--error-file", action="append", default=[])
    p_usage_auth = usage_sub.add_parser("auth-failure", help=argparse.SUPPRESS)
    p_usage_auth.add_argument("--email", required=True)

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

    argv = list(sys.argv[1:] if argv is None else argv)
    known = {
        "status", "capacity", "runs", "pick", "run", "codex", "claude", "mirror",
        "login", "errors", "watch", "brief", "enroll", "secret", "lane-usage",
        "_codex-binary", "_record-run", "wait", "kill", "sessions", "notify",
        "hooks", "_session-hook", "_tickle", "tickle", "muster",
    }
    if not argv or (argv[0] not in known and argv[0] not in ("-h", "--help")):
        argv = ["status", *argv]
    if argv[0] in ("run", "codex", "claude", "mirror"):
        rest = argv[1:]
        if argv[0] == "run":
            from . import delegate

            return delegate.main(rest)
        return _exec_tool(
            {
                "codex": "subfleet-codex",
                "claude": "subfleet-claude",
                "mirror": "subfleet-mirror",
            }[argv[0]],
            rest,
        )
    args = parser.parse_args(argv)
    if args.command == "pick":
        if args.family == "claude":
            args.min_headroom = (
                claude.DEFAULT_MIN_HEADROOM if args.min_headroom is None else args.min_headroom
            )
            return cmd_claude_pick(args)
        args.min_headroom = (
            snapshot.DEFAULT_MIN_HEADROOM if args.min_headroom is None else args.min_headroom
        )
        return cmd_pick(args)
    handlers = {
        "status": cmd_status,
        "capacity": cmd_capacity,
        "runs": cmd_runs,
        "login": cmd_login,
        "errors": cmd_errors,
        "watch": cmd_watch,
        "brief": cmd_brief,
        "enroll": cmd_enroll,
        "secret": cmd_secret,
        "lane-usage": cmd_lane_usage,
        "_codex-binary": cmd_codex_binary,
        "_record-run": cmd_record_run,
        "wait": cmd_wait,
        "kill": cmd_kill,
        "sessions": cmd_sessions,
        "notify": cmd_notify_session,
        "hooks": cmd_hooks,
        "_session-hook": cmd_session_hook,
        "_tickle": cmd_tickle_worker,
        "tickle": cmd_tickle,
        "muster": cmd_muster,
    }
    if args.command == "hooks" and not args.hooks_command:
        args.hooks_command = "status"
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
