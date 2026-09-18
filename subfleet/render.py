"""Human rendering: terminal table, morning-brief markdown section."""

from . import capacity_expiry
from .claude import LIVE_STALE_AFTER_MIN
from .snapshot import has_applicable_reset_credit, limited_reset_credit_homes
from .util import fmt_clock, now_local, parse_iso

VERDICT_LABELS = {
    "ok": "OK",
    "limited": "LIMITED",
    "auth-revoked": "AUTH-REVOKED",
    "auth-suspect": "AUTH-SUSPECT",
    "no-auth": "NO-AUTH",
    "free-plan": "FREE-PLAN (not a lane — upgrade to Pro)",
    "unknown": "UNKNOWN",
}


def _pct(w: dict | None) -> str:
    if not w or w.get("used_percent") is None:
        return "-"
    return f"{round(float(w['used_percent']))}%"


def _reset_or_dash(w: dict | None, now) -> str:
    """Reset clock, or '-' when the server reports no such window at all
    (e.g. wham stopped reporting a 5h window for Pro accounts ~2026-07)."""
    if not w or w.get("used_percent") is None:
        return "-"
    return _reset(w, now)


def _reset(w: dict | None, now) -> str:
    if not w:
        return "?"
    return fmt_clock(parse_iso(w.get("reset_at")), now)


def _short_home(home: str) -> str:
    return home.replace(str(__import__("pathlib").Path.home()), "~")


def _live_is_stale(live: dict | None, now) -> bool:
    """Snapshot builders stamp live readings with `stale`; older snapshots
    (and hand-built fixtures) carry only as_of, so fall back to computing the
    same threshold. No as_of at all ⇒ can't judge, keep legacy behavior."""
    if not live:
        return False
    if "stale" in live:
        return bool(live["stale"])
    as_of = parse_iso(live.get("as_of"))
    if as_of is None:
        return False
    return (now - as_of).total_seconds() / 60 > LIVE_STALE_AFTER_MIN


def _active_live(claude_section: dict) -> dict | None:
    """The active account's picked usage reading: section-level since 2026-08,
    per-account before that, and a bare statusline blob (oldest snapshots /
    hand-built fixtures) as the last resort — all subject to the same
    staleness demotion."""
    live = claude_section.get("live")
    if live:
        return live
    row = next(
        (a for a in claude_section.get("accounts") or []
         if isinstance(a, dict) and a.get("active")),
        {},
    )
    if row.get("live"):
        return row["live"]
    sl = claude_section.get("statusline")
    if sl and sl.get("five_hour_pct") is not None:
        return {
            "five_hour_pct": sl["five_hour_pct"],
            "seven_day_pct": sl.get("seven_day_pct"),
            "source": "statusline",
            "as_of": sl.get("updated_at"),
        }
    return None


def _stale_reading_line(live: dict, now, indent: str = "  ") -> str:
    bits = []
    if live.get("five_hour_pct") is not None:
        bits.append(f"5h {live['five_hour_pct']}%")
    if live.get("seven_day_pct") is not None:
        bits.append(f"week {live['seven_day_pct']}%")
    return (f"{indent}last reading: {' · '.join(bits)} — stale, from"
            f" {live.get('source', '?')} {fmt_clock(parse_iso(live.get('as_of')), now)}")


def _codex_expiry(snap: dict, now) -> dict:
    """Stored analysis for real snapshots; on-demand analysis for old fixtures.

    The analyzer reports incomplete fleet totals as ``None``, so this fallback
    can still show a known lane burn without inventing a fleet-wide number.
    """
    codex_section = snap.get("codex") or {}
    stored = codex_section.get("capacity_expiry")
    if isinstance(stored, dict):
        return stored
    return capacity_expiry.analyze(
        codex_section.get("homes") or [],
        capacity_expiry.read_history(),
        now=now,
    )


def _burn_label(value: dict | None) -> str:
    value = value or {}

    def rate(key: str) -> str:
        number = value.get(key)
        return f"{float(number):.1f}" if isinstance(number, (int, float)) else "-"

    return f"{rate('burn_6h_pct_per_hour')}/{rate('burn_24h_pct_per_hour')}"


