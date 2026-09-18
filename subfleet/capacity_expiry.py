"""Codex weekly burn rates and capacity-expiry projections.

The watchdog's compact history is intentionally append-only and heterogeneous:
ordinary samples, reset audit events, and records from older subfleet versions
share one JSONL file.  This module keeps parsing at a soft I/O boundary and the
forecast itself pure so callers and tests can supply fabricated records.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import paths
from .util import iso, now_local, parse_iso

SIX_HOURS = timedelta(hours=6)
TWENTY_FOUR_HOURS = timedelta(hours=24)
EXPIRING_SOON = timedelta(hours=72)
_SAMPLE_VERDICTS = {"ok", "limited", "exhausted"}


def _clock(value: datetime | None) -> datetime:
    current = value or now_local()
    return current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _clock(value)
    return parse_iso(value) if isinstance(value, str) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percent(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and 0.0 <= number <= 100.0 else None


def read_history(path: Path | str | None = None) -> list[dict]:
    """Read valid JSON-object history lines, ignoring absent/corrupt records."""
    history = Path(path) if path is not None else paths.history_path()
    records: list[dict] = []
    try:
        with history.open() as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except (OSError, UnicodeError):
        pass
    return records


def _lane_id(row: dict) -> str | None:
    value = row.get("home") or row.get("id")
    return str(value) if value is not None and str(value) else None


def _weekly_window(row: dict) -> dict:
    windows = row.get("windows")
    if isinstance(windows, dict):
        value = windows.get("weekly") or windows.get("secondary")
        if isinstance(value, dict):
            return value
    value = row.get("weekly")
    return value if isinstance(value, dict) else {}


def _sample_is_current(row: dict) -> bool:
    state = row.get("verdict")
    if state is None:
        state = row.get("status")
    return state is None or state in _SAMPLE_VERDICTS


def _history_weekly(value: dict) -> float | None:
    used = _percent(value.get("wk"))
    if used is not None:
        return used
    used = _percent(value.get("weekly_used_percent"))
    if used is not None:
        return used
    weekly = value.get("weekly")
    return _percent(weekly.get("used_percent")) if isinstance(weekly, dict) else None


def _history_is_current(value: dict) -> bool:
    state = value.get("v")
    if state is None:
        state = value.get("verdict", value.get("status"))
    return state is None or state in _SAMPLE_VERDICTS


def _history_index(records: Iterable[dict], now: datetime) -> tuple[
    dict[str, list[tuple[datetime, float]]], dict[str, list[datetime]]
]:
    samples: dict[str, list[tuple[datetime, float]]] = {}
    resets: dict[str, list[datetime]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        timestamp = _time(record.get("ts"))
        if timestamp is None or timestamp > now:
            continue
        if record.get("event") == "reset":
            lane = record.get("lane")
            if lane is not None and str(lane):
                resets.setdefault(str(lane), []).append(timestamp)
            continue
        codex = record.get("codex")
        if not isinstance(codex, dict):
            continue
        for lane, value in codex.items():
            if not isinstance(value, dict) or not _history_is_current(value):
                continue
            used = _history_weekly(value)
            if used is not None:
                samples.setdefault(str(lane), []).append((timestamp, used))
    return samples, resets


def _burn_rate(
    samples: Iterable[tuple[datetime, float]],
    resets: Iterable[datetime],
    *,
    now: datetime,
    horizon: timedelta,
) -> tuple[float | None, int]:
    """Endpoint slope for the latest monotonic segment inside one horizon."""
    cutoff = now - horizon
    # A dict deduplicates manual watchdog runs at an identical timestamp.  The
    # caller appends the current snapshot after history, so ground truth wins.
    by_time = {
        timestamp: used
        for timestamp, used in samples
        if cutoff <= timestamp <= now
    }
    points = sorted(by_time.items())
    reset_times = [stamp for stamp in resets if cutoff <= stamp <= now]
    if reset_times:
        last_reset = max(reset_times)
        points = [point for point in points if point[0] >= last_reset]

    # Natural weekly resets and pre-feature/manual redemptions appear as a
    # downward jump.  Only the newest window may be projected forward.
    segment_start = 0
    for index in range(1, len(points)):
        if points[index][1] < points[index - 1][1]:
            segment_start = index
    points = points[segment_start:]
    if len(points) < 2:
        return None, len(points)
    elapsed_hours = (points[-1][0] - points[0][0]).total_seconds() / 3600.0
    if elapsed_hours <= 0:
        return None, len(points)
    rate = max(0.0, (points[-1][1] - points[0][1]) / elapsed_hours)
    return round(rate, 4), len(points)


def analyze(homes: list[dict], records: Iterable[dict], *, now: datetime) -> dict:
    """Return per-lane burn/projection data plus honest fleet aggregates.

    ``homes`` accepts either snapshot rows (``windows.weekly`` / ``secondary``)
    or normalized capacity rows (top-level ``weekly``).  Duplicate snapshot
    aliases and explicitly non-Codex capacity rows are excluded.
    """
    current = _clock(now)
    history_samples, history_resets = _history_index(records, current)
    lanes: dict[str, dict] = {}
    resets_for_fleet: list[datetime] = []
    projected_resets: list[datetime] = []
    known_windows_left = 0.0
    known_projected_unused = 0.0
    current_complete = True
    projection_complete = True

    for row in homes:
        if not isinstance(row, dict) or row.get("duplicate_of"):
            continue
        if row.get("family") not in (None, "codex"):
            continue
        lane = _lane_id(row)
        if lane is None:
            continue
        weekly = _weekly_window(row)
        used = _percent(weekly.get("used_percent")) if _sample_is_current(row) else None
        reset = _time(weekly.get("reset_at"))
        if reset is not None and reset > current:
            resets_for_fleet.append(reset)

        samples = list(history_samples.get(lane, ()))
        if used is not None:
            samples.append((current, used))
            known_windows_left += max(0.0, 100.0 - used) / 100.0
        else:
            current_complete = False

        reset_events = history_resets.get(lane, ())
        burn_6h, samples_6h = _burn_rate(
            samples, reset_events, now=current, horizon=SIX_HOURS
        )
        burn_24h, samples_24h = _burn_rate(
            samples, reset_events, now=current, horizon=TWENTY_FOUR_HOURS
        )
        projection_rate = burn_6h if burn_6h is not None else burn_24h
        hours_to_reset = None
        projected_used = None
        projected_unused = None
        expiring_unused = False
        if reset is not None and reset > current:
            hours_to_reset = (reset - current).total_seconds() / 3600.0
        if used is not None and hours_to_reset is not None and projection_rate is not None:
            raw_projected_used = min(100.0, max(0.0, used + projection_rate * hours_to_reset))
            raw_projected_unused = max(0.0, 100.0 - raw_projected_used)
            projected_used = round(raw_projected_used, 2)
            projected_unused = round(raw_projected_unused, 2)
            known_projected_unused += raw_projected_unused / 100.0
            projected_resets.append(reset)
            expiring_unused = (
                raw_projected_unused > 20.0
                and timedelta(0) < reset - current < EXPIRING_SOON
            )
        else:
            projection_complete = False

        lanes[lane] = {
            "home": lane,
            "email": row.get("email"),
            "weekly_used_percent": used,
            "weekly_reset_at": iso(reset),
            "burn_6h_pct_per_hour": burn_6h,
            "burn_24h_pct_per_hour": burn_24h,
            "projection_rate_pct_per_hour": projection_rate,
            "samples_6h": samples_6h,
            "samples_24h": samples_24h,
            "hours_to_reset": round(hours_to_reset, 2) if hours_to_reset is not None else None,
            "projected_used_percent": projected_used,
            "projected_unused_percent": projected_unused,
            "expiring_unused": expiring_unused,
        }

    complete = current_complete and projection_complete
    return {
        "lanes": lanes,
        "lane_count": len(lanes),
        "windows_left": round(known_windows_left, 2) if current_complete else None,
        "known_windows_left": round(known_windows_left, 2),
        "projected_unused_windows": (
            round(known_projected_unused, 2) if projection_complete else None
        ),
        "projected_unused_windows_raw": (
            known_projected_unused if projection_complete else None
        ),
        "known_projected_unused_windows": round(known_projected_unused, 2),
        "known_projected_unused_windows_raw": known_projected_unused,
        "earliest_reset_at": iso(min(resets_for_fleet)) if resets_for_fleet else None,
        "expires_by": iso(max(projected_resets)) if projected_resets else None,
        "at_risk_lanes": [lane for lane, value in lanes.items() if value["expiring_unused"]],
        "complete": complete,
    }
