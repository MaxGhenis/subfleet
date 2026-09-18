import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest

from subfleet import capacity, claude, cli, codex, paths, run_ledger, snapshot


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
REQUIRED_ROW_KEYS = {
    "family", "id", "email", "five_hour", "weekly", "learned_capacity",
    "limited_until", "confidence", "status", "dispatchable", "headroom_score",
    "scoped_limits", "in_flight",
}
REQUIRED_WINDOW_KEYS = {"used_percent", "tokens", "capacity", "reset_at"}
REQUIRED_WINDOW_KEYS.add("confidence")


@pytest.fixture(autouse=True)
def isolated_capacity_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("DELEGATE_STATE_DIR", str(tmp_path / "delegate-state"))
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(tmp_path / "claude"))
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"accounts": [], "enrolled": {}}))
    monkeypatch.setenv("SUBFLEET_CLAUDE_ACCOUNTS", str(roster))
    monkeypatch.setattr(
        capacity,
        "_lane_secret_availability",
        lambda config: {email: True for email in capacity._enrolled_map(config)},
    )


def usage_record(ts, email="lane@x.com", total=10):
    return {
        "ts": ts.isoformat(),
        "email": email,
        "session_id": f"s-{total}",
        "input_tokens": total,
        "output_tokens": 0,
        "total_tokens": total,
    }


def live_claude_row(email="lane@x.com", five=20, weekly=30):
    return {
        "family": "claude",
        "id": email,
        "email": email,
        "five_hour": capacity._window(used_percent=five, confidence="live"),
        "weekly": capacity._window(used_percent=weekly, confidence="live"),
        "scoped_limits": [],
        "learned_capacity": None,
        "limited_until": None,
        "confidence": "live",
        "status": "ok",
        "dispatchable": False,
        "headroom_score": 100 - max(five, weekly),
        "active": True,
        "enrolled": True,
        "probe_status": "ok",
        "in_flight": 0,
    }


def patch_lane_roster(monkeypatch, *emails):
    monkeypatch.setattr(claude, "known_accounts", lambda: list(emails))
    monkeypatch.setattr(
        claude,
        "roster_config",
        lambda: {"accounts": list(emails), "enrolled": {email: f"secret-{email}" for email in emails}},
    )


