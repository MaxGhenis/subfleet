import json
from datetime import datetime, timedelta, timezone

import pytest

from subfleet import capacity_expiry, render, snapshot, watchdog


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def lane(home, used, *, reset_hours=24, verdict="ok", duplicate_of=None):
    reset = NOW + timedelta(hours=reset_hours)
    weekly = {
        "used_percent": used,
        "window_seconds": 604800,
        "reset_at": reset.isoformat(),
    }
    return {
        "home": home,
        "email": f"{home.rsplit('-', 1)[-1]}@example.com",
        "verdict": verdict,
        "duplicate_of": duplicate_of,
        "windows": {"weekly": weekly, "secondary": weekly},
    }


def history_sample(timestamp, values, verdict="ok"):
    return {
        "ts": timestamp.isoformat(),
        "codex": {
            home: {"v": verdict, "p5h": None, "wk": used}
            for home, used in values.items()
        },
    }


def half_hour_history():
    records = []
    # Lane 1 rises 0 -> 30 over the first 18h, then 30 -> 60 in the final 6h.
    # Lane 2 stays flat, so both endpoint rates are exactly testable.
    for half_hours_ago in range(48, 0, -1):
        hours_ago = half_hours_ago / 2
        if hours_ago >= 6:
            used = (24 - hours_ago) * (30 / 18)
        else:
            used = 30 + (6 - hours_ago) * 5
        records.append(history_sample(
            NOW - timedelta(hours=hours_ago),
            {"/h/.codex-1": used, "/h/.codex-2": 20},
        ))
    return records


def test_read_history_tolerates_missing_corrupt_and_non_object_lines(tmp_path):
    missing = tmp_path / "missing.jsonl"
    assert capacity_expiry.read_history(missing) == []

    path = tmp_path / "history.jsonl"
    sample = history_sample(NOW, {"/h/.codex-1": 10})
    reset = {
        "ts": NOW.isoformat(),
        "event": "reset",
        "lane": "/h/.codex-1",
        "email": "one@example.com",
        "credit_id": "credit-1",
        "remaining": 4,
    }
    path.write_text(
        json.dumps(sample) + "\n"
        + "{not json\n"
        + json.dumps(["not", "an", "object"]) + "\n"
        + json.dumps(reset) + "\n"
    )

    assert capacity_expiry.read_history(path) == [sample, reset]


def test_analyze_computes_6h_24h_burn_projection_and_fleet_totals():
    homes = [
        lane("/h/.codex-1", 60, reset_hours=10),
        lane("/h/.codex-2", 20, reset_hours=24),
    ]

    result = capacity_expiry.analyze(homes, half_hour_history(), now=NOW)
    first = result["lanes"]["/h/.codex-1"]
    second = result["lanes"]["/h/.codex-2"]

    assert first["burn_6h_pct_per_hour"] == pytest.approx(5.0)
    assert first["burn_24h_pct_per_hour"] == pytest.approx(2.5)
    assert first["projection_rate_pct_per_hour"] == pytest.approx(5.0)
    assert first["projected_unused_percent"] == 0
    assert second["burn_6h_pct_per_hour"] == 0
    assert second["burn_24h_pct_per_hour"] == 0
    assert second["projected_unused_percent"] == 80
    assert second["expiring_unused"] is True
    assert result["windows_left"] == 1.2
    assert result["projected_unused_windows"] == 0.8
    assert result["earliest_reset_at"] == (NOW + timedelta(hours=10)).isoformat(timespec="seconds")
    assert result["expires_by"] == (NOW + timedelta(hours=24)).isoformat(timespec="seconds")
    assert result["at_risk_lanes"] == ["/h/.codex-2"]
    assert result["complete"] is True


