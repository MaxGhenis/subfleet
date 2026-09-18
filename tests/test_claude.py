import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from subfleet import claude
from subfleet.util import now_local


def write_transcript(projects_dir, name, events):
    proj = projects_dir / "-Users-maxghenis"
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
        assert ident["email"] == "max@example.com"

    def test_known_accounts_deduped(self, env_paths, monkeypatch, tmp_path):
        monkeypatch.setenv("SUBFLEET_CLAUDE_ACCOUNTS", str(tmp_path / "absent.json"))
        (env_paths["claude_dir"] / "cc-mirror-accounts.json").write_text(
            json.dumps({"u1": "a@b.com", "u2": "a@b.com (new 2026-07-06)", "u3": "c@d.com"})
        )
        assert claude.known_accounts() == ["a@b.com", "c@d.com"]

    def test_roster_config_merged_with_mirror(self, env_paths, monkeypatch, tmp_path):
        cfg = tmp_path / "roster.json"
        cfg.write_text(json.dumps({"accounts": ["x@y.com", "a@b.com"]}))
        monkeypatch.setenv("SUBFLEET_CLAUDE_ACCOUNTS", str(cfg))
        (env_paths["claude_dir"] / "cc-mirror-accounts.json").write_text(
            json.dumps({"u1": "a@b.com", "u3": "c@d.com"})
        )
        assert claude.known_accounts() == ["a@b.com", "c@d.com", "x@y.com"]


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
    def _roster(self, tmp_path, monkeypatch, enrolled):
        cfg = tmp_path / "roster.json"
        cfg.write_text(json.dumps({"accounts": ["a@b.com", "c@d.com", "e@f.com"], "enrolled": enrolled}))
        monkeypatch.setenv("SUBFLEET_CLAUDE_ACCOUNTS", str(cfg))

    def test_enrolled_account_probed(self, env_paths, monkeypatch, tmp_path):
        self._roster(tmp_path, monkeypatch, {"c@d.com": "claude-quota-c@d.com"})

        def fake_secret(cmd, **kw):
            class R:
                returncode = 0
                stdout = "tok-c\n"
                stderr = ""
            return R()

        def fake_opener(req, t):
            assert "tok-c" in req.headers.get("Authorization", "")
            return 200, json.dumps({"five_hour": {"utilization": 12}}).encode()

        rows = claude.accounts_report("a@b.com", opener=fake_opener, secret_runner=fake_secret)
        by = {r["email"]: r for r in rows}
        assert by["a@b.com"]["active"] and not by["a@b.com"]["enrolled"]
        assert by["c@d.com"]["enrolled"] and by["c@d.com"]["probe"]["status"] == "ok"
        assert by["e@f.com"]["enrolled"] is False and "probe" not in by["e@f.com"]
        assert rows[0]["email"] == "a@b.com"  # active sorts first

    def test_missing_secret_flagged_not_fabricated(self, env_paths, monkeypatch, tmp_path):
        self._roster(tmp_path, monkeypatch, {"c@d.com": "claude-quota-c@d.com"})

        def no_secret(cmd, **kw):
            class R:
                returncode = 1
                stdout = ""
                stderr = "not found"
            return R()

        rows = claude.accounts_report(None, secret_runner=no_secret)
        row = next(r for r in rows if r["email"] == "c@d.com")
        assert row["probe"]["status"] == "secret-missing"

    def test_agent_secret_names_lists_services_without_values(self):
        seen = []

        def runner(cmd, **kwargs):
            seen.append((cmd, kwargs))

            class Result:
                returncode = 0
                stdout = "claude-quota-a@b.com\tmax\tlabel\nother\tmax\tlabel\n"
                stderr = ""

            return Result()

        assert claude.agent_secret_names(runner=runner) == {
            "claude-quota-a@b.com", "other",
        }
        assert seen[0][0][-1] == "list"
        assert "get" not in seen[0][0]


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

    def test_real_scoped_limit_and_spend_payload(self):
        body = (
            Path(__file__).parent / "fixtures" / "claude-oauth-usage-scoped.json"
        ).read_bytes()
        result = claude.probe_oauth_usage("tok", opener=lambda req, t: (200, body))

        scoped = next(limit for limit in result["limits"] if limit["scope_model"])
        assert scoped == {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 100,
            "severity": "critical",
            "resets_at": "2026-07-28T04:59:59.516672+00:00",
            "is_active": True,
            "scope_model": "Fable",
            "scope_surface": None,
        }
        assert result["spend"] == {
            "used_minor": 54316,
            "currency": "USD",
            "exponent": 2,
            "enabled": True,
        }
        assert result["extra_usage"] == {
            "used_minor": 54316.0,
            "currency": "USD",
            "exponent": 2,
            "enabled": True,
        }

    def test_missing_empty_and_malformed_limits_are_empty(self):
        for limits in ("missing", [], None, {}, "not-a-list", 5):
            payload = {"five_hour": {"utilization": 12}}
            if limits != "missing":
                payload["limits"] = limits
            body = json.dumps(payload).encode()
            result = claude.probe_oauth_usage(
                "tok", opener=lambda req, t, body=body: (200, body)
            )
            assert result["limits"] == []

    def test_malformed_limit_entries_and_scopes_are_tolerated(self):
        body = json.dumps(
            {
                "limits": [
                    None,
                    "bad",
                    {
                        "kind": "weekly_scoped",
                        "scope": {"model": "bad", "surface": "api"},
                    },
                    {"kind": "session", "scope": []},
                ]
            }
        ).encode()
        result = claude.probe_oauth_usage("tok", opener=lambda req, t: (200, body))
        assert [limit["kind"] for limit in result["limits"]] == [
            "weekly_scoped",
            "session",
        ]
        assert result["limits"][0]["scope_model"] is None
        assert result["limits"][0]["scope_surface"] == "api"

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


