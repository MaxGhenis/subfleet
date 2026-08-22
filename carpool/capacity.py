"""Cross-family capacity readings for dispatch and operator-facing views.

Live provider probes are cached briefly because ``delegate`` may be invoked in
bursts.  Claude lane setup tokens cannot query the usage endpoint, so their
readings come from a small append-only token ledger instead.  The functions in
this module deliberately accept probe, path, and clock seams so callers and
tests never need live network or keychain access.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import claude, codex, config, paths
from .util import atomic_write_json, iso, load_json, now_local, parse_iso, strip_private

LIVE_CACHE_TTL_SECONDS = 120
DEFAULT_PROBE_TIMEOUT = 5.0
MIN_DISPATCH_HEADROOM = 5.0
FIVE_HOURS = timedelta(hours=5)
SEVEN_DAYS = timedelta(days=7)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _resolve_now(now: datetime | None = None, clock: Callable[[], datetime] | None = None) -> datetime:
    return _aware(now if now is not None else (clock or now_local)())


def _timestamp(value: datetime | str | None) -> str:
    if isinstance(value, str):
        return value
    return iso(_aware(value) if isinstance(value, datetime) else now_local()) or ""


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _email_key(value: Any) -> str | None:
    """Case-insensitive account key; callers keep a separate display spelling."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()


def _record_matches_email(record: Any, email: str) -> bool:
    return isinstance(record, dict) and _email_key(record.get("email")) == _email_key(email)


def _successful_usage_record(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and not record.get("error")
        and not record.get("event")
        and _token_count(record.get("total_tokens")) is not None
    )


def read_ledger(path: Path | str | None = None) -> list[dict]:
    """Read valid object records from the lane ledger, ignoring torn lines."""
    ledger = Path(path) if path is not None else paths.lane_usage_path()
    try:
        lines = ledger.read_text().splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _append_ledger(record: dict, path: Path | str | None = None) -> None:
    ledger = Path(path) if path is not None else paths.lane_usage_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def parse_transcript_usage(
    transcript_path: Path | str, *, session_id: str | None = None
) -> dict[str, Any]:
    """Sum per-message usage, keeping only the last copy of each message id.

    Claude transcripts can repeat an assistant message while streaming or
    normalizing it.  Summing every occurrence double-counts those messages, so
    only the final usage object for a given message id contributes.
    """
    transcript = Path(transcript_path)
    by_message: dict[str, tuple[int, int]] = {}
    discovered_session = session_id
    with transcript.open() as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at line {line_number}: {exc.msg}") from exc
            if not isinstance(record, dict):
                continue
            discovered_session = (
                discovered_session
                or record.get("session_id")
                or record.get("sessionId")
            )
            message = record.get("message")
            message = message if isinstance(message, dict) else {}
            usage = message.get("usage")
            if not isinstance(usage, dict):
                usage = record.get("usage")
            if not isinstance(usage, dict):
                continue
            message_id = (
                message.get("id")
                or record.get("message_id")
                or record.get("messageId")
                or record.get("uuid")
                or f"line:{line_number}"
            )
            input_tokens = _token_count(usage.get("input_tokens")) or 0
            output_tokens = _token_count(usage.get("output_tokens")) or 0
            by_message[str(message_id)] = (input_tokens, output_tokens)

    if not by_message:
        raise ValueError("transcript contains no message usage records")
    total_input = sum(value[0] for value in by_message.values())
    total_output = sum(value[1] for value in by_message.values())
    return {
        "session_id": discovered_session or transcript.stem,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
    }


def append_lane_usage(
    email: str,
    transcript_path: Path | str,
    *,
    ts: datetime | str | None = None,
    session_id: str | None = None,
    ledger_path: Path | str | None = None,
) -> dict:
    """Parse and append one run without ever raising into the lane runner."""
    stamp = _timestamp(ts)
    try:
        usage = parse_transcript_usage(transcript_path, session_id=session_id)
        record = {"ts": stamp, "email": email, **usage}
    except Exception as exc:  # A failed accounting side effect must not fail inference.
        record = {"ts": stamp, "email": email, "error": str(exc)[:500]}
    try:
        _append_ledger(record, ledger_path)
    except Exception as exc:
        # The caller still receives a useful diagnostic even if the state
        # directory itself is unavailable.  A second append is intentionally
        # not attempted against the same failing destination.
        return {"ts": stamp, "email": email, "error": f"ledger append failed: {exc}"[:500]}
    return record


