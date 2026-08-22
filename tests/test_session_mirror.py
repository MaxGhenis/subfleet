"""Health, alerting, and rendering for the session-mirror heartbeat."""

import os
import time

from carpool import claude, paths, render, watchdog
from carpool.claude import session_mirror_health
from carpool.util import now_local


def touch(path, age_min=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("pass complete\n")
    timestamp = time.time() - age_min * 60
    os.utime(path, (timestamp, timestamp))
    return path


class TestSessionMirrorHealth:
    def test_fresh_heartbeat_and_available_scheduler_are_healthy(self, tmp_path):
        heartbeat = touch(tmp_path / "mirror-state.json", age_min=1)
        health = session_mirror_health(
            log_path=heartbeat, job_probe=lambda: True, run_probe=lambda: None
        )
        assert health["status"] == "healthy"
        assert health["age_min"] is not None

    def test_default_heartbeat_is_sidecar_not_output_log(self, env_paths):
        touch(paths.cc_mirror_log_path(), age_min=90)
        touch(paths.cc_mirror_heartbeat_path(), age_min=1)

        health = session_mirror_health(
            job_probe=lambda: True, run_probe=lambda: None
        )

        assert health["status"] == "healthy"

    def test_stale_heartbeat_without_in_flight_run_is_stalled(self, tmp_path):
        heartbeat = touch(tmp_path / "mirror-state.json", age_min=45)
        health = session_mirror_health(
            log_path=heartbeat, job_probe=lambda: True, run_probe=lambda: None
        )
        assert health["status"] == "stalled"

    def test_stale_heartbeat_tolerates_in_flight_run(self, tmp_path):
        heartbeat = touch(tmp_path / "mirror-state.json", age_min=12)
        health = session_mirror_health(
            log_path=heartbeat, job_probe=lambda: True, run_probe=lambda: 8.5
        )
        assert health["status"] == "running"
        assert health["run_min"] == 8.5

    def test_thirty_minute_run_is_tolerated_but_later_is_hung(self, tmp_path):
        heartbeat = touch(tmp_path / "mirror-state.json", age_min=45)

        at_cutoff = session_mirror_health(
            log_path=heartbeat, job_probe=lambda: True, run_probe=lambda: 30.0
        )
        after_cutoff = session_mirror_health(
            log_path=heartbeat, job_probe=lambda: True, run_probe=lambda: 30.1
        )

        assert at_cutoff["status"] == "running"
        assert after_cutoff["status"] == "stalled"

    def test_unavailable_scheduler_falls_back_to_heartbeat_age(self, tmp_path):
        heartbeat = touch(tmp_path / "mirror-state.json", age_min=1)
        health = session_mirror_health(
            log_path=heartbeat, job_probe=lambda: None, run_probe=lambda: None
        )
        assert health["status"] == "healthy"

    def test_scheduler_reports_absent_as_stalled(self, tmp_path):
        heartbeat = touch(tmp_path / "mirror-state.json", age_min=1)
        health = session_mirror_health(
            log_path=heartbeat, job_probe=lambda: False, run_probe=lambda: None
        )
        assert health["status"] == "stalled"

    def test_missing_heartbeat_is_absent(self, tmp_path):
        health = session_mirror_health(
            log_path=tmp_path / "missing-state.json",
            job_probe=lambda: True,
            run_probe=lambda: None,
        )
        assert health["status"] == "absent"
        assert health["age_min"] is None

    def test_default_missing_sidecar_is_absent(self, env_paths):
        assert session_mirror_health(
            job_probe=lambda: None, run_probe=lambda: None
        )["status"] == "absent"


class TestElapsedTimeParsing:
    def test_supported_formats(self):
        assert claude._parse_etime("30") == 0.5
        assert claude._parse_etime("08:30") == 8.5
        assert claude._parse_etime("02:03:00") == 123.0
        assert claude._parse_etime("1-00:30:00") == 1470.0
        assert claude._parse_etime("") is None
        assert claude._parse_etime("invalid") is None


def codex_entry():
    primary = {
        "used_percent": 5,
        "window_seconds": 18_000,
        "reset_at": "2099-01-01T12:00:00+00:00",
    }
    secondary = {
        "used_percent": 10,
        "window_seconds": 604_800,
        "reset_at": "2099-01-07T12:00:00+00:00",
    }
    return {
        "home": "/lanes/.codex-1",
        "is_primary_home": False,
        "account_id": "ACCT-1",
        "email": "codex@example.com",
        "plan": "pro",
        "auth_last_refresh": None,
        "verdict": "ok",
        "probe": {"status": "ok"},
        "windows": {
            "primary": primary,
            "secondary": secondary,
            "five_hour": primary,
            "weekly": secondary,
            "source": "live",
            "as_of": now_local().isoformat(timespec="seconds"),
        },
        "rollout_observed": None,
        "recent_errors": {"usage_limit": [], "auth_revoked": []},
        "duplicate_of": None,
        "shadowed_by_app": False,
    }


def mirror_snapshot(mirror):
    lane = {
        "email": "worker@example.com",
        "active": False,
        "verdict": "ok",
        "five_hour_used_percent": 10,
        "five_hour_reset_at": "2099-01-01T12:00:00+00:00",
        "weekly_used_percent": 10,
    }
    return {
        "generated_at": now_local().isoformat(timespec="seconds"),
        "codex": {
            "homes": [codex_entry()],
            "duplicates": [],
            "app_home": {"home": "/app/.codex", "status": "missing", "shadows": []},
            "fleet": {
                "total_homes": 1,
                "dispatchable_now": 1,
                "best_home": "/lanes/.codex-1",
                "earliest_reset": None,
            },
        },
        "claude": {
            "account": {"email": "active@example.com"},
            "accounts": [],
            "known_accounts": [],
            "subscription": "paid",
            "tier": "paid",
            "keychain": {"status": "ok"},
            "oauth_probe": {"status": "token-invalid"},
            "statusline": None,
            "session_mirror": mirror,
            "recent_errors": [],
            "active_limit": None,
            "verdict": "ok",
            "lanes": {
                "enrolled": 1,
                "dispatchable_now": 1,
                "best": lane["email"],
                "earliest_reset": None,
                "lanes": [lane],
            },
        },
    }


def stalled_mirror(job_loaded=True):
    return {
        "status": "stalled",
        "log": "mirror-state.json",
        "age_min": 42.0,
        "job_loaded": job_loaded,
        "as_of": "2026-08-18T07:00:00+00:00",
    }


class TestWatchdogMirrorCondition:
    def test_stalled_mirror_alerts_with_generic_recovery_command(self, env_paths):
        summary = watchdog.run(snap=mirror_snapshot(stalled_mirror()))
        assert "cc-mirror-stalled" in summary["alerts_sent"]
        notice = env_paths["notify_log"].read_text()
        assert "carpool mirror" in notice
        assert "42.0 min" in notice

    def test_missing_scheduler_names_the_cause_without_platform_path(self, env_paths):
        watchdog.run(snap=mirror_snapshot(stalled_mirror(job_loaded=False)))
        notice = env_paths["notify_log"].read_text().lower()
        assert "scheduler" in notice or "job not" in notice
        assert "carpool mirror" in notice

    def test_healthy_absent_and_unavailable_states_are_silent(self, env_paths):
        healthy = {"status": "healthy", "age_min": 0.5, "job_loaded": True}
        absent = {"status": "absent", "age_min": None, "job_loaded": None}
        for mirror in (healthy, absent, None):
            summary = watchdog.run(snap=mirror_snapshot(mirror))
            assert "cc-mirror-stalled" not in summary["alerts_sent"]

    def test_recovery_notice_when_mirror_heals(self, env_paths):
        watchdog.run(snap=mirror_snapshot(stalled_mirror()))
        healed = {"status": "healthy", "age_min": 0.2, "job_loaded": True}
        summary = watchdog.run(snap=mirror_snapshot(healed))
        assert "cc-mirror-stalled" in summary["recovered"]


class TestRenderMirror:
    def test_table_only_shows_unhealthy_mirror(self, env_paths):
        output = render.table(mirror_snapshot(stalled_mirror()))
        assert "session mirror STALLED" in output

        healthy = {"status": "healthy", "age_min": 0.5, "job_loaded": True}
        assert "session mirror" not in render.table(mirror_snapshot(healthy))
