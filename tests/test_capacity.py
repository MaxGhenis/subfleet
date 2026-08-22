import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from carpool import capacity, config, paths


NOW = datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)


def at(delta: timedelta) -> str:
    return (NOW + delta).isoformat(timespec="seconds")


def token_record(email: str, delta: timedelta, total: int) -> dict:
    return {
        "ts": at(delta),
        "email": email,
        "session_id": f"s-{total}",
        "input_tokens": total,
        "output_tokens": 0,
        "total_tokens": total,
    }


def test_transcript_last_message_occurrence_wins_and_append_errors(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(value)
            for value in (
                {
                    "sessionId": "session-1",
                    "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 2}},
                },
                {
                    "message": {
                        "id": "m2",
                        "usage": {
                            "input_tokens": 7,
                            "cache_creation_input_tokens": 5,
                            "cache_read_input_tokens": 3,
                            "output_tokens": 3,
                        },
                    }
                },
                {"message": {"id": "m1", "usage": {"input_tokens": 11, "output_tokens": 5}}},
            )
        ) + "\n{not json\n"
    )
    ledger = tmp_path / "lane-usage.jsonl"

    parsed = capacity.parse_transcript_usage(transcript)
    appended = capacity.append_lane_usage(
        "lane@example.com", transcript, ts=NOW, ledger_path=ledger
    )

    assert parsed == {
        "session_id": "session-1",
        "input_tokens": 26,
        "output_tokens": 8,
        "total_tokens": 34,
    }
    assert appended["total_tokens"] == 34

    broken = tmp_path / "broken.jsonl"
    broken.write_text("{not json\n")
    error = capacity.append_lane_usage(
        "lane@example.com", broken, ts=NOW, ledger_path=ledger
    )
    no_usage = tmp_path / "no-usage.jsonl"
    no_usage.write_text(json.dumps({"message": {"id": "m3", "content": []}}) + "\n")
    missing = capacity.append_lane_usage(
        "lane@example.com", no_usage, ts=NOW, ledger_path=ledger
    )
    records = capacity.read_ledger(ledger)
    assert "error" in error
    assert "no message usage" in missing["error"]
    assert records == [appended, error, missing]


def test_rolling_sums_fall_back_to_legacy_fields_and_ignore_bad_numbers():
    email = "lane@example.com"
    entries = [
        {
            "ts": at(timedelta()),
            "email": email,
            "input_tokens": 3,
            "output_tokens": 4,
        },
        token_record(email, timedelta(), float("nan")),
        token_record(email, timedelta(), float("inf")),
        token_record(email, timedelta(), 10 ** 400),
        token_record(email, timedelta(), 7),
    ]

    assert capacity.rolling_token_sums(email, now=NOW, entries=entries) == {
        "five_hour": 14,
        "weekly": 14,
    }


def test_rolling_windows_are_inclusive_and_ignore_future_errors_and_events():
    email = "lane@example.com"
    entries = [
        token_record(email, -timedelta(hours=5), 10),
        token_record(email, -timedelta(hours=5, seconds=1), 20),
        token_record(email, -timedelta(days=7), 30),
        token_record(email, -timedelta(days=7, seconds=1), 40),
        token_record(email, timedelta(seconds=1), 50),
        {"ts": at(-timedelta(hours=1)), "email": email, "error": "bad"},
        {
            "ts": at(-timedelta(hours=1)),
            "email": email,
            "event": "hard_limit",
            "window_tokens_5h": 999,
            "window_tokens_7d": 999,
        },
        token_record("other@example.com", -timedelta(minutes=1), 1000),
    ]

    assert capacity.rolling_token_sums(email, now=NOW, entries=entries) == {
        "five_hour": 10,
        "weekly": 60,
    }


