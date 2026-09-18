"""Open deterministic five-hour windows on otherwise idle Claude lanes.

Keepalives deliberately bypass the full lane runner: no prompt/output salvage
artifacts and no run-ledger directory. A successful request writes one compact
usage-ledger marker so the capacity model knows the exact window-open time.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterable, Iterator

from . import capacity, claude, paths, run_ledger
from .util import atomic_write_json, iso, load_json, now_local, parse_iso

FAMILY = "claude"
MODEL = "claude-haiku-4-5-20251001"
WINDOW = timedelta(hours=5)
TIMEOUT_SECONDS = 60
MAX_WORKERS = 4
AUTH_LOG_INTERVAL = timedelta(days=1)
STATE_SCHEMA_VERSION = 1

_AUTH_CODE = re.compile(r"(?<!\d)(401|403)(?!\d)")


def _clock(value: datetime | None = None) -> datetime:
    current = value or now_local()
    return current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)


def _enrolled(config: dict | None = None) -> list[tuple[str, str]]:
    config = claude.roster_config() if config is None else config
    value = config.get("enrolled") if isinstance(config, dict) else None
    if not isinstance(value, dict):
        return []
    return [
        (email, secret.strip())
        for email, secret in value.items()
        if isinstance(email, str)
        and "@" in email
        and isinstance(secret, str)
        and bool(secret.strip())
    ]


def _run_metas() -> list[dict]:
    rows = []
    for run_dir in run_ledger.run_directories():
        meta = load_json(run_dir / "meta.json")
        if isinstance(meta, dict) and meta.get("family") == FAMILY and not meta.get("event"):
            rows.append(meta)
    return rows


def latest_request_at(
    email: str,
    usage_records: Iterable[dict],
    run_metas: Iterable[dict],
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Latest evidence that a request reached, or is reaching, one lane.

    Completed runner entries count only with a session id: the full runner
    creates its run record before fetching a token, so a no-session rc=5 can
    mean that no provider request happened. A RUNNING entry is conservatively
    counted so a long request is never interrupted by a keepalive.
    """
    current = _clock(now)
    target = email.casefold()
    candidates: list[datetime] = []
    for record in usage_records:
        if str(record.get("email") or "").casefold() != target:
            continue
        timestamp = parse_iso(record.get("ts"))
        if timestamp is not None and timestamp <= current:
            candidates.append(timestamp)
    for meta in run_metas:
        if str(meta.get("lane") or "").casefold() != target:
            continue
        running = meta.get("finished_at") is None
        if not running and not meta.get("session_id"):
            continue
        timestamp = parse_iso(
            meta.get("started_at") if running else meta.get("finished_at")
        )
        if timestamp is not None and timestamp <= current:
            candidates.append(timestamp)
    return max(candidates) if candidates else None


def _auth_dead(
    email: str,
    lane_state: dict,
    usage_records: Iterable[dict],
    run_metas: Iterable[dict],
    *,
    now: datetime,
) -> tuple[bool, datetime | None, str | int | None]:
    """Return the newest explicit auth result, allowing later success/clear."""
    target = email.casefold()
    evidence: list[tuple[datetime, bool, str | int | None]] = []

    def add(value: Any, failed: bool, code: str | int | None = None) -> None:
        timestamp = parse_iso(value) if isinstance(value, str) else None
        if timestamp is not None and timestamp <= now:
            evidence.append((timestamp, failed, code))

    for record in usage_records:
        if str(record.get("email") or "").casefold() != target:
            continue
        successful = record.get("kind") == "keepalive" or (
            not record.get("event")
            and not record.get("error")
            and any(key in record for key in ("total_tokens", "input_tokens", "output_tokens"))
        )
        if successful:
            add(record.get("ts"), False)
    for meta in run_metas:
        if str(meta.get("lane") or "").casefold() != target or not meta.get("session_id"):
            continue
        if meta.get("finished_at") is None:
            continue
        if meta.get("rc") == 5:
            add(meta.get("finished_at"), True, "runner-rc-5")
        elif meta.get("rc") == 0:
            add(meta.get("finished_at"), False)
    # State is the newest local authority when timestamps have only second
    # precision. In particular, a re-enrollment clear must beat an auth failure
    # recorded earlier in the same second.
    add(lane_state.get("auth_failed_at"), True, lane_state.get("auth_code"))
    add(lane_state.get("last_opened_at"), False)
    add(lane_state.get("auth_cleared_at"), False)
    if not evidence:
        return False, None, None
    _index, (timestamp, failed, code) = max(
        enumerate(evidence), key=lambda item: (item[1][0], item[0])
    )
    return failed, timestamp, code


