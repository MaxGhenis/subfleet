import io
import json
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

from subfleet import codex
from subfleet.util import parse_reset_clock

import pytest

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
        (home / "auth.json").write_text(json.dumps(make_auth_json("acct-123", "a@b.com")))
        auth = codex.read_auth(home)
        assert auth["status"] == "ok"
        assert auth["account_id"] == "acct-123"
        assert auth["email"] == "a@b.com"
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
                "email": "x@y.com",
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
                "rate_limit_reset_credits": {
                    "available_count": 2,
                    "applicable_available_count": 1,
                },
            }
        ).encode()
        r = codex.probe_wham(self._auth(), opener=lambda req, t: (200, body))
        assert r["status"] == "ok"
        assert r["primary"]["used_percent"] == 3
        assert r["secondary"]["used_percent"] == 20
        assert r["additional"][0]["name"] == "Spark"
        assert r["reset_credits"] == {"available": 2, "applicable": 1}

    def test_absent_reset_credits_have_stable_none_shape(self):
        body = json.dumps({"rate_limit": {}}).encode()
        r = codex.probe_wham(self._auth(), opener=lambda req, t: (200, body))
        assert r["reset_credits"] == {"available": None, "applicable": None}

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


class TestResetCreditRequests:
    def _auth(self):
        return {"status": "ok", "account_id": "acct-a", "_access_token": "tok-a"}

    def test_list_uses_known_endpoint_and_usage_probe_headers(self):
        seen = []

        def opener(req, timeout):
            seen.append((req, timeout))
            return 200, json.dumps({
                "credits": [{"id": "credit-1", "status": "available"}],
                "available_count": 1,
            }).encode()

        result = codex.list_reset_credits(self._auth(), timeout=7, opener=opener)
        req, timeout = seen[0]
        headers = {key.casefold(): value for key, value in req.header_items()}
        assert req.full_url == codex.WHAM_RESET_CREDITS_URL
        assert req.get_method() == "GET" and req.data is None and timeout == 7
        assert headers["authorization"] == "Bearer tok-a"
        assert headers["chatgpt-account-id"] == "acct-a"
        assert result["status"] == "ok" and result["credits"][0]["id"] == "credit-1"

    def test_consume_posts_credit_id_with_a_fresh_uuid_per_attempt(self):
        requests = []

        def opener(req, timeout):
            requests.append(req)
            return 200, b'{"code":"reset","windows_reset":2}'

        first = codex.consume_reset_credit(self._auth(), "credit-1", opener=opener)
        second = codex.consume_reset_credit(self._auth(), "credit-1", opener=opener)
        bodies = [json.loads(req.data) for req in requests]

        assert all(req.full_url == codex.WHAM_RESET_CREDITS_CONSUME_URL for req in requests)
        assert all(req.get_method() == "POST" for req in requests)
        assert all(body["credit_id"] == "credit-1" for body in bodies)
        assert bodies[0]["redeem_request_id"] != bodies[1]["redeem_request_id"]
        assert all(str(uuid.UUID(body["redeem_request_id"])) == body["redeem_request_id"] for body in bodies)
        assert first["code"] == second["code"] == "reset"
        assert codex.reset_consume_succeeded(first)

    def test_consume_omits_optional_credit_id_and_rejects_non_success_code(self):
        bodies = []

        def opener(req, timeout):
            bodies.append(json.loads(req.data))
            return 200, b'{"code":"nothing_to_reset","windows_reset":0}'

        result = codex.consume_reset_credit(self._auth(), opener=opener)
        assert set(bodies[0]) == {"redeem_request_id"}
        assert not codex.reset_consume_succeeded(result)


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
        monkeypatch.setenv("SUBFLEET_STATE_DIR", str(tmp_path / "state"))
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
        monkeypatch.setenv("SUBFLEET_STATE_DIR", str(tmp_path / "state"))
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
    def test_expired_401_matches(self):
        assert codex.probe_looks_token_expired({
            "status": "http-401",
            "error": "Provided authentication token is expired. Please try signing in again.",
        })

    def test_other_signatures_do_not(self):
        assert not codex.probe_looks_token_expired(
            {"status": "token-revoked", "error": "invalidated"})
        assert not codex.probe_looks_token_expired(
            {"status": "http-403", "error": "token is expired"})
        assert not codex.probe_looks_token_expired(
            {"status": "http-401", "error": "No access"})
        assert not codex.probe_looks_token_expired({"status": "http-401"})
        assert not codex.probe_looks_token_expired({"status": "ok"})


