"""Small shared helpers: JWT claim decoding, time parsing/formatting, atomic IO."""

import base64
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def jwt_claims(token: str) -> dict:
    """Decode a JWT's payload without verifying. Returns {} on any problem."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def from_epoch(ts: int | float | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone()
    except (ValueError, OSError, OverflowError):
        return None


def fmt_clock(dt: datetime | None, now: datetime | None = None) -> str:
    """Human clock time in local tz: '6:29pm', with day qualifier when not today."""
    if dt is None:
        return "?"
    dt = dt.astimezone()
    now = (now or now_local()).astimezone()
    clock = dt.strftime("%I:%M%p").lstrip("0").lower()
    if dt.date() == now.date():
        return clock
    if dt.date() == (now + timedelta(days=1)).date():
        return f"{clock} tomorrow"
    return f"{clock} {dt.strftime('%a %-m/%-d')}"


_RESET_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?(?:\s*\(([A-Za-z_]+/[A-Za-z_]+)\))?",
    re.IGNORECASE,
)


def parse_reset_clock(text: str, event_time: datetime) -> datetime | None:
    """Turn 'resets 6:40pm (America/New_York)' / 'try again at 11:33 PM' into an
    absolute datetime: same day as the event in the stated (else local) tz, rolled
    to the next day if that clock time precedes the event."""
    m = _RESET_RE.search(text)
    if not m:
        return None
    hour, minute, ampm, zone = int(m.group(1)), int(m.group(2)), m.group(3).lower(), m.group(4)
    if hour == 12:
        hour = 0
    if ampm == "p":
        hour += 12
    try:
        tz = ZoneInfo(zone) if zone else None
    except Exception:
        tz = None
    local_event = event_time.astimezone(tz) if tz else event_time.astimezone()
    reset = local_event.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset < local_event:
        reset += timedelta(days=1)
    return reset


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def strip_private(obj):
    """Recursively drop keys starting with '_' (tokens etc.) before serializing."""
    if isinstance(obj, dict):
        return {k: strip_private(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_private(v) for v in obj]
    return obj
