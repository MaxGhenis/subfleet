"""Fable reserve: refuse non-Fable Claude dispatch that would spend Fable.

A Claude Max account has two weekly buckets: the shared all-models window
(``weekly_all``, drawn by every model) and a Fable-scoped window
(``weekly_scoped`` whose scope model displays as "Fable", drawn by Fable
alone). Fable draws on both, so every Opus/Sonnet/Haiku turn on an account
that still has Fable headroom shrinks the Fable that account can deliver this
week. Observed: 2026-09-06 farness/policybench at 94% shared / 49% Fable;
2026-09-08 axiom.org 100/27, maxghenis.com 100/63, rulesfoundation.org 100/50,
axiom-foundation.org 100/85 -- all Opus dispatch that ran before Fable.

Rule (Max, 2026-09-06: "we can use opus before fable is exhausted but not to
the degree where it'd cost us fable"): a non-Fable Claude model may run on an
account only while the shared window keeps ``cap_ratio`` times the Fable
window's remaining share, plus ``min_slack``, in hand::

    slack = (1 - shared_used) - cap_ratio * (1 - fable_used) >= min_slack

An unmeasured account is *reserved* (a missing measurement is not evidence of
slack). An account whose usage payload carries no Fable window (a Team seat)
has nothing to protect and is *open*. Mirrors subfleet-v2
``scheduler.reserve_verdict`` and its ``default_policy.json``.

Readings come from the v2 login dirs (``~/.subfleet/logins/<email>/``): a
full-scope ``claude auth login`` whose OAuth access token can read the usage
endpoint. Setup tokens (``claude-quota-<email>``) cannot -- they get a
standing 429 -- which is why v1 lanes were blind and this module reads the
login dirs instead. Access tokens last hours; an expired one is healed by a
detached one-word Fable request in that config dir (the CLI refreshes on
use), never by this process handling the refresh token itself. No token value
is ever written to the cache, a log, or a decision record.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import paths
from . import claude as claude_side
from .util import iso, load_json, now_local

FABLE_DISPLAY = "Fable"
KEYCHAIN_PREFIX = "Claude Code-credentials-"

#: Code default; ``reserve-policy.json`` in the state dir overrides keys.
#: There is deliberately no environment kill-switch: turning the reserve off is
#: an operator action (write ``{"enabled": false}`` to the policy file), not a
#: prefix an agent can add to a command.
DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "cap_ratio": 2.0,
    "min_slack": 0.05,
    "reading_ttl_s": 300.0,
    "usage_spacing_s": 3.0,
    "heal_interval_s": 1200.0,
    "heals_per_call": 3,
}

STATE_SLACK = "slack"
STATE_RESERVED = "reserved"
STATE_UNMEASURED = "unmeasured"
STATE_NO_FABLE = "no-fable"

#: Process-local pacing so a burst of dispatches never fires back-to-back GETs
#: (the endpoint penalises bursts with a standing 429).
_last_get_monotonic: float | None = None


def policy(path: Path | None = None) -> dict[str, Any]:
    """Effective reserve policy: defaults overlaid by the state-dir file."""
    merged = copy.deepcopy(DEFAULT_POLICY)
    raw = load_json(path or paths.reserve_policy_path(), None)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in merged and isinstance(value, (int, float, bool)):
                merged[key] = value
    return merged


def enabled(pol: dict[str, Any] | None = None) -> bool:
    return bool((pol or policy()).get("enabled"))


# --- login dirs and their keychain entries ----------------------------------

def login_home(email: str) -> Path:
    return paths.claude_logins_dir() / email


def keychain_service(home: Path | str) -> str:
    """Claude Code keys its keychain item to the exact config-dir string."""
    digest = hashlib.sha256(str(home).encode()).hexdigest()[:8]
    return f"{KEYCHAIN_PREFIX}{digest}"


def login_token(email: str, *, runner=subprocess.run,
                now_ms: int | None = None) -> tuple[str, str | None]:
    """``(status, access_token)``: ``ok`` | ``no-login`` | ``no-credential``
    | ``expired-token`` | ``unparseable``. The token is returned to the caller
    only; nothing here persists it."""
    home = login_home(email)
    if not home.is_dir():
        return "no-login", None
    try:
        out = runner(
            ["security", "find-generic-password", "-s", keychain_service(home), "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "no-credential", None
    if out.returncode != 0 or not out.stdout.strip():
        return "no-credential", None
    try:
        oauth = (json.loads(out.stdout.strip()) or {}).get("claudeAiOauth") or {}
    except json.JSONDecodeError:
        return "unparseable", None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return "no-credential", None
    expires = oauth.get("expiresAt")
    current = now_ms if now_ms is not None else int(time.time() * 1000)
    if isinstance(expires, (int, float)) and expires <= current:
        return "expired-token", None
    return "ok", token


# --- readings ---------------------------------------------------------------

def parse_reading(probe: dict[str, Any]) -> dict[str, Any]:
    """Shared and Fable weekly utilisation (percent) from a parsed usage probe.

    ``fable`` is ``None`` when the payload carries no Fable-scoped window.
    """
    shared = None
    fable = None
    fable_present = False
    resets = None
    five = probe.get("five_hour") if isinstance(probe.get("five_hour"), dict) else {}
    five_hour = five.get("used_percent")
    five_hour = float(five_hour) if isinstance(five_hour, (int, float)) else None
    for limit in probe.get("limits") or []:
        if not isinstance(limit, dict):
            continue
        kind = limit.get("kind")
        pct = limit.get("percent")
        if kind == "weekly_all" and isinstance(pct, (int, float)):
            shared = float(pct)
            resets = limit.get("resets_at") or resets
        elif kind == "weekly_scoped" and (
            str(limit.get("scope_model") or "").casefold() == FABLE_DISPLAY.casefold()
        ):
            fable_present = True
            if isinstance(pct, (int, float)):
                fable = float(pct)
    if shared is None:
        week = probe.get("seven_day") or {}
        pct = week.get("used_percent") if isinstance(week, dict) else None
        if isinstance(pct, (int, float)):
            shared = float(pct)
            resets = week.get("reset_at") or resets
    return {"shared": shared, "fable": fable, "fable_present": fable_present,
            "resets_at": resets, "five_hour": five_hour,
            "five_hour_resets_at": five.get("reset_at")}


def verdict(shared: float | None, fable: float | None, *,
            fable_present: bool = True,
            pol: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reserve state for one account from percent-used readings."""
    pol = pol or policy()
    if shared is None:
        return {"state": STATE_UNMEASURED, "slack": None}
    if not fable_present:
        return {"state": STATE_NO_FABLE, "slack": None}
    if fable is None:
        return {"state": STATE_UNMEASURED, "slack": None}
    slack = (1.0 - shared / 100.0) - float(pol["cap_ratio"]) * (1.0 - fable / 100.0)
    state = STATE_SLACK if slack >= float(pol["min_slack"]) else STATE_RESERVED
    return {"state": state, "slack": round(slack, 4)}