class TestRefreshViaCli:
    def _result(self, rc=0, stdout="", stderr=""):
        from types import SimpleNamespace

        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    def test_success_runs_exec_pinned_to_home(self, tmp_path):
        calls = {}

        def runner(cmd, **kwargs):
            calls["cmd"], calls["kwargs"] = cmd, kwargs
            return self._result(stdout="ok\n")

        r = codex.refresh_via_cli(tmp_path, runner=runner)
        assert r["status"] == "ok"
        assert calls["cmd"][0].endswith("codex") and calls["cmd"][1] == "exec"
        assert "--skip-git-repo-check" in calls["cmd"]
        assert codex.REFRESH_PROBE_MODEL in calls["cmd"]
        assert calls["kwargs"]["env"]["CODEX_HOME"] == str(tmp_path)
        assert calls["kwargs"]["cwd"] == str(tmp_path)

    def test_revoked_output_is_definitive_even_on_rc0(self, tmp_path):
        def runner(cmd, **kwargs):
            return self._result(rc=0, stderr=(
                "Your access token could not be refreshed because your "
                "refresh token was revoked"))

        assert codex.refresh_via_cli(tmp_path, runner=runner)["status"] == "revoked"

    def test_other_failure_reports_rc_and_tail(self, tmp_path):
        def runner(cmd, **kwargs):
            return self._result(rc=3, stderr="stream error\nblocked by content filter")

        r = codex.refresh_via_cli(tmp_path, runner=runner)
        assert r["status"] == "failed"
        assert r["rc"] == 3
        assert "content filter" in r["detail"]

    def test_timeout_is_failed_not_raised(self, tmp_path):
        import subprocess

        def runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        r = codex.refresh_via_cli(tmp_path, runner=runner)
        assert r["status"] == "failed"
        assert r["rc"] is None


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


class TestWindowClassification:
    def test_weekly_only_response_classifies_correctly(self):
        """wham as of 2026-07-25: primary IS weekly (604800s), no 5h at all."""
        body = json.dumps({
            "email": "x@y.com", "plan_type": "pro",
            "rate_limit": {
                "allowed": True, "limit_reached": False,
                "primary_window": {"used_percent": 70, "limit_window_seconds": 604800,
                                   "reset_at": 1785258158},
                "secondary_window": None,
            },
        }).encode()
        auth = {"status": "ok", "account_id": "a", "_access_token": "tok"}
        r = codex.probe_wham(auth, opener=lambda req, t: (200, body))
        assert r["five_hour"] is None
        assert r["weekly"]["used_percent"] == 70
        assert r["weekly"]["window_seconds"] == 604800

    def test_classic_response_still_maps_both(self):
        five = {"used_percent": 10, "window_seconds": 18000, "reset_at": None}
        week = {"used_percent": 40, "window_seconds": 604800, "reset_at": None}
        f, w = codex.classify_windows(five, week)
        assert f is five and w is week
        # Order-independent: server may reorder positions.
        f2, w2 = codex.classify_windows(week, five)
        assert f2 is five and w2 is week

    def test_no_duration_metadata_classifies_nothing(self):
        f, w = codex.classify_windows({"used_percent": 96, "reset_at": None}, None)
        assert f is None and w is None


class TestProtectedAccountConfig:
    def test_object_form_matches_email_and_id(self, tmp_path, monkeypatch):
        cfg = tmp_path / "codex-accounts.json"
        cfg.write_text(json.dumps({"protected_account": {
            "email": "App@X.com", "account_id": "ID-1"}}))
        monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(cfg))
        p = codex.protected_account()
        assert codex.is_protected_account("app@x.com", None, p)
        assert codex.is_protected_account(None, "id-1", p)
        assert not codex.is_protected_account("other@x.com", "id-2", p)

    def test_list_and_bare_string_forms(self, tmp_path, monkeypatch):
        cfg = tmp_path / "codex-accounts.json"
        cfg.write_text(json.dumps({"protected_account": ["app@x.com", "id-9"]}))
        monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(cfg))
        p = codex.protected_account()
        assert codex.is_protected_account("app@x.com", None, p)
        # Bare strings without @ are treated as account ids.
        assert codex.is_protected_account(None, "ID-9", p)

    def test_missing_or_blank_config_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(tmp_path / "absent.json"))
        assert codex.protected_account() is None
        cfg = tmp_path / "blank.json"
        cfg.write_text(json.dumps({"protected_account": {"email": "  "}}))
        monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(cfg))
        assert codex.protected_account() is None
        assert not codex.is_protected_account("app@x.com", "id-1", None)


