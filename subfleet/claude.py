"""Claude side: active-account identity, keychain OAuth probe, transcript error
scan, and the statusline tap state file.

Live-quota reality check (2026-07-11, revised 2026-08-12): Claude Code pipes
rate_limits (five_hour / seven_day used_percentage) into statusline commands
but caches nothing usable on disk, and the keychain OAuth token can be
server-invalid even while interactive sessions work (the app/harness holds its
own live auth). Statusline commands only run in the terminal CLI's interactive
TUI — the desktop app, SDK sessions, and `claude -p` never invoke them, so the
tap state can be weeks stale (last real capture 2026-07-22). Sources, freshest
first via pick_live_source:
- this run's keychain OAuth probe (best-effort, clearly labeled when invalid),
- the last successful probe payload (claude-oauth-raw.json read-back),
- the capacity live cache's active-account row,
- statusline tap state (terminal-TUI sessions only),
- transcript isApiErrorMessage events = observed hard limits with reset times.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import paths
from .util import from_epoch, iso, load_json, now_local, parse_iso, parse_reset_clock

OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"

LIMIT_PHRASES = ("hit your session limit", "usage limit", "hit your weekly limit", "rate limit")

# Lane dispatch gates (`subfleet pick claude`). A lane must clear this much headroom in
# EVERY probed window — unlike codex, the weekly window is a hard gate here,
# because a Claude account through its week rejects work however fresh its 5h
# window is.
DEFAULT_MIN_HEADROOM = 5.0
ACTIVE_HANDICAP = 10.0


def identity() -> dict:
    d = load_json(paths.claude_json(), {}) or {}
    acct = d.get("oauthAccount") or {}
    return {
        "email": acct.get("emailAddress"),
        "organization": acct.get("organizationName"),
        "account_uuid": acct.get("accountUuid"),
        "org_uuid": acct.get("organizationUuid"),
    }


def roster_config_path() -> Path:
    cfg_path = os.environ.get("SUBFLEET_CLAUDE_ACCOUNTS")
    return Path(cfg_path).expanduser() if cfg_path else Path(__file__).parent.parent / "claude-accounts.json"


def roster_config() -> dict:
    return load_json(roster_config_path(), {}) or {}


def known_accounts() -> list[str]:
    """Full Claude account roster: the committed claude-accounts.json config
    (authoritative, Max-confirmed) merged with whatever the desktop app's
    cc-mirror map has seen. Identity only — accounts without a local token are
    not probeable."""
    emails: set[str] = set()
    cfg = roster_config()
    emails.update(a for a in cfg.get("accounts", []) if isinstance(a, str) and "@" in a)
    d = load_json(paths.claude_dir() / "cc-mirror-accounts.json", {}) or {}
    emails.update(v.split(" (")[0] for v in d.values() if isinstance(v, str) and "@" in v)
    return sorted(emails)


def agent_secret_get(name: str, runner=subprocess.run) -> str | None:
    secret_bin = os.environ.get(
        "CLAUDE_LANE_AGENT_SECRET", str(Path.home() / "bin" / "agent-secret")
    )
    try:
        out = runner(
            [secret_bin, "get", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    tok = out.stdout.strip()
    return tok if out.returncode == 0 and tok else None


def agent_secret_names(runner=subprocess.run) -> set[str] | None:
    """List agent-keychain service names without reading secret values.

    Capacity only needs to know whether a configured lane can actually start.
    One cached list operation is both faster and less sensitive than fetching
    every setup token individually.
    """
    try:
        out = runner(
            [str(Path.home() / "bin" / "agent-secret"), "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return {
        line.split("\t", 1)[0].strip()
        for line in out.stdout.splitlines()
        if line.split("\t", 1)[0].strip()
    }


def accounts_report(active_email: str | None, timeout: float = 15.0,
                    opener=None, secret_runner=subprocess.run) -> list[dict]:
    """Per-account Claude quota, roster order (active first). Enrolled accounts
    are probed with their stored setup-token; everything else is identity-only.
    Never fabricates: an unprobeable account carries enrolled=False and no
    numbers."""
    cfg = roster_config()
    enrolled = {
        k: v for k, v in (cfg.get("enrolled") or {}).items() if isinstance(v, str) and v
    }
    rows = []
    for email in known_accounts():
        row = {"email": email, "active": email == active_email, "enrolled": email in enrolled}
        if row["enrolled"]:
            token = agent_secret_get(enrolled[email], runner=secret_runner)
            if token:
                row["probe"] = {
                    k: v
                    for k, v in probe_oauth_usage(token, timeout=timeout, opener=opener).items()
                    if k != "raw"
                }
            else:
                row["probe"] = {"status": "secret-missing", "secret": enrolled[email]}
        rows.append(row)
    rows.sort(key=lambda r: (not r["active"], not r["enrolled"], r["email"]))
    return rows


# ---------------------------------------------------------------------------
# Session-mirror health. subfleet mirror (launchd, every 60s) keeps the
# desktop app's account-scoped sidebar indexes in sync so login switches never
# hide sessions — the interactive-side sibling of the headless lanes. If the
# job dies, mirroring stops SILENTLY and Max only notices at the next account
# switch; the per-pass state sidecar's mtime is its heartbeat.
# ---------------------------------------------------------------------------

MIRROR_JOB_LABEL = "com.maxghenis.cos.subfleet-mirror"
MIRROR_STALL_MIN = 10.0  # job runs every 60s; 10 idle minutes = suspicious
# Heartbeat = the state sidecar, rewritten at the END of every pass (the log
# is silent on no-op runs under --quiet — using it fired a false "stalled" on
# 2026-08-19 07:08 during a quiet stretch). A heavy pass (observed 2026-08-18:
# 8.5 min mid-run during app-churn re-seeding) still looks idle until it
# finishes, so a live in-run process suppresses "stalled" until it has itself
# run absurdly long.
MIRROR_HANG_MIN = 30.0


def _mirror_job_loaded(runner=subprocess.run) -> bool | None:
    """Is the launchd job loaded? None when launchctl can't answer."""
    try:
        out = runner(
            ["launchctl", "print", f"gui/{os.getuid()}/{MIRROR_JOB_LABEL}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.returncode == 0


def _parse_etime(s: str) -> float | None:
    """ps etime ('SS', 'MM:SS', 'HH:MM:SS', 'DD-HH:MM:SS') -> minutes."""
    s = s.strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        d, _, s = s.partition("-")
        try:
            days = int(d)
        except ValueError:
            return None
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        h, m, sec = 0, 0, nums[0]
    elif len(nums) == 2:
        h, (m, sec) = 0, nums
    elif len(nums) == 3:
        h, m, sec = nums
    else:
        return None
    return round(days * 1440 + h * 60 + m + sec / 60, 1)


def _mirror_run_minutes(runner=subprocess.run) -> float | None:
    """Minutes the currently-running mirror process has been alive; None when
    no run is in flight (or the probe can't answer)."""
    try:
        out = runner(["pgrep", "-f", "bin/subfleet-mirror"],
                     capture_output=True, text=True, timeout=10)
        pids = [p for p in out.stdout.split() if p.isdigit()]
        if not pids:
            return None
        out = runner(["ps", "-o", "etime=", "-p", pids[0]],
                     capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_etime(out.stdout)


def session_mirror_health(log_path: Path | None = None, job_probe=None,
                          run_probe=None, now: datetime | None = None) -> dict:
    """status: 'healthy' | 'running' | 'stalled' | 'absent'.

    `log_path` is the HEARTBEAT file (default: the per-pass state sidecar,
    paths.cc_mirror_heartbeat_path(); the parameter name predates the switch).
    absent (no heartbeat file at all — not installed, or an isolated test env)
    is reported but never alerted on. running = heartbeat idle but a live
    in-run process under the hang threshold (long passes are normal under app
    churn). stalled — the silent-failure case the watchdog exists for — means
    the job is unloaded, the heartbeat is idle with no run in flight, or an
    in-flight run has hung past MIRROR_HANG_MIN."""
    log_path = log_path or paths.cc_mirror_heartbeat_path()
    now = now or now_local()
    try:
        mtime = log_path.stat().st_mtime
    except OSError:
        return {"status": "absent", "log": str(log_path), "age_min": None,
                "run_min": None, "job_loaded": None, "as_of": iso(now)}
    age_min = round((now.timestamp() - mtime) / 60, 1)
    job_loaded = (job_probe or _mirror_job_loaded)()
    run_min = (run_probe or _mirror_run_minutes)()
    if job_loaded is False:
        status = "stalled"
    elif age_min <= MIRROR_STALL_MIN:
        status = "healthy"
    elif run_min is not None and run_min <= MIRROR_HANG_MIN:
        status = "running"
    else:
        status = "stalled"
    return {
        "status": status,
        "log": str(log_path),
        "age_min": age_min,
        "run_min": run_min,
        "job_loaded": job_loaded,
        "as_of": iso(now),
    }


# ---------------------------------------------------------------------------
# Lane ranking (`subfleet pick claude`). A "lane" is an enrolled account: its setup-token
# lets a headless worker pin identity via CLAUDE_CODE_OAUTH_TOKEN, so dispatch
# routes around the active login's limits instead of rotating logins.
# ---------------------------------------------------------------------------


def _lane_windows(row: dict) -> dict[str, dict]:
    """Gating windows (five_hour, seven_day) from an account row's probe —
    only windows the server actually reported, never fabricated."""
    probe = row.get("probe") or {}
    out = {}
    for key in ("five_hour", "seven_day"):
        w = probe.get(key)
        if isinstance(w, dict) and w.get("used_percent") is not None:
            out[key] = w
    return out


def lane_verdict(row: dict, min_headroom: float = DEFAULT_MIN_HEADROOM) -> str:
    """Dispatchability verdict for one account row (from accounts_report)."""
    if not row.get("enrolled"):
        return "not-enrolled"
    status = (row.get("probe") or {}).get("status")
    if status != "ok":
        return status or "no-probe"
    windows = _lane_windows(row)
    if not windows:
        return "no-window-data"
    if any(float(w["used_percent"]) >= 100.0 - min_headroom for w in windows.values()):
        return "exhausted"
    return "ok"


def _lane_reset_at(row: dict, min_headroom: float) -> str | None:
    """When an exhausted lane becomes dispatchable again: every over-threshold
    window must reset, so the governing reset is the LATEST among them."""
    resets = [
        parse_iso(w.get("reset_at"))
        for w in _lane_windows(row).values()
        if float(w["used_percent"]) >= 100.0 - min_headroom
    ]
    resets = [r for r in resets if r]
    return iso(max(resets)) if resets else None


def rank_lanes(rows: list[dict], handicap: float = ACTIVE_HANDICAP,
               min_headroom: float = DEFAULT_MIN_HEADROOM) -> list[dict]:
    """Order dispatchable Claude lanes by worst-window usage.

    Unlike Codex's reset-order waterfall, Claude retains the active-account
    handicap so agent dispatch spares the desktop login. A lane 80% through
    its week ranks behind a lane at 30% however idle its 5h window.
    """
    ranked = []
    for row in rows:
        if lane_verdict(row, min_headroom=min_headroom) != "ok":
            continue
        windows = _lane_windows(row)
        used = {k: float(w["used_percent"]) for k, w in windows.items()}
        effective = max(used.values())
        score = effective + (handicap if row.get("active") else 0.0)
        ranked.append(
            {
                "email": row["email"],
                "active": bool(row.get("active")),
                "five_hour_used_percent": used.get("five_hour"),
                "weekly_used_percent": used.get("seven_day"),
                "effective_used_percent": effective,
                "five_hour_reset_at": (windows.get("five_hour") or {}).get("reset_at"),
                "weekly_reset_at": (windows.get("seven_day") or {}).get("reset_at"),
                "as_of": (row.get("probe") or {}).get("checked_at"),
                "score": round(score, 2),
            }
        )
    ranked.sort(key=lambda r: (r["score"], r["weekly_used_percent"] or 0.0, r["email"]))
    return ranked


def lanes_fleet(rows: list[dict], handicap: float = ACTIVE_HANDICAP,
                min_headroom: float = DEFAULT_MIN_HEADROOM) -> dict:
    """Fleet summary + per-lane verdicts for the snapshot, table, watchdog, and
    menu bar app. earliest_reset is the soonest any currently-exhausted lane
    becomes dispatchable."""
    now = now_local()
    ranked = rank_lanes(rows, handicap=handicap, min_headroom=min_headroom)
    lanes = []
    future_resets = []
    for row in rows:
        if not row.get("enrolled"):
            continue
        verdict = lane_verdict(row, min_headroom=min_headroom)
        windows = _lane_windows(row)

        def pct(key):
            w = windows.get(key)
            return float(w["used_percent"]) if w else None

        lane = {
            "email": row["email"],
            "active": bool(row.get("active")),
            "verdict": verdict,
            "five_hour_used_percent": pct("five_hour"),
            "weekly_used_percent": pct("seven_day"),
            "five_hour_reset_at": (windows.get("five_hour") or {}).get("reset_at"),
            "weekly_reset_at": (windows.get("seven_day") or {}).get("reset_at"),
            "reset_at": _lane_reset_at(row, min_headroom) if verdict == "exhausted" else None,
        }
        lanes.append(lane)
        reset = parse_iso(lane["reset_at"])
        if reset and reset > now:
            future_resets.append(reset)
    return {
        "enrolled": len(lanes),
        "dispatchable_now": len(ranked),
        "best": ranked[0]["email"] if ranked else None,
        "earliest_reset": iso(min(future_resets)) if future_resets else None,
        "lanes": lanes,
    }


def keychain_credentials(runner=subprocess.run) -> dict:
    """Read the Claude Code OAuth blob from the login keychain (single targeted
    item read — never a keychain dump). Token returned under a private key."""
    try:
        out = runner(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"status": "error", "error": str(e)}
    if out.returncode != 0:
        return {"status": "missing", "error": out.stderr.strip()[:200]}
    try:
        blob = json.loads(out.stdout.strip())
        oauth = blob.get("claudeAiOauth") or {}
    except json.JSONDecodeError:
        return {"status": "unparseable"}
    return {
        "status": "ok",
        "subscription": oauth.get("subscriptionType"),
        "tier": oauth.get("rateLimitTier"),
        "expires_at": iso(from_epoch((oauth.get("expiresAt") or 0) / 1000)),
        "_token": oauth.get("accessToken"),
    }


def probe_oauth_usage(token: str | None, timeout: float = 15.0, opener=None) -> dict:
    """Best-effort GET against the OAuth usage endpoint. The stored token is
    frequently stale (expired 19 days at probe time on 2026-07-11) — an
    'invalid' result here is NOT an outage, just an unusable local token."""
    checked_at = iso(now_local())
    if not token:
        return {"status": "no-token", "checked_at": checked_at}
    req = urllib.request.Request(
        OAUTH_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "subfleet/0.1",
        },
    )

    def default_opener(request, t):
        with urllib.request.urlopen(request, timeout=t) as r:
            return r.status, r.read()

    opener = opener or default_opener
    try:
        _, body = opener(req, timeout)
        data = json.loads(body.decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"status": "token-invalid", "checked_at": checked_at}
        if e.code == 429:
            # Auth passed (bad tokens 401): the account is currently
            # rate-limited or the endpoint is throttling this account.
            return {"status": "rate-limited", "checked_at": checked_at}
        return {"status": f"http-{e.code}", "checked_at": checked_at}
    except (OSError, ValueError) as e:
        return {"status": "network-error", "checked_at": checked_at, "error": str(e)}
    result = {"status": "ok", "checked_at": checked_at, "raw": data}
    result.update(parse_usage_payload(data))
    return result


def parse_usage_payload(data) -> dict:
    """Extract windows/limits/spend from a usage-endpoint payload. Shared by
    the live probe and the cached-payload read-back so the two can never drift.
    """
    result: dict = {}
    # Schema per claude 2.1.205 binary: five_hour, seven_day, plus per-model
    # buckets (seven_day_opus, seven_day_sonnet, ...). Extract generically.
    windows = {}
    if isinstance(data, dict):
        for key, w in data.items():
            if key == "five_hour" or key.startswith("seven_day"):
                if isinstance(w, dict):
                    pct = w.get("used_percentage", w.get("utilization"))
                    if pct is not None:
                        windows[key] = {"used_percent": pct, "reset_at": w.get("resets_at")}
    result["windows"] = windows
    for key in ("five_hour", "seven_day"):
        if key in windows:
            result[key] = windows[key]
    raw_limits = data.get("limits") if isinstance(data, dict) else None
    limits = []
    if isinstance(raw_limits, list):
        for limit in raw_limits:
            if not isinstance(limit, dict):
                continue
            scope = limit.get("scope")
            scope = scope if isinstance(scope, dict) else {}
            model = scope.get("model")
            model = model if isinstance(model, dict) else {}
            limits.append(
                {
                    "kind": limit.get("kind"),
                    "group": limit.get("group"),
                    "percent": limit.get("percent"),
                    "severity": limit.get("severity"),
                    "resets_at": limit.get("resets_at"),
                    "is_active": limit.get("is_active"),
                    "scope_model": model.get("display_name"),
                    "scope_surface": scope.get("surface"),
                }
            )
    result["limits"] = limits

    def monetary_usage(value):
        if not isinstance(value, dict):
            return None
        used = value.get("used")
        used = used if isinstance(used, dict) else {}
        return {
            "used_minor": used.get("amount_minor", value.get("used_credits")),
            "currency": used.get("currency", value.get("currency")),
            "exponent": used.get("exponent", value.get("decimal_places")),
            "enabled": value.get("enabled", value.get("is_enabled")),
        }

    if (spend := monetary_usage(data.get("spend") if isinstance(data, dict) else None)) is not None:
        result["spend"] = spend
    if (
        extra_usage := monetary_usage(
            data.get("extra_usage") if isinstance(data, dict) else None
        )
    ) is not None:
        result["extra_usage"] = extra_usage
    return result


def last_oauth_reading() -> dict | None:
    """Most recent successful usage-endpoint payload, persisted by the snapshot
    builder on every 200 (claude-oauth-raw.json). The freshest survivor when
    the current probe fails — the keychain token routinely goes stale between
    desktop sessions while the last good reading is only hours old."""
    d = load_json(paths.oauth_raw_path())
    if not isinstance(d, dict) or d.get("raw") is None:
        return None
    reading = {"status": "ok", "checked_at": d.get("checked_at")}
    reading.update(parse_usage_payload(d.get("raw")))
    return reading


def capacity_cached_reading(email: str | None = None) -> dict | None:
    """Active-account usage from the capacity live cache (refreshed by
    capacity.collect callers: delegate, the picks, `subfleet claude`). Returns a
    live-shaped dict or None. Only windows with live confidence qualify —
    ledger-estimated percentages describe a lane token's headless traffic, not
    the desktop login."""
    d = load_json(paths.capacity_cache_path())
    if not isinstance(d, dict):
        return None
    for row in d.get("accounts") or []:
        if not (isinstance(row, dict) and row.get("family") == "claude" and row.get("active")):
            continue
        if email and row.get("email") and row.get("email") != email:
            continue
        pcts = {}
        for src_key, dst_key in (("five_hour", "five_hour_pct"), ("weekly", "seven_day_pct")):
            w = row.get(src_key)
            if (isinstance(w, dict) and w.get("confidence") == "live"
                    and w.get("used_percent") is not None):
                pcts[dst_key] = w["used_percent"]
        if not pcts:
            return None
        return {
            "five_hour_pct": pcts.get("five_hour_pct"),
            "seven_day_pct": pcts.get("seven_day_pct"),
            "source": "capacity-cache",
            "as_of": d.get("probed_at"),
        }
    return None


# A usage reading older than this is context, not a headline: renderers demote
# it to a "last reading … stale" line and lead with the derived model instead.
LIVE_STALE_AFTER_MIN = 180.0


def pick_live_source(candidates: list[dict | None], now: datetime | None = None,
                     stale_after_min: float = LIVE_STALE_AFTER_MIN) -> dict | None:
    """Freshest usable usage reading for the active account.

    Candidates are live-shaped dicts ({five_hour_pct, seven_day_pct, source,
    as_of, ...}). The newest parseable as_of wins among those carrying at least
    one percentage; earlier list position breaks exact-timestamp ties. The
    winner is annotated with age_min and stale so renderers can demote old
    readings instead of headlining them."""
    now = now or now_local()
    usable = []
    for cand in candidates:
        if not cand:
            continue
        if cand.get("five_hour_pct") is None and cand.get("seven_day_pct") is None:
            continue
        as_of = parse_iso(cand.get("as_of"))
        if as_of is None:
            continue
        usable.append((as_of, cand))
    if not usable:
        return None
    as_of, best = max(usable, key=lambda pair: pair[0])
    age_min = round((now - as_of).total_seconds() / 60, 1)
    return {**best, "age_min": age_min, "stale": age_min > stale_after_min}


def statusline_state(max_age_min: float = 30) -> dict | None:
    """Latest rate_limits captured by the statusline tap, with freshness label.

    The tap only fires in terminal-TUI sessions (the desktop app and headless
    runs never invoke statusLine commands), so this state can be weeks old —
    callers must rank it by updated_at, never treat it as current."""
    d = load_json(paths.statusline_state_path())
    if not d:
        return None
    updated = parse_iso(d.get("updated_at"))
    age_min = None
    if updated:
        age_min = round((now_local() - updated).total_seconds() / 60, 1)
    rl = d.get("rate_limits") or {}

    def pct(key):
        w = rl.get(key) or {}
        v = w.get("used_percentage")
        return round(v, 1) if isinstance(v, (int, float)) else None

    def reset(key):
        w = rl.get(key) or {}
        return iso(from_epoch(w.get("resets_at")))

    return {
        "updated_at": d.get("updated_at"),
        "age_min": age_min,
        "fresh": age_min is not None and age_min <= max_age_min,
        "five_hour_pct": pct("five_hour"),
        "seven_day_pct": pct("seven_day"),
        "five_hour_reset_at": reset("five_hour"),
        "seven_day_reset_at": reset("seven_day"),
        "raw": rl,
    }


def _candidate_transcripts(hours: float) -> list[Path]:
    projects = paths.claude_dir() / "projects"
    if not projects.is_dir():
        return []
    cutoff = (now_local() - timedelta(hours=hours)).timestamp()
    out = []
    for proj in projects.iterdir():
        if not proj.is_dir():
            continue
        try:
            for f in proj.iterdir():
                if f.suffix == ".jsonl" and f.stat().st_mtime >= cutoff:
                    out.append(f)
        except OSError:
            continue
    return out


def transcript_limit_events(hours: float = 24, runner=subprocess.run) -> list[dict]:
    """Observed limit errors across recent Claude Code transcripts (all projects,
    including subagent lanes). Two-stage: grep (fast C scan of big files) for
    isApiErrorMessage lines, then JSON-parse just those lines."""
    files = _candidate_transcripts(hours)
    if not files:
        return []
    cutoff = now_local() - timedelta(hours=hours)
    events = []
    for chunk_start in range(0, len(files), 50):
        chunk = [str(f) for f in files[chunk_start : chunk_start + 50]]
        try:
            out = runner(
                ["grep", "-h", "isApiErrorMessage", *chunk],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in out.stdout.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not d.get("isApiErrorMessage"):
                continue
            ts = parse_iso(d.get("timestamp"))
            if ts is None or ts < cutoff:
                continue
            content = (d.get("message") or {}).get("content") or []
            text = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            ).strip()
            lowered = text.lower()
            if "session limit" in lowered:
                kind = "session-limit"
            elif "weekly limit" in lowered:
                kind = "weekly-limit"
            elif "usage limit" in lowered:
                kind = "usage-limit"
            elif d.get("apiErrorStatus") == 429 or "rate limit" in lowered:
                kind = "rate-limit"
            else:
                continue
            reset = parse_reset_clock(text, ts) if "reset" in lowered or "try again" in lowered else None
            events.append(
                {
                    "observed_at": iso(ts),
                    "kind": kind,
                    "text": text[:160],
                    "reset_at": iso(reset),
                    "session": d.get("sessionId"),
                }
            )
    events.sort(key=lambda e: e["observed_at"], reverse=True)
    # Collapse repeats: same kind+reset clock -> most recent sighting + count.
    collapsed: dict[tuple, dict] = {}
    for e in events:
        key = (e["kind"], e["reset_at"] or e["text"])
        if key in collapsed:
            collapsed[key]["count"] += 1
            sessions = collapsed[key].setdefault("sessions", set())
            sessions.add(e.get("session"))
        else:
            collapsed[key] = {**e, "count": 1, "sessions": {e.get("session")}}
    result = []
    for e in collapsed.values():
        e["sessions"] = len({s for s in e["sessions"] if s})
        result.append(e)
    result.sort(key=lambda e: e["observed_at"], reverse=True)
    return result


def active_limit(events: list[dict], now: datetime | None = None) -> dict | None:
    """The most recent observed limit whose reset time is still in the future."""
    now = now or now_local()
    for e in events:
        reset = parse_iso(e.get("reset_at"))
        if reset and reset > now:
            return e
    return None


def current_weekly_reset(observed_reset_iso: str | None, now: datetime | None = None) -> dict | None:
    """Roll an observed weekly resets_at forward to the current cycle.
    Observed anchors stay valid while the cycle is unbroken; a rolled-forward
    value is labeled derived."""
    anchor = parse_iso(observed_reset_iso)
    if anchor is None:
        return None
    now = now or now_local()
    reset = anchor
    derived = False
    while reset <= now:
        reset += timedelta(days=7)
        derived = True
    return {"reset_at": iso(reset), "confidence": "derived" if derived else "observed",
            "anchor": observed_reset_iso}


def _activity_intervals(hours: float = 48) -> list[tuple[datetime, datetime]]:
    """Coarse per-transcript activity spans: first event timestamp (head of
    file, else mtime) through mtime. Good to ~minutes, which is enough for
    5h-window inference."""
    out = []
    cutoff = now_local() - timedelta(hours=hours)
    for f in _candidate_transcripts(hours):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime).astimezone()
        except OSError:
            continue
        start = None
        try:
            with open(f, errors="ignore") as fh:
                head = fh.read(120000)
            i = head.find('"timestamp":"')
            if i > -1:
                start = parse_iso(head[i + 13 : i + 45].split('"')[0])
        except OSError:
            pass
        start = (start or mtime).astimezone()
        if start < cutoff:
            start = cutoff
        if start <= mtime:
            out.append((start, mtime))
    out.sort()
    return out


def derive_five_hour_window(now: datetime | None = None, hours: float = 48,
                            intervals: list[tuple[datetime, datetime]] | None = None) -> dict | None:
    """Estimate the active account's current 5h window from session activity.

    Model (validated against the 2026-07-22 statusline observation: continuous
    morning activity, window reset 4:50pm => opened ~11:50am on a chained
    boundary): a window opens at the first message after the previous window
    expired; under continuous activity windows chain back-to-back. Returns
    None when there is no activity-supported open window. Always labeled
    derived — an estimate, not a server reading."""
    now = now or now_local()
    intervals = _activity_intervals(hours) if intervals is None else sorted(intervals)
    window = timedelta(hours=5)
    w = None
    for start, end in intervals:
        if w is None or start >= w + window:
            w = start
        while end >= w + window:
            w = w + window
    if w is None or now >= w + window:
        return None
    return {"window_start": iso(w), "reset_at": iso(w + window), "confidence": "derived"}