def test_hard_limit_records_windows_and_learns_maxima(tmp_path):
    email = "lane@example.com"
    ledger = tmp_path / "lane-usage.jsonl"
    entries = [
        token_record(email, -timedelta(hours=1), 60),
        {
            "ts": at(-timedelta(days=8)),
            "email": email,
            "event": "hard_limit",
            "window_tokens_5h": 100,
            "window_tokens_7d": 200,
            "reset": None,
        },
        {
            "ts": at(-timedelta(days=1)),
            "email": email,
            "event": "hard_limit",
            "window_tokens_5h": 120,
            "window_tokens_7d": 180,
            "reset": None,
        },
    ]

    event = capacity.record_hard_limit(
        email,
        reset=NOW + timedelta(hours=2),
        ts=NOW,
        entries=entries,
        ledger_path=ledger,
    )

    assert event["window_tokens_5h"] == 60
    assert event["window_tokens_7d"] == 60
    assert event["learned_capacity"] == {"five_hour": 120, "weekly": 200}
    assert capacity.read_ledger(ledger) == [event]
    assert capacity.learned_capacities(email, entries=[*entries, event]) == {
        "five_hour": 120,
        "weekly": 200,
    }


def test_pending_hard_limit_fallback_expires_or_is_cleared_by_later_usage():
    email = "lane@example.com"
    live = {"codex": [], "claude": {"identity": {}, "probe": {"status": "skipped"}}}
    success = token_record(email, -timedelta(hours=2), 10)
    event = {
        "ts": at(-timedelta(minutes=30)),
        "email": email,
        "event": "hard_limit",
        "window_tokens_5h": 0,
        "window_tokens_7d": 0,
        "reset": "not-a-reset",
    }

    limited = capacity.account_rows(
        live,
        now=NOW,
        entries=[success, event],
        enrolled={email: "secret"},
        known_accounts=[email],
        cooldowns={},
    )[0]
    assert limited["pending_hard_limit"] is True
    assert limited["dispatchable"] is False
    assert limited["limited_until"] == at(timedelta(minutes=30))
    assert capacity.family_score([limited], "claude", now=NOW) == {
        "score": 0.0,
        "best_resource": None,
        "earliest_reset": at(timedelta(minutes=30)),
        "dispatchable": 0,
    }

    later_success = token_record(email, -timedelta(minutes=10), 5)
    cleared = capacity.account_rows(
        live,
        now=NOW,
        entries=[success, event, later_success],
        enrolled={email: "secret"},
        known_accounts=[email],
        cooldowns={},
    )[0]
    assert cleared["pending_hard_limit"] is False
    assert cleared["limited_until"] is None
    assert cleared["dispatchable"] is True

    expired_event = {**event, "ts": at(-timedelta(hours=2))}
    expired = capacity.account_rows(
        live,
        now=NOW,
        entries=[success, expired_event],
        enrolled={email: "secret"},
        known_accounts=[email],
        cooldowns={},
    )[0]
    assert expired["pending_hard_limit"] is False
    assert expired["dispatchable"] is True

    past_reset = {**event, "reset": at(-timedelta(minutes=1))}
    reset_passed = capacity.account_rows(
        live,
        now=NOW,
        entries=[success, past_reset],
        enrolled={email: "secret"},
        known_accounts=[email],
        cooldowns={},
    )[0]
    assert reset_passed["pending_hard_limit"] is False
    assert reset_passed["dispatchable"] is True

    future_success = token_record(email, timedelta(minutes=10), 5)
    still_limited = capacity.account_rows(
        live,
        now=NOW,
        entries=[success, event, future_success],
        enrolled={email: "secret"},
        known_accounts=[email],
        cooldowns={},
    )[0]
    assert still_limited["pending_hard_limit"] is True
    assert still_limited["dispatchable"] is False


