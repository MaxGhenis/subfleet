"""Claude identity, enrollment state, and limit-event scans."""

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import config, paths, secret_store
from .util import from_epoch, iso, load_json, now_local, parse_iso, parse_reset_clock

OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"

LIMIT_PHRASES = ("hit your session limit", "usage limit", "hit your weekly limit", "rate limit")

# Legacy lane-ranking gates. A lane must clear this much headroom in
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
    return config.accounts_path()


def roster_config() -> dict:
    return config.load()


def known_accounts() -> list[str]:
    """Full configured roster; accounts without a token remain identity-only."""
    cfg = roster_config()
    return sorted({a for a in cfg.get("accounts", []) if isinstance(a, str) and "@" in a})


def agent_secret_get(name: str, runner=None) -> str | None:
    return secret_store.get(name, runner=runner)


def accounts_report(active_email: str | None, timeout: float = 15.0,
                    opener=None, secret_runner=None) -> list[dict]:
    """Return enrollment state in roster order, with the active account first.

    Setup tokens are inference credentials, not quota-observation credentials.
    Keep the historical arguments for callers, but never send a stored setup
    token to the OAuth usage endpoint. Live dispatch capacity comes from the
    local usage ledger instead.
    """
    del timeout, opener
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
                row["probe"] = {"status": "inference-only"}
            else:
                row["probe"] = {"status": "secret-missing", "secret": enrolled[email]}
        rows.append(row)
    rows.sort(key=lambda r: (not r["active"], not r["enrolled"], r["email"]))
    return rows


MIRROR_STALL_MIN = 10.0
MIRROR_HANG_MIN = 30.0


def _mirror_job_loaded(runner=subprocess.run) -> bool | None:
    """Probe an optional configured scheduler job; None means unavailable."""
    label = config.mirror_job_label()
    if not label:
        return None
    try:
        result = runner(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def _parse_etime(value: str) -> float | None:
    """Convert ps etime (SS, MM:SS, HH:MM:SS, DD-HH:MM:SS) to minutes."""
    value = value.strip()
    if not value:
        return None
    days = 0
    if "-" in value:
        raw_days, _, value = value.partition("-")
        try:
            days = int(raw_days)
        except ValueError:
            return None
    try:
        parts = [int(part) for part in value.split(":")]
    except ValueError:
        return None
    if len(parts) == 1:
        hours, minutes, seconds = 0, 0, parts[0]
    elif len(parts) == 2:
        hours, (minutes, seconds) = 0, parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        return None
    return round(days * 1440 + hours * 60 + minutes + seconds / 60, 1)


def _mirror_run_minutes(runner=subprocess.run) -> float | None:
    """Return the age of an in-flight mirror pass, if one can be observed."""
    try:
        result = runner(
            ["pgrep", "-f", "bin/carpool-mirror"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        pids = [pid for pid in result.stdout.split() if pid.isdigit()]
        if not pids:
            return None
        result = runner(
            ["ps", "-o", "etime=", "-p", pids[0]],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_etime(result.stdout)


def session_mirror_health(
    log_path: Path | None = None,
    job_probe=None,
    run_probe=None,
    now: datetime | None = None,
) -> dict:
    """Report mirror health from its per-pass state sidecar heartbeat.

    A stale heartbeat is tolerated while a pass is in flight, until that pass
    exceeds the 30-minute hang cutoff. Missing heartbeats mean the optional
    mirror is absent and never cause an alert.
    """
    heartbeat = log_path or paths.cc_mirror_heartbeat_path()
    now = now or now_local()
    try:
        mtime = heartbeat.stat().st_mtime
    except OSError:
        return {
            "status": "absent",
            "log": str(heartbeat),
            "age_min": None,
            "run_min": None,
            "job_loaded": None,
            "as_of": iso(now),
        }
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
        "log": str(heartbeat),
        "age_min": age_min,
        "run_min": run_min,
        "job_loaded": job_loaded,
        "as_of": iso(now),
    }


# ---------------------------------------------------------------------------
# Legacy lane-ranking helpers. A "lane" is an enrolled account: its setup token
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
    """Order dispatchable lanes best-first. Mirrors the
    codex rank: gate on headroom, score by usage, and handicap the account
    backing the active desktop login so agent dispatch spares it. The score is
    the WORST window's usage — a lane 80% through its week ranks behind a lane
    at 30% however idle its 5h window."""
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
    """Fleet summary + per-lane verdicts for snapshots and monitoring.

    ``earliest_reset`` is the soonest any exhausted lane becomes dispatchable.
    """
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
            "User-Agent": "carpool/0.1",
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
    return result


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
