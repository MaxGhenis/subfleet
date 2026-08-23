from subfleet import render


NOW = "2026-08-22T12:00:00+00:00"


def snapshot(*, homes=None, duplicates=None, claude=None):
    homes = homes or []
    return {
        "generated_at": NOW,
        "codex": {
            "homes": homes,
            "duplicates": duplicates or [],
            "fleet": {
                "total_homes": len(homes),
                "dispatchable_now": sum(entry["verdict"] == "ok" for entry in homes),
                "best_home": homes[0]["home"] if homes else None,
                "earliest_reset": "2026-08-22T14:00:00+00:00" if homes else None,
            },
        },
        "claude": claude
        or {
            "accounts": [],
            "active_limit": None,
            "lanes": {"enrolled": 0, "dispatchable_now": 0},
        },
    }


def codex_home(home, verdict="ok"):
    return {"home": home, "verdict": verdict}


def test_brief_summarizes_both_fleets_and_active_usage():
    snap = snapshot(
        homes=[codex_home("/tmp/home/.codex-2")],
        claude={
            "accounts": [
                {
                    "email": "active@example.com",
                    "active": True,
                    "live": {
                        "five_hour_pct": 12.5,
                        "seven_day_pct": 34.0,
                        "source": "oauth",
                        "as_of": NOW,
                    },
                }
            ],
            "active_limit": None,
            "lanes": {
                "enrolled": 2,
                "dispatchable_now": 1,
                "best": "worker@example.com",
                "earliest_reset": "2026-08-22T15:00:00+00:00",
            },
        },
    )

    output = render.brief_md(snap)

    assert output.startswith("## AI capacity\n")
    assert "codex: 1/1 lanes dispatchable" in output
    assert "best /tmp/home/.codex-2" in output
    assert "- claude: 5h 12.5%, week 34.0%" in output
    assert "claude lanes: 1/2 dispatchable" in output
    assert "best worker@example.com" in output


def test_brief_reports_codex_auth_problems_and_duplicates():
    homes = [
        codex_home("/tmp/home/.codex-2", "auth-revoked"),
        codex_home("/tmp/home/.codex-3", "no-auth"),
    ]
    snap = snapshot(
        homes=homes,
        duplicates=[{"account_id": "example", "homes": [entry["home"] for entry in homes]}],
    )

    output = render.brief_md(snap)

    assert "/tmp/home/.codex-2 auth-revoked" in output
    assert "/tmp/home/.codex-3 no-auth" in output
    assert "DUPLICATE account bindings" in output
    assert "revocation trap" in output
    assert "`subfleet login codex <N|app>`" in output


def test_brief_prefers_active_limit_over_usage():
    snap = snapshot(
        claude={
            "accounts": [
                {
                    "active": True,
                    "live": {"five_hour_pct": 10, "source": "oauth", "as_of": NOW},
                }
            ],
            "active_limit": {
                "kind": "session-limit",
                "reset_at": "2026-08-22T13:00:00+00:00",
            },
            "lanes": {"enrolled": 0, "dispatchable_now": 0},
        }
    )

    output = render.brief_md(snap)

    assert "claude: LIMITED (session-limit)" in output
    assert "claude: 5h" not in output
    assert "claude lanes" not in output


def test_brief_reports_unknown_usage_without_active_reading():
    output = render.brief_md(snapshot())

    assert "- claude: usage unknown (no fresh reading)" in output


def test_brief_demotes_stale_active_usage():
    snap = snapshot(
        claude={
            "accounts": [
                {
                    "active": True,
                    "live": {
                        "five_hour_pct": 88,
                        "seven_day_pct": 66,
                        "source": "oauth-cache",
                        "as_of": "2026-08-22T08:00:00+00:00",
                    },
                }
            ],
            "active_limit": None,
            "lanes": {"enrolled": 0, "dispatchable_now": 0},
        }
    )

    output = render.brief_md(snap)

    assert "- claude: 5h 88" not in output
    assert "last reading: 5h 88% · week 66% — stale, from oauth-cache" in output
