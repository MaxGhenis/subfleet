"""Lane ranking (claude-pick): pure ranking, fleet summary, CLI, watchdog and
render wiring. All probes are mocked — no network, no keychain."""

import json
from datetime import timedelta

import pytest

from subfleet import capacity, claude, cli, watchdog
from subfleet.claude import lane_verdict, lanes_fleet, rank_lanes
from subfleet.util import iso, now_local


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
            lane_row("a@x.com", fh=60, wk=20),
            lane_row("b@x.com", fh=5, wk=10),
        ])
        assert ranked[0]["email"] == "b@x.com"

    def test_active_handicap_spares_anchor(self):
        ranked = rank_lanes([
            lane_row("anchor@x.com", fh=10, wk=10, active=True),
            lane_row("b@x.com", fh=15, wk=15),
        ])
        # 10+10 handicap > 15, so the alternate wins despite higher raw usage.
        assert ranked[0]["email"] == "b@x.com"

    def test_no_handicap_ranks_raw(self):
        ranked = rank_lanes([
            lane_row("anchor@x.com", fh=10, wk=10, active=True),
            lane_row("b@x.com", fh=15, wk=15),
        ], handicap=0)
        assert ranked[0]["email"] == "anchor@x.com"

    def test_weekly_window_is_a_hard_gate(self):
        # Fresh 5h window does not rescue a lane through its week.
        ranked = rank_lanes([
            lane_row("a@x.com", fh=2, wk=97),
            lane_row("b@x.com", fh=50, wk=50),
        ])
        assert [r["email"] for r in ranked] == ["b@x.com"]

    def test_worst_window_drives_score(self):
        ranked = rank_lanes([
            lane_row("a@x.com", fh=10, wk=60),
            lane_row("b@x.com", fh=30, wk=20),
        ])
        # effective a=60, b=30.
        assert ranked[0]["email"] == "b@x.com"

    def test_weekly_tiebreak(self):
        ranked = rank_lanes([
            lane_row("a@x.com", fh=30, wk=10),
            lane_row("b@x.com", fh=30, wk=30),
        ])
        # equal effective usage (30) -> lower weekly wins.
        assert ranked[0]["email"] == "a@x.com"

    def test_email_tiebreak_is_deterministic(self):
        ranked = rank_lanes([
            lane_row("b@x.com", fh=10, wk=10),
            lane_row("a@x.com", fh=10, wk=10),
        ])
        assert [r["email"] for r in ranked] == ["a@x.com", "b@x.com"]

    def test_dead_and_unenrolled_excluded(self):
        ranked = rank_lanes([
            lane_row("a@x.com", enrolled=False),
            lane_row("b@x.com", status="token-invalid"),
            lane_row("c@x.com", status="secret-missing"),
            lane_row("d@x.com", status="rate-limited"),
            lane_row("e@x.com", fh=98, wk=10),  # under 5% headroom
            lane_row("f@x.com", status="ok"),   # probe ok, no window fields
        ])
        assert ranked == []

    def test_single_window_lane_still_ranks(self):
        # Server omitted seven_day: rank on what was reported, never fabricate.
        ranked = rank_lanes([lane_row("a@x.com", fh=20)])
        assert ranked[0]["email"] == "a@x.com"
        assert ranked[0]["weekly_used_percent"] is None


class TestVerdicts:
    def test_verdict_labels(self):
        assert lane_verdict(lane_row("a@x.com", enrolled=False)) == "not-enrolled"
        assert lane_verdict(lane_row("a@x.com", status="token-invalid")) == "token-invalid"
        assert lane_verdict(lane_row("a@x.com", status="secret-missing")) == "secret-missing"
        assert lane_verdict(lane_row("a@x.com", status="ok")) == "no-window-data"
        assert lane_verdict(lane_row("a@x.com", fh=99, wk=10)) == "exhausted"
        assert lane_verdict(lane_row("a@x.com", fh=10, wk=99)) == "exhausted"
        assert lane_verdict(lane_row("a@x.com", fh=10, wk=10)) == "ok"