def test_account_rows_calibration_active_merge_and_family_scoring():
    lane = "lane@example.com"
    entries = [
        token_record(lane, -timedelta(hours=1), 50),
        {
            "ts": at(-timedelta(days=1)),
            "email": lane,
            "event": "hard_limit",
            "window_tokens_5h": 100,
            "window_tokens_7d": 200,
            "reset": at(-timedelta(hours=1)),
        },
    ]
    live = {
        "codex": [
            {
                "home": "/h/c1",
                "auth": {"status": "ok", "account_id": "c1", "email": "c1@example.com"},
                "probe": {
                    "status": "ok",
                    "allowed": True,
                    "limit_reached": False,
                    "primary": {"used_percent": 20, "reset_at": at(timedelta(hours=1))},
                    "secondary": {"used_percent": 50, "reset_at": at(timedelta(days=1))},
                },
            },
            {
                "home": "/h/c2",
                "auth": {"status": "ok", "account_id": "c2", "email": "c2@example.com"},
                "probe": {
                    "status": "ok",
                    "allowed": True,
                    "limit_reached": False,
                    "primary": {"used_percent": 40, "reset_at": at(timedelta(hours=1))},
                    "secondary": {"used_percent": 10, "reset_at": at(timedelta(days=1))},
                },
            },
        ],
        "claude": {
            "identity": {"email": lane},
            "credentials": {"status": "ok"},
            "probe": {
                "status": "ok",
                "five_hour": {"used_percent": 25, "reset_at": at(timedelta(hours=2))},
                "seven_day": {"used_percent": 30, "reset_at": at(timedelta(days=2))},
            },
        },
    }

    rows = capacity.account_rows(
        live,
        now=NOW,
        entries=entries,
        enrolled={lane: "secret"},
        known_accounts=[lane, "fresh@example.com"],
        cooldowns={},
    )
    by_id = {row["id"]: row for row in rows}

    assert by_id[lane]["confidence"] == "live"
    assert by_id[lane]["five_hour"]["used_percent"] == 25
    assert by_id[lane]["learned_capacity"] == {"five_hour": 100, "weekly": 200}
    assert by_id["fresh@example.com"]["five_hour"]["capacity"] is None
    scores = capacity.family_scores(rows, now=NOW)
    assert scores["codex"]["best_resource"] == "/h/c2"
    assert scores["codex"]["score"] == 60
    assert scores["claude"]["best_resource"] == lane
    assert scores["claude"]["score"] == 70


def test_free_plan_codex_row_is_not_dispatchable():
    row = capacity._codex_account_row(
        {
            "home": "/h/codex-1",
            "auth": {
                "status": "ok",
                "account_id": "account-1",
                "email": "free@example.com",
            },
            "probe": {
                "status": "ok",
                "plan_type": "free",
                "allowed": True,
                "limit_reached": False,
                "primary": {"used_percent": 1},
                "secondary": {"used_percent": 1},
            },
        },
        NOW,
    )

    assert row["status"] == "free-plan"
    assert row["dispatchable"] is False
    assert capacity.family_score([row], "codex", now=NOW)["best_resource"] is None


def test_codex_capacity_classifies_reversed_windows_by_duration():
    row = capacity._codex_account_row(
        {
            "home": "/h/codex-1",
            "auth": {"status": "ok", "account_id": "account-1"},
            "probe": {
                "status": "ok",
                "allowed": True,
                "limit_reached": False,
                "primary": {
                    "used_percent": 70,
                    "window_seconds": 604_800,
                },
                "secondary": {
                    "used_percent": 20,
                    "window_seconds": 18_000,
                },
            },
        },
        NOW,
    )

    assert row["five_hour"]["used_percent"] == 20
    assert row["weekly"]["used_percent"] == 70


