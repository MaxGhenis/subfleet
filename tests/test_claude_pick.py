"""Lane ranking (claude-pick): pure ranking, fleet summary, CLI, watchdog and
render wiring. All probes are mocked — no network, no keychain."""

import json
from datetime import timedelta

import pytest

from carpool import claude, cli, config, snapshot, watchdog
from carpool.claude import lane_verdict, lanes_fleet, rank_lanes
from carpool.util import iso, now_local


def lane_row(email, fh=None, wk=None, enrolled=True, active=False, status="ok",
             fh_reset=None, wk_reset=None):
    """An accounts_report row with a mocked oauth/usage probe."""
    row = {"email": email, "active": active, "enrolled": enrolled}
    if not enrolled:
        return row
    probe = {"status": status, "checked_at": "2026-07-18T12:00:00-04:00"}
    if status == "ok":
        windows = {}
        if fh is not None:
            probe["five_hour"] = {"used_percent": fh, "reset_at": fh_reset}
            windows["five_hour"] = probe["five_hour"]
        if wk is not None:
            probe["seven_day"] = {"used_percent": wk, "reset_at": wk_reset}
            windows["seven_day"] = probe["seven_day"]
        probe["windows"] = windows
    row["probe"] = probe
    return row


class TestRankLanes:
    def test_lowest_usage_wins(self):
        ranked = rank_lanes([
            lane_row("alpha@example.com", fh=60, wk=20),
            lane_row("beta@example.com", fh=5, wk=10),
        ])
        assert ranked[0]["email"] == "beta@example.com"

    def test_active_handicap_spares_anchor(self):
        ranked = rank_lanes([
            lane_row("anchor@example.com", fh=10, wk=10, active=True),
            lane_row("beta@example.com", fh=15, wk=15),
        ])
        # 10+10 handicap > 15, so the alternate wins despite higher raw usage.
        assert ranked[0]["email"] == "beta@example.com"

    def test_no_handicap_ranks_raw(self):
        ranked = rank_lanes([
            lane_row("anchor@example.com", fh=10, wk=10, active=True),
            lane_row("beta@example.com", fh=15, wk=15),
        ], handicap=0)
        assert ranked[0]["email"] == "anchor@example.com"

    def test_weekly_window_is_a_hard_gate(self):
        # Fresh 5h window does not rescue a lane through its week.
        ranked = rank_lanes([
            lane_row("alpha@example.com", fh=2, wk=97),
            lane_row("beta@example.com", fh=50, wk=50),
        ])
        assert [r["email"] for r in ranked] == ["beta@example.com"]

    def test_worst_window_drives_score(self):
        ranked = rank_lanes([
            lane_row("alpha@example.com", fh=10, wk=60),
            lane_row("beta@example.com", fh=30, wk=20),
        ])
        # effective a=60, b=30.
        assert ranked[0]["email"] == "beta@example.com"

    def test_weekly_tiebreak(self):
        ranked = rank_lanes([
            lane_row("alpha@example.com", fh=30, wk=10),
            lane_row("beta@example.com", fh=30, wk=30),
        ])
        # equal effective usage (30) -> lower weekly wins.
        assert ranked[0]["email"] == "alpha@example.com"

    def test_email_tiebreak_is_deterministic(self):
        ranked = rank_lanes([
            lane_row("beta@example.com", fh=10, wk=10),
            lane_row("alpha@example.com", fh=10, wk=10),
        ])
        assert [r["email"] for r in ranked] == ["alpha@example.com", "beta@example.com"]

    def test_dead_and_unenrolled_excluded(self):
        ranked = rank_lanes([
            lane_row("alpha@example.com", enrolled=False),
            lane_row("beta@example.com", status="token-invalid"),
            lane_row("charlie@example.com", status="secret-missing"),
            lane_row("delta@example.com", status="rate-limited"),
            lane_row("echo@example.com", fh=98, wk=10),  # under 5% headroom
            lane_row("foxtrot@example.com", status="ok"),   # probe ok, no window fields
        ])
        assert ranked == []

    def test_single_window_lane_still_ranks(self):
        # Server omitted seven_day: rank on what was reported, never fabricate.
        ranked = rank_lanes([lane_row("alpha@example.com", fh=20)])
        assert ranked[0]["email"] == "alpha@example.com"
        assert ranked[0]["weekly_used_percent"] is None


