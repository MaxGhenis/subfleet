"""Automatic, one-at-a-time redemption of gifted Codex reset credits.

The policy is deliberately separated from the picker and watchdog.  Both call
the same locked evaluator, so concurrent dispatch/watchdog cycles cannot spend
two of these scarce entitlements inside the configured minimum interval.
"""

from __future__ import annotations

import fcntl
import json
import math
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import codex, paths, run_ledger
from .util import atomic_write_json, iso, load_json, now_local, parse_iso

DEFAULTS = {
    "enabled": True,
    "headroom_floor_pct": 15.0,
    "min_interval_min": 30.0,
}
RESET_TYPE = "codex_rate_limits"
_LANE_NUMBER_RE = re.compile(r"(?:^|\.)codex-(\d+)$")


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def load_config(path: Path | str | None = None) -> dict:
    """Return validated policy settings; malformed fields keep safe defaults."""
    source = Path(path) if path is not None else codex.accounts_config_path()
    raw = load_json(source, {}) or {}
    configured = raw.get("auto_reset") if isinstance(raw, dict) else None
    configured = configured if isinstance(configured, dict) else {}
    enabled = configured.get("enabled")
    floor = _number(configured.get("headroom_floor_pct"))
    interval = _number(configured.get("min_interval_min"))
    return {
        "enabled": enabled if isinstance(enabled, bool) else DEFAULTS["enabled"],
        "headroom_floor_pct": (
            max(0.0, floor) if floor is not None else DEFAULTS["headroom_floor_pct"]
        ),
        "min_interval_min": (
            max(0.0, interval)
            if interval is not None else DEFAULTS["min_interval_min"]
        ),
    }


def _clock(value: datetime | None = None) -> datetime:
    value = value or now_local()
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _home(row: dict) -> str:
    return str(row.get("home") or row.get("id") or "")


def _weekly(row: dict) -> dict:
    windows = row.get("windows")
    if isinstance(windows, dict):
        value = windows.get("weekly") or windows.get("secondary")
    else:
        value = row.get("weekly")
    return value if isinstance(value, dict) else {}


def _weekly_headroom(row: dict) -> float | None:
    used = _number(_weekly(row).get("used_percent"))
    return None if used is None else max(0.0, min(100.0, 100.0 - used))


def _lane_number(row: dict) -> int:
    match = _LANE_NUMBER_RE.search(Path(_home(row)).name)
    return int(match.group(1)) if match else 1_000_000


def _limited_by_server(row: dict) -> bool:
    status = row.get("verdict", row.get("status"))
    reached = row.get("limit_reached")
    if reached is None:
        reached = (row.get("probe") or {}).get("limit_reached")
    return status == "limited" and reached is True


def _applicable_count(row: dict) -> int:
    value = (row.get("reset_credits") or {}).get("applicable")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _available_count(row: dict) -> int | None:
    value = (row.get("reset_credits") or {}).get("available")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def fleet_credits_remaining(rows: Iterable[dict], spent_home: str) -> int | None:
    """Exact known fleet count after one spend, or None if any lane is unknown."""
    counts = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("duplicate_of"):
            continue
        home = _home(row)
        if not home or home in seen:
            continue
        seen.add(home)
        count = _available_count(row)
        if count is None:
            return None
        counts.append(count)
    if spent_home not in seen:
        return None
    return max(0, sum(counts) - 1)


def _shadowed(row: dict) -> bool:
    return bool(row.get("shadowed_by_app") or row.get("is_shadowed_by_app"))


def _candidate_view(row: dict, in_flight: dict[tuple[str, str], int]) -> dict:
    home = _home(row)
    reset_at = _weekly(row).get("reset_at")
    count = row.get("in_flight")
    if not isinstance(count, int) or isinstance(count, bool):
        count = in_flight.get(("codex", home), 0)
    return {
        "home": home,
        "lane": home,
        "lane_number": _lane_number(row),
        "email": row.get("email") or row.get("account_id") or "?",
        "account_id": row.get("account_id"),
        "weekly_reset_at": reset_at,
        "in_flight": max(0, count),
        "shadowed_by_app": _shadowed(row),
        "row": row,
    }


def _candidate_sort_key(item: dict):
    reset = parse_iso(item.get("weekly_reset_at"))
    timestamp = reset.timestamp() if reset is not None else float("-inf")
    return (-timestamp, item["in_flight"], item["lane_number"], item["home"])


