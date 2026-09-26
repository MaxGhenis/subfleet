"""Assemble the full cross-account snapshot: codex homes + Claude, with honest
verdicts. Every number carries its provenance (live probe vs observed-at)."""

from datetime import datetime, timedelta
from pathlib import Path

from . import capacity, capacity_expiry, claude, codex, desktop_app, paths, reset_policy, run_ledger
from .util import atomic_write_json, iso, load_json, now_local, parse_iso, strip_private

# A home is dispatchable only with at least this much 5h-window headroom.
DEFAULT_MIN_HEADROOM = 5.0


def _snapshot_claude_accounts(rows: list[dict], generated_at: str | None) -> list[dict]:
    """Compatibility shape for the renderer/menu app, sourced from capacity."""
    accounts = []
    for row in rows:
        probe = None
        if row.get("enrolled"):
            five_hour = dict(row.get("five_hour") or {})
            weekly = dict(row.get("weekly") or {})
            probe = {
                "status": row.get("status") or "unknown",
                "checked_at": generated_at,
                "five_hour": five_hour,
                "seven_day": weekly,
                "windows": {"five_hour": five_hour, "seven_day": weekly},
                "confidence": row.get("confidence"),
            }
        accounts.append(
            {
                "email": row.get("email") or row.get("id"),
                "active": bool(row.get("active")),
                "enrolled": bool(row.get("enrolled")),
                "probe": probe,
                "capacity": row,
            }
        )
    return accounts


def _capacity_lane_fleet(rows: list[dict], now) -> dict:
    enrolled = [row for row in rows if row.get("enrolled")]
    summary = capacity.family_summaries(rows, now=now)["claude"]
    lanes = []
    for row in enrolled:
        five_hour = row.get("five_hour") or {}
        weekly = row.get("weekly") or {}
        lanes.append(
            {
                "email": row.get("email") or row.get("id"),
                "active": bool(row.get("active")),
                "verdict": "ok" if row.get("dispatchable") else row.get("status", "unknown"),
                "five_hour_used_percent": five_hour.get("used_percent"),
                "weekly_used_percent": weekly.get("used_percent"),
                "five_hour_tokens": five_hour.get("tokens"),
                "weekly_tokens": weekly.get("tokens"),
                "five_hour_reset_at": five_hour.get("reset_at"),
                "weekly_reset_at": weekly.get("reset_at"),
                "reset_at": row.get("limited_until"),
                "confidence": row.get("confidence"),
                "learned_capacity": row.get("learned_capacity"),
            }
        )
    return {
        "enrolled": len(enrolled),
        "dispatchable_now": sum(bool(row.get("dispatchable")) for row in enrolled),
        "best": summary.get("best_email") or summary.get("best"),
        "earliest_reset": summary.get("earliest_reset"),
        "lanes": lanes,
    }


def codex_verdict(auth: dict, probe: dict, observed: dict | None) -> str:
    if auth.get("status") != "ok":
        return "no-auth"
    status = probe.get("status")
    if status == "token-revoked":
        return "auth-revoked"
    if status == "ok":
        if codex.plan_is_free(probe.get("plan_type")):
            # A free-plan account is not a lane: no Pro models/quota. Seen
            # 2026-08-19 when a new lane was logged in before its Pro upgrade
            # — without this gate it ranked BEST (0% of a 30-day window).
            return "free-plan"
        if probe.get("limit_reached") or (probe.get("allowed") is False):
            return "limited"
        return "ok"
    # http-4xx on the usage endpoint with valid-looking local auth: auth trouble.
    if status and status.startswith("http-4"):
        return "auth-suspect"
    return "unknown"


