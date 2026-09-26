"""Liveness alerts (subfleet/liveness.py): a dead session with pending work
reaches Max by Telegram after the grace period, from a process that is not a
Claude session, with a fake roster and a fake tg."""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from subfleet import cli, liveness, notify, tickle, watchdog
from subfleet.util import iso, now_local
from test_notify import SESSION, registry  # noqa: F401  (fixture reuse)
from test_tickle import TOOL_USE, _entry, _write

DEAD = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


@pytest.fixture
def fake_tg(tmp_path, monkeypatch):
    log = tmp_path / "tg.log"
    script = tmp_path / "fake-tg"
    script.write_text(f'#!/bin/bash\n[ "${{TG_FAIL:-0}}" = 1 ] && {{ echo "ERROR: boom"; exit 1; }}\n'
                      f'printf "%s\\n---\\n" "$*" >> "{log}"\necho "sent ✓ message_id 7"\n')
    script.chmod(0o755)
    monkeypatch.setenv("SUBFLEET_TG", str(script))
    return log


def _dead_session(claude_dir: Path, session_id: str = DEAD, *, rows=None, idle_min: float = 15.0,
                  name: str = "ceremony-lane", cwd: str = "/work/repo") -> Path:
    sessions = claude_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "999998.json").write_text(json.dumps({
        "pid": 999998, "sessionId": session_id, "cwd": cwd, "name": name, "startedAt": 1787500000000,
        "messagingSocketPath": str(claude_dir / "gone.sock"), "kind": "interactive",
    }))
    projects = claude_dir / "projects" / "-work-repo"
    rows = rows or [_entry("user", "verify run 3", uuid="u1", age_s=idle_min * 60 + 30, cwd=cwd),
                    _entry("assistant", TOOL_USE, uuid="a1", age_s=idle_min * 60)]
    path = _write(projects / f"{session_id}.jsonl", rows)
    stamp = time.time() - idle_min * 60
    os.utime(path, (stamp, stamp))
    return path


def test_roster_lists_dead_sessions_waiting_on_a_process(registry, monkeypatch):
    claude_dir = registry["claude_dir"]
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    # the registry fixture's SESSION is alive (our own pid): excluded
    _write(claude_dir / "projects" / "-Users-max" / f"{SESSION}.jsonl", [_entry("assistant", TOOL_USE, uuid="a0", age_s=900)])
    _dead_session(claude_dir)
    done = "dddddddd-dddd-4ddd-8ddd-000000000002"
    _write(claude_dir / "projects" / "-work-repo" / f"{done}.jsonl",
           [_entry("user", "x", uuid="u2", age_s=900), _entry("assistant", "All done.", uuid="a2", age_s=800)])
    lane = "dddddddd-dddd-4ddd-8ddd-000000000003"
    _write(claude_dir / "projects" / "-work-repo" / f"{lane}.jsonl",
           [_entry("user", "# brief", uuid="u3", age_s=900, promptSource="sdk", entrypoint="sdk-cli"),
            _entry("assistant", TOOL_USE, uuid="a3", age_s=800)])
    census = liveness.roster()
    assert census["census_ok"] is True and SESSION in census["live_ids"]
    rows = census["rows"]
    assert [row["session_id"] for row in rows] == [DEAD]
    row = rows[0]
    assert row["name"] == "ceremony-lane" and row["cwd"] == "/work/repo" and row["dead_pid"] == 999998
    assert row["tail"] == "interrupted" and "Bash" in row["detail"] and 14 <= row["idle_min"] <= 16
    assert row["runs"] == [] and row["armed"] == [] and row["revives"] == []
    assert row["last_uuid"] == "a1" and row["transcript"].endswith(f"{DEAD}.jsonl")
    # a live revive host counts as alive
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {DEAD: [4321]})
    assert liveness.roster()["rows"] == []
    # a failed process census is not a verdict
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: None)
    assert liveness.roster() == {"rows": [], "live_ids": {SESSION}, "census_ok": False}