def rolling_token_sums(
    email: str,
    *,
    now: datetime | None = None,
    entries: Iterable[dict] | None = None,
    ledger_path: Path | str | None = None,
) -> dict[str, int]:
    """Inclusive rolling 5-hour and 7-day token totals for one lane."""
    current = _resolve_now(now)
    records = list(entries) if entries is not None else read_ledger(ledger_path)
    five_hour = 0
    weekly = 0
    for record in records:
        if not _record_matches_email(record, email):
            continue
        if record.get("error") or record.get("event"):
            continue
        observed = parse_iso(record.get("ts"))
        tokens = _token_count(record.get("total_tokens"))
        if observed is None or tokens is None:
            continue
        observed = _aware(observed)
        if observed > current or observed < current - SEVEN_DAYS:
            continue
        weekly += tokens
        if observed >= current - FIVE_HOURS:
            five_hour += tokens
    return {"five_hour": five_hour, "weekly": weekly}


def learned_capacities(
    email: str,
    *,
    entries: Iterable[dict] | None = None,
    ledger_path: Path | str | None = None,
) -> dict[str, int | None]:
    """Largest hard-limit observation for each window, over all history."""
    records = list(entries) if entries is not None else read_ledger(ledger_path)
    five_hour: int | None = None
    weekly: int | None = None
    for record in records:
        if not _record_matches_email(record, email):
            continue
        if record.get("event") != "hard_limit":
            continue
        observed_5h = _token_count(record.get("window_tokens_5h"))
        observed_7d = _token_count(record.get("window_tokens_7d"))
        if observed_5h:
            five_hour = max(five_hour or 0, observed_5h)
        if observed_7d:
            weekly = max(weekly or 0, observed_7d)
    return {"five_hour": five_hour, "weekly": weekly}


def record_hard_limit(
    email: str,
    *,
    reset: datetime | str | None = None,
    ts: datetime | str | None = None,
    entries: Iterable[dict] | None = None,
    ledger_path: Path | str | None = None,
) -> dict:
    """Append a calibration observation using the windows at limit time."""
    stamp = _timestamp(ts)
    at_limit = parse_iso(stamp) or now_local()
    records = list(entries) if entries is not None else read_ledger(ledger_path)
    windows = rolling_token_sums(email, now=at_limit, entries=records)
    prior = learned_capacities(email, entries=records)
    learned = {
        "five_hour": max(prior["five_hour"] or 0, windows["five_hour"]) or None,
        "weekly": max(prior["weekly"] or 0, windows["weekly"]) or None,
    }
    reset_value = iso(_aware(reset)) if isinstance(reset, datetime) else reset
    event = {
        "ts": stamp,
        "email": email,
        "event": "hard_limit",
        "window_tokens_5h": windows["five_hour"],
        "window_tokens_7d": windows["weekly"],
        "reset": reset_value,
        "learned_capacity": learned,
    }
    try:
        _append_ledger(event, ledger_path)
    except Exception as exc:
        return {**event, "error": f"ledger append failed: {exc}"[:500]}
    return event


def _cache_is_fresh(cache: dict, now: datetime, ttl_seconds: float) -> bool:
    if not isinstance(cache, dict) or not isinstance(cache.get("codex"), list):
        return False
    claude_cache = cache.get("claude")
    if not isinstance(claude_cache, dict) or not all(
        isinstance(claude_cache.get(key), dict)
        for key in ("identity", "credentials", "probe")
    ):
        return False
    checked = parse_iso(cache.get("checked_at")) if isinstance(cache, dict) else None
    if checked is None:
        return False
    age = (now - _aware(checked)).total_seconds()
    return 0 <= age <= ttl_seconds


