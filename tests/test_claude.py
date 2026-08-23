import json
import time
from datetime import datetime, timedelta, timezone

from subfleet import claude, config
from subfleet.util import now_local


def write_transcript(projects_dir, name, events):
    proj = projects_dir / "-home-example-user"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{name}.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in events))
    return f


def limit_event(ts, text, session="s1", status=None):
    e = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "timestamp": ts,
        "sessionId": session,
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if status:
        e["apiErrorStatus"] = status
    return e


class TestIdentity:
    def test_reads_oauth_account(self, env_paths):
        ident = claude.identity()
        assert ident["email"] == "active@example.com"

    def test_known_accounts_deduped(self, env_paths):
        config.save({
            "accounts": ["charlie@example.com", "alpha@example.com", "alpha@example.com"],
            "enrolled": {},
            "codex_homes": [],
        })
        assert claude.known_accounts() == ["alpha@example.com", "charlie@example.com"]

    def test_roster_config_is_authoritative(self, env_paths):
        config.save({
            "accounts": ["usage@example.com", "alpha@example.com"],
            "enrolled": {},
            "codex_homes": [],
        })
        (env_paths["claude_dir"] / "cc-mirror-accounts.json").write_text(
            json.dumps({"u1": "alpha@example.com", "u3": "charlie@example.com"})
        )
        assert claude.known_accounts() == ["alpha@example.com", "usage@example.com"]


class TestTranscriptScan:
    def test_session_limit_parsed_with_reset(self, env_paths):
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        write_transcript(
            env_paths["claude_dir"] / "projects",
            "sess-a",
            [
                {"type": "user", "timestamp": ts, "message": {"content": "hi"}},
                limit_event(ts, "You've hit your session limit · resets 6:40pm (America/New_York)"),
            ],
        )
        events = claude.transcript_limit_events(hours=24)
        assert len(events) == 1
        assert events[0]["kind"] == "session-limit"
        assert events[0]["reset_at"] is not None

    def test_repeats_collapse_with_count(self, env_paths):
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        text = "You've hit your session limit · resets 6:40pm (America/New_York)"
        write_transcript(
            env_paths["claude_dir"] / "projects",
            "sess-b",
            [limit_event(ts, text, session="s1"), limit_event(ts, text, session="s2")],
        )
        events = claude.transcript_limit_events(hours=24)
        assert len(events) == 1
        assert events[0]["count"] == 2
        assert events[0]["sessions"] == 2

    def test_old_files_skipped(self, env_paths):
        ts = "2026-01-01T00:00:00.000Z"
        f = write_transcript(
            env_paths["claude_dir"] / "projects",
            "sess-old",
            [limit_event(ts, "You've hit your session limit · resets 1:00pm (America/New_York)")],
        )
        old = time.time() - 100 * 3600
        import os

        os.utime(f, (old, old))
        assert claude.transcript_limit_events(hours=24) == []

    def test_429_without_text_is_rate_limit(self, env_paths):
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        write_transcript(
            env_paths["claude_dir"] / "projects",
            "sess-c",
            [limit_event(ts, "Rate limit exceeded, please slow down", status=429)],
        )
        events = claude.transcript_limit_events(hours=24)
        assert len(events) == 1
        assert events[0]["kind"] == "rate-limit"


class TestAccountsReport:
    def _roster(self, enrolled):
        config.save({
            "accounts": ["alpha@example.com", "charlie@example.com", "echo@example.com"],
            "enrolled": enrolled,
            "codex_homes": [],
        })

    def test_enrolled_setup_token_is_not_quota_probed(self, env_paths):
        self._roster({"charlie@example.com": "claude-quota-charlie@example.com"})

        def fake_secret(cmd, **kw):
            class R:
                returncode = 0
                stdout = "tok-c\n"
                stderr = ""
            return R()

        def fake_opener(req, t):
            raise AssertionError("stored setup tokens must not be sent to the usage endpoint")

        rows = claude.accounts_report("alpha@example.com", opener=fake_opener, secret_runner=fake_secret)
        by = {r["email"]: r for r in rows}
        assert by["alpha@example.com"]["active"] and not by["alpha@example.com"]["enrolled"]
        assert by["charlie@example.com"]["enrolled"]
        assert by["charlie@example.com"]["probe"]["status"] == "inference-only"
        assert by["echo@example.com"]["enrolled"] is False and "probe" not in by["echo@example.com"]
        assert rows[0]["email"] == "alpha@example.com"  # active sorts first

    def test_missing_secret_flagged_not_fabricated(self, env_paths):
        self._roster({"charlie@example.com": "claude-quota-charlie@example.com"})

        def no_secret(cmd, **kw):
            class R:
                returncode = 1
                stdout = ""
                stderr = "not found"
            return R()

        rows = claude.accounts_report(None, secret_runner=no_secret)
        row = next(r for r in rows if r["email"] == "charlie@example.com")
        assert row["probe"]["status"] == "secret-missing"


class TestOAuthUsageExtraction:
    def test_all_window_keys_extracted(self):
        body = json.dumps({
            "five_hour": {"utilization": 54, "resets_at": "2026-07-12T02:10:00Z"},
            "seven_day": {"utilization": 31, "resets_at": "2026-07-18T06:00:00Z"},
            "seven_day_opus": {"utilization": 12},
            "seven_day_sonnet": {"used_percentage": 56},
            "extra_field": {"utilization": 99},
        }).encode()
        r = claude.probe_oauth_usage("tok", opener=lambda req, t: (200, body))
        assert r["status"] == "ok"
        assert r["five_hour"]["used_percent"] == 54
        assert r["seven_day"]["used_percent"] == 31
        assert r["windows"]["seven_day_opus"]["used_percent"] == 12
        assert r["windows"]["seven_day_sonnet"]["used_percent"] == 56
        assert "extra_field" not in r["windows"]

    def test_429_maps_to_rate_limited(self):
        import io
        import urllib.error

        def opener(req, t):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {},
                                         io.BytesIO(b'{"error":{"type":"rate_limit_error"}}'))

        assert claude.probe_oauth_usage("tok", opener=opener)["status"] == "rate-limited"


class TestActiveLimit:
    def test_future_reset_is_active(self):
        future = (now_local() + timedelta(hours=1)).isoformat(timespec="seconds")
        events = [{"kind": "session-limit", "reset_at": future, "observed_at": "x"}]
        assert claude.active_limit(events) is not None

    def test_past_reset_is_not_active(self):
        past = (now_local() - timedelta(hours=1)).isoformat(timespec="seconds")
        events = [{"kind": "session-limit", "reset_at": past, "observed_at": "x"}]
        assert claude.active_limit(events) is None