def test_capacity_dispatch_handicaps_app_shadowed_codex_account(
    tmp_path, monkeypatch
):
    app = tmp_path / "app"
    app.mkdir()
    (app / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "account_id": "protected-account",
                    "id_token": "",
                    "access_token": "",
                }
            }
        )
    )
    monkeypatch.setenv("CARPOOL_CODEX_APP_HOME", str(app))
    live = {
        "codex": [
            {
                "home": "/h/codex-1",
                "auth": {"status": "ok", "account_id": "protected-account"},
                "probe": {
                    "status": "ok",
                    "primary": {"used_percent": 10},
                    "secondary": {"used_percent": 10},
                },
            },
            {
                "home": "/h/codex-2",
                "auth": {"status": "ok", "account_id": "alternate-account"},
                "probe": {
                    "status": "ok",
                    "primary": {"used_percent": 15},
                    "secondary": {"used_percent": 15},
                },
            },
        ],
        "claude": {"identity": {}, "probe": {"status": "skipped"}},
    }

    rows = capacity.account_rows(
        live,
        now=NOW,
        entries=[],
        enrolled={},
        known_accounts=[],
        cooldowns={},
    )

    by_home = {row["home"]: row for row in rows}
    assert by_home["/h/codex-1"]["shadowed_by_app"] is True
    assert capacity.family_score(rows, "codex", now=NOW)["best_resource"] == "/h/codex-2"


