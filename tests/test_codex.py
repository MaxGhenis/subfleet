import io
import json
import subprocess
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from subfleet import codex
from subfleet.util import parse_reset_clock

from conftest import make_auth_json


def write_rollout(home: Path, name: str, lines: list[dict], day="2026/07/11"):
    d = home / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("\n".join(json.dumps(x) for x in lines))
    return f


class TestReadAuth:
    def test_parses_account_email_plan(self, tmp_path):
        home = tmp_path / ".codex"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps(make_auth_json("acct-123", "alpha@example.com")))
        auth = codex.read_auth(home)
        assert auth["status"] == "ok"
        assert auth["account_id"] == "acct-123"
        assert auth["email"] == "alpha@example.com"
        assert auth["plan"] == "pro"
        assert auth["_access_token"]

    def test_missing_auth(self, tmp_path):
        assert codex.read_auth(tmp_path / "nope")["status"] == "missing"

    def test_corrupt_auth(self, tmp_path):
        home = tmp_path / ".codex"
        home.mkdir()
        (home / "auth.json").write_text("{not json")
        assert codex.read_auth(home)["status"] == "unreadable"


class TestProbeWham:
    def _auth(self):
        return {"status": "ok", "account_id": "a", "_access_token": "tok"}

    def test_ok_response(self):
        body = json.dumps(
            {
                "email": "usage@example.com",
                "plan_type": "pro",
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": {"used_percent": 3, "limit_window_seconds": 18000, "reset_at": 1783819934},
                    "secondary_window": {"used_percent": 20, "limit_window_seconds": 604800, "reset_at": 1784381766},
                },
                "additional_rate_limits": [
                    {"limit_name": "Spark", "rate_limit": {"limit_reached": False,
                     "primary_window": {"used_percent": 0, "limit_window_seconds": 18000, "reset_at": 1783820536}}}
                ],
            }
        ).encode()
        r = codex.probe_wham(self._auth(), opener=lambda req, t: (200, body))
        assert r["status"] == "ok"
        assert r["primary"]["used_percent"] == 3
        assert r["secondary"]["used_percent"] == 20
        assert r["additional"][0]["name"] == "Spark"

    def test_revoked_token(self):
        def opener(req, t):
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {},
                io.BytesIO(json.dumps({"error": {"code": "token_revoked", "message": "invalidated"}}).encode()),
            )

        r = codex.probe_wham(self._auth(), opener=opener)
        assert r["status"] == "token-revoked"

    def test_network_error(self):
        def opener(req, t):
            raise OSError("no route to host")

        r = codex.probe_wham(self._auth(), opener=opener)
        assert r["status"] == "network-error"

    def test_no_auth(self):
        assert codex.probe_wham({"status": "missing"})["status"] == "no-auth"

    def test_never_serializes_token(self):
        body = json.dumps({"rate_limit": {}}).encode()
        r = codex.probe_wham(self._auth(), opener=lambda req, t: (200, body))
        assert "tok" not in json.dumps(r)


class TestRollouts:
    def test_latest_rate_limits_newest_wins(self, tmp_path):
        home = tmp_path / ".codex"
        write_rollout(home, "rollout-a.jsonl", [
            {"timestamp": "2026-07-11T10:00:00.000Z", "payload": {"type": "token_count", "rate_limits": {
                "plan_type": "pro",
                "primary": {"used_percent": 50, "window_minutes": 300, "resets_at": 1783811345},
                "secondary": {"used_percent": 30, "window_minutes": 10080, "resets_at": 1784366027}}}},
            {"timestamp": "2026-07-11T12:00:00.000Z", "payload": {"type": "token_count", "rate_limits": {
                "plan_type": "pro",
                "primary": {"used_percent": 77, "window_minutes": 300, "resets_at": 1783811345},
                "secondary": {"used_percent": 31, "window_minutes": 10080, "resets_at": 1784366027}}}},
        ])
        obs = codex.latest_rollout_rate_limits(home)
        assert obs["primary"]["used_percent"] == 77
        assert obs["primary"]["window_seconds"] == 18000

    def test_no_rollouts(self, tmp_path):
        home = tmp_path / ".codex"
        home.mkdir()
        assert codex.latest_rollout_rate_limits(home) is None

    def test_limit_errors_extracted_with_reset(self, tmp_path):
        home = tmp_path / ".codex"
        write_rollout(home, "rollout-err.jsonl", [
            {"timestamp": "2026-07-11T02:03:55.000Z", "payload": {"type": "error", "message":
             "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 11:33 PM."}},
            # fixture-noise line without "try again at" must NOT count
            {"timestamp": "2026-07-11T02:05:00.000Z", "payload": {"type": "custom_tool_call_output",
             "output": "Reviewer CLI exited 1: You've hit your usage limit."}},
        ])
        errs = codex.recent_limit_errors(home, hours=24 * 365 * 10)
        assert len(errs["usage_limit"]) == 1
        assert errs["usage_limit"][0]["try_again"] == "11:33 PM"
        assert errs["usage_limit"][0]["reset_at"] is not None

    def test_revoked_refresh_detected(self, tmp_path):
        home = tmp_path / ".codex"
        write_rollout(home, "rollout-rev.jsonl", [
            {"timestamp": "2026-07-11T02:03:55.000Z", "payload": {"type": "error", "message":
             "Your access token could not be refreshed because your refresh token was revoked"}},
        ])
        errs = codex.recent_limit_errors(home, hours=24 * 365 * 10)
        assert len(errs["auth_revoked"]) == 1


