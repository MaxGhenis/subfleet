"""Focused safety and timing tests for Claude lane keepalives."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet import capacity, cli, keepalive, paths, render


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
EMAIL = "lane@example.com"
SECRET = "claude-lane-token"
TOKEN = "setup-token-value"


def _write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _configure_lanes(tmp_path: Path, monkeypatch, lanes: dict[str, str]) -> Path:
    config = tmp_path / "claude-accounts.json"
    config.write_text(json.dumps({"accounts": list(lanes), "enrolled": lanes}))
    monkeypatch.setenv("SUBFLEET_CLAUDE_ACCOUNTS", str(config))
    return config


def _secret_success(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, stdout=f"{TOKEN}\n", stderr="")


def _claude_success(command, **kwargs):
    return subprocess.CompletedProcess(
        command,
        0,
        stdout='{"is_error":false,"result":"ok"}\n',
        stderr="",
    )


def _never_called(*args, **kwargs):
    pytest.fail(f"unexpected subprocess call: {args!r} {kwargs!r}")


def test_idle_lane_uses_minimal_fake_claude_and_only_usage_ledger(
    tmp_path, monkeypatch
):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})
    observed_token = tmp_path / "observed-token"
    fake_claude = _write_executable(
        tmp_path / "claude",
        'printf "%s\\n" "$CLAUDE_CODE_OAUTH_TOKEN" > "$OBSERVED_TOKEN"\n'
        "printf '{\"is_error\":false,\"result\":\"ok\"}\\n'\n",
    )
    fake_secret = _write_executable(
        tmp_path / "agent-secret", f"printf '{TOKEN}\\n'\n"
    )
    monkeypatch.setenv("CLAUDE_LANE_CLAUDE", str(fake_claude))
    monkeypatch.setenv("CLAUDE_LANE_AGENT_SECRET", str(fake_secret))
    monkeypatch.setenv("OBSERVED_TOKEN", str(observed_token))
    monkeypatch.setenv("KEEPALIVE_SENTINEL", "preserved")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-token")
    monkeypatch.setenv("CLAUDE_LANE_DETACHED", "1")
    monkeypatch.setenv("CLAUDE_LANE_OWNED_PROMPT", "1")

    claude_calls = []
    secret_calls = []

    def claude_runner(command, **kwargs):
        claude_calls.append((command, kwargs))
        return subprocess.run(command, **kwargs)

    def secret_runner(command, **kwargs):
        secret_calls.append((command, kwargs))
        return subprocess.run(command, **kwargs)

    report = keepalive.run(
        now=NOW,
        runner=claude_runner,
        secret_runner=secret_runner,
    )

    assert report == {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "family": "claude",
        "dry_run": False,
        "opened": 1,
        "results": [
            {
                "email": EMAIL,
                "status": "opened",
                "attempted_at": NOW.isoformat(timespec="seconds"),
            }
        ],
    }
    assert secret_calls == [
        (
            [str(fake_secret), "get", SECRET],
            {"capture_output": True, "text": True, "timeout": 10},
        )
    ]
    assert len(claude_calls) == 1
    command, options = claude_calls[0]
    assert command == [
        str(fake_claude),
        "-p",
        "ok",
        "--model",
        "claude-haiku-4-5-20251001",
        "--output-format",
        "json",
    ]
    assert {key: value for key, value in options.items() if key != "env"} == {
        "capture_output": True,
        "text": True,
        "timeout": options["timeout"],
    }
    assert 0 < options["timeout"] <= 60
    child_env = options["env"]
    assert child_env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert child_env["KEEPALIVE_SENTINEL"] == "preserved"
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "ANTHROPIC_AUTH_TOKEN" not in child_env
    assert "CLAUDE_LANE_DETACHED" not in child_env
    assert "CLAUDE_LANE_OWNED_PROMPT" not in child_env
    assert observed_token.read_text() == f"{TOKEN}\n"

    assert capacity.read_ledger() == [
        {
            "email": EMAIL,
            "kind": "keepalive",
            "ts": NOW.isoformat(timespec="seconds"),
        }
    ]
    assert not paths.runs_dir().exists()
    serialized_state = paths.keepalive_state_path().read_text()
    assert TOKEN not in serialized_state
    assert SECRET not in serialized_state


def test_success_json_with_numeric_403_is_not_misclassified_as_auth(
    tmp_path, monkeypatch
):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})

    def success_with_403_metric(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"is_error":false,"result":"ok","duration_ms":403}\n',
            stderr="",
        )

    report = keepalive.run(
        now=NOW,
        runner=success_with_403_metric,
        secret_runner=_secret_success,
    )

    assert report["opened"] == 1
    assert report["results"][0]["status"] == "opened"
    state = json.loads(paths.keepalive_state_path().read_text())
    assert "auth_failed_at" not in state["lanes"][EMAIL]


def test_keychain_fetch_and_provider_share_one_sixty_second_budget(
    tmp_path, monkeypatch
):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})
    ticks = iter([100.0, 112.5])
    monkeypatch.setattr(keepalive, "monotonic", lambda: next(ticks))
    provider_timeouts = []

    def runner(command, **kwargs):
        provider_timeouts.append(kwargs["timeout"])
        return _claude_success(command, **kwargs)

    report = keepalive.run(
        now=NOW,
        runner=runner,
        secret_runner=_secret_success,
    )

    assert report["opened"] == 1
    assert provider_timeouts == [47.5]


def test_recent_usage_keeps_open_window_unpinged(tmp_path, monkeypatch):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})
    recent = NOW - timedelta(hours=4, minutes=59, seconds=59)
    existing = {
        "ts": recent.isoformat(timespec="seconds"),
        "email": EMAIL,
        "input_tokens": 2,
        "output_tokens": 1,
        "total_tokens": 3,
    }
    assert capacity.append_ledger(existing)

    report = keepalive.run(
        now=NOW,
        runner=_never_called,
        secret_runner=_never_called,
    )

    assert report["opened"] == 0
    assert report["results"] == [
        {
            "email": EMAIL,
            "status": "skipped-open",
            "last_request_at": recent.isoformat(timespec="seconds"),
        }
    ]
    assert capacity.read_ledger() == [existing]


def test_request_at_exact_five_hour_boundary_opens_new_window(
    tmp_path, monkeypatch
):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})
    boundary = NOW - timedelta(hours=5)
    assert capacity.append_ledger(
        {
            "ts": boundary.isoformat(timespec="seconds"),
            "email": EMAIL,
            "total_tokens": 1,
        }
    )
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return _claude_success(command, **kwargs)

    report = keepalive.run(
        now=NOW,
        runner=runner,
        secret_runner=_secret_success,
    )

    assert report["opened"] == 1
    assert report["results"][0]["status"] == "opened"
    assert len(calls) == 1
    assert capacity.read_ledger()[-1] == {
        "email": EMAIL,
        "kind": "keepalive",
        "ts": NOW.isoformat(timespec="seconds"),
    }


def test_recent_run_ledger_request_is_a_skip_fallback(tmp_path, monkeypatch):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})
    finished = NOW - timedelta(minutes=7)
    run_dir = paths.runs_dir() / "20260822-115300-existing-run"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "family": "claude",
                "lane": EMAIL,
                "started_at": (finished - timedelta(minutes=2)).isoformat(),
                "finished_at": finished.isoformat(),
                "session_id": "session-that-reached-claude",
                "rc": 0,
            }
        )
    )

    report = keepalive.run(
        now=NOW,
        runner=_never_called,
        secret_runner=_never_called,
    )

    assert report["results"] == [
        {
            "email": EMAIL,
            "status": "skipped-open",
            "last_request_at": finished.isoformat(timespec="seconds"),
        }
    ]
    assert not paths.lane_usage_path().exists()
    assert [path.name for path in paths.runs_dir().iterdir()] == [run_dir.name]


def test_403_latches_auth_dead_and_detail_is_emitted_at_most_daily(
    tmp_path, monkeypatch
):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})
    claude_calls = []
    secret_calls = []

    def auth_failure(command, **kwargs):
        claude_calls.append(command)
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="HTTP 403: setup token disabled"
        )

    def secret_runner(command, **kwargs):
        secret_calls.append(command)
        return _secret_success(command, **kwargs)

    first = keepalive.run(
        now=NOW,
        runner=auth_failure,
        secret_runner=secret_runner,
    )
    assert first["results"] == [
        {
            "email": EMAIL,
            "status": "failed",
            "attempted_at": NOW.isoformat(timespec="seconds"),
            "reason": "auth",
            "auth_code": 403,
        }
    ]
    assert len(claude_calls) == len(secret_calls) == 1
    assert not paths.lane_usage_path().exists()
    state = json.loads(paths.keepalive_state_path().read_text())
    lane = state["lanes"][EMAIL]
    assert lane["auth_failed_at"] == NOW.isoformat(timespec="seconds")
    assert lane["auth_code"] == 403
    assert lane["last_auth_log_at"] == NOW.isoformat(timespec="seconds")

    quiet = keepalive.run(
        now=NOW + timedelta(hours=23, minutes=59, seconds=59),
        runner=_never_called,
        secret_runner=_never_called,
    )
    assert quiet["results"] == [
        {
            "email": EMAIL,
            "status": "skipped-auth",
            "auth_log_due": False,
        }
    ]
    state = json.loads(paths.keepalive_state_path().read_text())
    assert state["lanes"][EMAIL]["last_auth_log_at"] == NOW.isoformat(
        timespec="seconds"
    )

    daily = keepalive.run(
        now=NOW + timedelta(days=1),
        runner=_never_called,
        secret_runner=_never_called,
    )
    assert daily["results"] == [
        {
            "email": EMAIL,
            "status": "skipped-auth",
            "auth_log_due": True,
            "auth_failed_at": NOW.isoformat(timespec="seconds"),
            "auth_code": 403,
        }
    ]
    state = json.loads(paths.keepalive_state_path().read_text())
    assert state["lanes"][EMAIL]["last_auth_log_at"] == (
        NOW + timedelta(days=1)
    ).isoformat(timespec="seconds")
    assert len(claude_calls) == len(secret_calls) == 1


def test_completed_runner_auth_failure_is_skipped_without_a_request(
    tmp_path, monkeypatch
):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})
    failed = NOW - timedelta(hours=6)
    run_dir = paths.runs_dir() / "20260822-060000-auth-failure"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "family": "claude",
                "lane": EMAIL,
                "started_at": (failed - timedelta(seconds=2)).isoformat(),
                "finished_at": failed.isoformat(),
                "session_id": "provider-auth-attempt",
                "rc": 5,
            }
        )
    )

    report = keepalive.run(
        now=NOW,
        runner=_never_called,
        secret_runner=_never_called,
    )

    assert report["results"] == [
        {
            "email": EMAIL,
            "status": "skipped-auth",
            "auth_log_due": True,
            "auth_failed_at": failed.isoformat(timespec="seconds"),
            "auth_code": "runner-rc-5",
        }
    ]
    assert not paths.lane_usage_path().exists()


def test_same_second_reenrollment_clear_supersedes_auth_failure(
    tmp_path, monkeypatch
):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})
    stamp = (NOW - timedelta(hours=6)).isoformat(timespec="seconds")
    paths.keepalive_state_path().parent.mkdir(parents=True)
    paths.keepalive_state_path().write_text(
        json.dumps(
            {
                "lanes": {
                    EMAIL: {
                        "auth_failed_at": stamp,
                        "auth_code": 403,
                        "auth_cleared_at": stamp,
                    }
                }
            }
        )
    )

    report = keepalive.run(
        now=NOW,
        runner=_claude_success,
        secret_runner=_secret_success,
    )

    assert report["opened"] == 1
    assert report["results"][0]["status"] == "opened"


def test_capacity_reset_is_observed_only_until_exact_expiry(
    tmp_path, monkeypatch
):
    config = _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})
    assert capacity.append_ledger(
        {
            "ts": NOW.isoformat(timespec="seconds"),
            "email": EMAIL,
            "kind": "keepalive",
        }
    )
    monkeypatch.setattr(capacity, "_probe_live_rows", lambda timeout, now: [])
    monkeypatch.setattr(
        capacity,
        "_lane_secret_availability",
        lambda roster: {EMAIL: True},
    )
    reset_at = (NOW + timedelta(hours=5)).isoformat(timespec="seconds")

    before = capacity.collect(
        force_refresh=True,
        now=NOW + timedelta(hours=4, minutes=59, seconds=59),
        accounts_file=config,
    )
    before_lane = next(row for row in before["accounts"] if row["email"] == EMAIL)
    assert before_lane["five_hour"]["reset_at"] == reset_at
    assert before_lane["five_hour"]["confidence"] == "observed"

    at_expiry = capacity.collect(
        force_refresh=True,
        now=NOW + timedelta(hours=5),
        accounts_file=config,
    )
    expired_lane = next(
        row for row in at_expiry["accounts"] if row["email"] == EMAIL
    )
    assert expired_lane["five_hour"]["reset_at"] is None
    assert expired_lane["five_hour"]["confidence"] == "estimated"
    assert capacity.active_keepalive_window(
        EMAIL,
        now=NOW + timedelta(hours=5),
    ) is None


def test_dry_run_has_no_subprocess_or_filesystem_side_effects(
    tmp_path, monkeypatch
):
    _configure_lanes(tmp_path, monkeypatch, {EMAIL: SECRET})

    report = keepalive.run(
        now=NOW,
        dry_run=True,
        runner=_never_called,
        secret_runner=_never_called,
    )

    assert report == {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "family": "claude",
        "dry_run": True,
        "opened": 0,
        "results": [
            {
                "email": EMAIL,
                "status": "would-open",
                "dry_run": True,
            }
        ],
    }
    assert not paths.keepalive_state_path().exists()
    assert not paths.keepalive_state_path().with_suffix(".json.lock").exists()
    assert not paths.lane_usage_path().exists()
    assert not paths.runs_dir().exists()
    assert not paths.state_dir().exists()


def test_cli_accepts_family_and_dry_run_and_prints_one_summary(
    monkeypatch, capsys
):
    seen = []

    def fake_run(**kwargs):
        seen.append(kwargs)
        return {
            "results": [
                {"email": EMAIL, "status": "would-open", "dry_run": True}
            ]
        }

    monkeypatch.setattr(keepalive, "run", fake_run)

    assert cli.main(["keepalive", "--family", "claude", "--dry-run"]) == 0
    assert seen == [{"family": "claude", "dry_run": True}]
    assert capsys.readouterr().out == f"{EMAIL}: opened (dry-run)\n"


def test_claude_table_shows_last_real_keepalive_pass_only(env_paths):
    from test_claude_pick import lane_snap, lanes_fleet

    snap = lane_snap(lanes_fleet([]))
    snap["generated_at"] = NOW.isoformat(timespec="seconds")
    assert "keepalive:" not in render.table(snap)

    snap["claude"]["keepalive"] = {
        "last_run": {
            "finished_at": NOW.isoformat(timespec="seconds"),
            "opened": 2,
            "dry_run": False,
        }
    }
    clock = NOW.astimezone().strftime("%H:%M")
    assert f"keepalive: last {clock}, 2 opened" in render.table(snap)


def test_provider_requests_are_concurrent_but_capped_at_four(
    tmp_path, monkeypatch
):
    lanes = {f"lane-{index}@example.com": f"secret-{index}" for index in range(8)}
    _configure_lanes(tmp_path, monkeypatch, lanes)
    lock = threading.Lock()
    four_running = threading.Event()
    active = 0
    maximum = 0

    def runner(command, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 4:
                four_running.set()
        assert four_running.wait(timeout=2), "four keepalive workers never overlapped"
        with lock:
            active -= 1
        return _claude_success(command, **kwargs)

    report = keepalive.run(
        now=NOW,
        runner=runner,
        secret_runner=_secret_success,
        max_workers=99,
    )

    assert maximum == 4
    assert report["opened"] == 8
    assert {result["status"] for result in report["results"]} == {"opened"}
    assert len(capacity.read_ledger()) == 8