def _load_cache(path: Path) -> dict[str, Any]:
    raw = load_json(path, None)
    return raw if isinstance(raw, dict) else {}


def _save_cache(path: Path, cache: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, sort_keys=True, indent=1))
        tmp.replace(path)
    except OSError:
        pass


def _pace(spacing: float, sleep: Callable[[float], None]) -> None:
    global _last_get_monotonic
    now = time.monotonic()
    if _last_get_monotonic is not None:
        wait = spacing - (now - _last_get_monotonic)
        if wait > 0:
            sleep(wait)
    _last_get_monotonic = time.monotonic()


def readings(emails: Iterable[str], *, pol: dict[str, Any] | None = None,
             probe=None, runner=subprocess.run, cache_path: Path | None = None,
             sleep: Callable[[float], None] = time.sleep,
             now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Per-account reserve readings, cached for ``reading_ttl_s`` seconds.

    Each value carries ``status`` (``ok`` or why not), ``state`` (see
    ``verdict``), ``slack``, ``shared``, ``fable``, ``checked_at``. A cached
    reading younger than the TTL is reused; otherwise one paced GET runs with
    the login dir's access token.
    """
    pol = pol or policy()
    probe = probe or (lambda token: claude_side.probe_oauth_usage(token, timeout=10.0))
    path = cache_path or paths.reserve_cache_path()
    cache = _load_cache(path)
    current = now or now_local()
    epoch_now = current.timestamp()
    ttl = float(pol["reading_ttl_s"])
    out: dict[str, dict[str, Any]] = {}
    dirty = False
    for email in emails:
        entry = cache.get(email)
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("epoch"), (int, float))
            and epoch_now - float(entry["epoch"]) < ttl
            and entry.get("status") == "ok"
        ):
            out[email] = dict(entry)
            continue
        status, token = login_token(email, runner=runner, now_ms=int(epoch_now * 1000))
        record: dict[str, Any] = {
            "epoch": epoch_now, "checked_at": iso(current), "status": status,
            "shared": None, "fable": None, "fable_present": None, "resets_at": None,
            "five_hour": None, "five_hour_resets_at": None,
        }
        if status == "ok":
            _pace(float(pol["usage_spacing_s"]), sleep)
            result = probe(token)
            token = None
            pstatus = result.get("status")
            if pstatus == "ok":
                record.update(parse_reading(result))
            elif pstatus == "token-invalid":
                record["status"] = "expired-token"
            else:
                record["status"] = str(pstatus or "probe-failed")
        state = verdict(
            record["shared"], record["fable"],
            fable_present=bool(record.get("fable_present")), pol=pol,
        )
        record.update(state)
        # Never persist anything but numbers, statuses and timestamps.
        cache[email] = {k: v for k, v in record.items()}
        dirty = True
        out[email] = dict(record)
    if dirty:
        _save_cache(path, cache)
    return out


# --- heal -------------------------------------------------------------------

def _heal_marker(email: str) -> Path:
    return paths.reserve_cache_path().parent / "reserve-heal" / f"{email}.marker"


def heal(email: str, *, pol: dict[str, Any] | None = None,
         popen=subprocess.Popen, which=shutil.which,
         now: float | None = None) -> bool:
    """Refresh an expired login access token by running one detached one-word
    Fable request in that config dir. Rate-limited per lane by a marker file.
    Returns True when a heal was launched."""
    pol = pol or policy()
    marker = _heal_marker(email)
    current = now if now is not None else time.time()
    try:
        if marker.exists() and current - marker.stat().st_mtime < float(pol["heal_interval_s"]):
            return False
    except OSError:
        return False
    binary = which("claude")
    home = login_home(email)
    if not binary or not home.is_dir():
        return False
    env = {
        key: value for key, value in os.environ.items()
        # A lane token or API key in the environment would outrank the
        # login and refresh nothing; the heal must run as the login itself.
        if key not in {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
        and not key.startswith("SUBFLEET_")
    }
    env["CLAUDE_CONFIG_DIR"] = str(home)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        popen(
            [binary, "-p", "reply with the single word ok", "--model", "fable",
             "--max-turns", "1", "--output-format", "json"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, env=env, cwd=str(home),
        )
    except OSError:
        return False
    return True


# --- the filter ---------------------------------------------------------------

def headroom_probe(email: str, *, readings_fn=None) -> dict[str, Any] | None:
    """A lane-headroom probe result built from the login dir's reading.

    Setup tokens get a standing 429 on the usage endpoint, so v1's just-in-time
    lane probe was blind; the account's full-scope login can read it. Returns
    ``{"status": "ok", "five_hour": {...}, "seven_day": {...}}`` in the shape of
    ``claude.probe_oauth_usage``, or ``None`` when no measured reading exists.
    """
    reading = (readings_fn or readings)([email]).get(email) or {}
    if reading.get("status") != "ok" or reading.get("shared") is None:
        return None
    return {
        "status": "ok",
        "five_hour": {"used_percent": reading.get("five_hour"),
                      "reset_at": reading.get("five_hour_resets_at")},
        "seven_day": {"used_percent": reading.get("shared"),
                      "reset_at": reading.get("resets_at")},
    }


def is_protected_model(model: str | None) -> bool:
    """True for every Claude model that is not Fable (the reserved model)."""
    canonical = (model or "").casefold()
    return "fable" not in canonical


def filter_lanes(rows: list[dict[str, Any]], *, model: str,
                 pol: dict[str, Any] | None = None,
                 readings_fn=None, heal_fn=None,
                 now: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep the lanes a non-Fable Claude model may use; explain the rest.

    Returns ``(kept, dropped)``; each dropped entry names the lane, its reserve
    state, slack and readings. Expired login tokens among the dropped lanes are
    healed in the background (bounded per call) so the next dispatch can
    measure them. Fable itself and a disabled policy pass every row through.
    """
    pol = pol or policy()
    if not enabled(pol) or not is_protected_model(model):
        return list(rows), []
    # Resolved at call time so tests (and operators) can swap the module seams.
    readings_fn = readings_fn or readings
    heal_fn = heal_fn or heal
    emails = [str(row.get("email") or row.get("id") or "") for row in rows]
    data = readings_fn([e for e in emails if e], pol=pol, now=now)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    heals = 0
    for row, email in zip(rows, emails):
        reading = data.get(email) or {"status": "no-email", "state": STATE_UNMEASURED,
                                     "slack": None, "shared": None, "fable": None}
        annotated = dict(row)
        annotated["reserve"] = {
            key: reading.get(key) for key in ("state", "slack", "shared", "fable", "status", "checked_at")
        }
        if reading.get("state") in (STATE_SLACK, STATE_NO_FABLE):
            kept.append(annotated)
            continue
        dropped.append({"lane": email, **annotated["reserve"]})
        if reading.get("status") == "expired-token" and heals < int(pol["heals_per_call"]):
            if heal_fn(email, pol=pol):
                heals += 1
    return kept, dropped


def describe(dropped: list[dict[str, Any]]) -> str:
    """One stderr-sized clause naming why each lane holds its reserve."""
    parts = []
    for item in dropped:
        state = item.get("state")
        if state == STATE_RESERVED:
            parts.append(
                f"{item['lane']} slack {item.get('slack')} "
                f"(shared {item.get('shared')}%, Fable {item.get('fable')}%)"
            )
        else:
            parts.append(f"{item['lane']} {state} ({item.get('status')})")
    return "; ".join(parts)


def table(emails: Iterable[str], *, pol: dict[str, Any] | None = None,
          readings_fn=None) -> list[dict[str, Any]]:
    """Rows for ``subfleet reserve``: one per account, verdict first."""
    pol = pol or policy()
    data = (readings_fn or readings)(list(emails), pol=pol)
    rows = []
    for email in emails:
        reading = data.get(email) or {}
        rows.append({
            "account": email,
            "state": reading.get("state"),
            "slack": reading.get("slack"),
            "shared_used": reading.get("shared"),
            "fable_used": reading.get("fable"),
            "status": reading.get("status"),
            "checked_at": reading.get("checked_at"),
            "resets_at": reading.get("resets_at"),
        })
    order = {STATE_SLACK: 0, STATE_NO_FABLE: 1, STATE_RESERVED: 2, STATE_UNMEASURED: 3}
    rows.sort(key=lambda r: (order.get(r["state"], 9), -(r["slack"] or -9), r["account"]))
    return rows


def human_table(rows: list[dict[str, Any]], pol: dict[str, Any] | None = None) -> str:
    pol = pol or policy()
    head = (
        f"FABLE RESERVE — slack = (1-shared) - {pol['cap_ratio']}×(1-fable); "
        f"non-Fable Claude models need slack ≥ {pol['min_slack']} "
        f"({'enabled' if pol.get('enabled') else 'DISABLED by policy file'})"
    )
    lines = [head, f"  {'account':28s} {'state':11s} {'slack':>7s} {'shared%':>8s} {'fable%':>7s}  status / checked"]
    for r in rows:
        slack = "" if r["slack"] is None else f"{r['slack']:+.2f}"
        shared = "" if r["shared_used"] is None else f"{r['shared_used']:.0f}"
        fable = "" if r["fable_used"] is None else f"{r['fable_used']:.0f}"
        lines.append(
            f"  {r['account']:28s} {str(r['state']):11s} {slack:>7s} {shared:>8s} {fable:>7s}  "
            f"{r['status']} {str(r['checked_at'] or '')[11:16]}"
        )
    return "\n".join(lines)
