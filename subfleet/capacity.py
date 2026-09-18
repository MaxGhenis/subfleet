"""Fast cross-family capacity view for dispatch and the monitor CLI.

Only probes that can report real subscription usage are used here: wham/usage
for each CODEX_HOME and the active Claude desktop login's keychain app token.
Claude setup-token lanes are inference-only, so their usage is estimated from
the append-only lane ledger and calibrated only when a hard limit is observed.
"""

from __future__ import annotations

import copy
import re
import fcntl
import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import claude, codex, paths, reset_policy, run_ledger
from .util import atomic_write_json, iso, load_json, now_local, parse_iso, parse_reset_clock

CACHE_TTL_SECONDS = 120
DEFAULT_MIN_HEADROOM = 5.0
INTERACTIVE_HANDICAP = 10.0
UNKNOWN_LIMIT_FALLBACK = timedelta(hours=1)
FIVE_HOURS = timedelta(hours=5)
SEVEN_DAYS = timedelta(days=7)
CLAUDE_MODEL_FAMILIES = ("Fable", "Opus", "Sonnet", "Haiku")
CLAUDE_MODEL_IDS = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5",
    "sonnet": "sonnet",
    "haiku": "claude-haiku-4-5-20251001",
}
# Retired pins that still name a served model. They normalize onto the current
# id everywhere (cooldowns, hard-limit records, revive targets, picks) because
# they draw on the same account-scoped "Fable" limit as the current model.
CLAUDE_MODEL_LEGACY_IDS = {
    "claude-fable-5": "claude-fable-5-1",
}
CLAUDE_PICK_MODELS = (
    "fable", "opus", "sonnet", "haiku",
    CLAUDE_MODEL_IDS["fable"], CLAUDE_MODEL_IDS["opus"],
    CLAUDE_MODEL_IDS["haiku"],
)
ACCOUNT_COOLDOWN_SCOPE = "*"


def _clock(value: datetime | None = None) -> datetime:
    value = value or now_local()
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    return parse_iso(value) if isinstance(value, str) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


_CONTEXT_SUFFIX = re.compile(r"\[[^\]]*\]$")


def split_claude_model(model: str) -> tuple[str, str]:
    """``("claude-opus-5", "[1m]")`` for the app's context-window suffix form.

    The desktop session store records the configured model with the suffix
    (``claude-fable-5[1m]``, ``fable[1m]``); transcripts' per-message model
    field never carries it.
    """
    match = _CONTEXT_SUFFIX.search(model)
    return (model[: match.start()], match.group(0)) if match else (model, "")


def normalize_claude_model(model: str | None) -> str | None:
    """Canonical full Claude model id for aliases and retired pins; preserve
    other ids. A ``[1m]``-style suffix survives on the result, so the value
    stays a valid ``--model`` argument for dispatch and revive."""
    if model is None:
        return None
    requested = str(model).strip()
    if not requested:
        return None
    base, suffix = split_claude_model(requested)
    folded = base.casefold()
    if folded in CLAUDE_MODEL_IDS:
        return CLAUDE_MODEL_IDS[folded] + suffix
    if folded in CLAUDE_MODEL_LEGACY_IDS:
        return CLAUDE_MODEL_LEGACY_IDS[folded] + suffix
    for full_id in CLAUDE_MODEL_IDS.values():
        if folded == full_id.casefold():
            return full_id + suffix
    return requested


def canonical_claude_model(model: str | None) -> str | None:
    """``normalize_claude_model`` without the suffix: the key for cooldown
    scopes, ledger records, and served-model comparisons."""
    normalized = normalize_claude_model(model)
    return split_claude_model(normalized)[0] if normalized else None


def _model_display_name(model_family: str) -> str:
    requested = split_claude_model(str(model_family).strip())[0].casefold()
    canonical = canonical_claude_model(model_family)
    canonical_folded = canonical.casefold() if canonical else requested
    for alias, full_id in CLAUDE_MODEL_IDS.items():
        if requested == alias or canonical_folded == full_id.casefold():
            return alias.title()
    return next(
        (display for display in CLAUDE_MODEL_FAMILIES if display.casefold() == requested),
        str(model_family),
    )


def _cooldown_scope(model: str | None) -> str:
    return canonical_claude_model(model) or ACCOUNT_COOLDOWN_SCOPE


def scoped_limit_exhausted(limit: dict | None) -> bool:
    """Whether one server-reported bucket is a hard model-family gate."""
    if not isinstance(limit, dict):
        return False
    percent = _number(limit.get("percent"))
    severity = limit.get("severity")
    return (
        percent is not None and percent >= 100.0
    ) or (
        isinstance(severity, str) and severity.casefold() == "critical"
    )


def scoped_limit_for(account: dict, model_family: str) -> dict | None:
    """Return the most binding scoped bucket for a Claude model family."""
    target = _model_display_name(model_family).casefold()
    matching = [
        limit
        for limit in account.get("scoped_limits") or []
        if isinstance(limit, dict)
        and isinstance(limit.get("scope_model"), str)
        and limit["scope_model"].casefold() == target
    ]
    if not matching:
        return None

    severity_rank = {"normal": 0, "warning": 1, "critical": 2}

    def binding_key(limit):
        severity = limit.get("severity")
        return (
            scoped_limit_exhausted(limit),
            _number(limit.get("percent")) or 0.0,
            severity_rank.get(severity.casefold(), 0)
            if isinstance(severity, str) else 0,
        )

    return max(matching, key=binding_key)


def model_window_for(account: dict, model: str) -> dict | None:
    """Return a live per-model seven-day bucket when the endpoint reported it."""
    family = _model_display_name(model).casefold()
    windows = account.get("model_windows") or {}
    if not isinstance(windows, dict):
        return None
    window = windows.get(family)
    return window if isinstance(window, dict) else None


def model_headroom_score(account: dict, model: str, *,
                         measured_only: bool = False) -> float | None:
    """Worst headroom for ranking, or provider-measured headroom for gating.

    Historical token ratios may rank lanes, but cannot establish exhaustion.
    Rows without a separate measured field retain their existing semantics.
    """
    values = []
    base = _number(account.get("measured_headroom_score", account.get("headroom_score"))
                   if measured_only else account.get("headroom_score"))
    if base is not None:
        values.append(base)
    model_used = _number((model_window_for(account, model) or {}).get("used_percent"))
    if model_used is not None:
        values.append(max(0.0, min(100.0, 100.0 - model_used)))
    scoped_used = _number((scoped_limit_for(account, model) or {}).get("percent"))
    if scoped_used is not None:
        values.append(max(0.0, min(100.0, 100.0 - scoped_used)))
    return min(values) if values else None


