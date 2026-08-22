"""Assemble the full cross-account snapshot: codex homes + Claude, with honest
verdicts. Every number carries its provenance (live probe vs observed-at)."""

from datetime import timedelta
from pathlib import Path

from . import claude, codex, paths
from .util import atomic_write_json, iso, now_local, parse_iso, strip_private

# A home is dispatchable only with at least this much 5h-window headroom.
DEFAULT_MIN_HEADROOM = 5.0


def _codex_verdict(auth: dict, probe: dict, observed: dict | None) -> str:
    if auth.get("status") != "ok":
        return "no-auth"
    status = probe.get("status")
    if status == "token-revoked":
        return "auth-revoked"
    if status == "ok":
        if probe.get("limit_reached") or (probe.get("allowed") is False):
            return "limited"
        return "ok"
    # http-4xx on the usage endpoint with valid-looking local auth: auth trouble.
    if status and status.startswith("http-4"):
        return "auth-suspect"
    return "unknown"


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
        verdict = _codex_verdict(auth, probe_result, observed)
        acct = auth.get("account_id")
        if acct:
            by_account.setdefault(acct, []).append(str(home))
        # Effective window data: live if we have it, else last observed.
        if probe_result.get("status") == "ok":
            eff = {"primary": probe_result.get("primary"),
                   "secondary": probe_result.get("secondary"),
                   "source": "live", "as_of": probe_result.get("checked_at")}
        elif observed:
            eff = {"primary": observed.get("primary"),
                   "secondary": observed.get("secondary"),
                   "source": "observed", "as_of": observed.get("observed_at")}
        else:
            eff = {"primary": None, "secondary": None, "source": "none", "as_of": None}
        home_entries.append(
            {
                "home": str(home),
                "is_primary_home": home == paths.primary_codex_home(),
                "account_id": acct,
                "email": auth.get("email") or probe_result.get("email"),
                "plan": probe_result.get("plan_type") or auth.get("plan"),
                "auth_last_refresh": auth.get("last_refresh"),
                "verdict": verdict,
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

    dispatchable = [
        e for e in home_entries
        if e["verdict"] == "ok" and not e["duplicate_of"]
        and _headroom(e) is not None and _headroom(e) >= DEFAULT_MIN_HEADROOM
    ]
    resets = [
        parse_iso((e["windows"].get("primary") or {}).get("reset_at"))
        for e in home_entries
        if e["verdict"] in ("limited", "ok") and e["windows"].get("primary")
    ]
    future_resets = [r for r in resets if r and r > now]

    codex_section = {
        "homes": home_entries,
        "duplicates": duplicates,
        "fleet": {
            "total_homes": len(home_entries),
            "dispatchable_now": len(dispatchable),
            "best_home": dispatchable_best(home_entries),
            "earliest_reset": iso(min(future_resets)) if future_resets else None,
        },
    }

    # Claude
    ident = claude.identity()
    creds = claude.keychain_credentials()
    active_email = ident.get("email")
    enrolled_map = claude.roster_config().get("enrolled") or {}
    # One consumer per token bucket: once the active account has a dedicated
    # enrolled token, the enrollment probe (accounts_report) owns it and the
    # keychain-token probe stands down.
    if not live:
        cprobe = {"status": "skipped"}
    elif active_email in enrolled_map:
        cprobe = {"status": "delegated-to-enrollment"}
    else:
        cprobe = (claude_probe_fn or claude.probe_oauth_usage)(creds.get("_token"), timeout=timeout)
    events = claude.transcript_limit_events(hours=transcript_hours)
    active = claude.active_limit(events, now=now)

    accounts = claude.accounts_report(active_email, timeout=timeout) if live else []

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
    # discovery for the percentage extraction (local file, no tokens).
    if cprobe.get("status") == "ok" and cprobe.get("raw") is not None:
        try:
            atomic_write_json(paths.oauth_raw_path(),
                              {"checked_at": cprobe.get("checked_at"), "raw": cprobe["raw"]})
        except OSError:
            pass

    active_live = _probe_live(cprobe, "oauth")
    active_probe_status = cprobe.get("status")
    for row in accounts:
        if not row["active"]:
            continue
        # The native login-token probe owns the active account unless that
        # account has a dedicated enrolled token.
        row_live = active_live or _probe_live(row.get("probe") or {}, "oauth-enrolled")
        if row_live:
            row["live"] = row_live
            active_live = row_live
        if (row.get("probe") or {}).get("status"):
            active_probe_status = row["probe"]["status"]
        row["oauth_status"] = active_probe_status

    # Live data supersedes transcript inference: an observed "session limit"
    # error with a future reset can be stale (a new window opened since).
    # Only keep it ACTIVE when live 5h usage corroborates.
    if active and active_live and (active_live.get("five_hour_pct") or 0) < 95 \
            and active_live.get("five_hour_pct") is not None:
        active = None

    if active:
        claude_verdict = "limited"
    elif active_live is not None:
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
        "lanes": claude.lanes_fleet(accounts),
        "known_accounts": claude.known_accounts(),
        "subscription": creds.get("subscription"),
        "tier": creds.get("tier"),
        "keychain": {k: v for k, v in creds.items() if not k.startswith("_")},
        "oauth_probe": {k: v for k, v in cprobe.items() if k != "raw"},
        "recent_errors": events[:8],
        "active_limit": active,
        "verdict": claude_verdict,
    }

    return strip_private(
        {
            "generated_at": iso(now),
            "codex": codex_section,
            "claude": claude_section,
        }
    )


def _headroom(entry: dict) -> float | None:
    primary = entry.get("windows", {}).get("primary")
    if not primary or primary.get("used_percent") is None:
        return None
    return 100.0 - float(primary["used_percent"])


def dispatchable_best(home_entries: list[dict], handicap: float = 10.0,
                      min_headroom: float = DEFAULT_MIN_HEADROOM,
                      stale_max_min: float = 30.0) -> str | None:
    ranked = rank_for_dispatch(home_entries, handicap=handicap,
                               min_headroom=min_headroom, stale_max_min=stale_max_min)
    return ranked[0]["home"] if ranked else None


def rank_for_dispatch(home_entries: list[dict], handicap: float = 10.0,
                      min_headroom: float = DEFAULT_MIN_HEADROOM,
                      stale_max_min: float = 30.0) -> list[dict]:
    """Order dispatchable homes best-first. Distinct-account aware (duplicate
    homes excluded), window-aware, with a configurable handicap that spares the
    primary home (its account may back interactive ChatGPT/Codex apps).

    A home with a failed live probe still qualifies on rollout data observed
    within `stale_max_min`, flagged stale so callers can decide."""
    now = now_local()
    candidates = []
    for e in home_entries:
        if e.get("duplicate_of"):
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
        used = 100.0 - headroom
        score = used + (handicap if e.get("is_primary_home") else 0.0)
        secondary = (e["windows"].get("secondary") or {}).get("used_percent") or 0.0
        candidates.append(
            {
                "home": e["home"],
                "account_id": e.get("account_id"),
                "email": e.get("email"),
                "five_hour_used_percent": used,
                "weekly_used_percent": secondary,
                "stale": stale,
                "as_of": e["windows"].get("as_of"),
                "score": round(score, 2),
            }
        )
    candidates.sort(key=lambda c: (c["stale"], c["score"], c["weekly_used_percent"]))
    return candidates