def table(snap: dict) -> str:
    now = (parse_iso(snap.get("generated_at")) or now_local()).astimezone()
    expiry = _codex_expiry(snap, now)
    expiry_lanes = expiry.get("lanes") or {}
    lines = [f"AI quota — {now.strftime('%Y-%m-%d %-I:%M%p %Z').lower()}", ""]
    lines.append("CODEX (ChatGPT accounts, one per CODEX_HOME)")
    header = (f"  {'home':<11} {'account':<26} {'5h':>5} {'resets':<14}"
              f" {'week':>5} {'resets':<14} {'burn %/h 6/24':>15}  status")
    lines.append(header)
    for e in snap["codex"]["homes"]:
        w = e["windows"]
        lane_expiry = expiry_lanes.get(e["home"]) or {}
        label = VERDICT_LABELS.get(e["verdict"], e["verdict"])
        if has_applicable_reset_credit(e):
            label += " · reset available"
        if lane_expiry.get("expiring_unused"):
            unused = lane_expiry.get("projected_unused_percent")
            if isinstance(unused, (int, float)):
                label += f" · → ~{round(float(unused))}% unused at reset"
        if e.get("duplicate_of"):
            label += f" (dup of {_short_home(e['duplicate_of'])})"
        src = ""
        if w["source"] == "observed" and w.get("as_of"):
            src = f"  [observed {fmt_clock(parse_iso(w['as_of']), now)}]"
        elif w["source"] == "none":
            src = "  [no data]"
        acct = e.get("email") or (e.get("account_id") or "?")[:12]
        lines.append(
            f"  {_short_home(e['home']):<11} {acct:<26} {_pct(w.get('five_hour')):>5}"
            f" {_reset_or_dash(w.get('five_hour'), now):<14} {_pct(w.get('weekly')):>5}"
            f" {_reset_or_dash(w.get('weekly'), now):<14} {_burn_label(lane_expiry):>15}"
            f"  {label}{src}"
        )
        for err in e["recent_errors"]["usage_limit"][:1]:
            lines.append(
                f"  {'':<11} last usage-limit error {fmt_clock(parse_iso(err['observed_at']), now)}"
                f" (retry {err['try_again']})"
            )
        for err in e["recent_errors"]["auth_revoked"][:1]:
            lines.append(
                f"  {'':<11} refresh-token-revoked error seen {fmt_clock(parse_iso(err['observed_at']), now)}"
            )
    app_home = snap["codex"].get("app_home")
    if app_home:
        if app_home.get("status") == "ok" and (app_home.get("email") or app_home.get("account_id")):
            who = app_home.get("email") or (app_home.get("account_id") or "?")[:12]
            shadows = app_home.get("shadows") or []
            note = (
                f" — same account as {', '.join(_short_home(h) for h in shadows)} (lane shadowed; revocation risk)"
                if shadows else " — not a lane account"
            )
            lines.append(f"  app {_short_home(app_home['home']):<8} {who}{note}")
        else:
            lines.append(f"  app {_short_home(app_home['home']):<8} (no login)")
    fleet = snap["codex"]["fleet"]
    best = fleet.get("best_home")
    fleet_line = f"  fleet: {fleet['dispatchable_now']}/{fleet['total_homes']} dispatchable"
    if best:
        fleet_line += f" · best: {_short_home(best)}"
    if fleet.get("earliest_reset"):
        fleet_line += f" · earliest reset: {fmt_clock(parse_iso(fleet['earliest_reset']), now)}"
    reset_holders = limited_reset_credit_homes(snap["codex"]["homes"])
    if reset_holders:
        fleet_line += f" · resets available: {len(reset_holders)}"
    windows_left = expiry.get("windows_left")
    projected_unused = expiry.get("projected_unused_windows")
    if expiry.get("complete") and isinstance(windows_left, (int, float)) \
            and isinstance(projected_unused, (int, float)):
        fleet_line += (
            f" · {float(windows_left):.1f} windows left"
            f" · ~{float(projected_unused):.1f} projected to expire unused"
        )
    lines += [fleet_line, ""]

    c = snap["claude"]
    acct = c["account"].get("email") or "?"
    tier = c.get("tier") or c.get("subscription") or "?"
    lines.append(f"CLAUDE (active login: {acct}, tier {tier})")
    keepalive_state = c.get("keepalive") or {}
    keepalive_run = keepalive_state.get("last_run") or {}
    keepalive_finished = parse_iso(keepalive_run.get("finished_at"))
    opened = keepalive_run.get("opened")
    if keepalive_finished is not None and isinstance(opened, int) and not isinstance(opened, bool):
        lines.append(
            f"  keepalive: last {keepalive_finished.astimezone().strftime('%H:%M')}, "
            f"{opened} opened"
        )
    active_row = next((a for a in c.get("accounts") or [] if a.get("active")), {})
    live = _active_live(c)
    stale = _live_is_stale(live, now)
    derived = active_row.get("derived") or {}
    d5, dw = derived.get("five_hour"), derived.get("weekly")
    headlined_d5 = False
    if live and live.get("five_hour_pct") is not None and not stale:
        wk = f" · week {live['seven_day_pct']}%" if live.get("seven_day_pct") is not None else ""
        models = " ".join(
            f"· {m} wk {round(p)}%" for m, p in (live.get("model_weeks") or {}).items()
        )
        lines.append(
            f"  5h window: {live['five_hour_pct']}% used"
            f" ({live['source']} {fmt_clock(parse_iso(live.get('as_of')), now)}){wk}"
            + (f" {models}" if models else "")
        )
    else:
        # No reading fresh enough to headline: lead with the derived activity
        # model when it supports an open window, and demote any stale reading
        # to labeled context instead of presenting it as current.
        probe_status = (c.get("oauth_probe") or {}).get("status", "?")
        hint = {
            "rate-limited": "usage endpoint 429 — account limited or throttled; retrying each cycle",
            "token-invalid": "keychain token stale — refreshes when a desktop session runs",
        }.get(probe_status, f"usage endpoint: {probe_status}")
        if d5:
            lines.append(
                f"  5h window: no fresh reading — resets ~{fmt_clock(parse_iso(d5['reset_at']), now)}"
                " (derived from activity)"
            )
            headlined_d5 = True
        else:
            lines.append(f"  5h window: unknown — {hint}")
        if live and (live.get("five_hour_pct") is not None
                     or live.get("seven_day_pct") is not None):
            lines.append(_stale_reading_line(live, now))
    if (d5 and not headlined_d5) or dw:
        parts = []
        if d5 and not headlined_d5:
            parts.append(f"5h resets ~{fmt_clock(parse_iso(d5['reset_at']), now)} (derived from activity)")
        if dw:
            tag = "observed" if dw.get("confidence") == "observed" else "derived"
            parts.append(f"week resets {fmt_clock(parse_iso(dw['reset_at']), now)} ({tag})")
        lines.append("  current windows: " + " · ".join(parts))
    active = c.get("active_limit")
    if active:
        lines.append(
            f"  ACTIVE LIMIT: {active['kind']} — resets {fmt_clock(parse_iso(active['reset_at']), now)}"
            f" (seen in {active.get('sessions', 1)} session(s))"
        )
    elif c["recent_errors"]:
        last = c["recent_errors"][0]
        lines.append(
            f"  last limit event: {last['kind']} at {fmt_clock(parse_iso(last['observed_at']), now)}"
            + (f", reset {fmt_clock(parse_iso(last['reset_at']), now)}" if last.get("reset_at") else "")
        )
    else:
        lines.append("  no limit errors observed in the last 24h")
    kc = c.get("keychain", {})
    probe = c.get("oauth_probe", {})
    if probe.get("status") == "token-invalid":
        lines.append(
            f"  keychain OAuth token: INVALID (expired {kc.get('expires_at', '?')[:10]})"
            " — informational; live sessions authenticate separately"
        )
    mirror = c.get("session_mirror") or {}
    if mirror.get("status") == "stalled":
        if mirror.get("job_loaded") is False:
            detail = "launchd job not loaded"
        elif mirror.get("run_min") is not None:
            detail = f"run hung for {mirror['run_min']} min"
        else:
            detail = f"heartbeat idle {mirror.get('age_min', '?')} min"
        lines.append(
            f"  session mirror STALLED ({detail}) — account switches will hide sessions;"
            " heal: launchctl kickstart -k gui/$UID/com.maxghenis.cos.subfleet-mirror"
        )
    accounts = c.get("accounts") or []
    fleet_c = c.get("lanes") or {}
    lanes = fleet_c.get("lanes") or []
    if lanes:
        fleet_line = (
            f"  lanes: {fleet_c.get('dispatchable_now', 0)}/{fleet_c.get('enrolled', 0)} dispatchable"
        )
        if fleet_c.get("best"):
            fleet_line += f" · best: {fleet_c['best']}"
        if fleet_c.get("earliest_reset"):
            fleet_line += f" · earliest reset: {fmt_clock(parse_iso(fleet_c['earliest_reset']), now)}"
        lines.append(fleet_line)
        lines.append(f"  {'lane':<28} {'5h':>5} {'resets':<14} {'week':>5}  status")

        def lane_pct(v):
            return f"{round(float(v))}%" if v is not None else "?"

        for l in lanes:
            status = "OK" if l["verdict"] == "ok" else l["verdict"].upper()
            if l.get("active"):
                status += " (active login)"
            lines.append(
                f"  {l['email']:<28} {lane_pct(l.get('five_hour_used_percent')):>5}"
                f" {fmt_clock(parse_iso(l.get('five_hour_reset_at')), now):<14}"
                f" {lane_pct(l.get('weekly_used_percent')):>5}  {status}"
            )
    else:
        # Pre-lanes snapshots (or none enrolled with old data): legacy per-account lines.
        for a in accounts:
            if a["active"] or not a.get("enrolled"):
                continue
            p = a.get("probe") or {}
            if p.get("status") == "ok":
                fh = (p.get("five_hour") or {}).get("used_percent")
                sd = (p.get("seven_day") or {}).get("used_percent")
                detail = " · ".join(
                    s for s in (
                        f"5h {round(fh)}%" if fh is not None else None,
                        f"wk {round(sd)}%" if sd is not None else None,
                    ) if s
                ) or "probed ok (no window fields)"
            else:
                detail = f"probe {p.get('status', '?')}"
            lines.append(f"  {a['email']:<28} {detail}")
    unenrolled = [a for a in accounts if not a["active"] and not a.get("enrolled")]
    if unenrolled:
        lines.append(
            f"  not enrolled ({len(unenrolled)}): "
            + ", ".join(a["email"].split("@")[1] for a in unenrolled)
            + "  — enroll: claude setup-token | subfleet enroll <email>"
        )
    return "\n".join(lines)


