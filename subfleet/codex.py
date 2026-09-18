"""Codex/ChatGPT side: auth.json inspection, live wham/usage probe, rollout scans.

Ground-truth hierarchy (server beats everything):
1. GET https://chatgpt.com/backend-api/wham/usage — live per-account quota + auth
   in one cheap authenticated call (verified 2026-07-11: returns rate_limit with
   primary/secondary windows, plan_type, email; a revoked access token returns
   401 {"code": "token_revoked"}).
2. Session rollout files ($CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl) —
   rate_limits snapshots recorded per turn, plus observed usage-limit error text
   with reset times ("try again at 11:33 PM"). Used when the network probe
   can't run, always labeled with observation time.

This module never writes to any CODEX_HOME and never refreshes tokens itself
(an UNPERSISTED refresh rotation is exactly the revocation trap this monitor
watches for). The one sanctioned refresh path is refresh_via_cli(): a one-shot
`codex exec` turn in the home, so the codex CLI refreshes AND atomically
persists its own token — the watchdog uses it to heal expired access tokens.
"""

import glob
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from .util import (
    from_epoch,
    iso,
    jwt_claims,
    load_json,
    now_local,
    parse_iso,
    parse_reset_clock,
)

WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
WHAM_RESET_CREDITS_URL = (
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
)
WHAM_RESET_CREDITS_CONSUME_URL = f"{WHAM_RESET_CREDITS_URL}/consume"
USER_AGENT = "subfleet/0.1 (codex_cli_rs compatible)"

USAGE_LIMIT_RE = re.compile(
    r"You've hit your usage limit[^\"\\]{0,120}?try again at (\d{1,2}:\d{2}\s*[AP]\.?M)",
    re.IGNORECASE,
)
REVOKED_RE = re.compile(r"refresh token was revoked", re.IGNORECASE)
# wham 401 for a merely EXPIRED access token (distinct from token_revoked):
# heal-able, because any real CLI call refreshes it — see refresh_via_cli.
TOKEN_EXPIRED_RE = re.compile(r"token (?:is |has )?expired|token_expired", re.IGNORECASE)


def read_auth(home: Path) -> dict:
    """Parse $CODEX_HOME/auth.json. The access token is returned under a private
    key ('_access_token') that snapshot serialization strips."""
    auth_file = home / "auth.json"
    if not auth_file.exists():
        return {"status": "missing", "home": str(home)}
    try:
        raw = json.loads(auth_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"status": "unreadable", "home": str(home), "error": str(e)}
    tokens = raw.get("tokens") or {}
    access = tokens.get("access_token") or ""
    claims = jwt_claims(access)
    auth_claims = claims.get("https://api.openai.com/auth", {})
    id_claims = jwt_claims(tokens.get("id_token") or "")
    return {
        "status": "ok",
        "home": str(home),
        "account_id": tokens.get("account_id") or auth_claims.get("chatgpt_account_id"),
        "email": id_claims.get("email"),
        "plan": auth_claims.get("chatgpt_plan_type"),
        "last_refresh": raw.get("last_refresh"),
        "access_token_exp": iso(from_epoch(claims.get("exp"))),
        "_access_token": access,
    }


API_KEY_AUTH_FIELDS = ("OPENAI_API_KEY", "CODEX_API_KEY")
API_LANE_OVERRIDE_ENV = "SUBFLEET_ALLOW_API_LANE"
API_LANE_REFUSED_RC = 7