class TestLanesFleet:
    def test_summary_counts_and_best(self):
        fleet = lanes_fleet([
            lane_row("a@x.com", fh=10, wk=10),
            lane_row("b@x.com", fh=99, wk=10),
            lane_row("c@x.com", enrolled=False),
        ])
        assert fleet["enrolled"] == 2
        assert fleet["dispatchable_now"] == 1
        assert fleet["best"] == "a@x.com"
        assert [l["verdict"] for l in fleet["lanes"]] == ["ok", "exhausted"]

    def test_earliest_reset_uses_governing_window(self):
        soon = iso(now_local() + timedelta(hours=1))
        later = iso(now_local() + timedelta(hours=3))
        much_later = iso(now_local() + timedelta(days=2))
        fleet = lanes_fleet([
            # 5h exhausted only: governed by the 5h reset (soon).
            lane_row("a@x.com", fh=99, wk=50, fh_reset=soon, wk_reset=much_later),
            # both windows exhausted: usable only when BOTH reset (the later one).
            lane_row("b@x.com", fh=99, wk=99, fh_reset=later, wk_reset=much_later),
        ])
        assert fleet["dispatchable_now"] == 0
        assert fleet["earliest_reset"] == soon
        by = {l["email"]: l for l in fleet["lanes"]}
        assert by["a@x.com"]["reset_at"] == soon
        assert by["b@x.com"]["reset_at"] == much_later

    def test_empty_roster(self):
        fleet = lanes_fleet([])
        assert fleet == {"enrolled": 0, "dispatchable_now": 0, "best": None,
                         "earliest_reset": None, "lanes": []}


def capacity_lane(email, fh=None, wk=None, *, fh_tokens=0, wk_tokens=0,
                  enrolled=True, active=False, status=None, fh_reset=None,
                  wk_reset=None, limited_until=None, confidence="observed",
                  in_flight=0, cooldowns=None, model_windows=None,
                  scoped_limits=None):
    percentages = [value for value in (fh, wk) if value is not None]
    headroom = 100.0 - max(percentages) if percentages else None
    if status is None:
        if not enrolled:
            status = "not-enrolled"
        elif headroom is not None and headroom < capacity.DEFAULT_MIN_HEADROOM:
            status = "exhausted"
        else:
            status = "ok"
    if status == "exhausted" and limited_until is None:
        governing = [
            reset for value, reset in ((fh, fh_reset), (wk, wk_reset))
            if value is not None and value >= 100.0 - capacity.DEFAULT_MIN_HEADROOM and reset
        ]
        limited_until = max(governing) if governing else None
    dispatchable = enrolled and status == "ok"
    effective_headroom = 0.0 if status in {"limited", "exhausted"} else headroom
    row = {
        "family": "claude",
        "id": email,
        "email": email,
        "active": active,
        "enrolled": enrolled,
        "five_hour": {
            "used_percent": fh,
            "tokens": fh_tokens,
            "capacity": None,
            "reset_at": fh_reset,
            "confidence": confidence,
        },
        "weekly": {
            "used_percent": wk,
            "tokens": wk_tokens,
            "capacity": None,
            "reset_at": wk_reset,
            "confidence": confidence,
        },
        "learned_capacity": None,
        "limited_until": limited_until,
        "confidence": confidence,
        "status": status,
        "dispatchable": dispatchable,
        "headroom_score": effective_headroom,
        "dispatch_score": (
            effective_headroom - (capacity.INTERACTIVE_HANDICAP if active else 0.0)
            if effective_headroom is not None else None
        ),
        "in_flight": in_flight,
    }
    if any(value is not None for value in (cooldowns, model_windows, scoped_limits)):
        row["cooldowns"] = dict(cooldowns or {})
        row["model_cooldowns"] = {
            scope: until
            for scope, until in row["cooldowns"].items()
            if scope != capacity.ACCOUNT_COOLDOWN_SCOPE
        }
        row["model_windows"] = dict(model_windows or {})
        row["scoped_limits"] = list(scoped_limits or [])
        row["model_states"] = capacity.claude_model_states(row)
    return row


