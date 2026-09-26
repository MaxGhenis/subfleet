"""Liveness alerts that depend on no Claude session being alive.

The 2026-09-05 incident (seat-death forensics, 2026-09-06): the desktop app's
update quit killed the ceremony session at 22:57; subfleet's one-shot revive
answered one turn and exited at 23:06, leaving four armed watchers and two
detached runs with no process to receive their notifications; the tail read
"completed", so nothing revived it again; and the only relay to Max was the
dead session itself. Max found it at 06:06. The orchestrator had noticed the
loss at 00:10 from ``ps`` and the roster — but its relay was also a session.

This module is the relay that is not a session. Every watchdog cycle (and
every revive pass) it lists sessions that are registered in
``~/.claude/sessions`` or wrote a transcript recently, have NO live process
and no live revive host, and are waiting on something only a live process
can receive: an interrupted tail, a completed tail whose last turn armed
background tasks / monitors / timers, detached ``subfleet run`` dispatches
still pending, or finished dispatches whose completion notice never reached
the session (notify.py: a push the inbox accepted but the transcript never
showed, past the follow-up grace). When such a session has stayed dead past the
grace period (``SUBFLEET_LIVENESS_GRACE_MIN``, default 10 — a revive pass
runs every two minutes, so that is several chances to leave a live host),
Max gets a Telegram through the ``tg`` CLI naming the session, its cwd, the
last transcript time, what is pending, and what revive did. One alert per
session and interruption point, re-sent at most every REALERT_HOURS while it
persists, and an all-clear when the session is live again.

Everything here is read-only against sessions and transcripts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import notify, paths, tickle
from .lanes import headless_transcript, lane_session_ids
from .util import atomic_write_json, fmt_clock, iso, load_json, now_local, parse_iso

DEFAULT_GRACE_MIN = 10.0
DEFAULT_MAX_AGE_H = 12.0
REALERT_HOURS = 6.0
_TITLE_SCAN_LINES = 4000


def grace_min(env: dict[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    try:
        return float(env.get("SUBFLEET_LIVENESS_GRACE_MIN") or DEFAULT_GRACE_MIN)
    except ValueError:
        return DEFAULT_GRACE_MIN


def max_age_h(env: dict[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    try:
        return float(env.get("SUBFLEET_LIVENESS_MAX_AGE_H") or DEFAULT_MAX_AGE_H)
    except ValueError:
        return DEFAULT_MAX_AGE_H


def state_path() -> Path:
    """Own state file: the watchdog's alerts.json recovery loop deactivates
    every key it does not itself own, which would re-fire these each cycle."""
    return paths.state_dir() / "liveness-alerts.json"


def tg_bin(env: dict[str, str] | None = None) -> Path:
    """The Telegram CLI the telegram skill documents (~/bin/tg); overridable."""
    env = os.environ if env is None else env
    explicit = env.get("SUBFLEET_TG")
    return Path(explicit).expanduser() if explicit else Path.home() / "bin" / "tg"


# --------------------------------------------------------------------------
# Roster: who has no process and is waiting on one
# --------------------------------------------------------------------------

def _registry_rows() -> dict[str, dict[str, Any]]:
    """Every ``~/.claude/sessions`` row keyed by session id, the liveliest
    row per session (a restarted session leaves stale rows for a while)."""
    rows: dict[str, dict[str, Any]] = {}
    try:
        entries = list(notify.sessions_dir().glob("*.json"))
    except OSError:
        return rows
    for entry in entries:
        data = load_json(entry)
        if not isinstance(data, dict) or not isinstance(data.get("sessionId"), str):
            continue
        pid = data.get("pid") if isinstance(data.get("pid"), int) else None
        row = {
            "session_id": data["sessionId"],
            "pid": pid,
            "alive": notify._pid_alive(pid),
            "cwd": data.get("cwd") if isinstance(data.get("cwd"), str) else None,
            "name": data.get("name") if isinstance(data.get("name"), str) else None,
            "started_at": data.get("startedAt") if isinstance(data.get("startedAt"), (int, float)) else 0,
        }
        current = rows.get(row["session_id"])
        key = (row["alive"], row["started_at"])
        if current is None or key > (current["alive"], current["started_at"]):
            rows[row["session_id"]] = row
    return rows


def _recent_transcripts(cutoff: float) -> dict[str, Path]:
    """Transcripts modified since ``cutoff`` (epoch seconds), by session id."""
    found: dict[str, tuple[float, Path]] = {}
    projects = paths.claude_dir() / "projects"
    try:
        directories = [d for d in projects.iterdir() if d.is_dir()]
    except OSError:
        return {}
    for directory in directories:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".jsonl"):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            session_id = entry.name[:-6]
            current = found.get(session_id)
            if current is None or mtime > current[0]:
                found[session_id] = (mtime, Path(entry.path))
    return {session_id: path for session_id, (_mtime, path) in found.items()}


def _transcript_title(path: Path) -> str | None:
    """The session's custom title, from the tail (bounded scan)."""
    seen = 0
    for line in tickle._lines_reversed(path):
        seen += 1
        if seen > _TITLE_SCAN_LINES:
            break
        if '"custom-title"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "custom-title":
            title = entry.get("customTitle")
            return title if isinstance(title, str) and title.strip() else None
    return None