class TestScanCache:
    def test_incremental_append_detected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-root"))
        home = tmp_path / ".codex"
        f = write_rollout(home, "rollout-x.jsonl", [
            {"timestamp": "2026-07-11T02:03:55.000Z", "payload": {"type": "error", "message":
             "You've hit your usage limit. Visit x or try again at 11:33 PM."}},
        ])
        first = codex.recent_limit_errors(home, hours=24 * 365 * 10)
        assert len(first["usage_limit"]) == 1
        # Append a new error; size/mtime change should trigger a tail-only parse.
        with open(f, "a") as fh:
            fh.write("\n" + json.dumps(
                {"timestamp": "2026-07-11T03:00:00.000Z", "payload": {"type": "error", "message":
                 "You've hit your usage limit. Visit x or try again at 4:44 PM."}}))
        second = codex.recent_limit_errors(home, hours=24 * 365 * 10)
        assert {e["try_again"] for e in second["usage_limit"]} == {"11:33 PM", "4:44 PM"}
        from subfleet import paths

        assert paths.rollout_cache_path().exists()

    def test_unchanged_file_not_regrepped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-root"))
        home = tmp_path / ".codex"
        write_rollout(home, "rollout-y.jsonl", [
            {"timestamp": "2026-07-11T02:03:55.000Z", "payload": {"type": "error", "message":
             "You've hit your usage limit. Visit x or try again at 11:33 PM."}},
        ])
        codex.scan_rollout_signals(home, max_age_hours=24 * 365 * 10)
        calls = []

        def spy_runner(*args, **kwargs):
            calls.append(args)
            import subprocess

            return subprocess.run(*args, **kwargs)

        out = codex.scan_rollout_signals(home, max_age_hours=24 * 365 * 10, runner=spy_runner)
        assert calls == []  # nothing changed -> no grep
        assert len(out["usage"]) == 1


class TestTokenExpiredSignature:
    def test_only_explicit_expired_401_matches(self):
        assert codex.probe_looks_token_expired(
            {
                "status": "http-401",
                "error": "Provided authentication token is expired. Please sign in again.",
            }
        )
        for probe in (
            {"status": "token-revoked", "error": "token expired"},
            {"status": "http-403", "error": "token expired"},
            {"status": "http-401", "error": "No access"},
            {"status": "http-401"},
            {"status": "ok"},
        ):
            assert not codex.probe_looks_token_expired(probe)


class TestRefreshViaCli:
    @pytest.fixture(autouse=True)
    def _resolved_binary(self, monkeypatch):
        monkeypatch.setattr(codex, "_codex_binary", lambda: "/tools/codex-real")

    @staticmethod
    def result(rc=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], rc, stdout, stderr)

    def test_success_runs_resolved_exec_pinned_to_home(self, tmp_path, monkeypatch):
        calls = {}
        monkeypatch.setattr(codex, "_codex_binary", lambda: "/tools/codex-real")

        def runner(command, **kwargs):
            calls.update(command=command, kwargs=kwargs)
            return self.result(stdout="ok\n")

        output = codex.refresh_via_cli(tmp_path, runner=runner)

        assert output["status"] == "ok"
        assert calls["command"][:2] == ["/tools/codex-real", "exec"]
        assert "--skip-git-repo-check" in calls["command"]
        assert codex.REFRESH_PROBE_MODEL in calls["command"]
        assert calls["kwargs"]["env"]["CODEX_HOME"] == str(tmp_path)
        assert calls["kwargs"]["cwd"] == str(tmp_path)

    def test_revocation_and_other_failures_are_classified(self, tmp_path):
        revoked = codex.refresh_via_cli(
            tmp_path,
            runner=lambda *args, **kwargs: self.result(
                stderr="refresh token was revoked"
            ),
        )
        failed = codex.refresh_via_cli(
            tmp_path,
            runner=lambda *args, **kwargs: self.result(
                rc=3, stderr="stream error\ncontent filter"
            ),
        )

        assert revoked["status"] == "revoked"
        assert failed["status"] == "failed" and failed["rc"] == 3
        assert "content filter" in failed["detail"]

    def test_timeout_is_a_soft_failure(self, tmp_path):
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        assert codex.refresh_via_cli(tmp_path, runner=timeout)["status"] == "failed"

    def test_missing_binary_is_a_soft_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(codex, "_codex_binary", lambda: None)

        def must_not_run(*args, **kwargs):
            raise AssertionError("no command should run without a resolved vendor binary")

        result = codex.refresh_via_cli(tmp_path, runner=must_not_run)

        assert result == {
            "status": "failed",
            "rc": None,
            "detail": "real Codex binary not found",
        }


class TestResetClock:
    def test_same_day(self):
        event = datetime(2026, 7, 11, 14, 0, tzinfo=timezone.utc)
        reset = parse_reset_clock("try again at 11:33 PM", event)
        assert reset is not None
        assert reset > event

    def test_rolls_to_next_day(self):
        event = datetime(2026, 7, 11, 23, 50, tzinfo=timezone.utc).astimezone()
        reset = parse_reset_clock("try again at 1:21 AM", event)
        assert reset.day != event.astimezone().day or reset > event

    def test_named_timezone(self):
        event = datetime(2026, 7, 11, 20, 0, tzinfo=timezone.utc)
        reset = parse_reset_clock("resets 6:40pm (America/New_York)", event)
        assert reset is not None
        assert reset.utcoffset().total_seconds() == -4 * 3600