def test_reset_events_and_downward_jumps_start_a_fresh_burn_segment():
    explicit = "/h/.codex-1"
    natural = "/h/.codex-2"
    records = [
        history_sample(NOW - timedelta(hours=6), {explicit: 90, natural: 90}),
        history_sample(NOW - timedelta(hours=5, minutes=30), {explicit: 95, natural: 95}),
        {
            "ts": (NOW - timedelta(hours=5)).isoformat(),
            "event": "reset",
            "lane": explicit,
            "email": "one@example.com",
            "credit_id": "credit-1",
            "remaining": 3,
        },
        "corrupt record supplied directly",
        ["also", "ignored"],
        history_sample(NOW - timedelta(hours=5), {explicit: 2, natural: 2}),
        history_sample(NOW - timedelta(hours=4), {explicit: 5, natural: 5}),
        history_sample(NOW - timedelta(hours=2), {explicit: 15, natural: 15}),
    ]
    homes = [lane(explicit, 30), lane(natural, 30)]

    result = capacity_expiry.analyze(homes, records, now=NOW)

    # Explicit reset keeps the sample at the reset timestamp; the other lane's
    # 95 -> 2 drop independently identifies the same fresh window.
    assert result["lanes"][explicit]["burn_6h_pct_per_hour"] == pytest.approx(5.6)
    assert result["lanes"][natural]["burn_6h_pct_per_hour"] == pytest.approx(5.6)
    assert result["lanes"][explicit]["samples_6h"] == 4
    assert result["lanes"][natural]["samples_6h"] == 4


def test_capacity_row_shape_and_untrusted_stale_samples_are_supported():
    home = "/h/.codex-7"
    current = {
        "family": "codex",
        "id": home,
        "email": "seven@example.com",
        "status": "ok",
        "weekly": {
            "used_percent": 40,
            "reset_at": (NOW + timedelta(hours=20)).isoformat(),
        },
    }
    records = [
        history_sample(NOW - timedelta(hours=6), {home: 10}),
        history_sample(NOW - timedelta(hours=3), {home: 99}, verdict="unknown"),
    ]

    result = capacity_expiry.analyze([current], records, now=NOW)
    value = result["lanes"][home]

    assert value["burn_6h_pct_per_hour"] == 5
    assert value["weekly_used_percent"] == 40
    assert value["weekly_reset_at"] == (NOW + timedelta(hours=20)).isoformat(timespec="seconds")


def test_expiring_unused_thresholds_are_strict_and_duplicates_are_excluded():
    homes = [
        lane("/h/.codex-1", 80, reset_hours=24),       # exactly 20% unused
        lane("/h/.codex-2", 79, reset_hours=72),       # exactly 72 hours
        lane("/h/.codex-3", 79, reset_hours=71.5),     # both thresholds crossed
        lane("/h/.codex-dup", 0, reset_hours=1, duplicate_of="/h/.codex-3"),
    ]
    records = [history_sample(
        NOW - timedelta(hours=1),
        {"/h/.codex-1": 80, "/h/.codex-2": 79, "/h/.codex-3": 79},
    )]

    result = capacity_expiry.analyze(homes, records, now=NOW)

    assert result["lanes"]["/h/.codex-1"]["expiring_unused"] is False
    assert result["lanes"]["/h/.codex-2"]["expiring_unused"] is False
    assert result["lanes"]["/h/.codex-3"]["expiring_unused"] is True
    assert "/h/.codex-dup" not in result["lanes"]


def test_missing_current_or_burn_data_keeps_fleet_totals_honest():
    no_usage = lane("/h/.codex-1", None)
    no_history = lane("/h/.codex-2", 25)

    result = capacity_expiry.analyze([no_usage, no_history], [], now=NOW)

    assert result["windows_left"] is None
    assert result["known_windows_left"] == 0.75
    assert result["projected_unused_windows"] is None
    assert result["known_projected_unused_windows"] == 0
    assert result["complete"] is False
    assert result["lanes"]["/h/.codex-2"]["projected_unused_percent"] is None