def roster(*, now: datetime | None = None, max_age_hours: float | None = None) -> dict[str, Any]:
    """Sessions with no live process that are waiting on one.

    Returns {"rows": [...], "live_ids": set, "census_ok": bool}. ``rows`` is
    empty when the process census failed (``census_ok`` False): a failed
    ``ps`` must not read as "everything died".
    """
    now = now or now_local()
    hours = max_age_h() if max_age_hours is None else max_age_hours
    cutoff = now.timestamp() - hours * 3600
    live_ids = {row["session_id"] for row in notify.live_sessions(include_lanes=True)}
    hosts = tickle.live_revive_sessions()
    if hosts is None:
        return {"rows": [], "live_ids": live_ids, "census_ok": False}
    # A host blocked on a dialog is a process, not a live session.
    stalled = tickle.stalled_revive_hosts()
    live_ids |= set(hosts) - set(stalled)
    lanes = lane_session_ids()
    registry = _registry_rows()
    transcripts = _recent_transcripts(cutoff)
    rows: list[dict[str, Any]] = []
    for session_id in sorted(set(registry) | set(transcripts)):
        if session_id in live_ids or session_id in lanes:
            continue
        transcript = transcripts.get(session_id) or notify.transcript_path(session_id)
        if transcript is None:
            continue
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        if headless_transcript(transcript) or tickle.retired(session_id):
            continue
        state = tickle.turn_state(transcript)
        continuation = tickle.continuation_needed(state, session_id)
        if continuation:
            tail, detail = "needs-continuation", continuation["detail"]
            armed, runs = continuation["armed"], continuation["runs"]
        else:
            tail, detail = state["state"], state["detail"]
            armed = [arm for arm in (state.get("armed_tasks") or []) if isinstance(arm, dict)]
            runs = tickle.pending_runs(session_id)
        # Completion notices nothing confirmed (a push that left no trace,
        # a parked notice) past the follow-up grace: the run finished and
        # the session that dispatched it has no process to hear about it.
        notices = [
            str(row.get("run_id")) for row in notify.unresolved_notices(session_id)
            if (anchor := notify.delivery_anchor(row)) is None
            or (now - anchor).total_seconds() >= tickle.followup_grace_s()
        ]
        if not (armed or runs or notices or tail == "interrupted"):
            continue
        record = tickle.load_record(session_id)
        revives = tickle.recent_revives(record, now=now, window_s=hours * 3600)
        entry = registry.get(session_id) or {}
        cwd = entry.get("cwd")
        if not cwd:
            cwd = tickle._session_meta_from_store(session_id).get("cwd")
        last_write = datetime.fromtimestamp(mtime).astimezone()
        rows.append({
            "session_id": session_id,
            "name": entry.get("name"),
            "title": _transcript_title(transcript),
            "cwd": cwd,
            "transcript": str(transcript),
            "last_write": iso(last_write),
            "idle_min": round((now - last_write).total_seconds() / 60, 1),
            "tail": tail,
            "detail": detail,
            "last_uuid": state.get("last_uuid"),
            "armed": armed,
            "runs": runs,
            "notices": notices,
            "host_stalled": (stalled.get(session_id) or {}).get("marker"),
            "dead_pid": entry.get("pid"),
            "revives": [{k: item.get(k) for k in ("at", "pid", "host", "lane", "model", "tmux_session")}
                        for item in revives],
        })
    rows.sort(key=lambda row: row["idle_min"])
    return {"rows": rows, "live_ids": live_ids, "census_ok": True}