class TestTranscriptLedger:
    def test_last_message_occurrence_wins(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 2}}}),
                    "{broken",
                    json.dumps({"message": {"id": "m2", "usage": {
                        "input_tokens": 5, "cache_creation_input_tokens": 7,
                        "cache_read_input_tokens": 3, "output_tokens": 1,
                    }}}),
                    json.dumps({"message": {"id": "m1", "usage": {"input_tokens": 20, "output_tokens": 3}}}),
                ]
            )
        )
        assert capacity.parse_transcript_usage(transcript) == {
            "input_tokens": 35,
            "output_tokens": 4,
            "total_tokens": 39,
        }

    def test_record_run_then_hard_limit_calibration(self, tmp_path):
        ledger = tmp_path / "usage.jsonl"
        capacity.append_ledger(usage_record(NOW - timedelta(hours=1), total=5), ledger)
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            json.dumps({"message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}}})
        )

        assert capacity.record_lane_run(
            "lane@x.com",
            "session-1",
            4,
            transcript,
            error="You've hit your session limit; resets 3:00pm (America/New_York)",
            now=NOW,
            ledger_path=ledger,
        ) is None

        records = capacity.read_ledger(ledger)
        assert records[-2]["total_tokens"] == 15
        assert records[-1]["event"] == "hard_limit"
        assert records[-1]["window_tokens_5h"] == 20
        assert records[-1]["window_tokens_7d"] == 20
        assert records[-1]["reset"] is not None

    def test_parse_failure_is_logged_and_never_raises(self, tmp_path):
        ledger = tmp_path / "usage.jsonl"
        assert capacity.record_lane_run(
            "lane@x.com", "missing", 0, tmp_path / "missing.jsonl",
            now=NOW, ledger_path=ledger,
        ) is None
        assert set(capacity.read_ledger(ledger)[0]) == {"ts", "email", "error"}

    def test_hard_limit_without_reset_gets_conservative_cooldown(self, tmp_path):
        ledger = tmp_path / "usage.jsonl"
        capacity.record_lane_run(
            "lane@x.com", "missing", 4, tmp_path / "missing.jsonl",
            now=NOW, ledger_path=ledger,
        )
        records = capacity.read_ledger(ledger)
        assert records[-1]["event"] == "hard_limit"
        assert records[-1]["window_tokens_5h"] == 0
        assert records[-1]["reset"] == (NOW + timedelta(hours=1)).isoformat()
        cooldowns = json.loads(paths.delegate_cooldowns_path().read_text())
        assert cooldowns["lane@x.com"] == {"*": records[-1]["reset"]}

    def test_model_limit_tags_event_and_only_cools_that_model(self, tmp_path):
        ledger = tmp_path / "usage.jsonl"
        reset = NOW + timedelta(days=2)

        capacity.record_lane_run(
            "lane@x.com",
            "missing",
            4,
            tmp_path / "missing.jsonl",
            model="claude-fable-5-1",
            reset=reset,
            now=NOW,
            ledger_path=ledger,
        )

        hard_limit = capacity.read_ledger(ledger)[-1]
        assert hard_limit["event"] == "hard_limit"
        assert hard_limit["model"] == "claude-fable-5-1"
        assert json.loads(paths.delegate_cooldowns_path().read_text()) == {
            "lane@x.com": {"claude-fable-5-1": reset.isoformat()},
        }

    def test_auth_failure_updates_existing_cooldown_state(self, tmp_path):
        ledger = tmp_path / "usage.jsonl"
        capacity.record_lane_run(
            "lane@x.com", "missing", 5, tmp_path / "missing.jsonl",
            model="claude-fable-5-1",
            now=NOW, ledger_path=ledger,
        )
        cooldowns = json.loads(paths.delegate_cooldowns_path().read_text())
        assert cooldowns["lane@x.com"] == {
            "*": (NOW + timedelta(days=30)).isoformat(),
        }

    def test_model_cooldowns_round_trip_and_never_shorten(self):
        fable_limit = NOW + timedelta(days=30)
        opus_limit = NOW + timedelta(hours=3)
        capacity.store_lane_cooldown("same@x.com", fable_limit, model="fable")
        capacity.store_lane_cooldown(
            "same@x.com", NOW + timedelta(hours=1), model="claude-fable-5-1"
        )
        capacity.store_lane_cooldown("same@x.com", opus_limit, model="opus")

        emails = [f"lane-{index}@x.com" for index in range(12)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(
                lambda email: capacity.store_lane_cooldown(email, NOW + timedelta(hours=2)),
                emails,
            ))

        cooldowns = json.loads(paths.delegate_cooldowns_path().read_text())
        assert cooldowns["same@x.com"] == {
            "claude-fable-5-1": fable_limit.isoformat(),
            "claude-opus-5": opus_limit.isoformat(),
        }
        assert set(emails) <= set(cooldowns)
        assert all(cooldowns[email] == {"*": (NOW + timedelta(hours=2)).isoformat()}
                   for email in emails)
        assert capacity.read_lane_cooldowns() == cooldowns

    def test_legacy_flat_cooldown_reads_as_account_wide(self):
        reset = NOW + timedelta(hours=4)
        path = paths.delegate_cooldowns_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"legacy@x.com": reset.isoformat()}))

        assert capacity.read_lane_cooldowns() == {
            "legacy@x.com": {"*": reset.isoformat()},
        }

    def test_forgiving_jsonl_and_inclusive_window_boundaries(self, tmp_path):
        ledger = tmp_path / "usage.jsonl"
        records = [
            usage_record(NOW - timedelta(hours=5), total=10),
            usage_record(NOW - timedelta(hours=5, seconds=1), total=20),
            usage_record(NOW - timedelta(days=7), total=30),
            usage_record(NOW - timedelta(days=7, seconds=1), total=40),
            usage_record(NOW + timedelta(seconds=1), total=50),
            usage_record(NOW, email="other@x.com", total=60),
            {"ts": NOW.isoformat(), "email": "lane@x.com", "event": "hard_limit",
             "window_tokens_5h": 999, "window_tokens_7d": 999},
        ]
        ledger.write_text("{bad\n" + "\n".join(json.dumps(row) for row in records) + "\n")
        assert len(capacity.read_ledger(ledger)) == len(records)
        assert capacity.rolling_token_sums("lane@x.com", now=NOW, path=ledger) == {
            "five_hour": 10,
            "weekly": 60,
        }

    def test_non_finite_and_overflow_token_values_are_ignored(self, tmp_path):
        ledger = tmp_path / "usage.jsonl"
        records = [
            usage_record(NOW, total=float("nan")),
            usage_record(NOW, total=float("inf")),
            usage_record(NOW, total=10 ** 400),
            usage_record(NOW, total=7),
        ]
        oversized_json_integer = (
            '{"ts":"' + NOW.isoformat() + '","email":"lane@x.com","total_tokens":'
            + "9" * 5000 + "}"
        )
        ledger.write_text(
            "\n".join([*(json.dumps(row) for row in records), oversized_json_integer]) + "\n"
        )
        assert capacity.rolling_token_sums("lane@x.com", now=NOW, path=ledger) == {
            "five_hour": 7,
            "weekly": 7,
        }

    def test_calibration_learns_each_window_independently(self):
        records = [
            {"email": "lane@x.com", "event": "hard_limit", "window_tokens_5h": 100,
             "window_tokens_7d": 500},
            {"email": "lane@x.com", "event": "hard_limit", "window_tokens_5h": 120,
             "window_tokens_7d": 450},
            {"email": "other@x.com", "event": "hard_limit", "window_tokens_5h": 999,
             "window_tokens_7d": 999},
        ]
        assert capacity.learned_capacities("lane@x.com", records=records) == {
            "five_hour": 120,
            "weekly": 500,
        }