def api_key_login(home: Path | str) -> bool:
    """True when $CODEX_HOME/auth.json is an OpenAI API-key login (metered
    platform billing) rather than a ChatGPT-account login (subscription).

    Lanes run on ChatGPT subscriptions only (Max, 2026-09-04: "dont use the
    api! its super expensive i need to use my sub"). `codex login
    --with-api-key` stores the key under OPENAI_API_KEY with auth_mode
    "apikey" and no tokens; a ChatGPT login has OPENAI_API_KEY null and
    auth_mode "chatgpt". Missing or unreadable auth is not an API login (the
    picker already treats those homes as undispatchable).
    """
    auth_file = Path(home).expanduser() / "auth.json"
    try:
        raw = json.loads(auth_file.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    if any(raw.get(field) for field in API_KEY_AUTH_FIELDS):
        return True
    mode = str(raw.get("auth_mode") or "").lower().replace("_", "").replace("-", "")
    return mode == "apikey"


def api_lane_allowed() -> bool:
    """Deliberate operator override for a metered-API dispatch; subfleet never
    sets it itself."""
    return os.environ.get(API_LANE_OVERRIDE_ENV) == "1"


def api_lane_refusal(home: Path | str) -> str | None:
    """Refusal message when HOME must not be dispatched to (an API-key login
    without the override), else None."""
    if api_lane_allowed() or not api_key_login(home):
        return None
    shown = str(Path(home).expanduser())
    home_dir = str(Path.home())
    if shown.startswith(home_dir):
        shown = "~" + shown[len(home_dir):]
    return (
        f"{shown} is an OpenAI API-key login (metered platform billing); lanes "
        f"run on ChatGPT subscriptions only ({API_LANE_OVERRIDE_ENV}=1 overrides "
        "deliberately)"
    )


def accounts_config_path() -> Path:
    """Codex-side account config, sibling of claude-accounts.json.
    SUBFLEET_CODEX_ACCOUNTS overrides (tests point it at tmp files)."""
    cfg_path = os.environ.get("SUBFLEET_CODEX_ACCOUNTS")
    return Path(cfg_path).expanduser() if cfg_path else Path(__file__).parent.parent / "codex-accounts.json"


def app_home_identity() -> dict:
    """What the ChatGPT/Codex desktop app (and bare interactive `codex`) is
    signed into right now: the bound identity of ~/.codex/auth.json
    (paths.app_codex_home()). The app rewrites that file on every sign-in/out,
    so this is read fresh each call, never cached. Token stripped."""
    from . import paths

    auth = read_auth(paths.app_codex_home())
    return {k: v for k, v in auth.items() if not str(k).startswith("_")}


def protected_account() -> dict | None:
    """Identify the account backing Max's interactive ChatGPT/Codex apps.

    Precedence (2026-08-19): the app home's live identity first — ~/.codex is
    the app's own auth store, so it IS the truth of what the app is signed
    into, and it follows Max's sign-in/outs automatically; then
    codex-accounts.json `protected_account` (an {email, account_id} object, a
    bare email/account_id string, or a list of either) as the fallback when
    the app home is missing/unreadable. Returns {"emails", "account_ids"}
    (lowercased sets) plus "source" ('app-home' | 'config'), or None when
    neither identifies an account. Codex exposes this as warning/reset-policy
    metadata; it no longer changes weekly-reset dispatch order."""
    app = app_home_identity()
    if app.get("status") == "ok" and (app.get("email") or app.get("account_id")):
        emails = {str(app["email"]).strip().lower()} if app.get("email") else set()
        ids = {str(app["account_id"]).strip().lower()} if app.get("account_id") else set()
        return {"emails": emails, "account_ids": ids, "source": "app-home"}
    raw = (load_json(accounts_config_path(), {}) or {}).get("protected_account")
    entries = raw if isinstance(raw, list) else [raw]
    emails: set[str] = set()
    account_ids: set[str] = set()
    for item in entries:
        if isinstance(item, str) and item.strip():
            (emails if "@" in item else account_ids).add(item.strip().lower())
        elif isinstance(item, dict):
            email, account_id = item.get("email"), item.get("account_id")
            if isinstance(email, str) and email.strip():
                emails.add(email.strip().lower())
            if isinstance(account_id, str) and account_id.strip():
                account_ids.add(account_id.strip().lower())
    if not emails and not account_ids:
        return None
    return {"emails": emails, "account_ids": account_ids, "source": "config"}


def is_protected_account(email: str | None, account_id: str | None,
                         protected: dict | None) -> bool:
    """Whether a home's bound identity matches the protected app account."""
    if not protected:
        return False
    if account_id and str(account_id).strip().lower() in protected["account_ids"]:
        return True
    return bool(email) and str(email).strip().lower() in protected["emails"]


def plan_is_free(plan_type) -> bool:
    """wham plan_type for an account with no paid ChatGPT plan. Only 'free'
    is gated — Plus/Team/Pro are all paid tiers with codex entitlement."""
    return isinstance(plan_type, str) and plan_type.strip().lower() == "free"


def _window(w: dict | None) -> dict | None:
    if not isinstance(w, dict):
        return None
    return {
        "used_percent": w.get("used_percent"),
        "window_seconds": w.get("limit_window_seconds"),
        "reset_at": iso(from_epoch(w.get("reset_at"))),
    }


def _optional_count(value) -> int | None:
    """Backend entitlement counts are integers; reject bool/malformed data."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def reset_credits_from_probe(probe: dict | None) -> dict:
    """Stable public reset-credit summary for snapshot/capacity consumers."""
    raw = probe.get("reset_credits") if isinstance(probe, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "available": _optional_count(raw.get("available")),
        "applicable": _optional_count(raw.get("applicable")),
    }


# A "5h" window is anything up to 6h; longer windows are weekly-class. The wham
# schema assigns meaning positionally and CHANGES OVER TIME (2026-07-11:
# primary=5h/secondary=weekly; by 2026-07-25: primary=weekly, secondary=null —
# no 5h reported at all), so consumers must classify by duration, never position.
FIVE_HOUR_MAX_SECONDS = 6 * 3600


def classify_windows(*windows: dict | None) -> tuple[dict | None, dict | None]:
    """(five_hour, weekly) from any number of window dicts, by duration."""
    five = week = None
    for w in windows:
        if not isinstance(w, dict):
            continue
        secs = w.get("window_seconds") or 0
        if 0 < secs <= FIVE_HOUR_MAX_SECONDS:
            five = five or w
        elif secs > FIVE_HOUR_MAX_SECONDS:
            week = week or w
    return five, week


def probe_wham(auth: dict, timeout: float = 15.0, opener=None) -> dict:
    """One live authenticated GET per account. Categorized result; never raises.

    `opener` is injectable for tests: callable(request, timeout) -> (status, body_bytes)
    or raises urllib.error.HTTPError / OSError.
    """
    checked_at = iso(now_local())
    if auth.get("status") != "ok" or not auth.get("_access_token"):
        return {"status": "no-auth", "checked_at": checked_at}
    req = urllib.request.Request(
        WHAM_USAGE_URL,
        headers={
            "Authorization": f"Bearer {auth['_access_token']}",
            "chatgpt-account-id": auth.get("account_id") or "",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    def default_opener(request, t):
        with urllib.request.urlopen(request, timeout=t) as r:
            return r.status, r.read()

    opener = opener or default_opener
    try:
        status, body = opener(req, timeout)
        payload = json.loads(body.decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {}
        code = (err_body.get("error") or {}).get("code") or ""
        if e.code == 401 and code == "token_revoked":
            return {
                "status": "token-revoked",
                "checked_at": checked_at,
                "error": (err_body.get("error") or {}).get("message") or "token revoked",
            }
        return {
            "status": f"http-{e.code}",
            "checked_at": checked_at,
            "error": (err_body.get("error") or {}).get("message") or f"HTTP {e.code}",
        }
    except (OSError, ValueError) as e:
        return {"status": "network-error", "checked_at": checked_at, "error": str(e)}

    rl = payload.get("rate_limit") or {}
    extras = []
    for item in payload.get("additional_rate_limits") or []:
        extra_rl = item.get("rate_limit") or {}
        extras.append(
            {
                "name": item.get("limit_name"),
                "limit_reached": extra_rl.get("limit_reached"),
                "primary": _window(extra_rl.get("primary_window")),
            }
        )
    primary = _window(rl.get("primary_window"))
    secondary = _window(rl.get("secondary_window"))
    five_hour, weekly = classify_windows(primary, secondary)
    raw_reset_credits = payload.get("rate_limit_reset_credits")
    raw_reset_credits = raw_reset_credits if isinstance(raw_reset_credits, dict) else {}
    return {
        "status": "ok",
        "checked_at": checked_at,
        "email": payload.get("email"),
        "plan_type": payload.get("plan_type"),
        "allowed": rl.get("allowed"),
        "limit_reached": rl.get("limit_reached"),
        "primary": primary,
        "secondary": secondary,
        "five_hour": five_hour,
        "weekly": weekly,
        "reset_credits": {
            "available": _optional_count(raw_reset_credits.get("available_count")),
            "applicable": _optional_count(
                raw_reset_credits.get("applicable_available_count")
            ),
        },
        "additional": extras,
    }


def _reset_request(
    auth: dict,
    url: str,
    *,
    timeout: float,
    opener,
    payload: dict | None = None,
) -> dict:
    """Authenticated reset-credit request with probe-style categorized errors."""
    checked_at = iso(now_local())
    if auth.get("status") != "ok" or not auth.get("_access_token"):
        return {"status": "no-auth", "checked_at": checked_at}
    headers = {
        "Authorization": f"Bearer {auth['_access_token']}",
        "chatgpt-account-id": auth.get("account_id") or "",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode()
        method = "POST"
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    def default_opener(request, request_timeout):
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            return response.status, response.read()

    opener = opener or default_opener
    try:
        status, body = opener(req, timeout)
        if status < 200 or status >= 300:
            return {
                "status": f"http-{status}",
                "checked_at": checked_at,
                "error": f"HTTP {status}",
            }
        decoded = json.loads(body.decode())
        if not isinstance(decoded, dict):
            raise ValueError("expected a JSON object")
    except urllib.error.HTTPError as exc:
        try:
            error_body = json.loads(exc.read().decode())
        except Exception:
            error_body = {}
        error = error_body.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else None
        return {
            "status": f"http-{exc.code}",
            "checked_at": checked_at,
            "error": message or f"HTTP {exc.code}",
        }
    except (OSError, UnicodeError, ValueError) as exc:
        return {"status": "network-error", "checked_at": checked_at, "error": str(exc)}
    return {"status": "ok", "checked_at": checked_at, **decoded}


def list_reset_credits(auth: dict, timeout: float = 15.0, opener=None) -> dict:
    """List gifted reset entitlements; this never calls purchase/add-credit paths."""
    return _reset_request(
        auth, WHAM_RESET_CREDITS_URL, timeout=timeout, opener=opener
    )


def consume_reset_credit(
    auth: dict,
    credit_id: str | None = None,
    timeout: float = 15.0,
    opener=None,
) -> dict:
    """Consume one gifted entitlement using a fresh idempotency UUID."""
    redeem_request_id = str(uuid.uuid4())
    payload = {"redeem_request_id": redeem_request_id}
    if credit_id is not None:
        payload["credit_id"] = credit_id
    result = _reset_request(
        auth,
        WHAM_RESET_CREDITS_CONSUME_URL,
        timeout=timeout,
        opener=opener,
        payload=payload,
    )
    return {**result, "redeem_request_id": redeem_request_id}


def reset_consume_succeeded(result: dict) -> bool:
    """The upstream success enum is exactly `reset`, with windows changed."""
    windows_reset = result.get("windows_reset")
    return (
        result.get("status") == "ok"
        and result.get("code") == "reset"
        and isinstance(windows_reset, int)
        and not isinstance(windows_reset, bool)
        and windows_reset > 0
    )


def probe_all(auths: list[dict], timeout: float = 15.0, opener=None) -> list[dict]:
    with ThreadPoolExecutor(max_workers=max(len(auths), 1)) as ex:
        return list(ex.map(lambda a: probe_wham(a, timeout=timeout, opener=opener), auths))


def probe_looks_token_expired(probe: dict) -> bool:
    """True when a wham probe 401'd over an expired ACCESS token — the
    heal-able signature (NOT token_revoked; 'refresh token was revoked' never
    appears in wham results, only in CLI refresh failures)."""
    return (
        probe.get("status") == "http-401"
        and bool(TOKEN_EXPIRED_RE.search(probe.get("error") or ""))
    )


REFRESH_PROBE_MODEL = "gpt-5.6-terra"
REFRESH_PROBE_PROMPT = "Reply with exactly: ok"


def _codex_binary() -> str:
    """Resolve the real codex CLI for contexts with a stripped PATH (launchd):
    the auto-heal ran as FileNotFoundError('codex') from the watchdog for a
    week (latched .codex-3 'failed' 2026-08-13) because launchd has no
    ~/bin. Order: $SUBFLEET_CODEX_BIN, PATH, then known install locations —
    the bun global install first (the ~/bin shim's own resolution order),
    then the shim itself (fine here: CODEX_HOME is always set explicitly,
    which the shim passes through without rotating)."""
    import shutil

    override = os.environ.get("SUBFLEET_CODEX_BIN")
    if override:
        return override
    found = shutil.which("codex")
    if found:
        return found
    home = Path.home()
    for cand in (
        home / ".bun" / "bin" / "codex",
        home / ".bun" / "install" / "global" / "node_modules" / ".bin" / "codex",
        home / "bin" / "codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return "codex"


def refresh_via_cli(home: Path | str, model: str = REFRESH_PROBE_MODEL,
                    timeout: float = 300.0, runner=subprocess.run) -> dict:
    """Heal an expired access token with one tiny `codex exec` turn (~8k tokens).

    The codex CLI refreshes the token at startup and persists the rotation
    atomically inside its own home — safe, unlike any refresh subfleet could
    perform itself, where an unpersisted rotation strands the lane (this
    recipe revived ~/.codex-2 on 2026-08-12 after 14h of AUTH-SUSPECT).

    Returns {"status": "ok"|"revoked"|"failed", "rc": int|None, "detail": str}.
    "revoked" ('refresh token was revoked') is definitive death until re-login.
    A failed/timed-out turn may still have refreshed the token, so callers
    should re-probe usage on ANY non-revoked outcome.
    """
    cmd = [_codex_binary(), "exec", "-m", model, "--skip-git-repo-check", REFRESH_PROBE_PROMPT]
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=timeout,
                   env={**os.environ, "CODEX_HOME": str(home)}, cwd=str(home))
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"status": "failed", "rc": None, "detail": f"{type(e).__name__}: {e}"[:300]}
    out = f"{r.stdout or ''}\n{r.stderr or ''}"
    if REVOKED_RE.search(out):
        return {"status": "revoked", "rc": r.returncode, "detail": "refresh token was revoked"}
    if r.returncode == 0:
        return {"status": "ok", "rc": 0, "detail": ""}
    tail = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return {"status": "failed", "rc": r.returncode, "detail": tail[:300]}


def _recent_rollouts(home: Path, max_age_hours: float) -> list[Path]:
    """All rollout files touched within the window, newest first. Homes under
    heavy orchestration accumulate hundreds per day (each exec probe writes
    one), so callers narrow further with _grep_candidates rather than a count cap."""
    pattern = str(home / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")
    cutoff = (now_local() - timedelta(hours=max_age_hours)).timestamp()
    fresh = []
    for p in glob.glob(pattern):
        try:
            mtime = Path(p).stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            fresh.append((mtime, Path(p)))
    fresh.sort(reverse=True)
    return [p for _, p in fresh]


def _grep_candidates(files: list[Path], pattern: str, runner=subprocess.run) -> list[Path]:
    """C-speed narrowing: which files contain the pattern at all."""
    matched: list[Path] = []
    for start in range(0, len(files), 100):
        chunk = [str(f) for f in files[start : start + 100]]
        try:
            out = runner(
                ["grep", "-lE", pattern, *chunk],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        matched.extend(Path(p) for p in out.stdout.splitlines() if p)
    order = {f: i for i, f in enumerate(files)}
    matched.sort(key=lambda f: order.get(f, 1 << 30))
    return matched


def _grep_lines(files: list[Path], pattern: str, runner=subprocess.run) -> list[str]:
    """Extract matching lines with grep (C speed) instead of iterating
    multi-hundred-MB session files in Python."""
    lines: list[str] = []
    for start in range(0, len(files), 50):
        chunk = [str(f) for f in files[start : start + 50]]
        try:
            out = runner(
                ["grep", "-hE", pattern, *chunk],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        lines.extend(out.stdout.splitlines())
    return lines


# ---------------------------------------------------------------------------
# Incremental rollout scanning. Orchestration-heavy homes hold hundreds of
# rollouts per day totaling GBs; a cold grep sweep costs ~20s of CPU. We scan
# each file once, remember (size, mtime) + extracted signals in a cache under
# the state dir, and on later runs read only the bytes appended since
# (rollouts are append-only JSONL).
# ---------------------------------------------------------------------------

SIGNAL_PATTERN = r"hit your usage limit|was revoked|\"rate_limits\""
_SIGNAL_SUBSTRINGS = ("hit your usage limit", "was revoked", '"rate_limits"')
_PER_FILE_CAP = 40


def _parse_signal_lines(lines: list[str], entry: dict) -> None:
    """Fold matching rollout lines into a cache entry in place."""
    for line in lines:
        if not any(s in line for s in _SIGNAL_SUBSTRINGS):
            continue
        ts = None
        d = None
        try:
            d = json.loads(line)
            ts = parse_iso(d.get("timestamp"))
        except json.JSONDecodeError:
            pass
        if ts is None:
            continue
        ts_iso = iso(ts)
        if d is not None:
            payload = d.get("payload") or {}
            rl = payload.get("rate_limits")
            if isinstance(rl, dict) and rl.get("primary"):
                prev = entry.get("rate_limits")
                if prev is None or (prev.get("observed_at") or "") < ts_iso:
                    entry["rate_limits"] = {"observed_at": ts_iso, "data": rl}
                continue
        m = USAGE_LIMIT_RE.search(line)
        if m:
            reset = parse_reset_clock(m.group(1), ts)
            entry.setdefault("usage", []).append(
                {"observed_at": ts_iso, "try_again": m.group(1), "reset_at": iso(reset)}
            )
            entry["usage"] = entry["usage"][-_PER_FILE_CAP:]
        elif REVOKED_RE.search(line):
            entry.setdefault("revoked", []).append({"observed_at": ts_iso})
            entry["revoked"] = entry["revoked"][-_PER_FILE_CAP:]


def _read_appended_lines(path: Path, offset: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            blob = f.read()
    except OSError:
        return []
    return blob.decode(errors="ignore").splitlines()


def _grep_lines_by_file(files: list[Path], pattern: str, runner=subprocess.run) -> dict[str, list[str]]:
    """Batched grep -H over many files; returns path -> matching lines. Paths
    here never contain ':' so the first colon splits reliably."""
    by_file: dict[str, list[str]] = {}
    for start in range(0, len(files), 50):
        chunk = [str(f) for f in files[start : start + 50]]
        try:
            out = runner(
                ["grep", "-HE", pattern, *chunk],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in out.stdout.splitlines():
            path, _, rest = line.partition(":")
            if rest:
                by_file.setdefault(path, []).append(rest)
    return by_file


def scan_rollout_signals(home: Path, max_age_hours: float = 48, runner=subprocess.run) -> dict:
    """Cache-backed sweep of this home's recent rollouts. Returns
    {"usage": [...], "revoked": [...], "rate_limits": newest-or-None}.

    Cost model: files already seen at their current (size, mtime) are free;
    grown files get a Python read of just the appended bytes (rollouts are
    append-only JSONL); brand-new files get one batched grep. A cold first
    sweep of an orchestration-heavy day is tens of seconds; steady state is
    near-zero."""
    from . import paths
    from .util import atomic_write_json, load_json

    cache_file = paths.rollout_cache_path()
    cache = load_json(cache_file, {}) or {}
    changed = False

    files = _recent_rollouts(home, max_age_hours)
    stats: dict[str, object] = {}
    new_files: list[Path] = []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        stats[str(f)] = st
        ent = cache.get(str(f))
        if ent is None or st.st_size < (ent.get("size") or 0):
            new_files.append(f)

    # Snapshot sizes BEFORE grepping so bytes appended mid-scan are re-read
    # next cycle instead of silently skipped.
    fresh_entries = {
        str(f): {"size": stats[str(f)].st_size, "mtime": stats[str(f)].st_mtime}
        for f in new_files
        if str(f) in stats
    }
    for path, lines in _grep_lines_by_file(new_files, SIGNAL_PATTERN, runner=runner).items():
        if path in fresh_entries:
            _parse_signal_lines(lines, fresh_entries[path])
    if fresh_entries:
        cache.update(fresh_entries)
        changed = True

    usage: list[dict] = []
    revoked: list[dict] = []
    newest_rl = None
    for f in files:
        key = str(f)
        st = stats.get(key)
        if st is None:
            continue
        ent = cache.get(key)
        if ent is None:
            continue
        if key not in fresh_entries and (
            ent.get("size") != st.st_size or ent.get("mtime") != st.st_mtime
        ):
            _parse_signal_lines(_read_appended_lines(f, ent.get("size") or 0), ent)
            ent["size"], ent["mtime"] = st.st_size, st.st_mtime
            changed = True
        usage.extend(ent.get("usage") or [])
        revoked.extend(ent.get("revoked") or [])
        rl = ent.get("rate_limits")
        if rl and (newest_rl is None or rl["observed_at"] > newest_rl["observed_at"]):
            newest_rl = rl

    # Prune entries whose file is gone or long out of any realistic window
    # (pruning by "not in this call's window" would evict entries that a
    # wider-window caller still wants, forcing pointless rescans).
    week_ago = (now_local() - timedelta(days=7)).timestamp()
    prefix = str(home)
    for key in [
        k
        for k, ent in cache.items()
        if k.startswith(prefix)
        and k not in stats
        and ((ent.get("mtime") or 0) < week_ago or not Path(k).exists())
    ]:
        del cache[key]
        changed = True
    if changed:
        try:
            atomic_write_json(cache_file, cache)
        except OSError:
            pass
    return {"usage": usage, "revoked": revoked, "rate_limits": newest_rl}


def latest_rollout_rate_limits(home: Path, max_age_hours: float = 48) -> dict | None:
    """Newest rate_limits snapshot recorded by any codex session in this home.
    Normalized to the wham shape, tagged with when it was observed."""
    found = scan_rollout_signals(home, max_age_hours=max_age_hours).get("rate_limits")
    if not found:
        return None
    rl = found["data"]

    def norm(w):
        if not isinstance(w, dict):
            return None
        return {
            "used_percent": w.get("used_percent"),
            "window_seconds": (w.get("window_minutes") or 0) * 60 or None,
            "reset_at": iso(from_epoch(w.get("resets_at"))),
        }

    return {
        "observed_at": found["observed_at"],
        "plan_type": rl.get("plan_type"),
        "primary": norm(rl.get("primary")),
        "secondary": norm(rl.get("secondary")),
    }


def recent_limit_errors(home: Path, hours: float = 24) -> dict:
    """Observed hard errors in recent rollouts: usage-limit refusals (with the
    server's stated retry clock) and revoked-refresh-token failures. Events are
    filtered by their own timestamps, not just file mtime (long-running session
    files span days)."""
    # File-mtime window == event window is sound: an event older than the
    # window can only live in a file whose mtime is at least that old.
    cutoff = now_local() - timedelta(hours=hours)
    signals = scan_rollout_signals(home, max_age_hours=hours)

    def within(events):
        out = []
        for e in events:
            ts = parse_iso(e.get("observed_at"))
            if ts is not None and ts >= cutoff:
                out.append(e)
        out.sort(key=lambda e: e["observed_at"], reverse=True)
        return out

    usage = within(signals["usage"])
    revoked = within(signals["revoked"])
    # Collapse repeats of the same reset clock to the most recent sighting.
    seen, unique_usage = set(), []
    for e in usage:
        key = e["reset_at"] or e["try_again"]
        if key in seen:
            continue
        seen.add(key)
        unique_usage.append(e)
    return {"usage_limit": unique_usage[:5], "auth_revoked": revoked[:3]}