def _potential_candidates(rows: Iterable[dict]) -> list[dict]:
    counts = run_ledger.in_flight_counts()
    return sorted([
        _candidate_view(row, counts)
        for row in rows
        if isinstance(row, dict)
        and not row.get("duplicate_of")
        and _limited_by_server(row)
        and _applicable_count(row) > 0
        and _home(row)
    ], key=_candidate_sort_key)


def ordered_candidates(rows: Iterable[dict]) -> list[dict]:
    """LIMITED reset holders, ordered by Max's furthest-reset-first rule."""
    candidates = _potential_candidates(rows)
    unshadowed = [item for item in candidates if not item["shadowed_by_app"]]
    if unshadowed:
        candidates = unshadowed
    return candidates


def _is_dispatchable(row: dict, dispatchable_homes: set[str] | None) -> bool:
    if dispatchable_homes is not None:
        return _home(row) in dispatchable_homes
    if isinstance(row.get("dispatchable"), bool):
        return bool(row["dispatchable"])
    return (
        row.get("verdict") == "ok"
        and not row.get("duplicate_of")
        and _weekly_headroom(row) is not None
    )


def _last_redeemed_at(state: dict) -> datetime | None:
    return parse_iso(state.get("last_redeemed_at")) if isinstance(state, dict) else None


def evaluate(
    rows: Iterable[dict],
    *,
    config: dict | None = None,
    state: dict | None = None,
    now: datetime | None = None,
    dispatchable_homes: set[str] | None = None,
) -> dict:
    """Pure trigger/ordering decision over snapshot- or capacity-shaped rows."""
    current = _clock(now)
    settings = dict(load_config() if config is None else config)
    state = dict(state or {})
    rows = [row for row in rows if isinstance(row, dict)]
    dispatchable = [
        row for row in rows if _is_dispatchable(row, dispatchable_homes)
    ]
    headroom = sum(_weekly_headroom(row) or 0.0 for row in dispatchable)
    if not dispatchable:
        triggered, reason = True, "no-dispatchable-lanes"
    elif headroom < float(settings["headroom_floor_pct"]):
        triggered, reason = True, "weekly-headroom-below-floor"
    else:
        triggered, reason = False, "weekly-headroom-sufficient"

    last = _last_redeemed_at(state)
    elapsed_min = (
        max(0.0, (current - last).total_seconds() / 60.0) if last else None
    )
    interval_ok = last is None or elapsed_min >= float(settings["min_interval_min"])
    candidates = ordered_candidates(rows)
    if not settings.get("enabled"):
        status = "disabled"
    elif not triggered:
        status = "not-triggered"
    elif not interval_ok:
        status = "interval-blocked"
    elif not candidates:
        status = "no-candidates"
    else:
        status = "ready"
    return {
        "status": status,
        "enabled": bool(settings.get("enabled")),
        "triggered": triggered,
        "trigger_reason": reason,
        "dispatchable": len(dispatchable),
        "weekly_headroom_pct": round(headroom, 3),
        "headroom_floor_pct": float(settings["headroom_floor_pct"]),
        "min_interval_min": float(settings["min_interval_min"]),
        "last_redeemed_at": iso(last),
        "interval_elapsed_min": round(elapsed_min, 3) if elapsed_min is not None else None,
        "candidates": candidates,
    }


def available_codex_credits(result: dict) -> list[dict]:
    credits = result.get("credits") if isinstance(result, dict) else None
    if result.get("status") != "ok" or not isinstance(credits, list):
        return []
    return [
        credit for credit in credits
        if isinstance(credit, dict)
        and credit.get("status") == "available"
        and credit.get("reset_type") == RESET_TYPE
        and isinstance(credit.get("id"), str)
        and bool(credit["id"])
    ]


def _public_candidate(candidate: dict) -> dict:
    return {
        key: value for key, value in candidate.items()
        if key not in {"row", "credit"}
    }


@contextmanager
def _policy_lock():
    state_path = paths.reset_policy_path()
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _remember_redemption(
    *, lane: str, email: str, credit_id: str, occurred: datetime,
    state: dict | None = None,
) -> dict:
    current = dict(state or load_json(paths.reset_policy_path(), {}) or {})
    stamp = iso(occurred)
    lane_history = current.get("last_redemptions")
    lane_history = dict(lane_history) if isinstance(lane_history, dict) else {}
    lane_history[lane] = stamp
    current.update({
        "last_redeemed_at": stamp,
        "lane": lane,
        "email": email,
        "credit_id": credit_id,
        "last_redemptions": lane_history,
    })
    atomic_write_json(paths.reset_policy_path(), current)
    return current