class TestCollection:
    def test_snapshot_uses_ledger_not_inference_token_usage_probes(
            self, env_paths, monkeypatch):
        patch_lane_roster(monkeypatch, "lane@x.com")
        monkeypatch.setattr(claude, "identity", lambda: {"email": "lane@x.com"})
        monkeypatch.setattr(
            claude, "keychain_credentials",
            lambda: {"status": "ok", "_token": "desktop-app-token"},
        )
        monkeypatch.setattr(claude, "statusline_state", lambda: None)
        monkeypatch.setattr(claude, "transcript_limit_events", lambda hours=24: [])
        monkeypatch.setattr(
            claude, "accounts_report",
            lambda *args, **kwargs: pytest.fail("inference setup-token was usage-probed"),
        )
        capacity.append_ledger(usage_record(datetime.now().astimezone(), total=12))
        seen = []

        def active_probe(token, timeout=15):
            seen.append(token)
            return {"status": "http-403"}

        data = snapshot.build(live=True, claude_probe_fn=active_probe)

        assert seen == ["desktop-app-token"]
        assert data["claude"]["oauth_probe"]["status"] == "http-403"
        assert data["claude"]["lanes"]["dispatchable_now"] == 1
        assert data["claude"]["lanes"]["best"] == "lane@x.com"
        lane = data["claude"]["lanes"]["lanes"][0]
        assert lane["verdict"] == "ok"
        assert lane["five_hour_tokens"] == 12
        assert lane["confidence"] == "estimated"
        assert data["claude"]["accounts"][0]["probe"]["status"] == "ok"

    def test_live_sources_only_and_active_lane_is_deduplicated(
        self, tmp_path, monkeypatch, codex_home_factory
    ):
        make_home, _ = codex_home_factory
        home = make_home("codex-one", "acct-1", "codex@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_HOMES", str(home))
        monkeypatch.setattr(
            codex,
            "probe_all",
            lambda auths, timeout=15: [{
                "status": "ok", "checked_at": NOW.isoformat(), "email": "codex@x.com",
                "allowed": True, "limit_reached": False,
                "reset_credits": {"available": 2, "applicable": 1},
                "primary": {"used_percent": 10, "reset_at": None},
                "secondary": {"used_percent": 20, "reset_at": None},
            }],
        )
        monkeypatch.setattr(claude, "identity", lambda: {"email": "lane@x.com"})
        monkeypatch.setattr(claude, "keychain_credentials", lambda: {"status": "ok", "_token": "secret"})
        monkeypatch.setattr(
            claude,
            "probe_oauth_usage",
            lambda token, timeout=15: {
                "status": "ok", "checked_at": NOW.isoformat(),
                "five_hour": {"used_percent": 25, "reset_at": None},
                "seven_day": {"used_percent": 35, "reset_at": None},
                "limits": [{
                    "kind": "weekly_scoped", "group": "weekly", "percent": 100,
                    "severity": "critical", "resets_at": "2026-07-28T04:59:59Z",
                    "is_active": True, "scope_model": "Fable", "scope_surface": None,
                }],
                "raw": {"must": "not cache"},
            },
        )
        monkeypatch.setattr(
            claude,
            "accounts_report",
            lambda *args, **kwargs: pytest.fail("setup-token usage probe called"),
        )
        patch_lane_roster(monkeypatch, "lane@x.com")

        data = capacity.collect(force_refresh=True, now=NOW)
        assert set(data) == {"generated_at", "cache", "accounts", "families"}
        claude_rows = [row for row in data["accounts"] if row["family"] == "claude"]
        assert len(claude_rows) == 1
        assert claude_rows[0]["active"] is True
        assert claude_rows[0]["dispatchable"] is True
        assert claude_rows[0]["scoped_limits"][0]["scope_model"] == "Fable"
        assert len(data["accounts"]) == 2
        codex_row = next(row for row in data["accounts"] if row["family"] == "codex")
        assert codex_row["reset_credits"] == {"available": 2, "applicable": 1}
        for row in data["accounts"]:
            assert REQUIRED_ROW_KEYS <= set(row)
            assert set(row["five_hour"]) == REQUIRED_WINDOW_KEYS
            assert set(row["weekly"]) == REQUIRED_WINDOW_KEYS

    def test_duplicate_codex_homes_collapse_to_one_account(
            self, monkeypatch, codex_home_factory):
        make_home, _ = codex_home_factory
        first = make_home("codex-one", "same-account", "codex@x.com")
        second = make_home("codex-two", "same-account", "codex@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_HOMES", f"{first}:{second}")
        monkeypatch.setattr(claude, "identity", lambda: {})

        def probes(auths, timeout=15):
            assert len(auths) == 1
            return [{
                "status": "ok", "allowed": True, "limit_reached": False,
                "primary": {"used_percent": 10, "reset_at": None},
                "secondary": {"used_percent": 20, "reset_at": None},
            }]

        monkeypatch.setattr(codex, "probe_all", probes)
        data = capacity.collect(force_refresh=True, now=NOW)
        rows = [row for row in data["accounts"] if row["family"] == "codex"]
        assert len(rows) == 1
        assert rows[0]["homes"] == [str(first), str(second)]

    def test_codex_dispatch_score_follows_weekly_reset_not_protection(
            self, tmp_path, monkeypatch, codex_home_factory):
        # The app account is bound to the SECOND home after a re-login
        # shuffle: it carries the handicap, the primary directory does not.
        make_home, _ = codex_home_factory
        first = make_home("codex-one", "acct-1", "lane@x.com")
        second = make_home("codex-two", "acct-2", "app@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_HOMES", f"{first}:{second}")
        cfg = tmp_path / "codex-protected.json"
        cfg.write_text(json.dumps({"protected_account": {"email": "app@x.com"}}))
        monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(cfg))
        monkeypatch.setattr(claude, "identity", lambda: {})
        monkeypatch.setattr(codex, "probe_all", lambda auths, timeout=15: [
            {"status": "ok", "allowed": True, "limit_reached": False,
             "primary": {"used_percent": used, "window_seconds": 18000,
                         "reset_at": (NOW + timedelta(hours=2)).isoformat()},
             "secondary": {"used_percent": weekly, "window_seconds": 604800,
                           "reset_at": reset.isoformat()}}
            for used, weekly, reset in (
                (70, 80, NOW + timedelta(days=1)),
                (5, 10, NOW + timedelta(days=3)),
            )
        ])
        data = capacity.collect(force_refresh=True, now=NOW)
        rows = {row["email"]: row for row in data["accounts"] if row["family"] == "codex"}
        app, lane = rows["app@x.com"], rows["lane@x.com"]
        assert app["is_protected_account"] is True and app["is_primary_home"] is False
        assert app["dispatch_score"] < lane["dispatch_score"]
        assert lane["is_protected_account"] is False and lane["is_primary_home"] is True
        assert data["families"]["codex"]["best"] == str(first)

    @pytest.mark.parametrize(
        ("probe_status", "expected_status", "all_limited"),
        [("network-error", "network-error", False), ("http-401", "http-401", False)],
    )
    def test_unknown_codex_probe_is_not_exhaustion(
            self, monkeypatch, codex_home_factory, probe_status, expected_status, all_limited):
        make_home, _ = codex_home_factory
        home = make_home("codex-one", "acct-1", "codex@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_HOMES", str(home))
        monkeypatch.setattr(claude, "identity", lambda: {})
        monkeypatch.setattr(codex, "probe_all", lambda auths, timeout=15: [
            {"status": probe_status}
        ])
        data = capacity.collect(force_refresh=True, now=NOW)
        row = next(row for row in data["accounts"] if row["family"] == "codex")
        assert row["status"] == expected_status and row["dispatchable"] is False
        assert data["families"]["codex"]["state"] == "unknown"
        assert data["families"]["codex"]["all_limited"] is all_limited
        assert data["families"]["codex"]["headroom_score"] is None

    def test_codex_below_five_percent_headroom_is_exhausted(
            self, monkeypatch, codex_home_factory):
        make_home, _ = codex_home_factory
        home = make_home("codex-one", "acct-1", "codex@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_HOMES", str(home))
        monkeypatch.setattr(claude, "identity", lambda: {})
        monkeypatch.setattr(codex, "probe_all", lambda auths, timeout=15: [{
            "status": "ok", "allowed": True, "limit_reached": False,
            "primary": {"used_percent": 96, "reset_at": None},
            "secondary": {"used_percent": 10, "reset_at": None},
        }])
        data = capacity.collect(force_refresh=True, now=NOW)
        row = next(row for row in data["accounts"] if row["family"] == "codex")
        assert row["status"] == "exhausted"
        assert row["headroom_score"] == 0
        assert data["families"]["codex"]["all_limited"] is True

    def test_exhausted_weekly_window_governs_codex_reset(
            self, monkeypatch, codex_home_factory):
        make_home, _ = codex_home_factory
        home = make_home("codex-one", "acct-1", "codex@x.com")
        five_reset = (NOW + timedelta(hours=1)).isoformat()
        weekly_reset = (NOW + timedelta(days=2)).isoformat()
        monkeypatch.setenv("SUBFLEET_CODEX_HOMES", str(home))
        monkeypatch.setattr(claude, "identity", lambda: {})
        monkeypatch.setattr(codex, "probe_all", lambda auths, timeout=15: [{
            "status": "ok", "allowed": True, "limit_reached": False,
            "primary": {"used_percent": 10, "reset_at": five_reset},
            "secondary": {"used_percent": 96, "reset_at": weekly_reset},
        }])

        data = capacity.collect(force_refresh=True, now=NOW)
        row = next(row for row in data["accounts"] if row["family"] == "codex")
        assert row["status"] == "exhausted"
        assert row["limited_until"] == weekly_reset
        assert data["families"]["codex"]["earliest_reset"] == weekly_reset

    @pytest.mark.parametrize(
        ("probe_status", "expected_status"),
        [("token-invalid", "ok"), ("http-403", "ok"), ("rate-limited", "limited")],
    )
    def test_active_claude_probe_errors_are_tolerated_for_enrolled_lane(
            self, monkeypatch, probe_status, expected_status):
        monkeypatch.setattr(paths, "codex_homes", lambda: [])
        patch_lane_roster(monkeypatch, "lane@x.com")
        monkeypatch.setattr(claude, "identity", lambda: {"email": "lane@x.com"})
        monkeypatch.setattr(
            claude, "keychain_credentials", lambda: {"status": "ok", "_token": "token"}
        )
        monkeypatch.setattr(
            claude, "probe_oauth_usage",
            lambda token, timeout=15: {"status": probe_status},
        )
        data = capacity.collect(force_refresh=True, now=NOW)
        row = next(row for row in data["accounts"] if row["email"] == "lane@x.com")
        assert row["probe_status"] == probe_status
        assert row["status"] == expected_status

    def test_non_enrolled_active_429_remains_a_live_visible_limit(self, monkeypatch):
        monkeypatch.setattr(paths, "codex_homes", lambda: [])
        monkeypatch.setattr(claude, "identity", lambda: {"email": "desktop@x.com"})
        monkeypatch.setattr(
            claude, "keychain_credentials", lambda: {"status": "ok", "_token": "token"}
        )
        monkeypatch.setattr(
            claude,
            "probe_oauth_usage",
            lambda token, timeout=15: {"status": "rate-limited"},
        )

        data = capacity.collect(force_refresh=True, now=NOW)
        row = next(row for row in data["accounts"] if row["email"] == "desktop@x.com")

        assert row["enrolled"] is False and row["dispatchable"] is False
        assert row["status"] == "rate-limited"
        assert row["headroom_score"] == 0
        assert row["confidence"] == "live"
        table = capacity.human_table(data)
        assert "desktop@x.com" in table and "rate-limited" in table and "live" in table

    def test_anonymous_active_429_remains_visible(self):
        active = capacity.active_claude_capacity_row(
            {"account_uuid": "desktop-uuid"}, {"status": "rate-limited"}, now=NOW
        )
        rows = capacity._claude_rows(
            [active], NOW, [], config={"accounts": [], "enrolled": {}},
            secret_availability={},
        )
        assert rows[0]["id"] == "desktop-uuid"
        assert rows[0]["status"] == "rate-limited"
        assert rows[0]["confidence"] == "live"

    def test_live_cache_ttl_but_ledger_is_merged_every_read(self, monkeypatch):
        calls = []
        secret_calls = []
        monkeypatch.setattr(
            capacity,
            "_probe_live_rows",
            lambda timeout, now: calls.append(now) or [],
        )
        monkeypatch.setattr(
            capacity,
            "_lane_secret_availability",
            lambda config: secret_calls.append(config) or {"lane@x.com": True},
        )
        patch_lane_roster(monkeypatch, "lane@x.com")

        first = capacity.collect(now=NOW)
        assert first["cache"]["hit"] is False
        capacity.append_ledger(usage_record(NOW, total=17))
        second = capacity.collect(now=NOW + timedelta(seconds=119))
        assert second["cache"]["hit"] is True
        lane = next(row for row in second["accounts"] if row["email"] == "lane@x.com")
        assert lane["five_hour"]["tokens"] == 17
        assert len(calls) == 1
        assert len(secret_calls) == 1

        third = capacity.collect(now=NOW + timedelta(seconds=120))
        assert third["cache"]["hit"] is False
        assert len(calls) == 2
        assert len(secret_calls) == 2

    def test_cache_is_sanitized(self, monkeypatch):
        row = live_claude_row()
        row.update({"_access_token": "top-secret", "raw": {"token": "also-secret"}})
        monkeypatch.setattr(capacity, "_probe_live_rows", lambda timeout, now: [row])
        patch_lane_roster(monkeypatch, "lane@x.com")
        capacity.collect(force_refresh=True, now=NOW)
        cached = paths.capacity_cache_path().read_text()
        assert "top-secret" not in cached
        assert "also-secret" not in cached

    def test_malformed_enrollment_entries_never_become_lanes(self, monkeypatch):
        monkeypatch.setattr(capacity, "_probe_live_rows", lambda timeout, now: [])
        config = {
            "accounts": ["valid@x.com", "null@x.com", "empty@x.com", "not-an-email"],
            "enrolled": {
                "valid@x.com": "secret-valid",
                "null@x.com": None,
                "empty@x.com": "  ",
                "not-an-email": "secret-invalid",
            },
        }
        monkeypatch.setattr(claude, "roster_config", lambda: config)
        monkeypatch.setattr(claude, "known_accounts", lambda: config["accounts"])

        data = capacity.collect(force_refresh=True, now=NOW)
        rows = {row["email"]: row for row in data["accounts"]}
        assert rows["valid@x.com"]["dispatchable"] is True
        assert rows["null@x.com"]["status"] == "not-enrolled"
        assert rows["empty@x.com"]["status"] == "not-enrolled"
        assert "not-an-email" not in rows
        assert data["families"]["claude"]["best"] == "valid@x.com"

    def test_missing_keychain_secret_is_not_dispatchable(self, monkeypatch):
        monkeypatch.setattr(capacity, "_probe_live_rows", lambda timeout, now: [])
        patch_lane_roster(monkeypatch, "missing@x.com")
        monkeypatch.setattr(
            capacity,
            "_lane_secret_availability",
            lambda config: {"missing@x.com": False},
        )

        data = capacity.collect(force_refresh=True, now=NOW)
        lane = next(row for row in data["accounts"] if row["email"] == "missing@x.com")

        assert lane["enrolled"] is True
        assert lane["secret_available"] is False
        assert lane["status"] == "secret-missing"
        assert lane["dispatchable"] is False
        assert data["families"]["claude"]["available"] is False
        assert data["families"]["claude"]["state"] == "unknown"