class TestClaudePickCli:
    def _patch_rows(self, monkeypatch, rows):
        data = {
            "generated_at": "2026-07-18T12:00:00-04:00",
            "cache": {"hit": True, "ttl_seconds": 120},
            "accounts": rows,
            "families": capacity.family_summaries(rows),
        }
        monkeypatch.setattr(capacity, "report", lambda: data)
        monkeypatch.setattr(
            claude, "accounts_report",
            lambda *args, **kwargs: pytest.fail("setup-token usage probe must not run"),
        )

    def test_best_email_on_stdout(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [
            capacity_lane("a@x.com", fh=40, wk=30),
            capacity_lane("b@x.com", fh=10, wk=10),
        ])
        rc = cli.main(["pick", "claude"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out.strip() == "b@x.com"
        assert "5h 10%" in out.err

    def test_no_lane_exits_1_with_earliest_reset(self, env_paths, monkeypatch, capsys):
        soon = iso(now_local() + timedelta(hours=2))
        self._patch_rows(monkeypatch, [
            capacity_lane("a@x.com", fh=99, wk=10, fh_reset=soon)
        ])
        rc = cli.main(["pick", "claude"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "no dispatchable claude lane" in err
        assert soon in err

    def test_zero_enrolled_prints_ritual(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [capacity_lane("a@x.com", enrolled=False)])
        rc = cli.main(["pick", "claude"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "claude setup-token" in err
        assert "subfleet enroll" in err

    def test_json_ranking_and_exclusions(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [
            capacity_lane("a@x.com", fh=10, wk=10),
            capacity_lane("b@x.com", fh=20, wk=20),
            capacity_lane("c@x.com", status="token-invalid"),
        ])
        rc = cli.main(["pick", "claude", "--json", "--all"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["best"] == "a@x.com"
        assert [r["email"] for r in out["ranked"]] == ["a@x.com", "b@x.com"]
        assert out["excluded"] == [{"email": "c@x.com", "verdict": "token-invalid", "reset_at": None}]
        assert out["enrolled"] == 3

    def test_handicap_flag_wiring(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [
            capacity_lane("anchor@x.com", fh=10, wk=10, active=True),
            capacity_lane("b@x.com", fh=15, wk=15),
        ])
        assert cli.main(["pick", "claude"]) == 0
        assert capsys.readouterr().out.strip() == "b@x.com"
        assert cli.main(["pick", "claude", "--no-handicap"]) == 0
        assert capsys.readouterr().out.strip() == "anchor@x.com"

    def test_cached_flag_uses_same_capacity_surface(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [capacity_lane("cached@x.com", fh=5, wk=5)])
        rc = cli.main(["pick", "claude", "--cached"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "cached@x.com"

    def test_hidden_exclusion_selects_next_lane(self, env_paths, monkeypatch, capsys):
        self._patch_rows(monkeypatch, [
            capacity_lane("first@x.com", fh=5, wk=5),
            capacity_lane("second@x.com", fh=10, wk=10),
        ])
        rc = cli.main(["pick", "claude", "--exclude", "first@x.com"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "second@x.com"

    def test_in_flight_is_only_an_equal_headroom_tiebreak(
        self, env_paths, monkeypatch, capsys
    ):
        self._patch_rows(monkeypatch, [
            capacity_lane("busy@x.com", fh=10, wk=10, in_flight=2),
            capacity_lane("idle@x.com", fh=10, wk=10, in_flight=0),
        ])
        assert cli.main(["pick", "claude"]) == 0
        assert capsys.readouterr().out.strip() == "idle@x.com"

        self._patch_rows(monkeypatch, [
            capacity_lane("busy@x.com", fh=5, wk=5, in_flight=2),
            capacity_lane("idle@x.com", fh=10, wk=10, in_flight=0),
        ])
        assert cli.main(["pick", "claude"]) == 0
        assert capsys.readouterr().out.strip() == "busy@x.com"

    def test_uncalibrated_inference_lane_ranks_by_estimated_tokens(
        self, env_paths, monkeypatch, capsys
    ):
        self._patch_rows(monkeypatch, [
            capacity_lane(
                "estimated@x.com", fh_tokens=123, wk_tokens=456,
                confidence="estimated",
            )
        ])
        rc = cli.main(["pick", "claude"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out.strip() == "estimated@x.com"
        assert "5h 123 tok" in out.err
        assert "[estimated]" in out.err

    @pytest.mark.parametrize(
        ("requested_model", "canonical_model", "expected"),
        [
            ("fable", "claude-fable-5-1", "opus-cooled@x.com"),
            ("claude-fable-5-1", "claude-fable-5-1", "opus-cooled@x.com"),
            ("opus", "claude-opus-5", "fable-cooled@x.com"),
            ("claude-opus-5", "claude-opus-5", "fable-cooled@x.com"),
        ],
    )
    def test_model_aliases_and_full_ids_filter_only_matching_cooldowns(
        self, env_paths, monkeypatch, capsys,
        requested_model, canonical_model, expected,
    ):
        until = iso(now_local() + timedelta(days=2))
        self._patch_rows(monkeypatch, [
            capacity_lane(
                "account-cooled@x.com", fh=1, wk=1,
                limited_until=until, cooldowns={"*": until},
            ),
            capacity_lane(
                "fable-cooled@x.com", fh=2, wk=2,
                cooldowns={"claude-fable-5-1": until},
            ),
            capacity_lane(
                "opus-cooled@x.com", fh=3, wk=3,
                cooldowns={"claude-opus-5": until},
            ),
            capacity_lane("healthy@x.com", fh=20, wk=20),
        ])

        rc = cli.main(["pick", "claude", "--model", requested_model, "--json", "--all"])
        out = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert out["model"] == canonical_model
        assert out["best"] == expected
        assert "account-cooled@x.com" not in {
            row["email"] for row in out["ranked"]
        }

    def test_default_pick_preserves_whole_lane_exclusion_for_any_cooldown(
        self, env_paths, monkeypatch, capsys
    ):
        until = iso(now_local() + timedelta(days=2))
        self._patch_rows(monkeypatch, [
            capacity_lane(
                "fable-cooled@x.com", fh=1, wk=1,
                cooldowns={"claude-fable-5-1": until},
            ),
            capacity_lane(
                "opus-cooled@x.com", fh=2, wk=2,
                cooldowns={"claude-opus-5": until},
            ),
            capacity_lane("healthy@x.com", fh=30, wk=30),
        ])

        rc = cli.main(["pick", "claude", "--json", "--all"])
        out = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert out["best"] == "healthy@x.com"
        assert [row["email"] for row in out["ranked"]] == ["healthy@x.com"]
        assert {row["email"] for row in out["excluded"]} == {
            "fable-cooled@x.com", "opus-cooled@x.com",
        }

    def test_server_fable_limit_does_not_block_opus_pick(
        self, env_paths, monkeypatch, capsys
    ):
        reset = iso(now_local() + timedelta(days=2))
        fable_limit = {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 100,
            "severity": "critical",
            "resets_at": reset,
            "is_active": True,
            "scope_model": "Fable",
            "scope_surface": None,
        }
        self._patch_rows(monkeypatch, [
            capacity_lane(
                "lane@x.com", fh=10, wk=10, scoped_limits=[fable_limit],
            )
        ])

        assert cli.main(["pick", "claude", "--model", "fable"]) == 1
        fable_out = capsys.readouterr()
        assert "no dispatchable claude lane" in fable_out.err

        assert cli.main(["pick", "claude", "--model", "opus"]) == 0
        assert capsys.readouterr().out.strip() == "lane@x.com"

    def test_model_weekly_bucket_controls_ranking_and_headroom(
        self, env_paths, monkeypatch, capsys
    ):
        self._patch_rows(monkeypatch, [
            capacity_lane(
                "a@x.com", fh=10, wk=10,
                model_windows={
                    "fable": {"used_percent": 90, "reset_at": None},
                    "opus": {"used_percent": 10, "reset_at": None},
                },
            ),
            capacity_lane(
                "b@x.com", fh=20, wk=20,
                model_windows={
                    "fable": {"used_percent": 20, "reset_at": None},
                    "opus": {"used_percent": 90, "reset_at": None},
                },
            ),
        ])

        assert cli.main([
            "pick", "claude", "--model", "fable", "--json", "--all",
        ]) == 0
        fable = json.loads(capsys.readouterr().out)
        assert [row["email"] for row in fable["ranked"]] == ["b@x.com", "a@x.com"]
        assert {row["email"]: row["headroom_score"] for row in fable["ranked"]} == {
            "b@x.com": 80.0,
            "a@x.com": 10.0,
        }

        assert cli.main([
            "pick", "claude", "--model", "claude-opus-5", "--json", "--all",
        ]) == 0
        opus = json.loads(capsys.readouterr().out)
        assert [row["email"] for row in opus["ranked"]] == ["a@x.com", "b@x.com"]
        assert {row["email"]: row["headroom_score"] for row in opus["ranked"]} == {
            "a@x.com": 90.0,
            "b@x.com": 10.0,
        }

    def test_noncritical_scoped_percent_controls_model_ranking(
        self, env_paths, monkeypatch, capsys
    ):
        def limit(percent, severity):
            return {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": percent,
                "severity": severity,
                "resets_at": None,
                "is_active": True,
                "scope_model": "Fable",
                "scope_surface": None,
            }

        self._patch_rows(monkeypatch, [
            capacity_lane(
                "generic-best@x.com", fh=10, wk=10,
                scoped_limits=[limit(90, "warning")],
            ),
            capacity_lane(
                "fable-best@x.com", fh=20, wk=20,
                scoped_limits=[limit(20, "normal")],
            ),
        ])

        assert cli.main([
            "pick", "claude", "--model", "fable", "--json", "--all",
        ]) == 0
        result = json.loads(capsys.readouterr().out)
        assert [row["email"] for row in result["ranked"]] == [
            "fable-best@x.com", "generic-best@x.com",
        ]
        assert [row["headroom_score"] for row in result["ranked"]] == [80, 10]

    def test_model_pick_honors_lowered_minimum_headroom(
        self, env_paths, monkeypatch, capsys
    ):
        self._patch_rows(monkeypatch, [
            capacity_lane(
                "low-opus@x.com", fh=10, wk=10,
                model_windows={
                    "opus": {"used_percent": 97, "reset_at": None},
                },
            ),
        ])

        assert cli.main([
            "pick", "claude", "--model", "opus", "--min-headroom", "0",
        ]) == 0
        assert capsys.readouterr().out.strip() == "low-opus@x.com"

    def test_model_data_does_not_revive_unknown_global_exhaustion(
        self, env_paths, monkeypatch, capsys
    ):
        self._patch_rows(monkeypatch, [
            capacity_lane(
                "globally-exhausted@x.com", fh=None, wk=None,
                status="exhausted",
                model_windows={
                    "opus": {"used_percent": 20, "reset_at": None},
                },
            ),
        ])

        assert cli.main([
            "pick", "claude", "--model", "opus", "--min-headroom", "0",
        ]) == 1
        output = capsys.readouterr()
        assert "globally-exhausted@x.com exhausted" in output.err

    def test_lowered_headroom_never_overrides_exact_model_cooldown(
        self, env_paths, monkeypatch, capsys
    ):
        until = iso(now_local() + timedelta(days=1))
        self._patch_rows(monkeypatch, [
            capacity_lane(
                "cooled-opus@x.com", fh=97, wk=97,
                cooldowns={"claude-opus-5": until},
            ),
        ])

        assert cli.main([
            "pick", "claude", "--model", "opus", "--min-headroom", "0",
        ]) == 1
        output = capsys.readouterr()
        assert "cooled-opus@x.com cooled" in output.err

    def test_json_surfaces_per_model_lane_states(
        self, env_paths, monkeypatch, capsys
    ):
        until = iso(now_local() + timedelta(days=2))
        self._patch_rows(monkeypatch, [
            capacity_lane(
                "lane@x.com", fh=10, wk=10,
                cooldowns={"claude-fable-5-1": until},
            )
        ])

        rc = cli.main([
            "pick", "claude", "--model", "opus", "--json", "--all",
        ])
        out = json.loads(capsys.readouterr().out)
        states = out["ranked"][0]["model_states"]

        assert rc == 0
        assert states["claude-fable-5-1"]["state"] == "cooled"
        assert states["claude-fable-5-1"]["until"] == until
        assert states["claude-opus-5"]["state"] == "ok"
        assert states["claude-opus-5"]["until"] is None


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
            "account": {"email": "anchor@x.com"},
            "known_accounts": [],
            "subscription": "max",
            "tier": "default_claude_max_20x",
            "keychain": {"status": "ok"},
            "oauth_probe": {"status": "token-invalid"},
            "statusline": None,
            "recent_errors": [],
            "active_limit": None,
            "verdict": "ok",
            "lanes": lanes_fleet_dict,
        },
    }


class TestWatchdogLaneConditions:
    def test_lane_auth_failure_alerts_with_ritual(self, env_paths):
        s = lane_snap(lanes_fleet([
            lane_row("a@x.com", fh=10, wk=10),
            lane_row("bad@x.com", status="token-invalid"),
        ]))
        summary = watchdog.run(snap=s)
        assert "claude-lane-auth:bad@x.com" in summary["alerts_sent"]
        log = env_paths["notify_log"].read_text()
        assert "subfleet enroll bad@x.com" in log

    def test_all_lanes_exhausted_warns_only_when_enrolled(self, env_paths):
        empty = lane_snap(lanes_fleet([]))
        assert "claude-lanes-empty" not in watchdog.run(snap=empty)["alerts_sent"]
        exhausted = lane_snap(lanes_fleet([lane_row("a@x.com", fh=99, wk=10)]))
        summary = watchdog.run(snap=exhausted)
        assert "claude-lanes-empty" in summary["alerts_sent"]

    def test_lane_auth_recovery_notice(self, env_paths):
        bad = lane_snap(lanes_fleet([lane_row("a@x.com", status="token-invalid")]))
        watchdog.run(snap=bad)
        good = lane_snap(lanes_fleet([lane_row("a@x.com", fh=10, wk=10)]))
        summary = watchdog.run(snap=good)
        assert "claude-lane-auth:a@x.com" in summary["recovered"]


class TestLaneRender:
    def test_table_shows_lane_rows_and_fleet_line(self, env_paths):
        from subfleet import render

        s = lane_snap(lanes_fleet([
            lane_row("a@x.com", fh=12, wk=34),
            lane_row("bad@x.com", status="token-invalid"),
        ]))
        s["claude"]["accounts"] = [lane_row("un@x.com", enrolled=False)]
        out = render.table(s)
        assert "lanes: 1/2 dispatchable" in out
        assert "best: a@x.com" in out
        assert "TOKEN-INVALID" in out
        assert "12%" in out and "34%" in out
        assert "not enrolled (1)" in out

    def test_brief_includes_lane_line(self, env_paths):
        from subfleet import render

        s = lane_snap(lanes_fleet([lane_row("a@x.com", fh=12, wk=34)]))
        brief = render.brief_md(s)
        assert "claude lanes: 1/1 dispatchable" in brief
        assert "best a@x.com" in brief

    def test_brief_omits_lane_line_when_none_enrolled(self, env_paths):
        from subfleet import render

        brief = render.brief_md(lane_snap(lanes_fleet([])))
        assert "claude lanes" not in brief
