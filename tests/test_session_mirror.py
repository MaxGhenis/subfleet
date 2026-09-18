"""cc-mirror-sessions health: the interactive-side sibling of the lanes.
A dead mirror job fails silently (sessions stop syncing across accounts), so
the watchdog treats a stale heartbeat log exactly like a capacity/auth cliff."""

import os
import time

from subfleet import claude, watchdog
from subfleet.claude import session_mirror_health
from subfleet.util import now_local


def touch(path, age_min=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("run ok\n")
    ts = time.time() - age_min * 60
    os.utime(path, (ts, ts))
    return path


class TestSessionMirrorHealth:
    def test_fresh_log_and_loaded_job_is_healthy(self, tmp_path):
        log = touch(tmp_path / "cc-mirror-sessions.log", age_min=1)
        h = session_mirror_health(log_path=log, job_probe=lambda: True,
                                  run_probe=lambda: None)
        assert h["status"] == "healthy"
        assert h["age_min"] is not None

    def test_default_heartbeat_is_the_state_sidecar_not_the_log(self, env_paths):
        # --quiet keeps the log silent on no-op runs; the sidecar is rewritten
        # every pass. Only the sidecar may drive the verdict.
        from subfleet import paths as p

        touch(p.cc_mirror_log_path(), age_min=90)          # stale log
        touch(p.cc_mirror_heartbeat_path(), age_min=1)     # fresh sidecar
        h = session_mirror_health(job_probe=lambda: True, run_probe=lambda: None)
        assert h["status"] == "healthy"

    def test_stale_log_with_no_run_is_stalled(self, tmp_path):
        log = touch(tmp_path / "cc-mirror-sessions.log", age_min=45)
        h = session_mirror_health(log_path=log, job_probe=lambda: True,
                                  run_probe=lambda: None)
        assert h["status"] == "stalled"

    def test_stale_log_with_live_run_is_running_not_stalled(self, tmp_path):
        # Observed live 2026-08-18: an 8.5-min in-flight pass during app-churn
        # re-seeding — the summary line lands only at END of run.
        log = touch(tmp_path / "cc-mirror-sessions.log", age_min=12)
        h = session_mirror_health(log_path=log, job_probe=lambda: True,
                                  run_probe=lambda: 8.5)
        assert h["status"] == "running"
        assert h["run_min"] == 8.5

    def test_hung_run_is_stalled(self, tmp_path):
        log = touch(tmp_path / "cc-mirror-sessions.log", age_min=50)
        h = session_mirror_health(log_path=log, job_probe=lambda: True,
                                  run_probe=lambda: 45.0)
        assert h["status"] == "stalled"

    def test_unloaded_job_is_stalled_even_with_fresh_log(self, tmp_path):
        log = touch(tmp_path / "cc-mirror-sessions.log", age_min=1)
        h = session_mirror_health(log_path=log, job_probe=lambda: False,
                                  run_probe=lambda: None)
        assert h["status"] == "stalled"

    def test_launchctl_unavailable_falls_back_to_log_age(self, tmp_path):
        log = touch(tmp_path / "cc-mirror-sessions.log", age_min=1)
        h = session_mirror_health(log_path=log, job_probe=lambda: None,
                                  run_probe=lambda: None)
        assert h["status"] == "healthy"

    def test_missing_log_is_absent_not_stalled(self, tmp_path):
        h = session_mirror_health(log_path=tmp_path / "nope.log",
                                  job_probe=lambda: True, run_probe=lambda: None)
        assert h["status"] == "absent"
        assert h["age_min"] is None

    def test_default_path_under_isolated_claude_dir_is_absent(self, env_paths):
        # env_paths points SUBFLEET_CLAUDE_DIR at a tmp dir with no sidecar —
        # the default-path probe must degrade to absent, never crash or alert.
        assert session_mirror_health(job_probe=lambda: None,
                                     run_probe=lambda: None)["status"] == "absent"


class TestEtimeParse:
    def test_formats(self):
        from subfleet.claude import _parse_etime

        assert _parse_etime("30") == 0.5
        assert _parse_etime("08:30") == 8.5
        assert _parse_etime("02:03:00") == 123.0
        assert _parse_etime("1-00:30:00") == 1470.0
        assert _parse_etime("") is None
        assert _parse_etime("garbage") is None


def mirror_snap(mirror):
    from test_claude_pick import lane_row
    from subfleet.claude import lanes_fleet
    from test_pick import entry

    return {
        "generated_at": now_local().isoformat(timespec="seconds"),
        "codex": {
            "homes": [entry("/h/.codex-3", 5, account="b")],
            "duplicates": [],
            "fleet": {"total_homes": 1, "dispatchable_now": 1,
                      "best_home": "/h/.codex-3", "earliest_reset": None},
        },
        "claude": {
            "account": {"email": "anchor@x.com"},
            "known_accounts": [],
            "subscription": "max",
            "tier": "default_claude_max_20x",
            "keychain": {"status": "ok"},
            "oauth_probe": {"status": "token-invalid"},
            "statusline": None,
            "session_mirror": mirror,
            "recent_errors": [],
            "active_limit": None,
            "verdict": "ok",
            "lanes": lanes_fleet([lane_row("a@x.com", fh=10, wk=10)]),
        },
    }


def stalled_mirror(job_loaded=True):
    return {"status": "stalled", "log": "/x/cc-mirror-sessions.log",
            "age_min": 42.0, "job_loaded": job_loaded, "as_of": "2026-08-18T07:00:00-04:00"}


class TestWatchdogMirrorCondition:
    def test_stalled_mirror_alerts_with_kickstart(self, env_paths):
        summary = watchdog.run(snap=mirror_snap(stalled_mirror()))
        assert "cc-mirror-stalled" in summary["alerts_sent"]
        log = env_paths["notify_log"].read_text()
        assert "launchctl kickstart" in log
        assert "42.0 min" in log

    def test_unloaded_job_names_the_cause(self, env_paths):
        watchdog.run(snap=mirror_snap(stalled_mirror(job_loaded=False)))
        assert "launchd job not loaded" in env_paths["notify_log"].read_text()

    def test_healthy_and_absent_stay_silent(self, env_paths):
        healthy = {"status": "healthy", "age_min": 0.5, "job_loaded": True}
        absent = {"status": "absent", "age_min": None, "job_loaded": None}
        for mirror in (healthy, absent, None):
            assert "cc-mirror-stalled" not in watchdog.run(snap=mirror_snap(mirror))["alerts_sent"]

    def test_recovery_notice_when_mirror_heals(self, env_paths):
        watchdog.run(snap=mirror_snap(stalled_mirror()))
        summary = watchdog.run(snap=mirror_snap(
            {"status": "healthy", "age_min": 0.2, "job_loaded": True}))
        assert "cc-mirror-stalled" in summary["recovered"]


class TestRenderMirror:
    def test_table_shows_stalled_line_only_when_stalled(self, env_paths):
        from subfleet import render

        out = render.table(mirror_snap(stalled_mirror()))
        assert "session mirror STALLED" in out
        out_ok = render.table(mirror_snap({"status": "healthy", "age_min": 0.5,
                                           "job_loaded": True}))
        assert "session mirror" not in out_ok