class TestFamilyScoring:
    def _no_live(self, monkeypatch, *emails):
        monkeypatch.setattr(capacity, "_probe_live_rows", lambda timeout, now: [])
        patch_lane_roster(monkeypatch, *emails)

    def test_calibrated_worst_window_sets_headroom(self, monkeypatch):
        self._no_live(monkeypatch, "lane@x.com")
        capacity.append_ledger(usage_record(NOW, total=60))
        capacity.append_ledger(usage_record(NOW - timedelta(hours=6), total=20))
        capacity.append_ledger({
            "ts": (NOW - timedelta(days=1)).isoformat(),
            "email": "lane@x.com",
            "event": "hard_limit",
            "window_tokens_5h": 100,
            "window_tokens_7d": 100,
            "reset": (NOW - timedelta(hours=1)).isoformat(),
        })

        data = capacity.collect(now=NOW)
        row = data["accounts"][0]
        assert row["five_hour"]["used_percent"] == 60
        assert row["weekly"]["used_percent"] == 80
        assert row["headroom_score"] == 20
        assert row["confidence"] == "observed"
        assert row["learned_capacity"] == {"five_hour": 100, "weekly": 100}
        assert data["families"]["claude"]["headroom_score"] == 20

    def test_uncalibrated_lane_is_available_with_null_score(self, monkeypatch):
        self._no_live(monkeypatch, "lane@x.com")
        capacity.append_ledger(usage_record(NOW, total=12))
        data = capacity.collect(now=NOW)
        row = data["accounts"][0]
        assert row["dispatchable"] is True
        assert row["headroom_score"] is None
        assert data["families"]["claude"]["available"] is True
        assert data["families"]["claude"]["headroom_score"] is None
        assert data["families"]["claude"]["best"] == "lane@x.com"

    def test_empty_families_have_unknown_not_zero_headroom(self):
        summaries = capacity.family_summaries([], now=NOW)
        assert summaries["codex"]["state"] == "empty"
        assert summaries["codex"]["headroom_score"] is None
        assert summaries["claude"]["state"] == "empty"
        assert summaries["claude"]["headroom_score"] is None

    def test_limited_lane_scores_zero_and_reports_earliest_reset(self, monkeypatch):
        self._no_live(monkeypatch, "lane@x.com")
        reset = NOW + timedelta(hours=2)
        paths.delegate_cooldowns_path().parent.mkdir(parents=True)
        paths.delegate_cooldowns_path().write_text(json.dumps({"lane@x.com": reset.isoformat()}))

        data = capacity.collect(now=NOW)
        row = data["accounts"][0]
        assert row["limited_until"] == reset.isoformat()
        assert row["dispatchable"] is False
        assert row["headroom_score"] == 0
        assert data["families"]["claude"]["available"] is False
        assert data["families"]["claude"]["headroom_score"] == 0
        assert data["families"]["claude"]["earliest_reset"] == reset.isoformat()

    def test_recent_hard_limit_without_reset_still_gates_lane(self, monkeypatch):
        self._no_live(monkeypatch, "lane@x.com")
        capacity.append_ledger({
            "ts": NOW.isoformat(), "email": "lane@x.com", "event": "hard_limit",
            "window_tokens_5h": 0, "window_tokens_7d": 0, "reset": None,
        })
        data = capacity.collect(now=NOW)
        row = data["accounts"][0]
        assert row["dispatchable"] is False and row["headroom_score"] == 0
        assert row["limited_until"] == (NOW + timedelta(hours=1)).isoformat()

    def test_only_legacy_hard_limit_calibrates_and_gates_globally(self, monkeypatch):
        self._no_live(monkeypatch, "lane@x.com")
        reset = (NOW + timedelta(days=2)).isoformat()
        capacity.append_ledger({
            "ts": NOW.isoformat(),
            "email": "lane@x.com",
            "event": "hard_limit",
            "model": "claude-fable-5-1",
            "window_tokens_5h": 100,
            "window_tokens_7d": 500,
            "reset": reset,
        })

        model_scoped = capacity.collect(now=NOW)["accounts"][0]
        assert model_scoped["learned_capacity"] is None
        assert model_scoped["limited_until"] is None
        assert model_scoped["dispatchable"] is True

        capacity.append_ledger({
            "ts": NOW.isoformat(),
            "email": "lane@x.com",
            "event": "hard_limit",
            "window_tokens_5h": 120,
            "window_tokens_7d": 600,
            "reset": reset,
        })

        legacy_account_wide = capacity.collect(now=NOW)["accounts"][0]
        assert legacy_account_wide["learned_capacity"] == {
            "five_hour": 120,
            "weekly": 600,
        }
        assert legacy_account_wide["limited_until"] == reset
        assert legacy_account_wide["dispatchable"] is False

    def test_mixed_live_and_estimated_windows_have_per_reading_confidence(
            self, monkeypatch):
        patch_lane_roster(monkeypatch, "lane@x.com")
        live = live_claude_row()
        live["weekly"] = capacity._window()
        monkeypatch.setattr(capacity, "_probe_live_rows", lambda timeout, now: [live])
        data = capacity.collect(now=NOW)
        row = data["accounts"][0]
        assert row["five_hour"]["confidence"] == "live"
        assert row["weekly"]["confidence"] == "estimated"
        assert row["confidence"] == "mixed"

    def test_explicit_accounts_file_controls_delegate_roster(self, tmp_path, monkeypatch):
        monkeypatch.setattr(capacity, "_probe_live_rows", lambda timeout, now: [])
        roster = tmp_path / "delegate-roster.json"
        roster.write_text(json.dumps({
            "accounts": ["delegate@x.com"],
            "enrolled": {"delegate@x.com": "secret"},
        }))
        data = capacity.collect(now=NOW, accounts_file=roster)
        assert [row["email"] for row in data["accounts"]] == ["delegate@x.com"]
        assert data["families"]["claude"]["available"] is True

    def test_non_enrolled_active_reset_is_not_lane_earliest_reset(self):
        early = (NOW + timedelta(minutes=30)).isoformat()
        later = (NOW + timedelta(hours=2)).isoformat()
        active = live_claude_row("active@x.com")
        active.update({
            "enrolled": False, "dispatchable": False, "status": "not-enrolled",
            "limited_until": early,
        })
        lane = live_claude_row("lane@x.com")
        lane.update({
            "enrolled": True, "dispatchable": False, "status": "limited",
            "limited_until": later, "headroom_score": 0,
        })
        summary = capacity.family_summaries([active, lane], now=NOW)["claude"]
        assert summary["earliest_reset"] == later

    def test_known_score_wins_then_unknown_uses_lowest_raw_tokens(self):
        known = live_claude_row("known@x.com", five=70, weekly=80)
        known["dispatchable"] = True
        unknown_high = live_claude_row("z@x.com")
        unknown_high.update({"headroom_score": None, "dispatchable": True})
        unknown_high["five_hour"].update({"used_percent": None, "tokens": 20})
        unknown_high["weekly"].update({"used_percent": None, "tokens": 50})
        unknown_low = json.loads(json.dumps(unknown_high))
        unknown_low["id"] = unknown_low["email"] = "a@x.com"
        unknown_low["weekly"]["tokens"] = 10

        summary = capacity.family_summaries([unknown_high, known, unknown_low], now=NOW)["claude"]
        assert summary["best"] == "known@x.com"
        summary = capacity.family_summaries([unknown_high, unknown_low], now=NOW)["claude"]
        assert summary["best"] == "a@x.com"

    def test_dispatch_handicap_can_spare_interactive_account(self):
        primary = live_claude_row("primary@x.com", five=10, weekly=10)
        primary.update({"headroom_score": 90, "dispatch_score": 80, "dispatchable": True})
        alternate = live_claude_row("alternate@x.com", five=15, weekly=15)
        alternate.update({"headroom_score": 85, "dispatch_score": 85, "dispatchable": True})
        summary = capacity.family_summaries([primary, alternate], now=NOW)["claude"]
        assert summary["best"] == "alternate@x.com"
        assert summary["headroom_score"] == 85

    def test_collect_exposes_current_in_flight_and_uses_it_on_ties(
        self, tmp_path, monkeypatch
    ):
        self._no_live(monkeypatch, "busy@x.com", "idle@x.com")
        prompt = tmp_path / "prompt.md"
        prompt.write_text("work")
        workdir = tmp_path / "work"
        workdir.mkdir()
        run_ledger.start_run(
            family="claude",
            model="claude-fable-5-1",
            lane="BUSY@x.com",
            workdir=workdir,
            prompt=prompt,
            out=tmp_path / "out.md",
        )

        data = capacity.collect(now=NOW)
        rows = {row["email"]: row for row in data["accounts"]}

        assert rows["busy@x.com"]["in_flight"] == 1
        assert rows["idle@x.com"]["in_flight"] == 0
        assert data["families"]["claude"]["best"] == "idle@x.com"

    def test_in_flight_never_overrides_a_better_dispatch_score(self):
        busy = live_claude_row("busy@x.com", five=5, weekly=5)
        idle = live_claude_row("idle@x.com", five=10, weekly=10)
        busy.update({"dispatchable": True, "dispatch_score": 95, "in_flight": 2})
        idle.update({"dispatchable": True, "dispatch_score": 90, "in_flight": 0})

        assert capacity.family_summaries([idle, busy])["claude"]["best"] == "busy@x.com"

    def test_scoped_fable_limit_only_blocks_fable_dispatch(self):
        row = live_claude_row()
        row.update({
            "dispatchable": True,
            "scoped_limits": [
                {
                    "kind": "weekly_scoped",
                    "group": "weekly",
                    "percent": 100,
                    "severity": "critical",
                    "resets_at": "2026-07-28T04:59:59Z",
                    "is_active": True,
                    "scope_model": "Fable",
                    "scope_surface": None,
                }
            ],
        })

        assert capacity.scoped_limit_for(row, "fAbLe")["kind"] == "weekly_scoped"
        assert capacity.dispatchable_for(row, "Fable") is False
        assert capacity.dispatchable_for(row, "Opus") is True
        assert capacity.dispatchable_for(row, "Sonnet") is True
        assert capacity.dispatchable_for(row, "Haiku") is True

        row["dispatchable"] = False
        assert all(
            capacity.dispatchable_for(row, family) is False
            for family in capacity.CLAUDE_MODEL_FAMILIES
        )

    def test_noncritical_scoped_limit_contributes_model_headroom(self):
        row = live_claude_row()
        row.update({
            "dispatchable": True,
            "headroom_score": 80,
            "scoped_limits": [{
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 90,
                "severity": "warning",
                "resets_at": "2026-07-28T04:59:59Z",
                "is_active": True,
                "scope_model": "Fable",
                "scope_surface": None,
            }],
        })

        assert capacity.model_headroom_score(row, "fable") == 10
        assert capacity.model_headroom_score(row, "opus") == 80
        assert capacity.dispatchable_for(row, "fable") is True
        assert capacity.model_state_for(row, "fable")["used_percent"] == 90

        row["scoped_limits"][0]["percent"] = 98
        state = capacity.model_state_for(row, "fable")
        assert state["state"] == "exhausted"
        assert state["until"] == "2026-07-28T04:59:59Z"

    def test_human_table_formats_percent_and_tokens(self, monkeypatch):
        self._no_live(monkeypatch, "lane@x.com")
        capacity.append_ledger(usage_record(NOW, total=12))
        text = capacity.human_table(capacity.collect(now=NOW))
        assert "family" in text
        assert "lane@x.com" in text
        assert "12 tok" in text

    def test_human_table_renders_critical_scoped_limit(self):
        row = live_claude_row()
        row.update({
            "dispatchable": True,
            "scoped_limits": [{
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 100,
                "severity": "critical",
                "resets_at": "2026-07-28T04:59:59Z",
                "is_active": True,
                "scope_model": "Fable",
                "scope_surface": None,
            }],
        })
        data = {
            "generated_at": NOW.isoformat(),
            "accounts": [row],
        }

        text = capacity.human_table(data)
        assert "ok except Fable" in text
        assert (
            "fable-weekly 100% critical resets 2026-07-28T04:59:59Z BLOCKED"
            in text
        )