def due(rows: list[dict[str, Any]], *, grace_minutes: float | None = None) -> list[dict[str, Any]]:
    """Rows dead for at least the grace period with no revive having left a
    live process (every row here has none: the roster excluded live ones)."""
    grace = grace_min() if grace_minutes is None else grace_minutes
    return [row for row in rows if row["idle_min"] >= grace]


# --------------------------------------------------------------------------
# The message and its transport
# --------------------------------------------------------------------------

def _arm_label(arm: dict[str, Any]) -> str:
    return tickle._arm_label(arm)


def format_alert(row: dict[str, Any], now: datetime | None = None) -> str:
    now = now or now_local()
    sid = row["session_id"]
    label = row.get("name") or row.get("title") or "unnamed"
    last_write = parse_iso(row.get("last_write"))
    lines = [
        f"🚨 subfleet liveness: session {sid[:8]} ({label}) has no live process and pending work",
        f"session: {sid}",
        f"cwd: {row.get('cwd') or '?'}",
        f"last transcript write: {fmt_clock(last_write, now)} ({int(row.get('idle_min') or 0)} min ago)",
        f"tail: {row.get('tail')} — {row.get('detail')}",
    ]
    armed = row.get("armed") or []
    if armed:
        labels = ", ".join(_arm_label(arm) for arm in armed[:8])
        more = f", +{len(armed) - 8} more" if len(armed) > 8 else ""
        lines.append(f"armed in its last turn, now orphaned: {labels}{more}")
    runs = row.get("runs") or []
    if runs:
        lines.append("detached runs still pending: " + ", ".join(runs[:6])
                     + (f", +{len(runs) - 6} more" if len(runs) > 6 else ""))
    notices = row.get("notices") or []
    if notices:
        lines.append("finished runs whose completion notice never reached it: " + ", ".join(notices[:6])
                     + (f", +{len(notices) - 6} more" if len(notices) > 6 else "")
                     + " (subfleet notices --session <id>)")
    revives = row.get("revives") or []
    if revives:
        last = revives[-1]
        lines.append(
            f"subfleet revive: {len(revives)} launch(es) in the window, last at "
            f"{fmt_clock(parse_iso(last.get('at')), now)} pid {last.get('pid')} host "
            f"{last.get('host') or 'print'} lane {last.get('lane') or '?'} — none left a live process"
        )
    else:
        lines.append("subfleet revive: no launch recorded in the window (subfleet revive --dry-run shows why)")
    if row.get("host_stalled"):
        lines.append(f"its tmux revive host is stalled on a dialog ({row['host_stalled']}); "
                     f"the next revive pass tears it down and retries on another lane — "
                     f"attach meanwhile: {tickle.tmux_attach_hint(sid)}")
    lines.append(f"cause of the process loss: {tickle.CAUSE_UNKNOWN}")
    lines.append(f"resume: open it in the app, or `claude --resume {sid}` in tmux; "
                 f"then `subfleet runs --mine` inside it")
    return "\n".join(lines)


def format_recovery(session_id: str, pid: int | None = None) -> str:
    who = f" (pid {pid})" if pid else ""
    return f"✅ subfleet liveness: session {session_id[:8]} is live again{who}"