class TestVerdicts:
    def test_verdict_labels(self):
        assert lane_verdict(lane_row("alpha@example.com", enrolled=False)) == "not-enrolled"
        assert lane_verdict(lane_row("alpha@example.com", status="token-invalid")) == "token-invalid"
        assert lane_verdict(lane_row("alpha@example.com", status="secret-missing")) == "secret-missing"
        assert lane_verdict(lane_row("alpha@example.com", status="ok")) == "no-window-data"
        assert lane_verdict(lane_row("alpha@example.com", fh=99, wk=10)) == "exhausted"
        assert lane_verdict(lane_row("alpha@example.com", fh=10, wk=99)) == "exhausted"
        assert lane_verdict(lane_row("alpha@example.com", fh=10, wk=10)) == "ok"


class TestLanesFleet:
    def test_summary_counts_and_best(self):
        fleet = lanes_fleet([
            lane_row("alpha@example.com", fh=10, wk=10),
            lane_row("beta@example.com", fh=99, wk=10),
            lane_row("charlie@example.com", enrolled=False),
        ])
        assert fleet["enrolled"] == 2
        assert fleet["dispatchable_now"] == 1
        assert fleet["best"] == "alpha@example.com"
        assert [l["verdict"] for l in fleet["lanes"]] == ["ok", "exhausted"]

    def test_earliest_reset_uses_governing_window(self):
        soon = iso(now_local() + timedelta(hours=1))
        later = iso(now_local() + timedelta(hours=3))
        much_later = iso(now_local() + timedelta(days=2))
        fleet = lanes_fleet([
            # 5h exhausted only: governed by the 5h reset (soon).
            lane_row("alpha@example.com", fh=99, wk=50, fh_reset=soon, wk_reset=much_later),
            # both windows exhausted: usable only when BOTH reset (the later one).
            lane_row("beta@example.com", fh=99, wk=99, fh_reset=later, wk_reset=much_later),
        ])
        assert fleet["dispatchable_now"] == 0
        assert fleet["earliest_reset"] == soon
        by = {l["email"]: l for l in fleet["lanes"]}
        assert by["alpha@example.com"]["reset_at"] == soon
        assert by["beta@example.com"]["reset_at"] == much_later

    def test_empty_roster(self):
        fleet = lanes_fleet([])
        assert fleet == {"enrolled": 0, "dispatchable_now": 0, "best": None,
                         "earliest_reset": None, "lanes": []}