class TestCodexBinaryResolution:
    """launchd has no ~/bin on PATH: the watchdog auto-heal died for a week
    with FileNotFoundError('codex'). The binary must resolve without PATH."""

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("SUBFLEET_CODEX_BIN", "/x/custom-codex")
        assert codex._codex_binary() == "/x/custom-codex"

    def test_known_locations_when_path_is_stripped(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SUBFLEET_CODEX_BIN", raising=False)
        import shutil as _sh
        monkeypatch.setattr(_sh, "which", lambda name: None)
        fake_home = tmp_path / "home"
        bun = fake_home / ".bun" / "bin"
        bun.mkdir(parents=True)
        binf = bun / "codex"
        binf.write_text("#!/bin/sh\n")
        binf.chmod(0o755)
        from pathlib import Path as _P
        monkeypatch.setattr(_P, "home", classmethod(lambda cls: fake_home))
        assert codex._codex_binary() == str(binf)

    def test_refresh_uses_resolved_binary(self, monkeypatch):
        monkeypatch.setenv("SUBFLEET_CODEX_BIN", "/x/custom-codex")
        seen = {}

        def fake_runner(cmd, **kw):
            seen["cmd"] = cmd
            class R:
                returncode = 0
                stdout = "ok"
                stderr = ""
            return R()

        r = codex.refresh_via_cli("/tmp/h", runner=fake_runner)
        assert r["status"] == "ok"
        assert seen["cmd"][0] == "/x/custom-codex"


class TestApiKeyLogin:
    """Lanes run on ChatGPT subscriptions only; an API-key home is refused."""

    @staticmethod
    def _home(tmp_path, payload):
        home = tmp_path / "home"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps(payload))
        return home

    def test_openai_api_key_login_is_detected(self, tmp_path):
        home = self._home(tmp_path, {
            "OPENAI_API_KEY": "sk-test", "auth_mode": "apikey", "tokens": None,
        })
        assert codex.api_key_login(home) is True

    @pytest.mark.parametrize("mode", ["apikey", "ApiKey", "api_key", "API-KEY"])
    def test_auth_mode_spellings_without_a_stored_key(self, tmp_path, mode):
        home = self._home(tmp_path, {"OPENAI_API_KEY": None, "auth_mode": mode})
        assert codex.api_key_login(home) is True

    def test_codex_api_key_field_counts(self, tmp_path):
        home = self._home(tmp_path, {"CODEX_API_KEY": "sk-test", "tokens": None})
        assert codex.api_key_login(home) is True

    def test_chatgpt_login_is_not_an_api_login(self, tmp_path):
        home = self._home(tmp_path, make_auth_json("acct-1", "a@b.com"))
        assert codex.api_key_login(home) is False
        assert codex.api_lane_refusal(home) is None

    def test_missing_corrupt_or_non_object_auth_is_not_an_api_login(self, tmp_path):
        assert codex.api_key_login(tmp_path / "nope") is False
        home = tmp_path / "home"
        home.mkdir()
        (home / "auth.json").write_text("{not json")
        assert codex.api_key_login(home) is False
        (home / "auth.json").write_text("[1, 2]")
        assert codex.api_key_login(home) is False

    def test_refusal_names_home_and_override(self, tmp_path, monkeypatch):
        monkeypatch.delenv(codex.API_LANE_OVERRIDE_ENV, raising=False)
        home = self._home(tmp_path, {"OPENAI_API_KEY": "sk-test", "auth_mode": "apikey"})
        message = codex.api_lane_refusal(home)
        assert message is not None
        assert "API-key login" in message
        assert "ChatGPT subscriptions only" in message
        assert f"{codex.API_LANE_OVERRIDE_ENV}=1" in message
        assert str(home) in message or home.name in message

    def test_override_env_allows_a_deliberate_api_dispatch(self, tmp_path, monkeypatch):
        home = self._home(tmp_path, {"OPENAI_API_KEY": "sk-test", "auth_mode": "apikey"})
        monkeypatch.setenv(codex.API_LANE_OVERRIDE_ENV, "1")
        assert codex.api_lane_allowed() is True
        assert codex.api_lane_refusal(home) is None
        monkeypatch.setenv(codex.API_LANE_OVERRIDE_ENV, "yes")
        assert codex.api_lane_allowed() is False
        assert codex.api_lane_refusal(home) is not None