class TestCapacityCli:
    def test_json_shape_without_live_probes(self, monkeypatch, capsys):
        row = live_claude_row()
        scoped_limits = [{
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 100,
            "severity": "critical",
            "resets_at": "2026-07-28T04:59:59Z",
            "is_active": True,
            "scope_model": "Fable",
            "scope_surface": None,
        }]
        row.update({
            "enrolled": True,
            "dispatchable": True,
            "scoped_limits": scoped_limits,
        })
        mocked = {
            "generated_at": NOW.isoformat(),
            "cache": {
                "hit": True,
                "probed_at": NOW.isoformat(),
                "age_seconds": 1,
                "ttl_seconds": 120,
            },
            "accounts": [row],
            "families": capacity.family_summaries([row], now=NOW),
        }
        monkeypatch.setattr(capacity, "report", lambda: mocked)

        assert cli.main(["capacity", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert set(data) == {"generated_at", "cache", "accounts", "families"}
        assert REQUIRED_ROW_KEYS <= set(data["accounts"][0])
        assert set(data["accounts"][0]["five_hour"]) == REQUIRED_WINDOW_KEYS
        assert set(data["accounts"][0]["weekly"]) == REQUIRED_WINDOW_KEYS
        assert data["accounts"][0]["scoped_limits"] == scoped_limits

    def test_enroll_accepts_expected_inference_scope_403(
            self, tmp_path, monkeypatch, capsys):
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps({"accounts": ["lane@x.com"], "enrolled": {}}))
        monkeypatch.setattr(claude, "known_accounts", lambda: ["lane@x.com"])
        monkeypatch.setattr(claude, "roster_config_path", lambda: roster)
        monkeypatch.setattr(
            claude, "probe_oauth_usage", lambda token: {"status": "http-403"}
        )
        monkeypatch.setattr(sys, "stdin", StringIO("setup-token"))
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
        )
        capacity.store_lane_cooldown("lane@x.com", NOW + timedelta(days=30))

        assert cli.main(["enroll", "lane@x.com"]) == 0
        assert "inference-only" in capsys.readouterr().out
        assert json.loads(roster.read_text())["enrolled"]["lane@x.com"] \
            == "claude-quota-lane@x.com"
        assert "lane@x.com" not in json.loads(paths.delegate_cooldowns_path().read_text())

        monkeypatch.setattr(capacity, "_probe_live_rows", lambda timeout, now: [])
        data = capacity.collect(force_refresh=True, now=NOW, accounts_file=roster)
        lane = next(row for row in data["accounts"] if row["email"] == "lane@x.com")
        assert lane["dispatchable"] is True