def last_lane_redemption(lane: str, state: dict | None = None) -> datetime | None:
    state = state if isinstance(state, dict) else load_json(paths.reset_policy_path(), {}) or {}
    lane_history = state.get("last_redemptions")
    value = lane_history.get(lane) if isinstance(lane_history, dict) else None
    if value is None and state.get("lane") == lane:
        value = state.get("last_redeemed_at")
    return parse_iso(value)


def short_window_limit_until(
    row: dict, *, now: datetime | None = None, state: dict | None = None,
) -> datetime | None:
    """Future rollout/cooldown short-window gate, superseded by a later reset."""
    current = _clock(now)
    lane = _home(row)
    redeemed = last_lane_redemption(lane, state=state)
    usage = ((row.get("recent_errors") or {}).get("usage_limit") or [])
    if usage and isinstance(usage[0], dict):
        observed = parse_iso(usage[0].get("observed_at"))
        reset = parse_iso(usage[0].get("reset_at"))
        if reset and reset > current and not (redeemed and observed and observed <= redeemed):
            return reset
    cooldowns = load_json(paths.delegate_cooldowns_path(), {}) or {}
    value = cooldowns.get(lane) if isinstance(cooldowns, dict) else None
    # The shared cooldown ledger migrated from ``{lane: iso}`` to
    # ``{lane: {"*": iso, model: iso}}``. Codex only uses account scope.
    if isinstance(value, dict):
        value = value.get("*")
    cooldown = parse_iso(value)
    return cooldown if cooldown and cooldown > current else None


def _append_history(record: dict) -> None:
    path = paths.history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def record_redemption(
    *,
    lane: str,
    email: str,
    credit_id: str,
    remaining: int | None,
    occurred: datetime | None = None,
    metadata: dict | None = None,
    remember: bool = True,
) -> str:
    """Write the exact history event plus the existing meta-only run event."""
    current = _clock(occurred)
    if remember:
        with _policy_lock():
            _remember_redemption(
                lane=lane, email=email, credit_id=credit_id, occurred=current
            )
    _append_history({
        "ts": iso(current),
        "event": "reset",
        "lane": lane,
        "email": email,
        "credit_id": credit_id,
        "remaining": int(remaining) if remaining is not None else None,
    })
    return run_ledger.record_event(
        "reset", family="codex", lane=lane, metadata=dict(metadata or {}),
        occurred=current,
    )