def get_live_probes(
    *,
    force: bool = False,
    ttl_seconds: float = LIVE_CACHE_TTL_SECONDS,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    cache_path: Path | str | None = None,
    codex_homes_fn: Callable[[], Iterable[Path]] | None = None,
    read_auth_fn: Callable[[Path], dict] | None = None,
    probe_all_fn: Callable[..., list[dict]] | None = None,
    claude_identity_fn: Callable[[], dict] | None = None,
    keychain_fn: Callable[[], dict] | None = None,
    claude_probe_fn: Callable[..., dict] | None = None,
) -> dict:
    """Return cached-or-live sanitized provider readings.

    Only the active Claude desktop credential is queried.  Enrolled lane setup
    tokens are intentionally never fetched or sent to the usage endpoint.
    """
    current = _resolve_now(now, clock)
    cache_file = Path(cache_path) if cache_path is not None else paths.capacity_cache_path()
    cached = load_json(cache_file, {}) or {}
    if not force and _cache_is_fresh(cached, current, ttl_seconds):
        return {**cached, "cache_hit": True}

    homes_reader = codex_homes_fn or paths.codex_homes
    auth_reader = read_auth_fn or codex.read_auth
    all_prober = probe_all_fn or codex.probe_all
    identity_reader = claude_identity_fn or claude.identity
    credentials_reader = keychain_fn or claude.keychain_credentials
    oauth_prober = claude_probe_fn or claude.probe_oauth_usage
    stamp = iso(current)

    try:
        homes = [Path(home) for home in homes_reader()]
    except Exception:
        homes = []
    auths: list[dict] = []
    for home in homes:
        try:
            auths.append(auth_reader(home))
        except Exception as exc:
            auths.append({"status": "unreadable", "home": str(home), "error": str(exc)})
    try:
        probes = list(all_prober(auths, timeout=timeout)) if auths else []
    except Exception as exc:
        probes = [
            {"status": "probe-error", "checked_at": stamp, "error": str(exc)}
            for _ in auths
        ]
    while len(probes) < len(auths):
        probes.append({"status": "probe-error", "checked_at": stamp, "error": "missing result"})
    codex_rows = [
        {"home": str(home), "auth": strip_private(auth), "probe": strip_private(probe)}
        for home, auth, probe in zip(homes, auths, probes)
    ]

    try:
        identity = identity_reader()
        identity = identity if isinstance(identity, dict) else {}
    except Exception as exc:
        identity = {"status": "error", "error": str(exc)}
    if not identity.get("email"):
        # A stale keychain item is not an active desktop account.  Standing
        # down here avoids needless keychain and network work in headless runs.
        credentials = {"status": "skipped", "reason": "no-active-login"}
        claude_probe = {"status": "skipped", "reason": "no-active-login", "checked_at": stamp}
    else:
        try:
            credentials = credentials_reader()
            credentials = credentials if isinstance(credentials, dict) else {"status": "unparseable"}
        except Exception as exc:
            credentials = {"status": "error", "error": str(exc)}
        try:
            claude_probe = oauth_prober(credentials.get("_token"), timeout=timeout)
            claude_probe = claude_probe if isinstance(claude_probe, dict) else {"status": "unparseable"}
        except Exception as exc:
            claude_probe = {"status": "probe-error", "checked_at": stamp, "error": str(exc)}

    document = strip_private(
        {
            "checked_at": stamp,
            "codex": codex_rows,
            "claude": {
                "identity": identity,
                "credentials": credentials,
                "probe": claude_probe,
            },
        }
    )
    try:
        atomic_write_json(cache_file, document)
    except OSError:
        pass
    return {**document, "cache_hit": False}


def _percent_reading(window: Any, *, confidence: str = "live") -> dict | None:
    if not isinstance(window, dict) or window.get("used_percent") is None:
        return None
    try:
        used = float(window["used_percent"])
    except (TypeError, ValueError):
        return None
    return {
        "unit": "percent",
        "used": used,
        "used_percent": used,
        "remaining_percent": max(0.0, 100.0 - used),
        "reset_at": window.get("reset_at"),
        "confidence": confidence,
    }


def _token_reading(tokens: int, capacity: int | None) -> dict:
    used_percent = (100.0 * tokens / capacity) if capacity else None
    return {
        "unit": "tokens",
        "used": tokens,
        "tokens": tokens,
        "capacity": capacity,
        "used_percent": used_percent,
        "remaining_percent": max(0.0, 100.0 - used_percent) if used_percent is not None else None,
        "reset_at": None,
        "confidence": "observed" if capacity else "estimated",
    }


def _future(value: Any, now: datetime) -> datetime | None:
    parsed = parse_iso(value) if isinstance(value, str) else value if isinstance(value, datetime) else None
    if parsed is None:
        return None
    parsed = _aware(parsed)
    return parsed if parsed > now else None


def _cooldown_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        candidate = value.get("limited_until") or value.get("until")
        return candidate if isinstance(candidate, str) else None
    return None