def _auth_log_due(lane_state: dict, now: datetime) -> bool:
    last = parse_iso(lane_state.get("last_auth_log_at"))
    return last is None or now - last >= AUTH_LOG_INTERVAL


def _child_environment(token: str) -> dict[str, str]:
    child = os.environ.copy()
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_LANE_DETACHED",
        "CLAUDE_LANE_OWNED_PROMPT",
    ):
        child.pop(name, None)
    child["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return child


def _ping_lane(
    email: str,
    secret: str,
    *,
    fixed_now: datetime | None,
    runner: Callable[..., subprocess.CompletedProcess],
    secret_runner: Callable[..., subprocess.CompletedProcess],
) -> dict:
    deadline = monotonic() + TIMEOUT_SECONDS
    attempted_at = _clock(fixed_now)
    token = claude.agent_secret_get(secret, runner=secret_runner)
    if not token:
        return {
            "email": email,
            "status": "failed",
            "attempted_at": iso(attempted_at),
            "reason": "secret-missing",
        }
    # The five-hour window begins when the provider request is sent, not while
    # the keychain is being read or this worker is waiting for a thread slot.
    request_started = _clock(fixed_now) if fixed_now is not None else now_local()
    remaining = deadline - monotonic()
    if remaining <= 0:
        return {
            "email": email,
            "status": "failed",
            "attempted_at": iso(attempted_at),
            "reason": "timeout",
        }
    command = [
        paths.claude_bin(),
        "-p",
        "ok",
        "--model",
        MODEL,
        "--output-format",
        "json",
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=remaining,
            env=_child_environment(token),
        )
    except subprocess.TimeoutExpired:
        return {
            "email": email,
            "status": "failed",
            "attempted_at": iso(request_started),
            "reason": "timeout",
        }
    except OSError as exc:
        return {
            "email": email,
            "status": "failed",
            "attempted_at": iso(request_started),
            "reason": f"exec-error:{type(exc).__name__}",
        }

    try:
        envelope = json.loads(completed.stdout)
    except (TypeError, ValueError):
        envelope = None
    # Successful JSON can legitimately contain the number 401 or 403 in a
    # duration/token field. Only inspect those codes after ruling out success.
    if (
        completed.returncode == 0
        and isinstance(envelope, dict)
        and envelope.get("is_error") is False
    ):
        return {
            "email": email,
            "status": "opened",
            "attempted_at": iso(request_started),
        }
    auth_match = _AUTH_CODE.search(f"{completed.stderr}\n{completed.stdout}")
    if auth_match:
        return {
            "email": email,
            "status": "failed",
            "attempted_at": iso(request_started),
            "reason": "auth",
            "auth_code": int(auth_match.group(1)),
        }
    if completed.returncode != 0:
        return {
            "email": email,
            "status": "failed",
            "attempted_at": iso(request_started),
            "reason": f"exit-{completed.returncode}",
        }
    if not isinstance(envelope, dict) or envelope.get("is_error") is not False:
        return {
            "email": email,
            "status": "failed",
            "attempted_at": iso(request_started),
            "reason": "bad-envelope",
        }
    raise AssertionError("unreachable keepalive response classification")


@contextmanager
def _state_lock() -> Iterator[None]:
    path = paths.keepalive_state_path()
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run(
    *,
    family: str = FAMILY,
    dry_run: bool = False,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    secret_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    max_workers: int = MAX_WORKERS,
) -> dict:
    """Run one concurrent keepalive pass and return sanitized lane results."""
    if family != FAMILY:
        raise ValueError(f"unsupported keepalive family: {family}")
    current = _clock(now)
    started_at = iso(current)
    enrolled = _enrolled()
    lock = nullcontext() if dry_run else _state_lock()
    with lock:
        raw_state = load_json(paths.keepalive_state_path(), {}) or {}
        state = raw_state if isinstance(raw_state, dict) else {}
        lane_states = state.get("lanes")
        lane_states = dict(lane_states) if isinstance(lane_states, dict) else {}
        records = capacity.read_ledger()
        metas = _run_metas()
        results_by_email: dict[str, dict] = {}
        pending: list[tuple[str, str]] = []

        for email, secret in enrolled:
            lane_state = lane_states.get(email)
            lane_state = dict(lane_state) if isinstance(lane_state, dict) else {}
            dead, failed_at, auth_code = _auth_dead(
                email, lane_state, records, metas, now=current
            )
            if dead:
                verbose = _auth_log_due(lane_state, current)
                results_by_email[email] = {
                    "email": email,
                    "status": "skipped-auth",
                    "auth_log_due": verbose,
                    **({"auth_failed_at": iso(failed_at), "auth_code": auth_code} if verbose else {}),
                }
                if not dry_run:
                    lane_state.update(
                        {
                            "last_outcome": "skipped-auth",
                            "last_checked_at": started_at,
                            "auth_failed_at": iso(failed_at),
                            "auth_code": auth_code,
                        }
                    )
                    if verbose:
                        lane_state["last_auth_log_at"] = started_at
                    lane_states[email] = lane_state
                continue

            last_request = latest_request_at(email, records, metas, now=current)
            if last_request is not None and current - last_request < WINDOW:
                results_by_email[email] = {
                    "email": email,
                    "status": "skipped-open",
                    "last_request_at": iso(last_request),
                }
                if not dry_run:
                    lane_state.update(
                        {"last_outcome": "skipped-open", "last_checked_at": started_at}
                    )
                    lane_states[email] = lane_state
                continue
            if dry_run:
                results_by_email[email] = {
                    "email": email,
                    "status": "would-open",
                    "dry_run": True,
                }
            else:
                pending.append((email, secret))

        if pending:
            worker_count = max(1, min(MAX_WORKERS, max_workers, len(pending)))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    email: executor.submit(
                        _ping_lane,
                        email,
                        secret,
                        fixed_now=now,
                        runner=runner,
                        secret_runner=secret_runner,
                    )
                    for email, secret in pending
                }
                for email, _secret in pending:
                    try:
                        results_by_email[email] = futures[email].result()
                    except Exception as exc:
                        results_by_email[email] = {
                            "email": email,
                            "status": "failed",
                            "attempted_at": started_at,
                            "reason": f"worker-error:{type(exc).__name__}",
                        }

        opened = 0
        if not dry_run:
            for email, _secret in pending:
                result = results_by_email[email]
                lane_state = lane_states.get(email)
                lane_state = dict(lane_state) if isinstance(lane_state, dict) else {}
                lane_state.update(
                    {
                        "last_outcome": result["status"],
                        "last_checked_at": started_at,
                        "last_attempt_at": result.get("attempted_at") or started_at,
                    }
                )
                if result["status"] == "opened":
                    marker = {
                        "ts": result["attempted_at"],
                        "email": email,
                        "kind": "keepalive",
                    }
                    if capacity.append_ledger(marker):
                        opened += 1
                        lane_state["last_opened_at"] = result["attempted_at"]
                        for key in ("auth_failed_at", "auth_code", "auth_cleared_at"):
                            lane_state.pop(key, None)
                    else:
                        result.update(status="failed", reason="ledger-write")
                        lane_state["last_outcome"] = "failed"
                elif result.get("reason") == "auth":
                    lane_state.pop("auth_cleared_at", None)
                    lane_state["auth_failed_at"] = result["attempted_at"]
                    lane_state["auth_code"] = result["auth_code"]
                    if _auth_log_due(lane_state, current):
                        lane_state["last_auth_log_at"] = started_at
                lane_states[email] = lane_state

            finished = _clock(now) if now is not None else now_local()
            ordered_results = [results_by_email[email] for email, _secret in enrolled]
            counts: dict[str, int] = {}
            for result in ordered_results:
                status = result["status"]
                counts[status] = counts.get(status, 0) + 1
            state.update(
                {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "updated_at": iso(_clock(finished)),
                    "last_run": {
                        "started_at": started_at,
                        "finished_at": iso(_clock(finished)),
                        "family": family,
                        "dry_run": False,
                        "opened": opened,
                        "counts": counts,
                    },
                    "lanes": lane_states,
                }
            )
            atomic_write_json(paths.keepalive_state_path(), state)

    ordered = [results_by_email[email] for email, _secret in enrolled]
    return {
        "generated_at": started_at,
        "family": family,
        "dry_run": dry_run,
        "opened": sum(result["status"] == "opened" for result in ordered),
        "results": ordered,
    }


def clear_auth_dead(email: str, *, now: datetime | None = None) -> bool:
    """Let a newly enrolled token receive one keepalive authentication try."""
    current = _clock(now)
    try:
        with _state_lock():
            state = load_json(paths.keepalive_state_path(), {}) or {}
            state = state if isinstance(state, dict) else {}
            lanes = state.get("lanes")
            lanes = dict(lanes) if isinstance(lanes, dict) else {}
            lane = lanes.get(email)
            lane = dict(lane) if isinstance(lane, dict) else {}
            for key in ("auth_failed_at", "auth_code", "last_auth_log_at"):
                lane.pop(key, None)
            lane["auth_cleared_at"] = iso(current)
            lanes[email] = lane
            state.update(
                {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "updated_at": iso(current),
                    "lanes": lanes,
                }
            )
            atomic_write_json(paths.keepalive_state_path(), state)
        return True
    except OSError:
        return False