def model_cooldown_for(account: dict, model: str) -> str | None:
    """Serialized active cooldown for the exact model (account gate is separate)."""
    scopes = account.get("model_cooldowns") or {}
    if not isinstance(scopes, dict):
        return None
    target = _cooldown_scope(model)
    exact = scopes.get(target)
    if isinstance(exact, str):
        return exact
    # Rows written by an older process may still carry a retired pin as the
    # scope key; match by canonical id so the cooldown is not silently lost.
    for raw_scope, value in scopes.items():
        if (
            isinstance(raw_scope, str) and isinstance(value, str)
            and raw_scope != ACCOUNT_COOLDOWN_SCOPE
            and _cooldown_scope(raw_scope) == target
        ):
            return value
    return None


def account_cooldown_for(account: dict) -> str | None:
    scopes = account.get("cooldowns") or {}
    if not isinstance(scopes, dict):
        return None
    value = scopes.get(ACCOUNT_COOLDOWN_SCOPE)
    return value if isinstance(value, str) else None


def dispatchable_for(account: dict, model_family: str) -> bool:
    """Model-aware gate: account quota, persisted scope, then live model data."""
    headroom = model_headroom_score(account, model_family, measured_only=True)
    return (
        bool(account.get("dispatchable"))
        and account_cooldown_for(account) is None
        and model_cooldown_for(account, model_family) is None
        and not scoped_limit_exhausted(scoped_limit_for(account, model_family))
        and (headroom is None or headroom >= DEFAULT_MIN_HEADROOM)
    )


def model_state_for(account: dict, model: str) -> dict:
    """Terse JSON-safe state for one lane/model pair."""
    canonical = normalize_claude_model(model) or str(model)
    window = model_window_for(account, canonical)
    limit = scoped_limit_for(account, canonical)
    cooldown = model_cooldown_for(account, canonical)
    account_cooldown = account_cooldown_for(account)
    headroom = model_headroom_score(account, canonical)
    measured_headroom = model_headroom_score(account, canonical, measured_only=True)
    known_used = [
        value for value in (
            _number((window or {}).get("used_percent")),
            _number((limit or {}).get("percent")),
        )
        if value is not None
    ]
    if account_cooldown:
        state, until = "cooled", account_cooldown
    elif cooldown:
        state, until = "cooled", cooldown
    elif scoped_limit_exhausted(limit):
        state, until = "limited", limit.get("resets_at") if limit else None
    elif not account.get("dispatchable"):
        state = str(account.get("status") or "unavailable")
        until = account.get("limited_until")
    elif measured_headroom is not None and measured_headroom < DEFAULT_MIN_HEADROOM:
        state = "exhausted"
        resets = [
            value for value in (
                (window or {}).get("reset_at"),
                (limit or {}).get("resets_at"),
            )
            if isinstance(value, str)
        ]
        until = max(resets) if resets else None
    else:
        state, until = "ok", None
    return {
        "state": state,
        "until": until,
        "used_percent": max(known_used) if known_used else None,
        "headroom_score": headroom,
        "measured_headroom_score": measured_headroom,
    }


def claude_model_states(account: dict) -> dict[str, dict]:
    """States for the dispatch models surfaced by `pick claude --json`."""
    return {
        full_id: model_state_for(account, full_id)
        for full_id in CLAUDE_MODEL_IDS.values()
    }