class TestClaudePickCli:
    def _patch_rows(self, monkeypatch, rows, active="anchor@example.com"):
        capacity_rows = []
        for lane in rows:
            probe = lane.get("probe") or {}
            five = probe.get("five_hour")
            weekly = probe.get("seven_day")
            status = probe.get("status") or "not-enrolled"
            used = [
                value
                for value in (
                    (five or {}).get("used_percent"),
                    (weekly or {}).get("used_percent"),
                )
                if value is not None
            ]
            exhausted = bool(used) and max(used) >= 95
            reset_candidates = [
                window.get("reset_at")
                for window in (five, weekly)
                if window and window.get("used_percent", 0) >= 95 and window.get("reset_at")
            ]
            capacity_rows.append(
                {
                    "family": "claude",
                    "id": lane["email"],
                    "email": lane["email"],
                    "resource": lane["email"],
                    "active": lane.get("active", lane["email"] == active),
                    "enrolled": lane.get("enrolled", False),
                    "five_hour": five,
                    "weekly": weekly,
                    "learned_capacity": None,
                    "limited_until": max(reset_candidates) if reset_candidates else None,
                    "confidence": "live" if used else "estimated",
                    "status": "exhausted" if exhausted else status,
                    "dispatchable": bool(lane.get("enrolled"))
                    and status == "ok"
                    and not exhausted,
                }
            )
        monkeypatch.setattr(
            cli.capacity,
            "build",
            lambda: {"generated_at": "2026-07-18T12:00:00-04:00", "accounts": capacity_rows},
        )
        monkeypatch.setattr(
            claude,
            "accounts_report",
            lambda *args, **kwargs: pytest.fail("setup-token usage probe must not run"),
        )

    def test_best_email_on_stdout(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [
            lane_row("alpha@example.com", fh=40, wk=30),
            lane_row("beta@example.com", fh=10, wk=10),
        ])
        rc = cli.main(["pick", "claude"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out.strip() == "beta@example.com"
        assert "5h 10%" in out.err

    def test_no_lane_exits_1_with_earliest_reset(self, env_paths, monkeypatch, capsys):
        soon = iso(now_local() + timedelta(hours=2))
        self._patch_rows(monkeypatch, [lane_row("alpha@example.com", fh=99, wk=10, fh_reset=soon)])
        rc = cli.main(["pick", "claude"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "no dispatchable claude lane" in err
        assert soon in err

    def test_zero_enrolled_prints_ritual(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [lane_row("alpha@example.com", enrolled=False)])
        rc = cli.main(["pick", "claude"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "claude setup-token" in err
        assert "carpool enroll" in err

    def test_json_ranking_and_exclusions(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [
            lane_row("alpha@example.com", fh=10, wk=10),
            lane_row("beta@example.com", fh=20, wk=20),
            lane_row("charlie@example.com", status="token-invalid"),
        ])
        rc = cli.main(["pick", "claude", "--json", "--all"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["best"] == "alpha@example.com"
        assert [r["email"] for r in out["ranked"]] == ["alpha@example.com", "beta@example.com"]
        assert out["excluded"] == [{"email": "charlie@example.com", "verdict": "token-invalid", "reset_at": None}]
        assert out["enrolled"] == 3

    def test_handicap_flag_wiring(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [
            lane_row("anchor@example.com", fh=10, wk=10, active=True),
            lane_row("beta@example.com", fh=15, wk=15),
        ])
        assert cli.main(["pick", "claude"]) == 0
        assert capsys.readouterr().out.strip() == "beta@example.com"
        assert cli.main(["pick", "claude", "--no-handicap"]) == 0
        assert capsys.readouterr().out.strip() == "anchor@example.com"

    def test_cached_uses_unified_capacity_report(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [lane_row("cached@example.com", fh=5, wk=5)])
        rc = cli.main(["pick", "claude", "--cached"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "cached@example.com"


def test_capacity_picker_accepts_uncalibrated_ledger_lane_and_uses_raw_tokens():
    report = {
        "accounts": [
            {
                "family": "claude",
                "id": "busy@example.com",
                "email": "busy@example.com",
                "enrolled": True,
                "dispatchable": True,
                "status": "estimated",
                "confidence": "estimated",
                "five_hour": {"tokens": 300, "used_percent": None},
                "weekly": {"tokens": 900, "used_percent": None},
            },
            {
                "family": "claude",
                "id": "fresh@example.com",
                "email": "fresh@example.com",
                "enrolled": True,
                "dispatchable": True,
                "status": "estimated",
                "confidence": "estimated",
                "five_hour": {"tokens": 50, "used_percent": None},
                "weekly": {"tokens": 100, "used_percent": None},
            },
        ]
    }

    ranked, excluded = cli._capacity_lane_ranking(
        report, handicap=10, min_headroom=5
    )

    assert [row["email"] for row in ranked] == [
        "fresh@example.com",
        "busy@example.com",
    ]
    assert excluded == []
    assert all(row["score"] is None for row in ranked)


def test_uncalibrated_picker_displays_estimated_token_usage(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.capacity,
        "build",
        lambda: {
            "generated_at": "2026-07-18T12:00:00-04:00",
            "accounts": [
                {
                    "family": "claude",
                    "id": "lane@example.com",
                    "email": "lane@example.com",
                    "enrolled": True,
                    "dispatchable": True,
                    "status": "estimated",
                    "confidence": "estimated",
                    "five_hour": {"tokens": 123, "used_percent": None},
                    "weekly": {"tokens": 456, "used_percent": None},
                }
            ],
        },
    )

    assert cli.main(["pick", "claude"]) == 0
    output = capsys.readouterr()
    assert output.out.strip() == "lane@example.com"
    assert "5h 123 tok" in output.err
    assert "[estimated]" in output.err


def test_snapshot_uses_desktop_probe_and_ledger_for_enrolled_lanes(
    env_paths, monkeypatch
):
    config.save(
        {
            "accounts": ["active@example.com", "lane@example.com"],
            "enrolled": {
                "active@example.com": "token-active",
                "lane@example.com": "token-lane",
            },
            "codex_homes": [],
        }
    )
    monkeypatch.setattr(
        claude,
        "identity",
        lambda: {"email": "active@example.com", "organization": None},
    )
    monkeypatch.setattr(
        claude,
        "keychain_credentials",
        lambda: {"status": "ok", "_token": "desktop-token"},
    )
    monkeypatch.setattr(claude, "transcript_limit_events", lambda **kwargs: [])
    monkeypatch.setattr(
        claude,
        "session_mirror_health",
        lambda **kwargs: {"status": "absent"},
    )
    monkeypatch.setattr(
        claude,
        "accounts_report",
        lambda *args, **kwargs: pytest.fail("setup tokens must not be usage-probed"),
    )
    seen = []

    def probe(token, timeout=15):
        seen.append(token)
        return {
            "status": "ok",
            "checked_at": "2026-07-18T12:00:00-04:00",
            "five_hour": {"used_percent": 12},
            "seven_day": {"used_percent": 34},
            "windows": {},
        }

    result = snapshot.build(live=True, claude_probe_fn=probe)

    assert seen == ["desktop-token"]
    assert result["claude"]["lanes"]["enrolled"] == 2
    assert result["claude"]["lanes"]["dispatchable_now"] == 2
    assert all("probe" not in row for row in result["claude"]["accounts"])


def test_snapshot_does_not_probe_claude_without_an_active_identity(monkeypatch):
    monkeypatch.setattr(claude, "identity", lambda: {})
    monkeypatch.setattr(
        claude,
        "keychain_credentials",
        lambda: {"status": "skipped", "_token": None},
    )
    monkeypatch.setattr(claude, "transcript_limit_events", lambda **kwargs: [])
    monkeypatch.setattr(
        claude,
        "session_mirror_health",
        lambda **kwargs: {"status": "absent"},
    )

    result = snapshot.build(
        live=True,
        claude_probe_fn=lambda *args, **kwargs: pytest.fail("probe without identity"),
    )

    assert result["claude"]["oauth_probe"]["status"] == "no-identity"


def lane_snap(lanes_fleet_dict):
    """Minimal snapshot with healthy codex and a given claude lane fleet."""
    from test_pick import entry

    homes = [entry("/h/.codex-3", 5, account="b")]
    return {
        "generated_at": now_local().isoformat(timespec="seconds"),
        "codex": {
            "homes": homes,
            "duplicates": [],
            "fleet": {"total_homes": 1, "dispatchable_now": 1,
                      "best_home": "/h/.codex-3", "earliest_reset": None},
        },
        "claude": {
            "account": {"email": "anchor@example.com"},
            "known_accounts": [],
            "subscription": "max",
            "tier": "default_claude_max_20x",
            "keychain": {"status": "ok"},
            "oauth_probe": {"status": "token-invalid"},
            "recent_errors": [],
            "active_limit": None,
            "verdict": "ok",
            "lanes": lanes_fleet_dict,
        },
    }


class TestWatchdogLaneConditions:
    def test_lane_auth_failure_alerts_with_ritual(self, env_paths):
        s = lane_snap(lanes_fleet([
            lane_row("alpha@example.com", fh=10, wk=10),
            lane_row("broken@example.com", status="token-invalid"),
        ]))
        summary = watchdog.run(snap=s)
        assert "claude-lane-auth:broken@example.com" in summary["alerts_sent"]
        log = env_paths["notify_log"].read_text()
        assert "carpool enroll broken@example.com" in log

    def test_all_lanes_exhausted_warns_only_when_enrolled(self, env_paths):
        empty = lane_snap(lanes_fleet([]))
        assert "claude-lanes-empty" not in watchdog.run(snap=empty)["alerts_sent"]
        exhausted = lane_snap(lanes_fleet([lane_row("alpha@example.com", fh=99, wk=10)]))
        summary = watchdog.run(snap=exhausted)
        assert "claude-lanes-empty" in summary["alerts_sent"]

    def test_lane_auth_recovery_notice(self, env_paths):
        bad = lane_snap(lanes_fleet([lane_row("alpha@example.com", status="token-invalid")]))
        watchdog.run(snap=bad)
        good = lane_snap(lanes_fleet([lane_row("alpha@example.com", fh=10, wk=10)]))
        summary = watchdog.run(snap=good)
        assert "claude-lane-auth:alpha@example.com" in summary["recovered"]


class TestLaneRender:
    def test_table_shows_lane_rows_and_fleet_line(self, env_paths):
        from carpool import render

        s = lane_snap(lanes_fleet([
            lane_row("alpha@example.com", fh=12, wk=34),
            lane_row("broken@example.com", status="token-invalid"),
        ]))
        s["claude"]["accounts"] = [lane_row("unenrolled@example.com", enrolled=False)]
        out = render.table(s)
        assert "lanes: 1/2 dispatchable" in out
        assert "best: alpha@example.com" in out
        assert "TOKEN-INVALID" in out
        assert "12%" in out and "34%" in out
        assert "not enrolled (1)" in out