def test_retired_fable_pin_normalizes_onto_the_current_fable_model():
    """2026-09-02: subfleet had been dispatching Claude Fable 5 (claude-fable-5)
    while the app ran 5.1. The retired pin stays accepted but always means
    the current id — the two share one account-scoped Fable limit."""
    from subfleet import delegate

    assert capacity.CLAUDE_MODEL_IDS["fable"] == "claude-fable-5-1"
    assert delegate.MODEL_NAMES["fable"] == capacity.CLAUDE_MODEL_IDS["fable"]
    assert capacity.normalize_claude_model("fable") == "claude-fable-5-1"
    assert capacity.normalize_claude_model("claude-fable-5") == "claude-fable-5-1"
    assert capacity.normalize_claude_model(" Claude-Fable-5 ") == "claude-fable-5-1"
    assert capacity.normalize_claude_model("claude-fable-5-1") == "claude-fable-5-1"
    assert capacity.normalize_claude_model("claude-fable-5-20260829") == "claude-fable-5-20260829"
    assert capacity._model_display_name("claude-fable-5") == "Fable"
    assert set(capacity.claude_model_states({})) == set(capacity.CLAUDE_MODEL_IDS.values())


def test_retired_fable_cooldowns_and_hard_limits_bind_the_current_model():
    until = NOW + timedelta(hours=2)
    cooldowns = paths.delegate_cooldowns_path()
    cooldowns.parent.mkdir(parents=True, exist_ok=True)
    cooldowns.write_text(json.dumps({"lane@x.com": {"claude-fable-5": until.isoformat()}}))

    assert capacity.read_lane_cooldowns() == {
        "lane@x.com": {"claude-fable-5-1": until.isoformat()},
    }
    assert capacity.lane_cooldown("lane@x.com", model="fable", now=NOW) == until
    assert capacity.lane_cooldown("lane@x.com", model="claude-fable-5-1", now=NOW) == until
    assert capacity.lane_cooldown("lane@x.com", model="claude-fable-5", now=NOW) == until
    assert capacity.lane_cooldown("lane@x.com", model="opus", now=NOW) is None

    # a capacity row written by an older process still keys the retired pin
    row = {"model_cooldowns": {"claude-fable-5": until.isoformat()}, "dispatchable": True}
    assert capacity.model_cooldown_for(row, "claude-fable-5-1") == until.isoformat()
    assert capacity.model_cooldown_for(row, "fable") == until.isoformat()
    assert capacity.model_cooldown_for(row, "claude-opus-5") is None
    assert capacity.dispatchable_for(row, "fable") is False
    assert capacity.dispatchable_for(row, "opus") is True

    records = [{
        "email": "lane@x.com", "event": "hard_limit", "model": "claude-fable-5",
        "reset": until.isoformat(), "ts": NOW.isoformat(),
    }]
    assert capacity._hard_limit_until("lane@x.com", records, NOW, model="claude-fable-5-1") == until
    assert capacity._hard_limit_until("lane@x.com", records, NOW, model="claude-opus-5") is None