def _hard_limit_deadline(record: dict, now: datetime) -> datetime | None:
    """Future reset, or the one-hour rc4 fallback for a missing reset."""
    reset_value = record.get("reset")
    parsed_reset = (
        parse_iso(reset_value)
        if isinstance(reset_value, str)
        else reset_value if isinstance(reset_value, datetime) else None
    )
    if parsed_reset is not None:
        parsed_reset = _aware(parsed_reset)
        return parsed_reset if parsed_reset > now else None
    observed = parse_iso(record.get("ts"))
    if observed is None:
        return None
    fallback = _aware(observed) + timedelta(hours=1)
    return fallback if fallback > now else None


def _pending_hard_limit_resets(
    entries: Iterable[dict], email: str, now: datetime
) -> list[datetime]:
    """Active hard limits since the lane's latest successful ledger run.

    Ledger order is authoritative because adjacent usage and hard-limit records
    can share the same second-resolution timestamp.  A later successful run
    proves older observed limits stale and clears them.
    """
    records = list(entries)
    last_success = -1
    for index, record in enumerate(records):
        observed = parse_iso(record.get("ts")) if isinstance(record, dict) else None
        if (
            _record_matches_email(record, email)
            and _successful_usage_record(record)
            and observed is not None
            and _aware(observed) <= now
        ):
            last_success = index
    resets: list[datetime] = []
    for record in records[last_success + 1:]:
        if not _record_matches_email(record, email) or record.get("event") != "hard_limit":
            continue
        deadline = _hard_limit_deadline(record, now)
        if deadline is not None:
            resets.append(deadline)
    return resets


def _codex_account_row(item: dict, now: datetime) -> dict:
    auth = item.get("auth") if isinstance(item.get("auth"), dict) else {}
    probe = item.get("probe") if isinstance(item.get("probe"), dict) else {}
    home = str(item.get("home") or auth.get("home") or "")
    email = probe.get("email") or auth.get("email")
    account_id = auth.get("account_id") or email or home
    five_hour = _percent_reading(probe.get("primary"))
    weekly = _percent_reading(probe.get("secondary"))
    status = probe.get("status") or auth.get("status") or "unknown"
    limited = bool(probe.get("limit_reached") or probe.get("allowed") is False)
    reset_candidates: list[datetime] = []
    for reading in (five_hour, weekly):
        if reading and reading["remaining_percent"] < MIN_DISPATCH_HEADROOM:
            limited = True
            reset = _future(reading.get("reset_at"), now)
            if reset:
                reset_candidates.append(reset)
    if limited and five_hour:
        reset = _future(five_hour.get("reset_at"), now)
        if reset:
            reset_candidates.append(reset)
    limited_until = iso(max(reset_candidates)) if reset_candidates else None
    dispatchable = (
        status == "ok"
        and five_hour is not None
        and not limited
        and (weekly is None or weekly["used_percent"] < 100.0)
    )
    return {
        "family": "codex",
        "id": str(account_id),
        "email": email,
        "home": home or None,
        "resource": home or str(account_id),
        "five_hour": five_hour,
        "weekly": weekly,
        "learned_capacity": None,
        "limited_until": limited_until,
        "confidence": "live",
        "dispatchable": dispatchable,
        "status": status,
        "auth_status": auth.get("status"),
    }


def _prefer_codex_row(left: dict, right: dict) -> dict:
    """Choose one canonical resource when two homes share an account id."""
    left_key = (bool(left.get("dispatchable")), left.get("status") == "ok")
    right_key = (bool(right.get("dispatchable")), right.get("status") == "ok")
    return right if right_key > left_key else left