def _token_number(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _window(*, used_percent: float | int | None = None,
            tokens: int | None = None, capacity: int | None = None,
            reset_at: str | None = None, confidence: str | None = None) -> dict:
    return {
        "used_percent": used_percent,
        "tokens": tokens,
        "capacity": capacity,
        "reset_at": reset_at,
        "confidence": confidence,
    }


def _sanitize(value: Any) -> Any:
    """Remove credentials and bulky raw probe payloads before caching."""
    if isinstance(value, dict):
        return {
            key: _sanitize(item)
            for key, item in value.items()
            if not key.startswith("_")
            and key not in {"raw", "access_token", "refresh_token", "token"}
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Claude lane usage ledger
# ---------------------------------------------------------------------------


def read_ledger(path: Path | str | None = None) -> list[dict]:
    """Read valid JSON-object lines, ignoring missing/corrupt records."""
    ledger = Path(path) if path is not None else paths.lane_usage_path()
    records: list[dict] = []
    try:
        with ledger.open() as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except (OSError, UnicodeError):
        pass
    return records


def append_ledger(record: dict, path: Path | str | None = None) -> bool:
    """Append one compact JSON record. Ledger failures are deliberately soft."""
    ledger = Path(path) if path is not None else paths.lane_usage_path()
    try:
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a") as stream:
            stream.write(line)
        return True
    except (OSError, TypeError, ValueError):
        return False


def parse_transcript_usage(path: Path | str) -> dict[str, int]:
    """Sum per-message Claude usage, with the last copy of each message winning.

    Claude transcripts may repeat an assistant message as it is updated. Only
    `message.id` identifies the billable message; counting JSONL rows would
    therefore overstate usage.
    """
    latest: dict[str, tuple[int, int]] = {}
    transcript = Path(path)
    try:
        with transcript.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                message = event.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                input_tokens = sum(
                    _token_number(usage.get(key)) or 0
                    for key in (
                        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                    )
                )
                output_tokens = _token_number(usage.get("output_tokens"))
                if input_tokens == 0 and output_tokens is None and not any(
                    key in usage for key in (
                        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                    )
                ):
                    continue
                latest[message["id"]] = (input_tokens or 0, output_tokens or 0)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read transcript: {exc}") from exc
    if not latest:
        raise ValueError("no per-message usage found in transcript")
    input_total = sum(item[0] for item in latest.values())
    output_total = sum(item[1] for item in latest.values())
    return {
        "input_tokens": input_total,
        "output_tokens": output_total,
        "total_tokens": input_total + output_total,
    }


def _usage_total(record: dict) -> int | None:
    if record.get("event") or record.get("error"):
        return None
    total = _token_number(record.get("total_tokens"))
    if total is not None:
        return total
    input_tokens = _token_number(record.get("input_tokens"))
    output_tokens = _token_number(record.get("output_tokens"))
    if input_tokens is None and output_tokens is None:
        return None
    return (input_tokens or 0) + (output_tokens or 0)


def rolling_token_sums(email: str, *, now: datetime | None = None,
                       records: Iterable[dict] | None = None,
                       path: Path | str | None = None) -> dict[str, int]:
    """Token sums in inclusive [now-window, now] intervals."""
    current = _clock(now)
    records = read_ledger(path) if records is None else records
    five_hour = 0
    weekly = 0
    for record in records:
        if record.get("email") != email:
            continue
        timestamp = _parse_time(record.get("ts"))
        total = _usage_total(record)
        if timestamp is None or total is None or timestamp > current:
            continue
        if timestamp >= current - SEVEN_DAYS:
            weekly += total
        if timestamp >= current - FIVE_HOURS:
            five_hour += total
    return {"five_hour": five_hour, "weekly": weekly}


def active_keepalive_window(email: str, *, now: datetime | None = None,
                            records: Iterable[dict] | None = None,
                            path: Path | str | None = None) -> dict | None:
    """Latest observed keepalive-opened window, until its exact 5h expiry."""
    current = _clock(now)
    records = read_ledger(path) if records is None else records
    opened = [
        timestamp
        for record in records
        if record.get("kind") == "keepalive"
        and str(record.get("email") or "").casefold() == email.casefold()
        and (timestamp := _parse_time(record.get("ts"))) is not None
        and timestamp <= current
    ]
    if not opened:
        return None
    latest = max(opened)
    reset = latest + FIVE_HOURS
    if current >= reset:
        return None
    return {"opened_at": iso(latest), "reset_at": iso(reset), "confidence": "observed"}


def learned_capacities(email: str, *, records: Iterable[dict] | None = None,
                       path: Path | str | None = None) -> dict[str, int | None]:
    """Historical token denominators for ranking, not provider quota limits.

    Legacy unscoped events do not identify which window or model was binding.
    Their cumulative token sums cannot establish current account exhaustion.
    """
    records = read_ledger(path) if records is None else records
    five_hour: int | None = None
    weekly: int | None = None
    for record in records:
        if record.get("email") != email or record.get("event") != "hard_limit":
            continue
        # Legacy events had no model and remain account-wide. New model-bucket
        # limits must not calibrate the generic weekly window or they would
        # make healthy models look exhausted from mixed-model token totals.
        if _cooldown_scope(record.get("model")) != ACCOUNT_COOLDOWN_SCOPE:
            continue
        observed_5h = _token_number(record.get("window_tokens_5h"))
        observed_7d = _token_number(record.get("window_tokens_7d"))
        if observed_5h:
            five_hour = max(five_hour or 0, observed_5h)
        if observed_7d:
            weekly = max(weekly or 0, observed_7d)
    return {"five_hour": five_hour, "weekly": weekly}


def _resolve_transcript(session_id: str, transcript_path: Path | str | None,
                        workdir: Path | str | None) -> Path:
    if transcript_path is not None:
        return Path(transcript_path)
    candidates: list[Path] = []
    if workdir is not None:
        direct = Path(workdir) / f"{session_id}.jsonl"
        if direct.is_file():
            candidates.append(direct)
    projects = paths.claude_dir() / "projects"
    try:
        direct = projects / f"{session_id}.jsonl"
        if direct.is_file():
            candidates.append(direct)
        # Primary Claude Code transcripts live exactly one project directory
        # below `projects`; avoid an unbounded recursive walk of the worktree.
        candidates.extend(candidate for candidate in projects.glob(
            f"*/{session_id}.jsonl"
        ) if candidate.is_file())
    except OSError:
        pass
    if not candidates:
        raise ValueError(f"transcript not found for session {session_id}")
    try:
        return max(candidates, key=lambda candidate: candidate.stat().st_mtime)
    except OSError:
        return candidates[-1]


def _error_text(error: str | Path | None) -> str:
    if error is None:
        return ""
    if isinstance(error, Path):
        try:
            return error.read_text(errors="replace")
        except OSError:
            return str(error)
    return str(error)


def _reset_value(reset: datetime | str | None, error: str | Path | None,
                 event_time: datetime) -> str | None:
    if isinstance(reset, datetime):
        return iso(_clock(reset))
    if isinstance(reset, str):
        parsed = parse_iso(reset)
        if parsed:
            return iso(parsed)
        parsed = parse_reset_clock(reset, event_time)
        if parsed:
            return iso(parsed)
    text = _error_text(error)
    parsed = parse_reset_clock(text, event_time) if text else None
    return iso(parsed)


def _normalize_cooldown_data(data: Any) -> dict[str, dict[str, str]]:
    """Normalize legacy flat cooldowns and the model-scoped ledger shape."""
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for lane, value in data.items():
        if not isinstance(lane, str):
            continue
        raw_scopes = {ACCOUNT_COOLDOWN_SCOPE: value} if isinstance(value, str) else value
        if not isinstance(raw_scopes, dict):
            continue
        scopes: dict[str, str] = {}
        for raw_scope, raw_until in raw_scopes.items():
            if not isinstance(raw_scope, str) or not isinstance(raw_until, str):
                continue
            scope = ACCOUNT_COOLDOWN_SCOPE if raw_scope == ACCOUNT_COOLDOWN_SCOPE \
                else _cooldown_scope(raw_scope)
            current = _parse_time(scopes.get(scope))
            candidate = _parse_time(raw_until)
            if candidate is None:
                continue
            scopes[scope] = iso(max(current, candidate) if current else candidate)
        if scopes:
            normalized[lane] = scopes
    return normalized


def read_lane_cooldowns() -> dict[str, dict[str, str]]:
    """Read cooldowns in canonical nested form without changing the file."""
    return _normalize_cooldown_data(load_json(paths.delegate_cooldowns_path(), {}) or {})


def store_lane_cooldown(email: str, until: datetime, *, model: str | None = None) -> None:
    """Atomically extend one account or model cooldown; never shorten it."""
    path = paths.delegate_cooldowns_path()
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = _normalize_cooldown_data(load_json(path, {}) or {})
            scopes = data.setdefault(email, {})
            scope = _cooldown_scope(model)
            current = _parse_time(scopes.get(scope))
            candidate = _clock(until)
            scopes[scope] = iso(max(current, candidate) if current else candidate)
            atomic_write_json(path, data)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def clear_lane_cooldown(email: str) -> bool:
    """Clear an auth cooldown after successful re-enrollment under the same lock."""
    path = paths.delegate_cooldowns_path()
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = _normalize_cooldown_data(load_json(path, {}) or {})
            data.pop(email, None)
            atomic_write_json(path, data)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return True
    except OSError:
        return False


def _store_cooldown(email: str, until: datetime, *, model: str | None = None) -> None:
    """Backward-compatible private alias for the lane runner hook."""
    store_lane_cooldown(email, until, model=model)


def record_lane_run(email: str, session_id: str, rc: int,
                    transcript_path: Path | str | None = None, *,
                    workdir: Path | str | None = None,
                    model: str | None = None,
                    error: str | Path | None = None,
                    reset: datetime | str | None = None,
                    now: datetime | None = None,
                    ledger_path: Path | str | None = None) -> None:
    """Record one Claude lane run without ever changing the runner's outcome.

    On rc=4 the normal usage (or parse-error record) is appended first, then a
    hard-limit calibration event whose sums include that just-finished run.
    """
    event_time = _clock(now)
    stamp = iso(event_time)
    model_scope = _cooldown_scope(model)
    try:
        transcript = _resolve_transcript(session_id, transcript_path, workdir)
        usage = parse_transcript_usage(transcript)
        append_ledger(
            {"ts": stamp, "email": email, "session_id": session_id, **usage},
            ledger_path,
        )
    except Exception as exc:  # A monitoring sidecar must never fail the run.
        append_ledger(
            {"ts": stamp, "email": email, "error": str(exc)[:500]},
            ledger_path,
        )
    if rc == 4:
        try:
            sums = rolling_token_sums(email, now=event_time, path=ledger_path)
            limited_until = _reset_value(reset, error, event_time)
            if limited_until is None:
                # Match delegate's established cooldown fallback. A missing
                # reset must never make a just-limited lane immediately
                # dispatchable again, but we also avoid inventing a full week.
                limited_until = iso(event_time + UNKNOWN_LIMIT_FALLBACK)
            append_ledger(
                {
                    "ts": stamp,
                    "email": email,
                    "event": "hard_limit",
                    "model": model_scope,
                    "window_tokens_5h": sums["five_hour"],
                    "window_tokens_7d": sums["weekly"],
                    "reset": limited_until,
                },
                ledger_path,
            )
            parsed_limit = _parse_time(limited_until)
            if parsed_limit:
                _store_cooldown(email, parsed_limit, model=model_scope)
        except Exception:
            pass
    elif rc == 5:
        _store_cooldown(email, event_time + timedelta(days=30))
    return None


# ---------------------------------------------------------------------------
# Live probes and normalized account rows
# ---------------------------------------------------------------------------


def _score(five_hour: dict, weekly: dict) -> float | None:
    percentages = [
        float(value)
        for value in (five_hour.get("used_percent"), weekly.get("used_percent"))
        if _number(value) is not None
    ]
    if not percentages:
        return None
    return round(min(100.0, max(0.0, min(100.0 - value for value in percentages))), 2)


def _codex_dispatch_score(weekly: dict, now: datetime) -> float | None:
    """Higher is better: the soonest weekly reset has the least-negative score."""
    # A just-past clock is normal while WHAM propagates a natural reset. Treat
    # it as due-now, matching snapshot.rank_for_dispatch, rather than demoting
    # the lane behind every future reset.
    reset = _parse_time(weekly.get("reset_at"))
    if reset is None:
        return None
    return round(-max(0.0, (reset - now).total_seconds()), 2)


def _live_window(value: dict | None) -> dict:
    value = value if isinstance(value, dict) else {}
    used_percent = value.get("used_percent")
    return _window(
        used_percent=used_percent,
        reset_at=value.get("reset_at"),
        confidence="live" if _number(used_percent) is not None else None,
    )


def _probe_codex_rows(timeout: float, now: datetime) -> list[dict]:
    homes = paths.codex_homes()
    auths = [codex.read_auth(home) for home in homes]
    unique: list[tuple[Path, dict]] = []
    aliases: dict[str, list[str]] = {}
    seen_accounts: dict[str, str] = {}
    for home, auth in zip(homes, auths):
        account_id = auth.get("account_id")
        if account_id and account_id in seen_accounts:
            aliases.setdefault(seen_accounts[account_id], []).append(str(home))
            continue
        unique.append((home, auth))
        if account_id:
            seen_accounts[account_id] = str(home)

    probes = codex.probe_all([auth for _, auth in unique], timeout=timeout)
    rows = []
    primary_home = paths.primary_codex_home()
    protected = codex.protected_account()
    for (home, auth), probe in zip(unique, probes):
        # Duration-classified by the probe (wham's positional meaning drifts;
        # by 2026-07-25 primary IS the weekly window and no 5h is reported).
        # Windows without duration metadata fall back to positional meaning.
        five_src, week_src = probe.get("five_hour"), probe.get("weekly")
        if five_src is None and week_src is None:
            five_src, week_src = codex.classify_windows(
                probe.get("primary"), probe.get("secondary")
            )
        if five_src is None and week_src is None:
            five_src, week_src = probe.get("primary"), probe.get("secondary")
        five_hour = _live_window(five_src)
        weekly = _live_window(week_src)
        probe_status = probe.get("status") or "unknown"
        measured_headroom = _score(five_hour, weekly)
        if auth.get("status") != "ok":
            status = "no-auth"
        elif probe_status != "ok":
            status = probe_status
        elif codex.plan_is_free(probe.get("plan_type")):
            status = "free-plan"
        elif probe.get("limit_reached") or probe.get("allowed") is False:
            status = "limited"
        elif measured_headroom is not None and measured_headroom < DEFAULT_MIN_HEADROOM:
            status = "exhausted"
        elif measured_headroom is None:
            status = "no-window-data"
        else:
            status = "ok"
        account_id = auth.get("account_id")
        is_primary = home == primary_home
        # Preserve app/protected identity as displayed warning and reset-policy
        # metadata. It deliberately does not affect Codex dispatch order.
        spared = (
            codex.is_protected_account(
                auth.get("email") or probe.get("email"), account_id, protected)
            if protected is not None else is_primary
        )
        headroom = 0.0 if status in {"limited", "exhausted"} else measured_headroom
        recent_errors = codex.recent_limit_errors(home, hours=12)
        row = {
                "family": "codex",
                "id": str(home),
                "email": auth.get("email") or probe.get("email"),
                "reset_credits": codex.reset_credits_from_probe(probe),
                "five_hour": five_hour,
                "weekly": weekly,
                "scoped_limits": [],
                "spend": None,
                "extra_usage": None,
                "learned_capacity": None,
                "limited_until": _window_limit(five_hour, weekly, now)
                if status in {"limited", "exhausted"} else None,
                "confidence": "live",
                "status": status,
                "base_status": status,
                "dispatchable": status == "ok",
                "headroom_score": headroom,
                "dispatch_score": _codex_dispatch_score(weekly, now)
                if status == "ok" else None,
                "home": str(home),
                "homes": [str(home), *aliases.get(str(home), [])],
                "account_id": account_id,
                "is_primary_home": is_primary,
                "is_protected_account": spared,
                "is_shadowed_by_app": bool(
                    spared and protected and protected.get("source") == "app-home"
                ),
                "probe_status": probe_status,
                "limit_reached": probe.get("limit_reached") is True,
                "recent_errors": recent_errors,
            }
        rows.append(_apply_codex_dynamic_gate(row, now))
    return rows


def active_claude_capacity_row(identity: dict, probe: dict, *,
                               now: datetime | None = None) -> dict | None:
    """Normalize one active desktop-login probe without touching lane tokens."""
    current = _clock(now)
    email = identity.get("email")
    account_id = email or identity.get("account_uuid")
    if not account_id:
        return None
    five_hour = _live_window(probe.get("five_hour"))
    weekly = _live_window(probe.get("seven_day"))
    model_windows = {
        key.removeprefix("seven_day_").casefold(): _live_window(value)
        for key, value in (probe.get("windows") or {}).items()
        if isinstance(key, str) and key.startswith("seven_day_")
        and isinstance(value, dict)
    }
    status = probe.get("status") or "unknown"
    if status == "ok" and _score(five_hour, weekly) == 0:
        status = "exhausted"
    return {
        "family": "claude",
        "id": str(account_id),
        "email": email,
        "five_hour": five_hour,
        "weekly": weekly,
        "model_windows": model_windows,
        "scoped_limits": copy.deepcopy(probe.get("limits"))
        if isinstance(probe.get("limits"), list) else [],
        "spend": copy.deepcopy(probe.get("spend"))
        if isinstance(probe.get("spend"), dict) else None,
        "extra_usage": copy.deepcopy(probe.get("extra_usage"))
        if isinstance(probe.get("extra_usage"), dict) else None,
        "learned_capacity": None,
        "limited_until": _window_limit(five_hour, weekly, current)
        if status in {"rate-limited", "limited", "exhausted"} else None,
        "confidence": "live",
        "status": status,
        "dispatchable": False,
        "headroom_score": 0.0 if status in {"rate-limited", "limited", "exhausted"}
        else _score(five_hour, weekly),
        "dispatch_score": None,
        "active": True,
        "probe_status": probe.get("status") or "unknown",
    }


def _probe_active_claude(timeout: float, now: datetime) -> list[dict]:
    identity = claude.identity()
    if not (identity.get("email") or identity.get("account_uuid")):
        return []
    credentials = claude.keychain_credentials()
    probe = claude.probe_oauth_usage(credentials.get("_token"), timeout=timeout)
    row = active_claude_capacity_row(identity, probe, now=now)
    return [row] if row else []


def _probe_live_rows(timeout: float, now: datetime) -> list[dict]:
    # Both families have independent credentials/endpoints. Probe them in
    # parallel so a cold cache miss costs roughly one provider timeout rather
    # than their sum (Codex homes are parallelized again inside probe_all).
    with ThreadPoolExecutor(max_workers=2) as executor:
        codex_future = executor.submit(_probe_codex_rows, timeout, now)
        claude_future = executor.submit(_probe_active_claude, timeout, now)
        return codex_future.result() + claude_future.result()


def _roster_config(accounts_file: Path | str | None = None) -> dict:
    try:
        config = (
            load_json(Path(accounts_file), {})
            if accounts_file is not None
            else claude.roster_config()
        )
    except Exception:
        config = {}
    return config if isinstance(config, dict) else {}


def _enrolled_map(config: dict) -> dict[str, str]:
    value = config.get("enrolled") or {}
    if not isinstance(value, dict):
        return {}
    return {
        email: secret.strip()
        for email, secret in value.items()
        if isinstance(email, str) and "@" in email
        and isinstance(secret, str) and bool(secret.strip())
    }


def _roster_fingerprint(config: dict) -> str:
    payload = json.dumps(sorted(_enrolled_map(config).items()), separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _lane_secret_availability(config: dict) -> dict[str, bool]:
    enrolled = _enrolled_map(config)
    if not enrolled:
        return {}
    names = claude.agent_secret_names()
    if names is None:
        return {email: False for email in enrolled}
    return {email: secret in names for email, secret in enrolled.items()}


def _cache_rows(now: datetime, timeout: float, force_refresh: bool,
                ttl_seconds: int, config: dict) -> tuple[list[dict], dict, dict[str, bool]]:
    cache_path = paths.capacity_cache_path()
    cached = load_json(cache_path, {}) or {}
    probed_at = _parse_time(cached.get("probed_at"))
    age = (now - probed_at).total_seconds() if probed_at else None
    roster_fingerprint = _roster_fingerprint(config)
    valid = (
        not force_refresh
        and isinstance(cached.get("accounts"), list)
        and isinstance(cached.get("lane_secret_available"), dict)
        and cached.get("roster_fingerprint") == roster_fingerprint
        and age is not None
        and 0 <= age < ttl_seconds
    )
    if valid:
        availability = {
            str(email): bool(available)
            for email, available in cached["lane_secret_available"].items()
        }
        return copy.deepcopy(cached["accounts"]), {
            "hit": True,
            "probed_at": cached.get("probed_at"),
            "age_seconds": round(age, 3),
            "ttl_seconds": ttl_seconds,
        }, availability
    # The keychain name listing is local and independent of both network
    # families. Run it alongside the live probes and cache only booleans.
    with ThreadPoolExecutor(max_workers=2) as executor:
        rows_future = executor.submit(_probe_live_rows, timeout, now)
        secrets_future = executor.submit(_lane_secret_availability, config)
        rows = _sanitize(rows_future.result())
        availability = secrets_future.result()
    stamp = iso(now)
    try:
        atomic_write_json(cache_path, {
            "probed_at": stamp,
            "accounts": rows,
            "roster_fingerprint": roster_fingerprint,
            "lane_secret_available": availability,
        })
    except OSError:
        pass
    return rows, {
        "hit": False,
        "probed_at": stamp,
        "age_seconds": 0.0,
        "ttl_seconds": ttl_seconds,
    }, availability


# ---------------------------------------------------------------------------
# Dynamic ledger/cooldown merge and family scoring
# ---------------------------------------------------------------------------


def _future(value: str | None, now: datetime) -> datetime | None:
    parsed = _parse_time(value)
    return parsed if parsed and parsed > now else None


def _window_limit(five_hour: dict, weekly: dict, now: datetime) -> str | None:
    exhausted_resets = []
    for window in (five_hour, weekly):
        used = _number(window.get("used_percent"))
        reset = _future(window.get("reset_at"), now)
        if used is not None and used > 100.0 - DEFAULT_MIN_HEADROOM and reset:
            exhausted_resets.append(reset)
    if exhausted_resets:
        # When both windows are exhausted, both must reset before dispatch.
        return iso(max(exhausted_resets))
    # A generic server `limit_reached` normally refers to the 5h window.
    return iso(_future(five_hour.get("reset_at"), now)
               or _future(weekly.get("reset_at"), now))


def _cooldowns(now: datetime) -> dict[str, dict[str, datetime]]:
    return {
        lane: future_scopes
        for lane, scopes in read_lane_cooldowns().items()
        if (future_scopes := {
            scope: parsed
            for scope, value in scopes.items()
            if (parsed := _future(value, now)) is not None
        })
    }


def lane_cooldown(key: str, *, model: str | None = None,
                  any_model: bool = False,
                  now: datetime | None = None) -> datetime | None:
    """Future cooldown applying to a lane and optional exact Claude model.

    Account-wide ``*`` always applies. With ``any_model=True``, return the
    latest active scope, which preserves model-blind pick's historical gate.
    """
    scopes = _cooldowns(_clock(now)).get(str(key), {})
    if any_model:
        candidates = list(scopes.values())
    else:
        candidates = [scopes.get(ACCOUNT_COOLDOWN_SCOPE)]
        if model is not None:
            candidates.append(scopes.get(_cooldown_scope(model)))
    return max((value for value in candidates if value), default=None)


def _apply_codex_dynamic_gate(row: dict, now: datetime) -> dict:
    """Overlay rollout/cooldown short-window gates on a cached Codex row."""
    base_status = row.get("base_status") or row.get("status") or "unknown"
    row["base_status"] = base_status
    row["status"] = base_status
    row["dispatchable"] = base_status == "ok"
    short_until = reset_policy.short_window_limit_until(row, now=now)
    row["short_window_until"] = iso(short_until)
    if short_until and base_status == "ok":
        row["status"] = "cooldown"
        row["dispatchable"] = False
        row["limited_until"] = iso(short_until)
    elif base_status == "ok":
        row["limited_until"] = None
    row["dispatch_score"] = (
        _codex_dispatch_score(row.get("weekly") or {}, now)
        if row["dispatchable"] else None
    )
    return row


def _hard_limit_until(email: str, records: Iterable[dict], now: datetime,
                      *, model: str | None = None) -> datetime | None:
    target = _cooldown_scope(model)
    resets = []
    for record in records:
        if record.get("email") != email or record.get("event") != "hard_limit":
            continue
        scope = _cooldown_scope(record.get("model"))
        if scope != ACCOUNT_COOLDOWN_SCOPE and scope != target:
            continue
        reset = _future(record.get("reset"), now)
        if reset is None:
            observed = _parse_time(record.get("ts"))
            fallback = observed + UNKNOWN_LIMIT_FALLBACK if observed else None
            reset = fallback if fallback and fallback > now else None
        if reset:
            resets.append(reset)
    return max(resets) if resets else None


def _ratio(tokens: int, capacity: int | None) -> float | None:
    return round(100.0 * tokens / capacity, 2) if capacity else None


def _claude_rows(live_rows: list[dict], now: datetime, records: list[dict],
                 accounts_file: Path | str | None = None, *, config: dict | None = None,
                 secret_availability: dict[str, bool] | None = None) -> list[dict]:
    active_by_email = {
        row.get("email"): row for row in live_rows
        if row.get("family") == "claude" and row.get("email")
    }
    anonymous_active = [
        copy.deepcopy(row) for row in live_rows
        if row.get("family") == "claude" and not row.get("email")
    ]
    config = _roster_config(accounts_file) if config is None else config
    enrolled = set(_enrolled_map(config))
    if secret_availability is None:
        secret_availability = _lane_secret_availability(config)
    known = {
        email for email in (config.get("accounts") or [])
        if isinstance(email, str) and "@" in email
    }
    if accounts_file is None:
        try:
            known.update(
                email for email in claude.known_accounts()
                if isinstance(email, str) and "@" in email
            )
        except Exception:
            pass
    emails = known | enrolled | set(active_by_email)
    cooldowns = _cooldowns(now)
    rows = []
    for email in sorted(emails, key=lambda value: (value not in active_by_email, value)):
        base = copy.deepcopy(active_by_email.get(email))
        active_desktop_row = base is not None
        enrolled_lane = email in enrolled
        sums = rolling_token_sums(email, now=now, records=records)
        learned = learned_capacities(email, records=records)
        learned_value = learned if any(value is not None for value in learned.values()) else None
        if base is None:
            base = {
                "family": "claude",
                "id": email,
                "email": email,
                "five_hour": _window(),
                "weekly": _window(),
                "model_windows": {},
                "scoped_limits": [],
                "spend": None,
                "extra_usage": None,
                "active": False,
                "probe_status": None,
            }
        else:
            if not isinstance(base.get("scoped_limits"), list):
                base["scoped_limits"] = []
            base.setdefault("spend", None)
            base.setdefault("extra_usage", None)
            if not isinstance(base.get("model_windows"), dict):
                base["model_windows"] = {}
        five_hour = base["five_hour"]
        weekly = base["weekly"]
        # Preserve the actual provider readings before filling missing windows
        # with historical ratios. Keepalive reset observations do not measure
        # utilization, and a legacy hard-limit denominator is not a quota.
        measured_windows = (
            (dict(five_hour), dict(weekly))
            if base.get("probe_status") == "ok" else ({}, {})
        )
        measured_score = _score(*measured_windows)
        # An active account without a lane token is represented solely by its
        # desktop live probe. Historical lane-ledger estimates must not hide a
        # live 401/403/429 or masquerade as desktop usage.
        if enrolled_lane or not active_desktop_row:
            five_hour["tokens"] = sums["five_hour"]
            five_hour["capacity"] = learned["five_hour"]
            weekly["tokens"] = sums["weekly"]
            weekly["capacity"] = learned["weekly"]
            for window, tokens, learned_capacity in (
                (five_hour, sums["five_hour"], learned["five_hour"]),
                (weekly, sums["weekly"], learned["weekly"]),
            ):
                live_reading = (
                    base.get("probe_status") == "ok"
                    and _number(window.get("used_percent")) is not None
                )
                if live_reading:
                    window["confidence"] = "live"
                else:
                    window["used_percent"] = _ratio(tokens, learned_capacity)
                    window["confidence"] = "estimated"
            keepalive_window = active_keepalive_window(
                email, now=now, records=records
            )
            if keepalive_window and five_hour.get("confidence") != "live":
                five_hour["reset_at"] = keepalive_window["reset_at"]
                if five_hour.get("used_percent") is None:
                    five_hour["confidence"] = "observed"

        cooldown_scopes = dict(cooldowns.get(email, {}))
        ledger_model_scopes = {
            _cooldown_scope(record.get("model"))
            for record in records
            if record.get("email") == email and record.get("event") == "hard_limit"
            and _cooldown_scope(record.get("model")) != ACCOUNT_COOLDOWN_SCOPE
        }
        for model_scope in ledger_model_scopes:
            observed = _hard_limit_until(
                email, records, now, model=model_scope
            )
            current = cooldown_scopes.get(model_scope)
            if observed:
                cooldown_scopes[model_scope] = max(current, observed) if current else observed

        limits = [value for value in (
            cooldown_scopes.get(ACCOUNT_COOLDOWN_SCOPE),
            _hard_limit_until(email, records, now),
        ) if value]
        base_limit = _future(base.get("limited_until"), now)
        if base_limit:
            limits.append(base_limit)
        limited_until = max(limits) if limits else None
        score = _score(five_hour, weekly)
        live_hard_limit = base.get("probe_status") == "rate-limited"
        if not enrolled_lane:
            live_status = base.get("status")
            status = live_status if active_desktop_row and live_status not in {None, "ok"} \
                else "not-enrolled"
        elif not secret_availability.get(email, False):
            status = "secret-missing"
        elif limited_until or live_hard_limit:
            status = "limited"
        elif measured_score is not None and measured_score < DEFAULT_MIN_HEADROOM:
            status = "exhausted"
        else:
            # A failed desktop app-token probe does not make its separately
            # enrolled inference-token lane unavailable.
            status = "ok"
        if status in {"limited", "exhausted", "rate-limited"}:
            score = 0.0
        if status == "exhausted" and limited_until is None:
            limited_until = _parse_time(_window_limit(*measured_windows, now))
        dispatchable = enrolled_lane and status == "ok"
        dispatch_score = (
            round(score - (INTERACTIVE_HANDICAP if base.get("active") else 0.0), 2)
            if score is not None else None
        )
        labels = {
            window.get("confidence") for window in (five_hour, weekly)
            if window.get("confidence")
        }
        confidence = next(iter(labels)) if len(labels) == 1 else "mixed"
        if active_desktop_row and not enrolled_lane:
            confidence = base.get("confidence") or confidence
        base.update(
            {
                "id": email,
                "email": email,
                "five_hour": five_hour,
                "weekly": weekly,
                "model_windows": base.get("model_windows") or {},
                "cooldowns": {
                    scope: iso(value) for scope, value in cooldown_scopes.items()
                },
                "model_cooldowns": {
                    scope: iso(value) for scope, value in cooldown_scopes.items()
                    if scope != ACCOUNT_COOLDOWN_SCOPE
                },
                "learned_capacity": learned_value,
                "limited_until": iso(limited_until),
                "confidence": confidence,
                "status": status,
                "dispatchable": dispatchable,
                "headroom_score": score,
                "measured_headroom_score": measured_score,
                "dispatch_score": dispatch_score,
                "enrolled": enrolled_lane,
                "secret_available": secret_availability.get(email) if enrolled_lane else None,
            }
        )
        base["model_states"] = claude_model_states(base)
        rows.append(base)
    for row in anonymous_active:
        if not isinstance(row.get("scoped_limits"), list):
            row["scoped_limits"] = []
        row.setdefault("spend", None)
        row.setdefault("extra_usage", None)
        row.setdefault("model_windows", {})
        row.setdefault("cooldowns", {})
        row.setdefault("model_cooldowns", {})
        live_status = row.get("status")
        row.update({
            "learned_capacity": None,
            "status": live_status if live_status not in {None, "ok"} else "not-enrolled",
            "dispatchable": False,
            "enrolled": False,
            "secret_available": None,
        })
        row["model_states"] = claude_model_states(row)
        rows.append(row)
    return rows


def claude_lane_rows(*, active_identity: dict | None = None,
                     active_probe: dict | None = None,
                     now: datetime | None = None,
                     accounts_file: Path | str | None = None,
                     records: list[dict] | None = None) -> list[dict]:
    """Ledger-backed rows for monitors that already probed the desktop token.

    Enrolled service names are checked for keychain presence, but setup-token
    values are never read here and are never sent to the usage endpoint.
    """
    current = _clock(now)
    live_rows = []
    if active_identity is not None:
        row = active_claude_capacity_row(
            active_identity, active_probe or {"status": "skipped"}, now=current
        )
        if row:
            live_rows.append(row)
    rows = _claude_rows(
        live_rows,
        current,
        read_ledger() if records is None else records,
        accounts_file=accounts_file,
    )
    return _with_in_flight(rows)


def _with_in_flight(rows: list[dict]) -> list[dict]:
    """Decorate uncached capacity rows with current ledger concurrency."""
    counts = run_ledger.in_flight_counts()
    for row in rows:
        family = row.get("family")
        if family == "codex":
            lanes = row.get("homes") or [row.get("home") or row.get("id")]
            row["in_flight"] = sum(
                counts.get(("codex", str(lane)), 0)
                for lane in dict.fromkeys(lanes) if lane
            )
        elif family == "claude":
            lane = row.get("email") or row.get("id")
            row["in_flight"] = counts.get(
                ("claude", str(lane).casefold()), 0
            ) if lane else 0
        else:
            row["in_flight"] = 0
    return rows


def _raw_tokens(row: dict) -> tuple[float, float]:
    weekly = _token_number((row.get("weekly") or {}).get("tokens"))
    five_hour = _token_number((row.get("five_hour") or {}).get("tokens"))
    return (float("inf") if weekly is None else weekly,
            float("inf") if five_hour is None else five_hour)


def _best_dispatchable(rows: list[dict]) -> dict | None:
    available = [row for row in rows if row.get("dispatchable")]
    if not available:
        return None
    # The desktop login is the last resort (Max, standing order): any other
    # dispatchable lane — measured or blind — comes first; alone, it serves.
    non_login = [row for row in available if not row.get("active")]
    available = non_login or available
    known = [row for row in available if row.get("dispatch_score", row.get("headroom_score")) is not None]
    pool = known or available
    if pool and all(row.get("family") == "codex" for row in pool):
        # Codex is a waterfall by weekly expiry. In-flight remains displayed,
        # but never spreads work away from the earliest-reset window.
        return min(
            pool,
            key=lambda row: (
                row.get("dispatch_score") is None,
                -float(row.get("dispatch_score") or 0.0),
                str(row.get("id") or row.get("home") or ""),
            ),
        )
    if known:
        return min(
            pool,
            key=lambda row: (
                -float(row.get("dispatch_score", row["headroom_score"])),
                int(row.get("in_flight") or 0),
                *_raw_tokens(row),
                str(row["id"]),
            ),
        )
    return min(
        pool,
        key=lambda row: (
            int(row.get("in_flight") or 0),
            bool(row.get("active")),
            *_raw_tokens(row),
            str(row["id"]),
        ),
    )


def family_summaries(accounts: list[dict], *, now: datetime | None = None) -> dict:
    current = _clock(now)
    summaries = {}
    for family in ("codex", "claude"):
        rows = [row for row in accounts if row.get("family") == family]
        relevant = rows if family == "codex" else [row for row in rows if row.get("enrolled")]
        best = _best_dispatchable(relevant)
        limited_statuses = {"limited", "exhausted", "cooldown"}
        all_limited = bool(relevant) and all(
            row.get("status") in limited_statuses for row in relevant
        )
        if best:
            state = "available"
        elif all_limited:
            state = "limited"
        elif relevant:
            state = "unknown"
        else:
            state = "empty"
        resets = [
            reset for row in relevant
            if (reset := _future(row.get("limited_until"), current)) is not None
        ]
        summaries[family] = {
            "available": best is not None,
            "all_limited": all_limited,
            "state": state,
            "headroom_score": (
                best.get("headroom_score") if best else (0.0 if all_limited else None)
            ),
            "best": best.get("id") if best else None,
            "best_id": best.get("id") if best else None,
            "best_email": best.get("email") if best else None,
            "confidence": best.get("confidence") if best else None,
            "dispatchable": sum(bool(row.get("dispatchable")) for row in relevant),
            "accounts": len(rows),
            "earliest_reset": iso(min(resets)) if resets else None,
        }
    return summaries


def collect(*, force_refresh: bool = False, now: datetime | None = None,
            timeout: float = 15.0, ttl_seconds: int = CACHE_TTL_SECONDS,
            accounts_file: Path | str | None = None) -> dict:
    """Return the complete cached-live + fresh-ledger capacity report."""
    current = _clock(now)
    config = _roster_config(accounts_file)
    live_rows, cache, secret_availability = _cache_rows(
        current, timeout, force_refresh, ttl_seconds, config
    )
    records = read_ledger()
    codex_rows = [copy.deepcopy(row) for row in live_rows if row.get("family") == "codex"]
    # Normalize pre-feature cache rows during their short remaining TTL too.
    for row in codex_rows:
        row["reset_credits"] = codex.reset_credits_from_probe(row)
        home = row.get("home") or row.get("id")
        if home:
            row["recent_errors"] = codex.recent_limit_errors(Path(str(home)), hours=12)
        _apply_codex_dynamic_gate(row, current)
    accounts = _with_in_flight(codex_rows + _claude_rows(
        live_rows,
        current,
        records,
        accounts_file=accounts_file,
        config=config,
        secret_availability=secret_availability,
    ))
    return {
        "generated_at": iso(current),
        "cache": cache,
        "accounts": accounts,
        "families": family_summaries(accounts, now=current),
    }


def report(**kwargs) -> dict:
    """Public noun-style alias used by CLI and delegate integrations."""
    return collect(**kwargs)


def _format_window(window: dict | None) -> str:
    window = window or {}
    percent = _number(window.get("used_percent"))
    if percent is not None:
        return f"{percent:.0f}%"
    tokens = _token_number(window.get("tokens"))
    return f"{tokens} tok" if tokens is not None else "-"


def _format_scoped_limit(limit: dict) -> str:
    model = str(limit.get("scope_model") or "?").casefold()
    group = str(limit.get("group") or limit.get("kind") or "limit").casefold()
    percent = _number(limit.get("percent"))
    percent_text = f"{percent:.0f}%" if percent is not None else "?%"
    severity = str(limit.get("severity") or "unknown").casefold()
    reset = str(limit.get("resets_at") or "?")
    marker = " BLOCKED" if scoped_limit_exhausted(limit) else ""
    return f"{model}-{group} {percent_text} {severity} resets {reset}{marker}"


def human_table(data: dict) -> str:
    """Compact terminal view; percentages are utilization, not remaining quota."""
    lines = [
        f"AI capacity — {data.get('generated_at', '?')}",
        f"{'family':<7} {'account':<30} {'5h':>10} {'week':>10} "
        f"{'learned':>17} {'limited until':<25} {'confidence':<10} status",
    ]
    for row in data.get("accounts") or []:
        learned = row.get("learned_capacity")
        if learned:
            learned_text = f"{learned.get('five_hour') or '-'} / {learned.get('weekly') or '-'}"
        else:
            learned_text = "-"
        account = row.get("email") or row.get("id") or "?"
        scoped = [
            limit for limit in row.get("scoped_limits") or []
            if isinstance(limit, dict) and limit.get("scope_model")
        ]
        blocked_models = [
            str(limit["scope_model"]) for limit in scoped
            if scoped_limit_exhausted(limit)
        ]
        display_status = row.get("status") or "?"
        if display_status == "ok" and blocked_models:
            display_status = "ok except " + "/".join(blocked_models)
        lines.append(
            f"{row.get('family', '?'):<7} {str(account):<30.30} "
            f"{_format_window(row.get('five_hour')):>10} "
            f"{_format_window(row.get('weekly')):>10} "
            f"{learned_text:>17.17} {str(row.get('limited_until') or '-'):<25.25} "
            f"{str(row.get('confidence') or '-'):<10} {display_status}"
        )
        lines.extend(f"        scoped: {_format_scoped_limit(limit)}" for limit in scoped)
    return "\n".join(lines)