def test_known_projection_over_one_alerts_even_with_one_unknown_lane():
    homes = [
        lane("/h/.codex-1", 20, reset_hours=24),
        lane("/h/.codex-2", 20, reset_hours=24),
        lane("/h/.codex-3", 50, reset_hours=24),
    ]
    records = [history_sample(
        NOW - timedelta(hours=1),
        {"/h/.codex-1": 20, "/h/.codex-2": 20},
    )]

    analysis = capacity_expiry.analyze(homes, records, now=NOW)

    assert analysis["projected_unused_windows"] is None
    assert analysis["known_projected_unused_windows_raw"] == pytest.approx(1.6)
    snap = render_snapshot(homes, analysis)
    assert "codex-capacity-expiring" in {
        condition["key"] for condition in watchdog.evaluate_conditions(snap)
    }


def test_snapshot_build_attaches_current_capacity_expiry_analysis(
    env_paths,
    monkeypatch,
):
    records = [{"ts": NOW.isoformat(), "codex": {}}]
    seen = {}
    monkeypatch.setattr(capacity_expiry, "read_history", lambda: records)

    def fake_analyze(homes, supplied_records, *, now):
        seen.update({"homes": homes, "records": supplied_records, "now": now})
        return {"marker": "attached"}

    monkeypatch.setattr(capacity_expiry, "analyze", fake_analyze)

    built = snapshot.build(live=False)

    assert built["codex"]["capacity_expiry"] == {"marker": "attached"}
    assert seen["homes"] == built["codex"]["homes"]
    assert seen["records"] is records
    assert seen["now"].isoformat(timespec="seconds") == built["generated_at"]


def render_snapshot(homes, analysis=None):
    rendered_homes = []
    for value in homes:
        value = dict(value)
        windows = dict(value["windows"])
        windows.update({
            "primary": None,
            "five_hour": None,
            "source": "live",
            "as_of": NOW.isoformat(),
        })
        value.update({
            "windows": windows,
            "account_id": value["email"],
            "recent_errors": {"usage_limit": [], "auth_revoked": []},
            "probe": {"status": "ok"},
        })
        rendered_homes.append(value)
    codex = {
        "homes": rendered_homes,
        "duplicates": [],
        "fleet": {
            "total_homes": len(rendered_homes),
            "dispatchable_now": len(rendered_homes),
            "best_home": rendered_homes[0]["home"] if rendered_homes else None,
            "earliest_reset": None,
        },
    }
    if analysis is not None:
        codex["capacity_expiry"] = analysis
    return {
        "generated_at": NOW.isoformat(),
        "codex": codex,
        "claude": {
            "account": {"email": "max@example.com"},
            "accounts": [],
            "subscription": "max",
            "tier": "max",
            "keychain": {},
            "oauth_probe": {"status": "unknown"},
            "statusline": None,
            "recent_errors": [],
            "active_limit": None,
            "lanes": {},
        },
    }


def test_render_shows_burn_lane_expiry_and_complete_fleet_projection():
    homes = [
        lane("/h/.codex-1", 60, reset_hours=10),
        lane("/h/.codex-2", 20, reset_hours=24),
    ]
    analysis = capacity_expiry.analyze(homes, half_hour_history(), now=NOW)
    snap = render_snapshot(homes, analysis)

    table = render.table(snap)
    assert "burn %/h 6/24" in table
    assert "5.0/2.5" in table
    assert "0.0/0.0" in table
    assert "→ ~80% unused at reset" in table
    assert "1.2 windows left · ~0.8 projected to expire unused" in table

    brief = render.brief_md(snap)
    assert "- codex: 1.2 windows left, ~0.8 projected to expire unused by " in brief


def test_render_on_demand_analysis_and_incomplete_totals(
    monkeypatch,
):
    homes = [lane("/h/.codex-1", 25)]
    snap = render_snapshot(homes)
    monkeypatch.setattr(capacity_expiry, "read_history", lambda: [])

    table = render.table(snap)
    brief = render.brief_md(snap)

    assert "burn %/h 6/24" in table and "-/-" in table
    assert "fleet: 1/1 dispatchable" in table
    assert "windows left" not in table
    assert "projected to expire unused" not in brief