def account_rows(
    live: dict,
    *,
    now: datetime | None = None,
    entries: Iterable[dict] | None = None,
    ledger_path: Path | str | None = None,
    cooldowns: Mapping[str, Any] | None = None,
    cooldowns_path: Path | str | None = None,
    enrolled: Mapping[str, Any] | Iterable[str] | None = None,
    known_accounts: Iterable[str] | None = None,
) -> list[dict]:
    """Normalize live probes, lane estimates, calibration, and cooldown state."""
    current = _resolve_now(now)
    records = list(entries) if entries is not None else read_ledger(ledger_path)
    if cooldowns is None:
        cooldown_path = Path(cooldowns_path) if cooldowns_path is not None else paths.cooldowns_path()
        value = load_json(cooldown_path, {}) or {}
        cooldowns = value if isinstance(value, dict) else {}

    cfg = config.load()
    if enrolled is None:
        value = cfg.get("enrolled") or {}
        enrolled = value if isinstance(value, dict) else {}
    if isinstance(enrolled, Mapping):
        enrolled_values = list(enrolled.keys())
    elif isinstance(enrolled, str):
        enrolled_values = [enrolled]
    else:
        enrolled_values = list(enrolled)
    enrolled_by_key = {
        key: value.strip()
        for value in enrolled_values
        if (key := _email_key(value)) is not None
    }
    enrolled_keys = set(enrolled_by_key)
    if known_accounts is None:
        value = cfg.get("accounts") or []
        known_accounts = value if isinstance(value, list) else []
    known_values = [known_accounts] if isinstance(known_accounts, str) else list(known_accounts)

    by_codex_id: dict[str, dict] = {}
    for item in live.get("codex") or []:
        if not isinstance(item, dict):
            continue
        row = _codex_account_row(item, current)
        prior = by_codex_id.get(row["id"])
        by_codex_id[row["id"]] = _prefer_codex_row(prior, row) if prior else row

    claude_live = live.get("claude") if isinstance(live.get("claude"), dict) else {}
    identity = claude_live.get("identity") if isinstance(claude_live.get("identity"), dict) else {}
    active_email = identity.get("email")
    active_key = _email_key(active_email)
    probe = claude_live.get("probe") if isinstance(claude_live.get("probe"), dict) else {}

    display_by_key: dict[str, str] = {}

    def remember_display(value: Any) -> None:
        key = _email_key(value)
        if key is not None and key not in display_by_key:
            display_by_key[key] = value.strip()

    # Public config spelling is canonical; ledger and active-login variants
    # merge into it without changing the resource passed to lane runners.
    for value in known_values:
        remember_display(value)
    for value in enrolled_values:
        remember_display(value)
    for record in records:
        if isinstance(record, dict):
            remember_display(record.get("email"))
    remember_display(active_email)

    cooldowns_by_key: dict[str, list[Any]] = {}
    for cooldown_email, cooldown_value in cooldowns.items():
        key = _email_key(cooldown_email)
        if key is not None:
            cooldowns_by_key.setdefault(key, []).append(cooldown_value)

    claude_rows: list[dict] = []
    for email_key in sorted(display_by_key, key=lambda key: display_by_key[key].casefold()):
        email = display_by_key[email_key]
        sums = rolling_token_sums(email, now=current, entries=records)
        learned = learned_capacities(email, entries=records)
        five_hour = _token_reading(sums["five_hour"], learned["five_hour"])
        weekly = _token_reading(sums["weekly"], learned["weekly"])
        confidence = "observed" if any(learned.values()) else "estimated"
        status = confidence
        active = email_key == active_key
        if active and probe.get("status") == "ok":
            five_hour = _percent_reading(probe.get("five_hour")) or five_hour
            weekly = _percent_reading(probe.get("seven_day")) or weekly
            confidence = "live"
            status = "ok"
        elif active and probe.get("status") == "rate-limited":
            confidence = "live"
            status = "rate-limited"

        pending_limit_resets = _pending_hard_limit_resets(records, email, current)
        resets = list(pending_limit_resets)
        for cooldown_value in cooldowns_by_key.get(email_key, []):
            cooldown = _future(_cooldown_value(cooldown_value), current)
            if cooldown:
                resets.append(cooldown)
        live_exhausted = False
        for reading in (five_hour, weekly):
            if reading and reading.get("used_percent") is not None \
                    and float(reading.get("remaining_percent") or 0.0) < MIN_DISPATCH_HEADROOM:
                live_exhausted = True
                reset = _future(reading.get("reset_at"), current)
                if reset:
                    resets.append(reset)
        limited_until = iso(max(resets)) if resets else None
        is_enrolled = email_key in enrolled_keys
        resource = enrolled_by_key.get(email_key, email)
        dispatchable = (
            is_enrolled
            and limited_until is None
            and not pending_limit_resets
            and not live_exhausted
            and status != "rate-limited"
        )
        learned_value = learned if any(value is not None for value in learned.values()) else None
        claude_rows.append(
            {
                "family": "claude",
                "id": email,
                "email": email,
                "home": None,
                "resource": resource,
                "five_hour": five_hour,
                "weekly": weekly,
                "learned_capacity": learned_value,
                "limited_until": limited_until,
                "confidence": confidence,
                "dispatchable": dispatchable,
                "status": status,
                "active": active,
                "enrolled": is_enrolled,
                "pending_hard_limit": bool(pending_limit_resets),
            }
        )

    return [*by_codex_id.values(), *claude_rows]


