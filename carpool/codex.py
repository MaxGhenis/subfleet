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

This module never writes to any CODEX_HOME and never triggers token refresh
(a refresh rotates the account's refresh token — that rotation is exactly the
revocation trap this monitor watches for).
"""

import glob
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from . import config
from .util import from_epoch, iso, jwt_claims, now_local, parse_iso, parse_reset_clock

WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USER_AGENT = "carpool/0.1 (codex_cli_rs compatible)"

USAGE_LIMIT_RE = re.compile(
    r"You've hit your usage limit[^\"\\]{0,120}?try again at (\d{1,2}:\d{2}\s*[AP]\.?M)",
    re.IGNORECASE,
)
REVOKED_RE = re.compile(r"refresh token was revoked", re.IGNORECASE)
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


def app_home_identity() -> dict:
    """Read the desktop app's current identity without exposing token fields."""
    from . import paths

    return {
        key: value
        for key, value in read_auth(paths.app_codex_home()).items()
        if not str(key).startswith("_")
    }


def protected_account() -> dict | None:
    """Return the account whose interactive quota should be spared.

    The observed app-home identity is authoritative. ``protected_account`` in
    ``accounts.json`` is used only when the app home has no readable identity.
    The configured value may be an email/account-id string, an object with
    ``email``/``account_id``, or a list of either.
    """
    app = app_home_identity()
    if app.get("status") == "ok" and (app.get("email") or app.get("account_id")):
        return {
            "emails": {str(app["email"]).strip().lower()} if app.get("email") else set(),
            "account_ids": (
                {str(app["account_id"]).strip().lower()} if app.get("account_id") else set()
            ),
            "source": "app-home",
        }

    raw = config.load().get("protected_account")
    entries = raw if isinstance(raw, list) else [raw]
    emails: set[str] = set()
    account_ids: set[str] = set()
    for item in entries:
        if isinstance(item, str) and item.strip():
            destination = emails if "@" in item else account_ids
            destination.add(item.strip().lower())
        elif isinstance(item, dict):
            email = item.get("email")
            account_id = item.get("account_id")
            if isinstance(email, str) and email.strip():
                emails.add(email.strip().lower())
            if isinstance(account_id, str) and account_id.strip():
                account_ids.add(account_id.strip().lower())
    if not emails and not account_ids:
        return None
    return {"emails": emails, "account_ids": account_ids, "source": "config"}


def is_protected_account(
    email: str | None, account_id: str | None, protected: dict | None
) -> bool:
    if not protected:
        return False
    if account_id and str(account_id).strip().lower() in protected["account_ids"]:
        return True
    return bool(email) and str(email).strip().lower() in protected["emails"]


def plan_is_free(plan_type) -> bool:
    """Only the literal free plan is excluded; paid plan names remain open."""
    return isinstance(plan_type, str) and plan_type.strip().lower() == "free"


def _window(w: dict | None) -> dict | None:
    if not isinstance(w, dict):
        return None
    return {
        "used_percent": w.get("used_percent"),
        "window_seconds": w.get("limit_window_seconds"),
        "reset_at": iso(from_epoch(w.get("reset_at"))),
    }


FIVE_HOUR_MAX_SECONDS = 6 * 3600


def classify_windows(*windows: dict | None) -> tuple[dict | None, dict | None]:
    """Classify quota windows by duration rather than unstable API position."""
    five_hour = weekly = None
    for window in windows:
        if not isinstance(window, dict):
            continue
        seconds = window.get("window_seconds") or 0
        if 0 < seconds <= FIVE_HOUR_MAX_SECONDS:
            five_hour = five_hour or window
        elif seconds > FIVE_HOUR_MAX_SECONDS:
            weekly = weekly or window
    return five_hour, weekly


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
        "additional": extras,
    }


def probe_all(auths: list[dict], timeout: float = 15.0, opener=None) -> list[dict]:
    with ThreadPoolExecutor(max_workers=max(len(auths), 1)) as ex:
        return list(ex.map(lambda a: probe_wham(a, timeout=timeout, opener=opener), auths))


def probe_looks_token_expired(probe: dict) -> bool:
    return (
        probe.get("status") == "http-401"
        and bool(TOKEN_EXPIRED_RE.search(probe.get("error") or ""))
    )


REFRESH_PROBE_MODEL = "gpt-5.6-terra"
REFRESH_PROBE_PROMPT = "Reply with exactly: ok"


def _codex_binary() -> str:
    """Resolve the real CLI even when a scheduler provides a minimal PATH."""
    import shutil

    override = os.environ.get("CARPOOL_CODEX_BIN")
    if override:
        return override

    repository_shim = (Path(__file__).resolve().parent.parent / "bin" / "codex").resolve()

    def usable(candidate: Path | str | None) -> str | None:
        if not candidate:
            return None
        path = Path(candidate).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            return None
        try:
            if path.resolve() == repository_shim:
                return None
            # Installed copies of our PATH shim must not resolve back to
            # themselves and recurse forever.
            if "codex PATH shim" in path.read_text(errors="ignore")[:512]:
                return None
        except OSError:
            return None
        return str(path)

    home = Path.home()
    for candidate in (
        home / ".bun" / "bin" / "codex",
        home / ".bun" / "install" / "global" / "node_modules" / ".bin" / "codex",
        shutil.which("codex"),
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ):
        resolved = usable(candidate)
        if resolved:
            return resolved
    return "codex"


def refresh_via_cli(
    home: Path | str,
    model: str = REFRESH_PROBE_MODEL,
    timeout: float = 300.0,
    runner=subprocess.run,
) -> dict:
    """Let the vendor CLI refresh and persist its own token atomically."""
    command = [
        _codex_binary(),
        "exec",
        "-m",
        model,
        "--skip-git-repo-check",
        REFRESH_PROBE_PROMPT,
    ]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "CODEX_HOME": str(home)},
            cwd=str(home),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "failed",
            "rc": None,
            "detail": f"{type(exc).__name__}: {exc}"[:300],
        }
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    if REVOKED_RE.search(output):
        return {"status": "revoked", "rc": result.returncode, "detail": "refresh token was revoked"}
    if result.returncode == 0:
        return {"status": "ok", "rc": 0, "detail": ""}
    tail = output.strip().splitlines()[-1].strip() if output.strip() else ""
    return {"status": "failed", "rc": result.returncode, "detail": tail[:300]}


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