def test_context_suffix_survives_dispatch_ids_but_not_scope_keys(capsys):
    """The desktop store records `claude-fable-5[1m]` / `fable[1m]`; revive
    must keep the suffix on --model while cooldowns and comparisons key the
    bare canonical id."""
    assert capacity.split_claude_model("claude-opus-5[1m]") == ("claude-opus-5", "[1m]")
    assert capacity.split_claude_model("claude-opus-5") == ("claude-opus-5", "")
    assert capacity.normalize_claude_model("claude-fable-5[1m]") == "claude-fable-5-1[1m]"
    assert capacity.normalize_claude_model("fable[1m]") == "claude-fable-5-1[1m]"
    assert capacity.normalize_claude_model("claude-opus-5[1m]") == "claude-opus-5[1m]"
    assert capacity.canonical_claude_model("claude-fable-5[1m]") == "claude-fable-5-1"
    assert capacity.canonical_claude_model("fable[1m]") == "claude-fable-5-1"
    assert capacity.canonical_claude_model(None) is None
    assert capacity._cooldown_scope("claude-fable-5[1m]") == "claude-fable-5-1"
    assert capacity._model_display_name("claude-opus-5[1m]") == "Opus"
    assert capacity._model_display_name("fable[1m]") == "Fable"
    until = (NOW + timedelta(hours=1)).isoformat()
    row = {"model_cooldowns": {"claude-fable-5-1": until}, "dispatchable": True}
    assert capacity.model_cooldown_for(row, "claude-fable-5[1m]") == until
    assert capacity.dispatchable_for(row, "fable[1m]") is False
    assert capacity.dispatchable_for(row, "claude-opus-5[1m]") is True
    # the runner's private helper prints the dispatch form
    assert cli.main(["_canonical-model", "fable"]) == 0
    assert cli.main(["_canonical-model", "claude-fable-5[1m]"]) == 0
    assert cli.main(["_canonical-model", "custom-model"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "claude-fable-5-1", "claude-fable-5-1[1m]", "custom-model",
    ]