def _reading_remaining(reading: Any) -> float | None:
    if not isinstance(reading, dict):
        return None
    value = reading.get("remaining_percent")
    if value is not None:
        try:
            return max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            return None
    used = reading.get("used_percent")
    try:
        return max(0.0, min(100.0, 100.0 - float(used))) if used is not None else None
    except (TypeError, ValueError):
        return None


def _row_score(row: dict) -> float:
    known = [
        remaining for remaining in (
            _reading_remaining(row.get("five_hour")),
            _reading_remaining(row.get("weekly")),
        )
        if remaining is not None
    ]
    if known:
        return min(known)
    # Inference-only Claude tokens have no denominator until the first observed
    # hard limit.  Optimistic rotation is safer than treating every fresh lane
    # as exhausted.  Unknown Codex readings, by contrast, cannot be dispatched.
    return 100.0 if row.get("family") == "claude" else 0.0


def family_score(
    rows: Iterable[dict], family: str, *, now: datetime | None = None
) -> dict[str, Any]:
    current = _resolve_now(now)
    family_rows = [row for row in rows if isinstance(row, dict) and row.get("family") == family]

    def reset_eligible(row: dict) -> bool:
        if family == "claude" and row.get("enrolled") is False:
            return False
        if family == "codex" and row.get("auth_status") not in (None, "ok"):
            return False
        return True

    future_resets = [
        reset for reset in (
            _future(row.get("limited_until"), current)
            for row in family_rows
            if reset_eligible(row)
        )
        if reset is not None
    ]
    candidates: list[tuple[float, str, dict]] = []
    for row in family_rows:
        if (
            not row.get("dispatchable")
            or row.get("pending_hard_limit")
            or _future(row.get("limited_until"), current)
        ):
            continue
        score = _row_score(row)
        candidates.append((score, str(row.get("resource") or row.get("id") or ""), row))
    candidates.sort(key=lambda value: (-value[0], value[1]))
    best = candidates[0] if candidates else None
    return {
        "score": round(best[0], 2) if best else 0.0,
        "best_resource": best[2].get("resource") if best else None,
        "earliest_reset": iso(min(future_resets)) if future_resets else None,
        "dispatchable": len(candidates),
    }


def family_scores(rows: Iterable[dict], *, now: datetime | None = None) -> dict[str, dict]:
    materialized = list(rows)
    return {
        "codex": family_score(materialized, "codex", now=now),
        "claude": family_score(materialized, "claude", now=now),
    }


def build(
    *,
    force: bool = False,
    ttl_seconds: float = LIVE_CACHE_TTL_SECONDS,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    clock: Callable[[], datetime] | None = None,
    cache_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    cooldowns_path: Path | str | None = None,
    codex_homes_fn: Callable[[], Iterable[Path]] | None = None,
    read_auth_fn: Callable[[Path], dict] | None = None,
    probe_all_fn: Callable[..., list[dict]] | None = None,
    claude_identity_fn: Callable[[], dict] | None = None,
    keychain_fn: Callable[[], dict] | None = None,
    claude_probe_fn: Callable[..., dict] | None = None,
) -> dict:
    """Build the normalized capacity report used by CLI and delegate."""
    current = _resolve_now(clock=clock)
    live = get_live_probes(
        force=force,
        ttl_seconds=ttl_seconds,
        timeout=timeout,
        now=current,
        cache_path=cache_path,
        codex_homes_fn=codex_homes_fn,
        read_auth_fn=read_auth_fn,
        probe_all_fn=probe_all_fn,
        claude_identity_fn=claude_identity_fn,
        keychain_fn=keychain_fn,
        claude_probe_fn=claude_probe_fn,
    )
    rows = account_rows(
        live,
        now=current,
        ledger_path=ledger_path,
        cooldowns_path=cooldowns_path,
    )
    return {
        "generated_at": iso(current),
        "cache": {
            "checked_at": live.get("checked_at"),
            "hit": bool(live.get("cache_hit")),
            "ttl_seconds": ttl_seconds,
        },
        "accounts": rows,
        "families": family_scores(rows, now=current),
    }