def brief_md(snap: dict) -> str:
    """Compact section for the chief-of-staff morning brief."""
    now = parse_iso(snap.get("generated_at")) or now_local()
    expiry = _codex_expiry(snap, now)
    lines = ["## AI capacity"]
    fleet = snap["codex"]["fleet"]
    parts = [f"codex: {fleet['dispatchable_now']}/{fleet['total_homes']} lanes dispatchable"]
    if fleet.get("best_home"):
        parts.append(f"best {_short_home(fleet['best_home'])}")
    if fleet.get("earliest_reset"):
        parts.append(f"earliest reset {fmt_clock(parse_iso(fleet['earliest_reset']), now)}")
    lines.append("- " + " · ".join(parts))
    reset_holders = limited_reset_credit_homes(snap["codex"]["homes"])
    if reset_holders:
        lines.append(
            f"- codex: {len(reset_holders)} limited lanes hold an unused reset credit"
        )
    windows_left = expiry.get("windows_left")
    projected_unused = expiry.get("projected_unused_windows")
    expires_by = parse_iso(expiry.get("expires_by"))
    if expiry.get("complete") and isinstance(windows_left, (int, float)) \
            and isinstance(projected_unused, (int, float)) and expires_by is not None:
        lines.append(
            f"- codex: {float(windows_left):.1f} windows left, "
            f"~{float(projected_unused):.1f} projected to expire unused by "
            f"{fmt_clock(expires_by, now)}"
        )
    problems = []
    for e in snap["codex"]["homes"]:
        if e["verdict"] in ("auth-revoked", "auth-suspect", "no-auth"):
            problems.append(f"{_short_home(e['home'])} {e['verdict']}")
    if snap["codex"]["duplicates"]:
        dups = ", ".join(
            " + ".join(_short_home(h) for h in d["homes"]) for d in snap["codex"]["duplicates"]
        )
        problems.append(f"DUPLICATE account bindings: {dups} (revocation trap)")
    if problems:
        lines.append("- ⚠ codex auth: " + "; ".join(problems) + " — fix: `CODEX_HOME=<home> codex login`")
    c = snap["claude"]
    live = _active_live(c)
    stale = _live_is_stale(live, now)
    if c.get("active_limit"):
        a = c["active_limit"]
        lines.append(
            f"- claude: LIMITED ({a['kind']}) — resets {fmt_clock(parse_iso(a['reset_at']), now)}"
        )
    elif live and live.get("five_hour_pct") is not None and not stale:
        wk = f", week {live['seven_day_pct']}%" if live.get("seven_day_pct") is not None else ""
        stamp = (
            "" if live.get("source") == "oauth"
            else f" ({live.get('source')} {fmt_clock(parse_iso(live.get('as_of')), now)})"
        )
        lines.append(f"- claude: 5h {live['five_hour_pct']}%{wk}{stamp}")
    elif live and (live.get("five_hour_pct") is not None
                   or live.get("seven_day_pct") is not None):
        lines.append("- claude:" + _stale_reading_line(live, now, indent=" "))
    else:
        lines.append("- claude: usage unknown (no fresh reading)")
    fleet_c = c.get("lanes") or {}
    if fleet_c.get("enrolled"):
        lane_parts = [
            f"claude lanes: {fleet_c.get('dispatchable_now', 0)}/{fleet_c['enrolled']} dispatchable"
        ]
        if fleet_c.get("best"):
            lane_parts.append(f"best {fleet_c['best']}")
        if fleet_c.get("earliest_reset"):
            lane_parts.append(f"earliest reset {fmt_clock(parse_iso(fleet_c['earliest_reset']), now)}")
        lines.append("- " + " · ".join(lane_parts))
    return "\n".join(lines)