class TestStatuslineState:
    def test_fresh_state(self, env_paths):
        state = env_paths["state"]
        state.mkdir(parents=True, exist_ok=True)
        (state / "claude-statusline.json").write_text(
            json.dumps(
                {
                    "updated_at": now_local().isoformat(timespec="seconds"),
                    "rate_limits": {
                        "five_hour": {"used_percentage": 42.5},
                        "seven_day": {"used_percentage": 12.0},
                    },
                }
            )
        )
        sl = claude.statusline_state()
        assert sl["fresh"] is True
        assert sl["five_hour_pct"] == 42.5
        assert sl["seven_day_pct"] == 12.0

    def test_stale_state_flagged(self, env_paths):
        state = env_paths["state"]
        state.mkdir(parents=True, exist_ok=True)
        old = (now_local() - timedelta(hours=3)).isoformat(timespec="seconds")
        (state / "claude-statusline.json").write_text(
            json.dumps({"updated_at": old, "rate_limits": {"five_hour": {"used_percentage": 10}}})
        )
        sl = claude.statusline_state()
        assert sl["fresh"] is False

    def test_missing_state(self, env_paths):
        assert claude.statusline_state() is None


class TestDerivations:
    def test_weekly_anchor_still_future_is_observed(self):
        from subfleet.util import now_local
        future = (now_local() + timedelta(days=2)).isoformat(timespec="seconds")
        out = claude.current_weekly_reset(future)
        assert out["confidence"] == "observed"
        assert out["reset_at"] == future

    def test_weekly_anchor_past_rolls_forward_as_derived(self):
        from subfleet.util import now_local, parse_iso
        past = (now_local() - timedelta(days=9)).isoformat(timespec="seconds")
        out = claude.current_weekly_reset(past)
        assert out["confidence"] == "derived"
        reset = parse_iso(out["reset_at"])
        assert reset > now_local()
        assert (reset - parse_iso(past)).total_seconds() % (7 * 86400) < 61

    def test_five_hour_window_opens_after_gap(self):
        from subfleet.util import now_local
        now = now_local()
        intervals = [(now - timedelta(hours=20), now - timedelta(hours=19)),
                     (now - timedelta(hours=2), now - timedelta(minutes=5))]
        out = claude.derive_five_hour_window(now=now, intervals=intervals)
        assert out is not None
        from subfleet.util import parse_iso
        assert abs((parse_iso(out["window_start"]) - (now - timedelta(hours=2))).total_seconds()) < 61
        assert out["confidence"] == "derived"

    def test_five_hour_chains_through_continuous_activity(self):
        from subfleet.util import now_local, parse_iso
        now = now_local()
        intervals = [(now - timedelta(hours=7), now - timedelta(minutes=1))]
        out = claude.derive_five_hour_window(now=now, intervals=intervals)
        # first window [t-7h, t-2h), chained second window opened t-2h
        assert abs((parse_iso(out["window_start"]) - (now - timedelta(hours=2))).total_seconds()) < 61

    def test_no_open_window_when_activity_expired(self):
        from subfleet.util import now_local
        now = now_local()
        intervals = [(now - timedelta(hours=9), now - timedelta(hours=6))]
        assert claude.derive_five_hour_window(now=now, intervals=intervals) is None