def send(text: str, *, dry_run: bool = False, runner=subprocess.run) -> dict[str, Any]:
    """Telegram via the ``tg`` CLI; the chief-of-staff ``notify`` transport
    (Telegram-first, email fallback) only when ``tg`` is missing or fails."""
    if dry_run:
        print(f"[dry-run] LIVENESS ALERT:\n{text}\n", file=sys.stderr)
        return {"sent": True, "transport": "dry-run"}
    tg = tg_bin()
    error = None
    if tg.exists():
        try:
            out = runner([str(tg), text], capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                return {"sent": True, "transport": "tg"}
            error = f"tg rc={out.returncode}: {(out.stderr or out.stdout or '')[:200].strip()}"
        except (OSError, subprocess.SubprocessError) as exc:
            error = f"tg: {exc.__class__.__name__}: {exc}"
    else:
        error = f"tg missing at {tg}"
    fallback = paths.notify_bin()
    if fallback.exists():
        subject, _, body = text.partition("\n")
        try:
            out = runner([str(fallback), subject, body], capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                return {"sent": True, "transport": "notify", "error": error}
            error += f"; notify rc={out.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            error += f"; notify: {exc.__class__.__name__}: {exc}"
    print(f"subfleet liveness: alert not sent ({error})", file=sys.stderr)
    return {"sent": False, "transport": None, "error": error}


# --------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------

def run(*, dry_run: bool = False, now: datetime | None = None,
        grace_minutes: float | None = None, runner=subprocess.run) -> dict[str, Any]:
    """Evaluate, alert, remember. Safe to call from the watchdog and from
    every revive pass: the state file dedupes across both."""
    now = now or now_local()
    census = roster(now=now)
    rows = census["rows"]
    pending_ids = {row["session_id"] for row in rows}
    ready = due(rows, grace_minutes=grace_minutes) if census["census_ok"] else []
    state = load_json(state_path(), {}) or {}
    if not isinstance(state, dict):
        state = {}
    sent: list[str] = []
    recovered: list[str] = []
    for row in ready:
        key = row["session_id"]
        prev = state.get(key) if isinstance(state.get(key), dict) else {}
        last_sent = parse_iso(prev.get("last_sent"))
        new_point = prev.get("last_uuid") != row.get("last_uuid")
        overdue = last_sent is None or (now - last_sent).total_seconds() >= REALERT_HOURS * 3600
        if not prev.get("active") or new_point or overdue:
            result = send(format_alert(row, now), dry_run=dry_run, runner=runner)
            if result.get("sent"):
                state[key] = {"active": True, "last_sent": iso(now), "last_uuid": row.get("last_uuid"),
                              "transport": result.get("transport"), "tail": row.get("tail")}
                sent.append(key)
        else:
            state[key] = {**prev, "active": True}
    if census["census_ok"]:
        for key, prev in list(state.items()):
            if not isinstance(prev, dict) or not prev.get("active") or key in pending_ids:
                continue
            if key in census["live_ids"]:
                entry = notify.find_session(key)
                send(format_recovery(key, (entry or {}).get("pid")), dry_run=dry_run, runner=runner)
                recovered.append(key)
            state[key] = {**prev, "active": False, "cleared_at": iso(now)}
    if not dry_run and (state or state_path().exists()):
        # nothing to remember and nothing remembered: leave no file behind
        try:
            atomic_write_json(state_path(), state)
        except OSError as exc:
            print(f"subfleet liveness: could not save state: {exc}", file=sys.stderr)
    return {
        "generated_at": iso(now),
        "census_ok": census["census_ok"],
        "cold": [{k: row.get(k) for k in ("session_id", "name", "tail", "idle_min", "armed", "runs", "notices")}
                 for row in rows],
        "due": [row["session_id"] for row in ready],
        "alerts_sent": sent,
        "recovered": recovered,
    }


def format_summary(summary: dict[str, Any]) -> str:
    if not summary.get("census_ok", True):
        return "subfleet liveness: process census failed; no verdict this pass"
    cold = summary.get("cold") or []
    if not cold:
        return "subfleet liveness: no dead session is waiting on a process"
    lines = [f"subfleet liveness: {len(cold)} dead session(s) waiting on a process"
             f" · alerted {len(summary.get('alerts_sent') or [])} · recovered {len(summary.get('recovered') or [])}"]
    for row in cold:
        armed = len(row.get("armed") or [])
        runs = len(row.get("runs") or [])
        notices = len(row.get("notices") or [])
        flag = "ALERTED" if row.get("session_id") in (summary.get("alerts_sent") or []) else (
            "due" if row.get("session_id") in (summary.get("due") or []) else "in grace")
        lines.append(f"  {str(row.get('session_id'))[:8]}… {str(row.get('name') or '-'):<24.24} "
                     f"{str(row.get('tail')):<19} idle {int(row.get('idle_min') or 0):>4}m "
                     f"armed {armed} runs {runs} notices {notices}  {flag}")
    return "\n".join(lines)