def effective_windows(probe_result: dict, observed: dict | None) -> dict:
    """Effective window data for one home: live if we have it, else last
    observed. Shared by build() and the watchdog's expired-token heal, which
    re-probes a home mid-cycle."""
    if probe_result.get("status") == "ok":
        f, w = codex.classify_windows(
            probe_result.get("primary"), probe_result.get("secondary"))
        if f is None and w is None:
            f, w = probe_result.get("primary"), probe_result.get("secondary")
        return {"primary": f, "secondary": w, "five_hour": f, "weekly": w,
                "source": "live", "as_of": probe_result.get("checked_at")}
    if observed:
        f, w = codex.classify_windows(
            observed.get("primary"), observed.get("secondary"))
        if f is None and w is None:
            f, w = observed.get("primary"), observed.get("secondary")
        return {"primary": f, "secondary": w, "five_hour": f, "weekly": w,
                "source": "observed", "as_of": observed.get("observed_at")}
    return {"primary": None, "secondary": None, "five_hour": None,
            "weekly": None, "source": "none", "as_of": None}


def build(live: bool = True, timeout: float = 15.0, transcript_hours: float = 24,
          errors_hours: float = 12, probe_fn=None, claude_probe_fn=None) -> dict:
    """probe_fn/claude_probe_fn are injectable for tests.

    Rollout files are only swept where they add information: the observed
    windows fallback runs when the live probe couldn't answer, and the error
    scan is bounded by errors_hours (interactive callers pass a small window
    to keep cold runs snappy; the watchdog uses the default)."""
    now = now_local()
    homes = paths.codex_homes()
    auths = [codex.read_auth(h) for h in homes]

    if live:
        probe = probe_fn or codex.probe_wham
        probes = codex.probe_all(auths, timeout=timeout) if probe_fn is None else [
            probe(a) for a in auths
        ]
    else:
        probes = [{"status": "skipped"} for _ in auths]

    home_entries = []
    by_account: dict[str, list[str]] = {}
    for home, auth, probe_result in zip(homes, auths, probes):
        observed = None
        if probe_result.get("status") != "ok":
            observed = codex.latest_rollout_rate_limits(home)
        errors = codex.recent_limit_errors(home, hours=errors_hours)
        verdict = codex_verdict(auth, probe_result, observed)
        acct = auth.get("account_id")
        if acct:
            by_account.setdefault(acct, []).append(str(home))
        eff = effective_windows(probe_result, observed)
        home_entries.append(
            {
                "home": str(home),
                "is_primary_home": home == paths.primary_codex_home(),
                "account_id": acct,
                "email": auth.get("email") or probe_result.get("email"),
                "plan": probe_result.get("plan_type") or auth.get("plan"),
                "auth_last_refresh": auth.get("last_refresh"),
                "verdict": verdict,
                "reset_credits": codex.reset_credits_from_probe(probe_result),
                "probe": probe_result,
                "windows": eff,
                "rollout_observed": observed,
                "recent_errors": errors,
            }
        )

    duplicates = [
        {"account_id": acct, "homes": hs} for acct, hs in by_account.items() if len(hs) > 1
    ]
    # Mark non-canonical duplicate homes so pick() never double-dispatches an account.
    seen: set[str] = set()
    for entry in home_entries:
        acct = entry.get("account_id")
        entry["duplicate_of"] = None
        if acct:
            if acct in seen:
                entry["duplicate_of"] = next(
                    e["home"] for e in home_entries if e.get("account_id") == acct
                )
            seen.add(acct)

    # The desktop app's home (~/.codex) is observed, never dispatched to. When
    # the app is signed into a lane's account, that lane is "shadowed": two
    # token copies of one account, and whichever refreshes first revokes the
    # other (observed 2026-08-04/13/17). The watchdog names it, and automatic
    # reset redemption avoids it unless every candidate is shadowed. Codex
    # dispatch order itself is strictly the weekly-reset waterfall.
    app_auth = codex.app_home_identity()
    app_acct = app_auth.get("account_id")
    shadows = [e["home"] for e in home_entries if app_acct and e.get("account_id") == app_acct]
    for e in home_entries:
        e["shadowed_by_app"] = e["home"] in shadows
    app_home = {
        "home": str(paths.app_codex_home()),
        "status": app_auth.get("status"),
        "account_id": app_acct,
        "email": app_auth.get("email"),
        "plan": app_auth.get("plan"),
        "auth_last_refresh": app_auth.get("last_refresh"),
        "shadows": shadows,
    }

    codex_section = {
        "homes": home_entries,
        "app_home": app_home,
        "duplicates": duplicates,
        "fleet": codex_fleet(home_entries, now),
        "capacity_expiry": capacity_expiry.analyze(
            home_entries,
            capacity_expiry.read_history(),
            now=now,
        ),
    }

    # Claude
    ident = claude.identity()
    creds = claude.keychain_credentials()
    if not live:
        cprobe = {"status": "skipped"}
    elif not (ident.get("email") or ident.get("account_uuid")):
        cprobe = {"status": "no-identity"}
    else:
        cprobe = (claude_probe_fn or claude.probe_oauth_usage)(creds.get("_token"), timeout=timeout)
    sl = claude.statusline_state()
    events = claude.transcript_limit_events(hours=transcript_hours)
    active = claude.active_limit(events, now=now)

    lane_rows = capacity.claude_lane_rows(
        active_identity=ident,
        active_probe=cprobe,
        now=now,
    )
    accounts = _snapshot_claude_accounts(lane_rows, iso(now))

    def _probe_live(probe: dict, source: str) -> dict | None:
        if probe.get("status") != "ok":
            return None
        return {
            "five_hour_pct": (probe.get("five_hour") or {}).get("used_percent"),
            "seven_day_pct": (probe.get("seven_day") or {}).get("used_percent"),
            "model_weeks": {
                k.removeprefix("seven_day_"): w.get("used_percent")
                for k, w in (probe.get("windows") or {}).items()
                if k.startswith("seven_day_") and w.get("used_percent") is not None
            },
            "source": source,
            "as_of": probe.get("checked_at"),
        }

    # Preserve the raw usage payload once the endpoint answers 200 — schema
    # discovery for the percentage extraction, and the read-back source for
    # last_oauth_reading below (local file, no tokens).
    if cprobe.get("status") == "ok" and cprobe.get("raw") is not None:
        try:
            atomic_write_json(paths.oauth_raw_path(),
                              {"checked_at": cprobe.get("checked_at"), "raw": cprobe["raw"]})
        except OSError:
            pass

    active_live = None
    active_probe_status = cprobe.get("status")
    for row in accounts:
        if not row["active"]:
            continue
        # Freshest source wins for the active account: this run's app-token
        # probe, the last successful probe payload, the capacity live cache,
        # then the statusline tap (terminal-TUI sessions only — can be weeks
        # old). pick_live_source stamps age and flags stale readings so they
        # are demoted downstream, never headlined. Enrolled setup-tokens are
        # inference-only and are never usage-probed.
        candidates = [_probe_live(cprobe, "oauth")]
        cached_probe = claude.last_oauth_reading()
        if cached_probe:
            candidates.append(_probe_live(cached_probe, "oauth-cache"))
        candidates.append(claude.capacity_cached_reading(ident.get("email")))
        if sl and sl.get("five_hour_pct") is not None:
            candidates.append({
                "five_hour_pct": sl["five_hour_pct"],
                "seven_day_pct": sl.get("seven_day_pct"),
                "source": "statusline",
                "as_of": sl.get("updated_at"),
            })
        row_live = claude.pick_live_source(candidates, now=now)
        if row_live:
            row["live"] = row_live
            active_live = row_live
        row["derived"] = {
            "five_hour": claude.derive_five_hour_window(now=now),
            "weekly": claude.current_weekly_reset(
                (sl or {}).get("seven_day_reset_at"), now=now),
        }
        row["oauth_status"] = active_probe_status
        if sl and sl.get("five_hour_pct") is not None:
            row["statusline"] = {
                "five_hour_pct": sl["five_hour_pct"],
                "seven_day_pct": sl.get("seven_day_pct"),
                "fresh": sl.get("fresh"),
                "updated_at": sl.get("updated_at"),
            }

    # Live data supersedes transcript inference: an observed "session limit"
    # error with a future reset can be stale (a new window opened since).
    # Only a same-run probe ("oauth") corroborates — cached readings may
    # predate the limit event.
    if active and active_live and (active_live.get("five_hour_pct") or 0) < 95 \
            and active_live.get("five_hour_pct") is not None \
            and active_live.get("source") == "oauth":
        active = None

    if active:
        claude_verdict = "limited"
    elif active_live is not None and not active_live.get("stale"):
        claude_verdict = "ok"
    elif active_probe_status == "rate-limited":
        # Endpoint 429 with auth passing: the account is limited or the
        # endpoint is throttling — either way, treat as constrained.
        claude_verdict = "rate-limited"
    else:
        claude_verdict = "unknown"

    claude_section = {
        "account": ident,
        "accounts": accounts,
        "live": active_live,
        "lanes": _capacity_lane_fleet(lane_rows, now),
        "known_accounts": claude.known_accounts(),
        "subscription": creds.get("subscription"),
        "tier": creds.get("tier"),
        "keychain": {k: v for k, v in creds.items() if not k.startswith("_")},
        "oauth_probe": {k: v for k, v in cprobe.items() if k != "raw"},
        "statusline": sl,
        "session_mirror": claude.session_mirror_health(now=now),
        "recent_errors": events[:8],
        "active_limit": active,
        "verdict": claude_verdict,
    }
    keepalive_state = load_json(paths.keepalive_state_path())
    if isinstance(keepalive_state, dict):
        claude_section["keepalive"] = keepalive_state

    return strip_private(
        {
            "generated_at": iso(now),
            "codex": codex_section,
            "claude": claude_section,
            # The desktop app's staged update (read from its main.log): a
            # scheduled kill of every app-hosted session, time unknown.
            "desktop_app": desktop_app.status(now=now),
        }
    )