class TestLastOauthReading:
    def test_missing_file(self, env_paths):
        assert claude.last_oauth_reading() is None

    def test_parses_cached_payload(self, env_paths):
        state = env_paths["state"]
        state.mkdir(parents=True, exist_ok=True)
        checked = (now_local() - timedelta(hours=2)).isoformat(timespec="seconds")
        (state / "claude-oauth-raw.json").write_text(
            json.dumps(
                {
                    "checked_at": checked,
                    "raw": {
                        "five_hour": {"used_percentage": 42.5, "resets_at": None},
                        "seven_day": {"utilization": 61, "resets_at": None},
                        "seven_day_opus": {"used_percentage": 12, "resets_at": None},
                    },
                }
            )
        )
        r = claude.last_oauth_reading()
        assert r["status"] == "ok"
        assert r["checked_at"] == checked
        assert r["five_hour"]["used_percent"] == 42.5
        assert r["seven_day"]["used_percent"] == 61  # utilization fallback
        assert r["windows"]["seven_day_opus"]["used_percent"] == 12

    def test_malformed_returns_none(self, env_paths):
        state = env_paths["state"]
        state.mkdir(parents=True, exist_ok=True)
        (state / "claude-oauth-raw.json").write_text("not json")
        assert claude.last_oauth_reading() is None
        (state / "claude-oauth-raw.json").write_text(json.dumps({"checked_at": "x"}))
        assert claude.last_oauth_reading() is None


class TestCapacityCachedReading:
    def _write(self, state, row, probed_at=None):
        state.mkdir(parents=True, exist_ok=True)
        (state / "capacity-live-cache.json").write_text(
            json.dumps(
                {
                    "probed_at": probed_at
                    or now_local().isoformat(timespec="seconds"),
                    "accounts": [row],
                }
            )
        )

    def test_live_confidence_row(self, env_paths):
        stamp = (now_local() - timedelta(minutes=30)).isoformat(timespec="seconds")
        self._write(
            env_paths["state"],
            {
                "family": "claude",
                "active": True,
                "email": "a@x.com",
                "five_hour": {"used_percent": 33, "confidence": "live"},
                "weekly": {"used_percent": 44, "confidence": "live"},
            },
            probed_at=stamp,
        )
        r = claude.capacity_cached_reading("a@x.com")
        assert r == {
            "five_hour_pct": 33,
            "seven_day_pct": 44,
            "source": "capacity-cache",
            "as_of": stamp,
        }

    def test_ledger_estimated_windows_excluded(self, env_paths):
        self._write(
            env_paths["state"],
            {
                "family": "claude",
                "active": True,
                "email": "a@x.com",
                "five_hour": {"used_percent": 33, "confidence": "estimated"},
                "weekly": {"used_percent": 44, "confidence": "estimated"},
            },
        )
        assert claude.capacity_cached_reading("a@x.com") is None

    def test_email_mismatch_skipped(self, env_paths):
        self._write(
            env_paths["state"],
            {
                "family": "claude",
                "active": True,
                "email": "other@x.com",
                "five_hour": {"used_percent": 33, "confidence": "live"},
                "weekly": {"used_percent": 44, "confidence": "live"},
            },
        )
        assert claude.capacity_cached_reading("a@x.com") is None

    def test_missing_cache(self, env_paths):
        assert claude.capacity_cached_reading("a@x.com") is None


class TestPickLiveSource:
    def test_newest_wins_and_annotated(self):
        old = (now_local() - timedelta(hours=4)).isoformat(timespec="seconds")
        new = (now_local() - timedelta(minutes=10)).isoformat(timespec="seconds")
        picked = claude.pick_live_source(
            [
                {"five_hour_pct": 56, "source": "statusline", "as_of": old},
                {"five_hour_pct": 42, "source": "oauth-cache", "as_of": new},
            ]
        )
        assert picked["source"] == "oauth-cache"
        assert picked["stale"] is False
        assert 9 <= picked["age_min"] <= 11

    def test_old_reading_flagged_stale(self):
        old = (now_local() - timedelta(hours=4)).isoformat(timespec="seconds")
        picked = claude.pick_live_source(
            [{"five_hour_pct": 56, "source": "statusline", "as_of": old}]
        )
        assert picked["stale"] is True
        assert picked["age_min"] > claude.LIVE_STALE_AFTER_MIN

    def test_unusable_candidates_dropped(self):
        fresh = now_local().isoformat(timespec="seconds")
        assert claude.pick_live_source([]) is None
        assert claude.pick_live_source([None]) is None
        assert (
            claude.pick_live_source(
                [
                    {"five_hour_pct": None, "seven_day_pct": None,
                     "source": "oauth", "as_of": fresh},
                    {"five_hour_pct": 10, "source": "x", "as_of": None},
                ]
            )
            is None
        )

    def test_tie_prefers_earlier_candidate(self):
        stamp = now_local().isoformat(timespec="seconds")
        picked = claude.pick_live_source(
            [
                {"five_hour_pct": 1, "source": "oauth", "as_of": stamp},
                {"five_hour_pct": 2, "source": "oauth-cache", "as_of": stamp},
            ]
        )
        assert picked["source"] == "oauth"

    def test_weekly_only_reading_is_usable(self):
        fresh = now_local().isoformat(timespec="seconds")
        picked = claude.pick_live_source(
            [{"five_hour_pct": None, "seven_day_pct": 30,
              "source": "capacity-cache", "as_of": fresh}]
        )
        assert picked["seven_day_pct"] == 30
        assert picked["stale"] is False