def _probe_propagation(
    auth: dict,
    *,
    opener=None,
    probe_fn: Callable | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_timeout: float = 90.0,
    poll_interval: float = 10.0,
) -> tuple[dict, int]:
    probe_fn = probe_fn or codex.probe_wham
    interval = max(0.1, float(poll_interval))
    attempts = max(1, int(max(0.0, poll_timeout) // interval) + 1)
    latest: dict = {"status": "not-probed"}
    for attempt in range(1, attempts + 1):
        latest = probe_fn(auth, opener=opener)
        if (
            latest.get("status") == "ok"
            and latest.get("limit_reached") is not True
            and latest.get("allowed") is not False
        ):
            return latest, attempt
        if attempt < attempts:
            sleep_fn(interval)
    return latest, attempts


def _probe_weekly(probe: dict) -> dict:
    weekly = probe.get("weekly")
    if isinstance(weekly, dict):
        return weekly
    _, classified = codex.classify_windows(probe.get("primary"), probe.get("secondary"))
    return classified or {}


def apply_probe_to_snapshot_row(row: dict, probe: dict) -> dict:
    """Replace one snapshot home's quota/verdict fields after propagation."""
    five, weekly = codex.classify_windows(
        probe.get("primary"), probe.get("secondary")
    )
    if five is None and weekly is None:
        five, weekly = probe.get("five_hour"), probe.get("weekly")
    if five is None and weekly is None:
        five, weekly = probe.get("primary"), probe.get("secondary")
    if probe.get("status") != "ok":
        verdict = row.get("verdict", "unknown")
    elif codex.plan_is_free(probe.get("plan_type")):
        verdict = "free-plan"
    elif probe.get("limit_reached") or probe.get("allowed") is False:
        verdict = "limited"
    else:
        verdict = "ok"
    row.update({
        "email": probe.get("email") or row.get("email"),
        "plan": probe.get("plan_type") or row.get("plan"),
        "verdict": verdict,
        "probe": {key: value for key, value in probe.items() if not str(key).startswith("_")},
        "reset_credits": codex.reset_credits_from_probe(probe),
        "windows": {
            "primary": five,
            "secondary": weekly,
            "five_hour": five,
            "weekly": weekly,
            "source": "live",
            "as_of": probe.get("checked_at"),
        },
        "rollout_observed": None,
    })
    return row


def _probe_propagated(probe: dict) -> bool:
    return (
        probe.get("status") == "ok"
        and probe.get("limit_reached") is not True
        and probe.get("allowed") is not False
    )


def apply_redemption_to_snapshot_row(
    row: dict, redemption: dict, *, now: datetime | None = None,
) -> dict:
    """Apply a confirmed consume even while the usage endpoint stays stale."""
    current = parse_iso(redemption.get("redeemed_at")) or _clock(now)
    probe = redemption.get("probe") or {}
    prior_windows = row.get("windows") or {}
    classified_five, classified_weekly = codex.classify_windows(
        prior_windows.get("primary"), prior_windows.get("secondary")
    )
    five_value = prior_windows.get("five_hour")
    weekly_value = prior_windows.get("weekly")
    prior_five = dict(
        five_value if isinstance(five_value, dict) else classified_five or {}
    )
    prior_weekly = dict(
        weekly_value if isinstance(weekly_value, dict) else classified_weekly or {}
    )
    prior_credits = dict(row.get("reset_credits") or {})
    apply_probe_to_snapshot_row(row, probe)

    propagated = redemption.get("propagated")
    if propagated is None:
        propagated = _probe_propagated(probe)
    if propagated:
        return row

    weekly_reset = (
        parse_iso(redemption.get("weekly_reset_at"))
        or current + timedelta(days=7)
    )
    five_seconds = _number(prior_five.get("window_seconds")) or 5 * 3600
    five = {
        **prior_five,
        "used_percent": 0.0,
        "window_seconds": five_seconds,
        "reset_at": iso(current + timedelta(seconds=five_seconds)),
    }
    weekly = {
        **prior_weekly,
        "used_percent": 0.0,
        "window_seconds": _number(prior_weekly.get("window_seconds")) or 7 * 86400,
        "reset_at": iso(weekly_reset),
    }
    probe_view = {
        key: value for key, value in probe.items() if not str(key).startswith("_")
    }
    row.update({
        "verdict": "ok",
        "probe": {
            **probe_view,
            "status": "ok",
            "allowed": True,
            "limit_reached": False,
            "propagation_pending": True,
        },
        "windows": {
            "primary": five,
            "secondary": weekly,
            "five_hour": five,
            "weekly": weekly,
            "source": "reset-confirmed",
            "as_of": iso(current),
        },
        "reset_credits": {
            "available": (
                max(0, prior_credits["available"] - 1)
                if isinstance(prior_credits.get("available"), int)
                and not isinstance(prior_credits.get("available"), bool)
                else None
            ),
            "applicable": 0,
        },
        "propagation_probe": probe_view,
        "rollout_observed": None,
    })
    return row


def run(
    rows: Iterable[dict],
    *,
    dry_run: bool = False,
    opener=None,
    now: datetime | None = None,
    config: dict | None = None,
    dispatchable_homes: set[str] | None = None,
    probe_fn: Callable | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_timeout: float = 90.0,
    poll_interval: float = 10.0,
    clock_fn: Callable[[], datetime] = now_local,
) -> dict:
    """Evaluate once and consume at most one credit, returning full evidence."""
    current = _clock(now)
    rows = [row for row in rows if isinstance(row, dict)]
    with _policy_lock():
        state = load_json(paths.reset_policy_path(), {}) or {}
        decision = evaluate(
            rows,
            config=config,
            state=state,
            now=current,
            dispatchable_homes=dispatchable_homes,
        )
        candidates = decision["candidates"]
        public = [_public_candidate(candidate) for candidate in candidates]
        decision["candidates"] = public
        ready = decision["status"] == "ready"
        if not ready and not dry_run:
            return decision

        inspected: list[dict] = []
        selected = None
        selected_auth = None
        concrete: list[dict] = []
        potential = _potential_candidates(rows)
        unshadowed = [item for item in potential if not item["shadowed_by_app"]]
        shadowed = [item for item in potential if item["shadowed_by_app"]]
        groups = [group for group in (unshadowed, shadowed) if group]
        for group in groups:
            group_concrete: list[dict] = []
            for candidate in group:
                auth = codex.read_auth(Path(candidate["home"]))
                listed = codex.list_reset_credits(auth, opener=opener)
                credits = available_codex_credits(listed)
                item = _public_candidate(candidate)
                item["credit_id"] = credits[0]["id"] if credits else None
                item["credit_count"] = len(credits)
                item["list_status"] = listed.get("status")
                inspected.append(item)
                if credits:
                    actual = {
                        **candidate,
                        "credit": credits[0],
                        "credit_count": len(credits),
                        "list_status": listed.get("status"),
                    }
                    group_concrete.append(actual)
                    if selected is None:
                        selected = actual
                        selected_auth = auth
                if selected is not None and not dry_run:
                    break
            if group_concrete:
                concrete = group_concrete
                break
        if dry_run:
            # Only concrete candidates survive the GET gate; shadowed lanes
            # are listed only when no unshadowed concrete credit exists.
            decision["candidates"] = [
                _public_candidate(item) | {
                    "credit_id": item["credit"]["id"],
                }
                for item in concrete
            ]
            decision["selected"] = (
                _public_candidate(selected) | {"credit_id": selected["credit"]["id"]}
                if selected else None
            )
            if ready:
                decision["status"] = (
                    "dry-run-ready" if selected else "no-concrete-credit"
                )
            return decision
        decision["candidates"] = inspected
        if selected is None or selected_auth is None:
            decision["status"] = "no-concrete-credit"
            return decision

        credit_id = selected["credit"]["id"]
        consumed = codex.consume_reset_credit(selected_auth, credit_id, opener=opener)
        decision["consume"] = consumed
        if not codex.reset_consume_succeeded(consumed):
            decision["status"] = "consume-failed"
            return decision

        redeemed_at = _clock(clock_fn())
        remaining = fleet_credits_remaining(rows, selected["home"])
        _remember_redemption(
            lane=selected["home"], email=selected["email"],
            credit_id=credit_id, occurred=redeemed_at, state=state,
        )
        base_metadata = {
            "account": selected["email"],
            "account_id": selected.get("account_id"),
            "credit_id": credit_id,
            "redeem_request_id": consumed.get("redeem_request_id"),
            "automatic": True,
            "trigger_reason": decision["trigger_reason"],
            "response": {
                "status": consumed.get("status"),
                "code": consumed.get("code"),
                "windows_reset": consumed.get("windows_reset"),
                "error": consumed.get("error"),
            },
        }
        try:
            # Make the irreversible consume durable before the laggy endpoint
            # poll, which can run for 90 seconds or be interrupted.
            audit_run_id = record_redemption(
                lane=selected["home"], email=selected["email"],
                credit_id=credit_id, remaining=remaining, occurred=redeemed_at,
                metadata=base_metadata, remember=False,
            )
            audit_error = None
        except (OSError, TypeError, ValueError) as exc:
            audit_run_id = None
            audit_error = str(exc)
        # The reset supersedes a dispatch-time 15-minute cooldown immediately.
        try:
            from . import capacity

            capacity.clear_lane_cooldown(selected["home"])
        except (ImportError, OSError):
            pass

    try:
        after, poll_attempts = _probe_propagation(
            selected_auth,
            opener=opener,
            probe_fn=probe_fn,
            sleep_fn=sleep_fn,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
        )
    except Exception as exc:  # the consume is already durable and authoritative
        after = {"status": "propagation-error", "error": str(exc)}
        poll_attempts = 0
    propagated = _probe_propagated(after)
    weekly = _probe_weekly(after)
    reset_at = (
        weekly.get("reset_at") if propagated else None
    ) or iso(redeemed_at + timedelta(days=7))
    after_metadata = {
        "after": {
            "probe_status": after.get("status"),
            "weekly_used_percent": weekly.get("used_percent"),
            "weekly_reset_at": reset_at,
            "poll_attempts": poll_attempts,
            "propagated": propagated,
        },
    }
    if audit_run_id:
        try:
            run_ledger.update_event_metadata(audit_run_id, after_metadata)
        except (OSError, TypeError, ValueError) as exc:
            audit_error = str(exc)
    decision.update({
        "status": "redeemed",
        "redeemed": {
            **_public_candidate(selected),
            "credit_id": credit_id,
            "remaining": remaining,
            "redeemed_at": iso(redeemed_at),
            "weekly_reset_at": reset_at,
            "probe": after,
            "poll_attempts": poll_attempts,
            "propagated": propagated,
        },
        "audit_error": audit_error,
    })
    return decision