def _headroom(entry: dict) -> float | None:
    windows = entry.get("windows", {})
    five = windows.get("five_hour") or windows.get("primary")
    weekly = windows.get("weekly") or windows.get("secondary")
    readings = [
        float(window["used_percent"])
        for window in (five, weekly)
        if isinstance(window, dict) and window.get("used_percent") is not None
    ]
    if not readings:
        return None
    return min(100.0 - used for used in readings)


def codex_fleet(home_entries: list[dict], now) -> dict:
    """Fleet roll-up (dispatchable count, best home, earliest reset). Reused by
    the watchdog to recount after the expired-token heal changes verdicts."""
    dispatchable = rank_for_dispatch(home_entries, now=now)
    resets = [
        parse_iso(((e["windows"].get("five_hour") or e["windows"].get("weekly")) or {}).get("reset_at"))
        for e in home_entries
        if e["verdict"] in ("limited", "ok") and (e["windows"].get("five_hour") or e["windows"].get("weekly"))
    ]
    future_resets = [r for r in resets if r and r > now]
    return {
        "total_homes": len(home_entries),
        "dispatchable_now": len(dispatchable),
        "best_home": dispatchable[0]["home"] if dispatchable else None,
        "earliest_reset": iso(min(future_resets)) if future_resets else None,
        "resets_available": len(limited_reset_credit_homes(home_entries)),
    }