def test_run_alerts_after_the_grace_period_via_tg_and_dedupes(fake_tg, monkeypatch):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    _dead_session(claude_dir, idle_min=6)
    tickle.save_record(DEAD, {"history": [{"at": iso(now_local() - timedelta(minutes=4)), "revive": True,
                                           "pid": 64107, "host": "print", "lane": "max@example.com"}]})
    within = liveness.run(grace_minutes=10)
    assert within["due"] == [] and within["alerts_sent"] == [] and [c["session_id"] for c in within["cold"]] == [DEAD]
    assert not fake_tg.exists()
    assert "in grace" in liveness.format_summary(within)

    fired = liveness.run(grace_minutes=5)
    assert fired["alerts_sent"] == [DEAD] and fired["due"] == [DEAD]
    text = fake_tg.read_text()
    assert f"session {DEAD[:8]} (ceremony-lane) has no live process and pending work" in text
    assert f"session: {DEAD}" in text and "cwd: /work/repo" in text
    assert "last transcript write:" in text and "min ago)" in text
    assert "tail: interrupted — a tool call never got its result (Bash)" in text
    assert "subfleet revive: 1 launch(es) in the window" in text and "pid 64107 host print lane max@example.com" in text
    assert "none left a live process" in text and tickle.CAUSE_UNKNOWN in text
    assert f"claude --resume {DEAD}" in text
    assert "ALERTED" in liveness.format_summary(fired)
    state = json.loads(liveness.state_path().read_text())
    assert state[DEAD]["active"] is True and state[DEAD]["transport"] == "tg" and state[DEAD]["last_uuid"] == "a1"

    # same point, still dead: no second message inside the re-alert window
    again = liveness.run(grace_minutes=5)
    assert again["alerts_sent"] == [] and fake_tg.read_text().count("---") == 1
    # a new interruption point alerts at once
    _dead_session(claude_dir, idle_min=6, rows=[_entry("user", "again", uuid="u9", age_s=400),
                                                _entry("assistant", TOOL_USE, uuid="a9", age_s=360)])
    assert liveness.run(grace_minutes=5)["alerts_sent"] == [DEAD]
    assert fake_tg.read_text().count("---") == 2
    # and the same point re-alerts once the window has passed
    state = json.loads(liveness.state_path().read_text())
    state[DEAD]["last_sent"] = iso(now_local() - timedelta(hours=liveness.REALERT_HOURS + 1))
    liveness.state_path().write_text(json.dumps(state))
    assert liveness.run(grace_minutes=5)["alerts_sent"] == [DEAD]


def test_completed_tail_with_armed_watchers_or_pending_runs_is_reported(fake_tg, monkeypatch):
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    bg = "Command running in background with ID: bhit7b0nw. Output is being written to: /tmp/t.output."
    rows = [_entry("user", "continue", uuid="u1", age_s=1300),
            _entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"run_in_background": True}}],
                   uuid="a1", age_s=1250),
            _entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": bg}], uuid="u2", age_s=1240),
            _entry("assistant", [{"type": "text", "text": "Waiting on the watcher."}], uuid="a2", age_s=1200)]
    _dead_session(claude_dir, rows=rows, idle_min=20)
    runs = Path(os.environ["SUBFLEET_STATE_DIR"]) / "runs" / "20260905-230519-plan-a-live-ingest-r4"
    runs.mkdir(parents=True)
    (runs / "meta.json").write_text(json.dumps({"id": runs.name, "family": "claude", "model": "m", "lane": "l",
                                                "caller": {"session_id": DEAD}, "pid": os.getpid(), "finished_at": None}))
    summary = liveness.run(grace_minutes=10)
    assert summary["alerts_sent"] == [DEAD]
    text = fake_tg.read_text()
    assert "tail: needs-continuation" in text
    assert "armed in its last turn, now orphaned: shell:bhit7b0nw" in text
    assert "detached runs still pending: 20260905-230519-plan-a-live-ingest-r4" in text
    assert "subfleet revive: no launch recorded in the window" in text


def test_a_completion_notice_that_never_reached_a_dead_session_keeps_it_on_the_roster(fake_tg, monkeypatch):
    """The 2026-09-06 21:40 shape: the run finished, its push was accepted by
    the seat's inbox and left no trace, the app then paused the seat. The
    tail reads completed and nothing is RUNNING, yet the session is waiting
    on a process — for the notice."""
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    rows = [_entry("user", "status?", uuid="u1", age_s=1300),
            _entry("assistant", "Status at 21:33 EDT, all read from disk.", uuid="a1", age_s=1200)]
    _dead_session(claude_dir, rows=rows, idle_min=20)
    assert liveness.roster()["rows"] == []  # a finished tail with nothing pending
    run_id = "20260906-212548-gate3-floors-v3-c4"
    stamp = iso(now_local() - timedelta(minutes=15))
    notify.append_notice(DEAD, {
        "run_id": run_id, "session_id": DEAD, "ts": stamp, "rc": 0, "text": f"subfleet: run {run_id} FINISHED",
        "pushed": True, "push": {"delivered": True, "at": stamp, "pid": 999998}, "surfaced": False,
    })
    fresh = iso(now_local() - timedelta(seconds=30))  # still inside the follow-up grace: not pending work yet
    notify.append_notice(DEAD, {
        "run_id": "20260907-1-fresh", "session_id": DEAD, "ts": fresh, "rc": 0, "text": "subfleet: run 20260907-1-fresh FINISHED",
        "pushed": True, "push": {"delivered": True, "at": fresh, "pid": 999998}, "surfaced": False,
    })
    [row] = liveness.roster()["rows"]
    assert row["notices"] == [run_id] and row["tail"] == "completed" and row["runs"] == []
    summary = liveness.run(grace_minutes=10)
    assert summary["alerts_sent"] == [DEAD] and summary["cold"][0]["notices"] == [run_id]
    text = fake_tg.read_text()
    assert f"finished runs whose completion notice never reached it: {run_id}" in text
    assert "notices 1" in liveness.format_summary(summary)