def test_cooldown_updates_are_locked_case_insensitive_and_never_shorten(tmp_path):
    path = tmp_path / "cooldowns.json"
    long_limit = NOW + timedelta(days=30)

    assert capacity.store_lane_cooldown("Lane@Example.com", long_limit, path=path)
    assert capacity.store_lane_cooldown(
        "lane@example.com", NOW + timedelta(hours=1), path=path
    )

    stored = json.loads(path.read_text())
    assert stored == {"Lane@Example.com": at(timedelta(days=30))}
    emails = [f"lane-{index}@example.com" for index in range(8)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(
            executor.map(
                lambda email: capacity.store_lane_cooldown(
                    email, NOW + timedelta(hours=2), path=path
                ),
                emails,
            )
        )
    assert set(emails) <= set(json.loads(path.read_text()))
    assert capacity.clear_lane_cooldown("LANE@example.com", path=path)
    assert set(json.loads(path.read_text())) == set(emails)


def test_auth_failure_extends_lane_cooldown_for_thirty_days(tmp_path):
    path = tmp_path / "cooldowns.json"

    assert capacity.record_auth_failure("lane@example.com", ts=NOW, path=path)

    assert json.loads(path.read_text()) == {
        "lane@example.com": at(timedelta(days=30))
    }


def test_malformed_enrollments_and_missing_secrets_are_not_dispatchable():
    config.save(
        {
            "accounts": [
                "valid@example.com",
                "missing@example.com",
                "null@example.com",
                "empty@example.com",
            ],
            "enrolled": {
                "valid@example.com": "secret-valid",
                "missing@example.com": "secret-missing",
                "null@example.com": None,
                "empty@example.com": "  ",
            },
            "codex_homes": [],
        }
    )
    live = {"codex": [], "claude": {"identity": {}, "probe": {"status": "skipped"}}}

    rows = capacity.account_rows(
        live,
        now=NOW,
        entries=[],
        cooldowns={},
        secret_lookup_fn=lambda name: "token" if name == "secret-valid" else None,
    )
    by_email = {row["email"]: row for row in rows}

    assert by_email["valid@example.com"]["dispatchable"] is True
    assert by_email["missing@example.com"]["status"] == "secret-missing"
    assert by_email["missing@example.com"]["dispatchable"] is False
    assert by_email["null@example.com"]["enrolled"] is False
    assert by_email["empty@example.com"]["enrolled"] is False


def test_claude_email_matching_is_case_insensitive_and_preserves_config_spelling():
    configured = "Lane@Example.com"
    entries = [
        token_record("LANE@example.COM", -timedelta(hours=1), 50),
        {
            "ts": at(-timedelta(minutes=30)),
            "email": "lane@EXAMPLE.com",
            "event": "hard_limit",
            "window_tokens_5h": 100,
            "window_tokens_7d": 200,
            "reset": None,
        },
    ]
    live = {
        "codex": [],
        "claude": {
            "identity": {"email": "lAnE@eXaMpLe.CoM"},
            "probe": {
                "status": "ok",
                "five_hour": {"used_percent": 10},
                "seven_day": {"used_percent": 20},
            },
        },
    }
    cooldown = at(timedelta(hours=3))

    rows = capacity.account_rows(
        live,
        now=NOW,
        entries=entries,
        enrolled={"lane@example.COM": "secret"},
        known_accounts=[configured],
        cooldowns={"LANE@EXAMPLE.COM": cooldown},
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == configured
    assert row["resource"] == "lane@example.COM"
    assert row["active"] is True and row["enrolled"] is True
    assert row["confidence"] == "live"
    assert row["learned_capacity"] == {"five_hour": 100, "weekly": 200}
    assert row["pending_hard_limit"] is True
    assert row["limited_until"] == cooldown
    assert row["dispatchable"] is False
    assert capacity.rolling_token_sums("lane@example.com", now=NOW, entries=entries) == {
        "five_hour": 50,
        "weekly": 50,
    }
    assert capacity.learned_capacities("LANE@example.com", entries=entries) == {
        "five_hour": 100,
        "weekly": 200,
    }


def test_family_score_optimistic_uncalibrated_and_all_limited_reset():
    reset = NOW + timedelta(hours=3)
    available = {
        "family": "claude",
        "id": "fresh@example.com",
        "resource": "fresh@example.com",
        "five_hour": capacity._token_reading(123, None),
        "weekly": capacity._token_reading(456, None),
        "limited_until": None,
        "dispatchable": True,
    }
    limited = {
        **available,
        "id": "limited@example.com",
        "resource": "limited@example.com",
        "limited_until": reset.isoformat(timespec="seconds"),
        "dispatchable": False,
    }

    assert capacity.family_score([available, limited], "claude", now=NOW)["score"] == 100
    summary = capacity.family_score([limited], "claude", now=NOW)
    assert summary["score"] == 0
    assert summary["best_resource"] is None
    assert summary["earliest_reset"] == reset.isoformat(timespec="seconds")


def test_family_earliest_reset_filters_explicitly_ineligible_rows():
    soon = at(timedelta(hours=1))
    legacy = at(timedelta(hours=2))
    later = at(timedelta(hours=3))
    claude_rows = [
        {"family": "claude", "enrolled": False, "limited_until": soon},
        {"family": "claude", "limited_until": legacy},
        {"family": "claude", "enrolled": True, "limited_until": later},
    ]
    assert capacity.family_score(claude_rows, "claude", now=NOW)["earliest_reset"] == legacy
    assert capacity.family_score(
        [claude_rows[0], claude_rows[2]], "claude", now=NOW
    )["earliest_reset"] == later

    codex_rows = [
        {"family": "codex", "auth_status": "missing", "limited_until": soon},
        {"family": "codex", "limited_until": legacy},
        {"family": "codex", "auth_status": "ok", "limited_until": later},
    ]
    assert capacity.family_score(codex_rows, "codex", now=NOW)["earliest_reset"] == legacy
    assert capacity.family_score(
        [codex_rows[0], codex_rows[2]], "codex", now=NOW
    )["earliest_reset"] == later


def test_live_probe_cache_ttl_and_sanitization(tmp_path):
    cache_file = tmp_path / "capacity-cache.json"
    calls = {"codex": 0, "claude": 0}

    def homes():
        return [tmp_path / "codex-home"]

    def auth(home):
        return {
            "status": "ok",
            "home": str(home),
            "account_id": "acct",
            "email": "codex@example.com",
            "_access_token": "codex-secret",
        }

    def probe_all(auths, timeout):
        calls["codex"] += 1
        return [
            {
                "status": "ok",
                "primary": {"used_percent": calls["codex"], "reset_at": None},
                "secondary": {"used_percent": 2, "reset_at": None},
            }
        ]

    def keychain():
        return {"status": "ok", "_token": "claude-secret"}

    def claude_probe(token, timeout):
        assert token == "claude-secret"
        calls["claude"] += 1
        return {"status": "ok", "five_hour": {"used_percent": calls["claude"]}}

    kwargs = {
        "cache_path": cache_file,
        "codex_homes_fn": homes,
        "read_auth_fn": auth,
        "probe_all_fn": probe_all,
        "claude_identity_fn": lambda: {"email": "lane@example.com"},
        "keychain_fn": keychain,
        "claude_probe_fn": claude_probe,
    }
    first = capacity.get_live_probes(now=NOW, **kwargs)
    edge = capacity.get_live_probes(now=NOW + timedelta(seconds=120), **kwargs)
    stale = capacity.get_live_probes(now=NOW + timedelta(seconds=121), **kwargs)

    assert first["cache_hit"] is False
    assert edge["cache_hit"] is True
    assert stale["cache_hit"] is False
    assert calls == {"codex": 2, "claude": 2}
    serialized = cache_file.read_text()
    assert "codex-secret" not in serialized
    assert "claude-secret" not in serialized
    assert not any(key.startswith("_") for key in json.loads(serialized)["codex"][0]["auth"])


def test_live_probe_skips_keychain_and_oauth_without_active_identity(tmp_path):
    def forbidden():
        pytest.fail("keychain accessed without an active desktop identity")

    live = capacity.get_live_probes(
        now=NOW,
        cache_path=tmp_path / "cache.json",
        codex_homes_fn=lambda: [],
        claude_identity_fn=lambda: {"email": None},
        keychain_fn=forbidden,
        claude_probe_fn=lambda *args, **kwargs: pytest.fail("oauth probed without active identity"),
    )

    assert live["claude"]["credentials"]["status"] == "skipped"
    assert live["claude"]["probe"]["status"] == "skipped"


def test_fresh_malformed_cache_is_a_miss_and_reprobes(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"checked_at": NOW.isoformat(), "codex": []}))
    calls = {"homes": 0, "identity": 0}

    def homes():
        calls["homes"] += 1
        return []

    def identity():
        calls["identity"] += 1
        return {"email": None}

    live = capacity.get_live_probes(
        now=NOW + timedelta(seconds=1),
        cache_path=cache_file,
        codex_homes_fn=homes,
        claude_identity_fn=identity,
    )

    assert live["cache_hit"] is False
    assert calls == {"homes": 1, "identity": 1}
    cached = json.loads(cache_file.read_text())
    assert isinstance(cached["codex"], list)
    assert all(
        isinstance(cached["claude"][key], dict)
        for key in ("identity", "credentials", "probe")
    )


def test_build_uses_tmp_state_paths_and_has_report_shape(tmp_path):
    config.save(
        {
            "accounts": ["lane@example.com"],
            "enrolled": {"lane@example.com": "lane-secret"},
            "codex_homes": [],
        }
    )
    report = capacity.build(
        clock=lambda: NOW,
        cache_path=tmp_path / "cache.json",
        ledger_path=tmp_path / "ledger.jsonl",
        cooldowns_path=tmp_path / "cooldowns.json",
        codex_homes_fn=lambda: [],
        claude_identity_fn=lambda: {"email": "lane@example.com"},
        keychain_fn=lambda: {"status": "ok", "_token": "secret"},
        claude_probe_fn=lambda token, timeout: {
            "status": "ok",
            "five_hour": {"used_percent": 10},
            "seven_day": {"used_percent": 20},
        },
        secret_lookup_fn=lambda name: "setup-token",
    )

    assert set(report) == {"generated_at", "cache", "accounts", "families"}
    assert report["accounts"][0]["resource"] == "lane@example.com"
    assert report["families"]["claude"]["score"] == 80
    assert paths.lane_usage_path().name == "lane-usage.jsonl"
    assert paths.capacity_cache_path().name == "capacity-cache.json"