def has_applicable_reset_credit(entry: dict) -> bool:
    """Whether a LIMITED lane has a gifted reset for its active limit."""
    applicable = (entry.get("reset_credits") or {}).get("applicable")
    return (
        entry.get("verdict") == "limited"
        and isinstance(applicable, int)
        and not isinstance(applicable, bool)
        and applicable > 0
    )


def limited_reset_credit_homes(home_entries: list[dict]) -> list[dict]:
    """Distinct LIMITED lanes whose gifted reset applies to the active limit."""
    return [
        entry for entry in home_entries
        if not entry.get("duplicate_of") and has_applicable_reset_credit(entry)
    ]


def dispatchable_best(home_entries: list[dict], handicap: float = 10.0,
                      min_headroom: float = DEFAULT_MIN_HEADROOM,
                      stale_max_min: float = 30.0) -> str | None:
    ranked = rank_for_dispatch(home_entries, handicap=handicap,
                               min_headroom=min_headroom, stale_max_min=stale_max_min)
    return ranked[0]["home"] if ranked else None


def rank_for_dispatch(home_entries: list[dict], handicap: float = 10.0,
                      min_headroom: float = DEFAULT_MIN_HEADROOM,
                      stale_max_min: float = 30.0,
                      now=None) -> list[dict]:
    """Order dispatchable homes by weekly reset ascending (Codex waterfall).

    Distinct-account and minimum-headroom gates remain. The protected/app
    identity and in-flight count remain visible metadata, but neither affects
    Codex ordering: concentrate load on the soonest-expiring weekly window.

    A home with a failed live probe still qualifies on rollout data observed
    within `stale_max_min`, flagged stale so callers can decide."""
    now = now or now_local()
    protected = codex.protected_account()
    in_flight = run_ledger.in_flight_counts()
    candidates = []
    for e in home_entries:
        if e.get("duplicate_of"):
            continue
        if (e.get("probe") or {}).get("limit_reached") is True:
            continue
        short_limit = reset_policy.short_window_limit_until(e, now=now)
        if short_limit is not None:
            continue
        stale = False
        if e["verdict"] == "ok":
            pass
        elif e["verdict"] == "unknown" and e["windows"]["source"] == "observed":
            as_of = parse_iso(e["windows"].get("as_of"))
            if not as_of or now - as_of > timedelta(minutes=stale_max_min):
                continue
            primary = e["windows"].get("primary") or {}
            reset = parse_iso(primary.get("reset_at"))
            if primary.get("used_percent") is not None and primary["used_percent"] >= 100 - min_headroom:
                if not reset or reset > now:
                    continue
            stale = True
        else:
            continue
        headroom = _headroom(e)
        if headroom is None or headroom < min_headroom:
            continue
        five = e["windows"].get("five_hour") or e["windows"].get("primary") or {}
        weekly = e["windows"].get("weekly") or e["windows"].get("secondary") or {}
        five_used = float(five["used_percent"]) if five.get("used_percent") is not None else 0.0
        weekly_used = float(weekly["used_percent"]) if weekly.get("used_percent") is not None else 0.0
        weekly_reset = parse_iso(weekly.get("reset_at"))
        spared = (
            codex.is_protected_account(e.get("email"), e.get("account_id"), protected)
            if protected is not None
            else bool(e.get("is_primary_home"))
        )
        candidates.append(
            {
                "home": e["home"],
                "account_id": e.get("account_id"),
                "email": e.get("email"),
                "five_hour_used_percent": five_used,
                "weekly_used_percent": weekly_used,
                "weekly_reset_at": weekly.get("reset_at"),
                "stale": stale,
                "protected": spared,
                "as_of": e["windows"].get("as_of"),
                "score": (
                    round(-max(0.0, (weekly_reset - now).total_seconds()), 2)
                    if weekly_reset else None
                ),
                "headroom_score": round(headroom, 2),
                "in_flight": in_flight.get(("codex", str(e["home"])), 0),
            }
        )
    candidates.sort(
        key=lambda c: (
            c["weekly_reset_at"] is None,
            parse_iso(c["weekly_reset_at"]) or datetime.max.replace(tzinfo=now.tzinfo),
            c["stale"],
            c["home"],
        )
    )
    return candidates