def test_recovery_notice_when_the_session_is_live_again(fake_tg, registry, monkeypatch):
    claude_dir = registry["claude_dir"]
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    # SESSION is alive via the registry fixture; make it look dead first
    live_row = claude_dir / "sessions" / f"{registry['pid']}.json"
    hidden = live_row.read_text()
    live_row.unlink()
    _dead_session(claude_dir, SESSION, idle_min=30)
    assert liveness.run(grace_minutes=10)["alerts_sent"] == [SESSION]
    live_row.write_text(hidden)
    summary = liveness.run(grace_minutes=10)
    assert summary["recovered"] == [SESSION] and summary["alerts_sent"] == []
    assert f"session {SESSION[:8]} is live again (pid {registry['pid']})" in fake_tg.read_text()
    state = json.loads(liveness.state_path().read_text())
    assert state[SESSION]["active"] is False and state[SESSION]["cleared_at"]
    # a session that merely aged out (or finished) clears silently
    assert liveness.run(grace_minutes=10) == {**liveness.run(grace_minutes=10), "alerts_sent": [], "recovered": []}


def test_send_uses_tg_then_the_notify_transport(env_paths, fake_tg, monkeypatch):
    assert liveness.send("hello\nbody")["transport"] == "tg"
    assert "hello\nbody" in fake_tg.read_text()
    monkeypatch.setenv("TG_FAIL", "1")
    result = liveness.send("subject line\nthe body")
    assert result == {"sent": True, "transport": "notify", "error": result["error"]} and "rc=1" in result["error"]
    assert "SUBJECT:subject line" in env_paths["notify_log"].read_text() and "BODY:the body" in env_paths["notify_log"].read_text()
    monkeypatch.setenv("SUBFLEET_TG", str(env_paths["tmp"] / "missing-tg"))
    assert liveness.send("x\ny")["transport"] == "notify"
    monkeypatch.setenv("SUBFLEET_NOTIFY", str(env_paths["tmp"] / "missing-notify"))
    lost = liveness.send("x\ny")
    assert lost["sent"] is False and "tg missing" in lost["error"]
    assert liveness.send("x", dry_run=True) == {"sent": True, "transport": "dry-run"}


def test_nothing_depends_on_a_live_session(fake_tg, monkeypatch, tmp_path):
    """No registry at all, no live sessions, no hooks: the alert still goes out."""
    claude_dir = Path(os.environ["SUBFLEET_CLAUDE_DIR"])
    assert not (claude_dir / "sessions").exists()
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    projects = claude_dir / "projects" / "-work-repo"
    path = _write(projects / f"{DEAD}.jsonl", [_entry("user", "go", uuid="u1", age_s=1300, cwd="/work/repo"),
                                               _entry("assistant", TOOL_USE, uuid="a1", age_s=1200)])
    stamp = time.time() - 1200
    os.utime(path, (stamp, stamp))
    assert notify.live_sessions() == []
    summary = liveness.run(grace_minutes=10)
    assert summary["alerts_sent"] == [DEAD]
    text = fake_tg.read_text()
    assert "(unnamed)" in text and "cwd: /work/repo" in text  # cwd from the transcript itself


def test_watchdog_and_revive_pass_run_the_liveness_check(env_paths, monkeypatch, capsys):
    from test_pick import entry
    from test_watchdog import snap

    calls = []
    monkeypatch.setattr(liveness, "run", lambda **kw: calls.append(kw) or {"alerts_sent": ["x"], "recovered": [], "cold": [], "due": ["x"], "census_ok": True})
    summary = watchdog.run(snap=snap([entry("/h/.codex-3", 5, account="b")]))
    assert summary["liveness"]["alerts_sent"] == ["x"] and calls[0]["dry_run"] is False
    monkeypatch.setattr(tickle, "auto_revive", lambda **kwargs: [])
    assert cli.main(["revive"]) == 0
    assert calls[-1]["dry_run"] is False and "subfleet liveness" in capsys.readouterr().out
    assert cli.main(["revive", "--dry-run"]) == 0 and calls[-1]["dry_run"] is True
    # a liveness failure never takes the watchdog or the revive pass down
    def boom(**kw):
        raise RuntimeError("census exploded")
    monkeypatch.setattr(liveness, "run", boom)
    assert watchdog.run(snap=snap([entry("/h/.codex-3", 5, account="b")]))["liveness"]["error"] == "census exploded"
    assert cli.main(["revive"]) == 0


def test_liveness_cli(fake_tg, monkeypatch, capsys):
    monkeypatch.setattr(tickle.capacity, "read_ledger", lambda path=None: [])
    monkeypatch.setattr(tickle, "live_revive_sessions", lambda: {})
    assert cli.main(["liveness"]) == 0
    assert "no dead session is waiting on a process" in capsys.readouterr().out
    _dead_session(Path(os.environ["SUBFLEET_CLAUDE_DIR"]), idle_min=20)
    assert cli.main(["liveness", "--dry-run", "--grace", "10", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["alerts_sent"] == [DEAD] and "[dry-run] LIVENESS ALERT" in captured.err
    assert not fake_tg.exists() and not liveness.state_path().exists()
    assert cli.main(["liveness", "--grace", "10"]) == 0
    assert "ALERTED" in capsys.readouterr().out and fake_tg.exists()
