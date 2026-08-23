"""The desktop app home is observed, but never used as a dispatch lane."""

import json

from subfleet import codex, config, paths, render, snapshot, watchdog
from subfleet.util import now_local
from conftest import make_auth_json, wham_ok


def bind(home, account_id, email):
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(make_auth_json(account_id, email)))
    return home


def update_config(**changes):
    current = config.load()
    current.update(changes)
    config.save(current)


def codex_entry(home, used, account, verdict="ok", *, email=None):
    primary = {
        "used_percent": used,
        "window_seconds": 18_000,
        "reset_at": "2099-01-01T12:00:00+00:00",
    }
    secondary = {
        "used_percent": 20,
        "window_seconds": 604_800,
        "reset_at": "2099-01-07T12:00:00+00:00",
    }
    return {
        "home": home,
        "is_primary_home": False,
        "account_id": account,
        "email": email or f"{account.lower()}@example.com",
        "plan": "free" if verdict == "free-plan" else "pro",
        "auth_last_refresh": None,
        "verdict": verdict,
        "probe": {"status": "ok"},
        "windows": {
            "primary": primary,
            "secondary": secondary,
            # Keep the semantic aliases present while the public snapshot
            # schema migrates away from API-position names.
            "five_hour": primary,
            "weekly": secondary,
            "source": "live",
            "as_of": now_local().isoformat(timespec="seconds"),
        },
        "rollout_observed": None,
        "recent_errors": {"usage_limit": [], "auth_revoked": []},
        "duplicate_of": None,
        "shadowed_by_app": False,
    }


def healthy_claude_section(*, mirror=None):
    lane = {
        "email": "worker@example.com",
        "active": False,
        "verdict": "ok",
        "five_hour_used_percent": 10,
        "five_hour_reset_at": "2099-01-01T12:00:00+00:00",
        "weekly_used_percent": 20,
    }
    return {
        "account": {"email": "active@example.com"},
        "accounts": [],
        "known_accounts": [],
        "subscription": "paid",
        "tier": "paid",
        "keychain": {"status": "ok"},
        "oauth_probe": {"status": "token-invalid"},
        "statusline": None,
        "session_mirror": mirror,
        "recent_errors": [],
        "active_limit": None,
        "verdict": "ok",
        "lanes": {
            "enrolled": 1,
            "dispatchable_now": 1,
            "best": lane["email"],
            "earliest_reset": None,
            "lanes": [lane],
        },
    }


def snapshot_document(homes, *, app_home=None, dispatchable=None):
    n_ok = sum(
        1
        for entry in homes
        if entry["verdict"] == "ok" and not entry.get("duplicate_of")
    )
    return {
        "generated_at": now_local().isoformat(timespec="seconds"),
        "codex": {
            "homes": homes,
            "duplicates": [],
            "app_home": app_home
            or {"home": "/app/.codex", "status": "missing", "shadows": []},
            "fleet": {
                "total_homes": len(homes),
                "dispatchable_now": n_ok if dispatchable is None else dispatchable,
                "best_home": next(
                    (entry["home"] for entry in homes if entry["verdict"] == "ok"),
                    None,
                ),
                "earliest_reset": None,
            },
        },
        "claude": healthy_claude_section(),
    }


class TestLaneDiscovery:
    def test_app_home_is_not_a_numbered_lane(self, monkeypatch, tmp_path):
        fake_home = tmp_path / "home"
        for name in (".codex", ".codex-1", ".codex-2", ".codex-5", ".codex-backup"):
            (fake_home / name).mkdir(parents=True)
        monkeypatch.setattr(paths, "HOME", fake_home)
        monkeypatch.delenv("SUBFLEET_CODEX_HOMES", raising=False)
        monkeypatch.delenv("SUBFLEET_CODEX_APP_HOME", raising=False)
        update_config(codex_homes=None, codex_app_home=None)

        assert [path.name for path in paths.codex_homes()] == [
            ".codex-1",
            ".codex-2",
            ".codex-5",
        ]
        assert paths.app_codex_home() == fake_home / ".codex"
        assert paths.primary_codex_home() == fake_home / ".codex-1"

    def test_empty_override_means_no_lanes(self, monkeypatch):
        monkeypatch.setenv("SUBFLEET_CODEX_HOMES", "")
        assert paths.codex_homes() == []

    def test_explicit_config_cannot_turn_app_home_into_a_lane(self, tmp_path, monkeypatch):
        app = tmp_path / ".codex"
        lane = tmp_path / ".codex-1"
        update_config(
            codex_app_home=str(app),
            codex_homes=[str(lane), str(app), str(lane)],
        )
        monkeypatch.delenv("SUBFLEET_CODEX_APP_HOME", raising=False)

        assert paths.codex_homes() == [lane]

    def test_app_home_env_overrides_public_config(self, monkeypatch, tmp_path):
        configured = tmp_path / "configured-app-home"
        overridden = tmp_path / "environment-app-home"
        update_config(codex_app_home=str(configured))
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(overridden))
        assert paths.app_codex_home() == overridden


class TestProtectedAccount:
    def test_live_app_identity_beats_config_fallback(self, monkeypatch, tmp_path):
        app = bind(tmp_path / "app-home", "ACCT-APP", "app@example.com")
        update_config(
            codex_app_home=str(app),
            protected_account={"email": "fallback@example.com"},
        )
        monkeypatch.delenv("SUBFLEET_CODEX_APP_HOME", raising=False)

        protected = codex.protected_account()

        assert protected["source"] == "app-home"
        assert codex.is_protected_account("APP@example.com", None, protected)
        assert codex.is_protected_account(None, "acct-app", protected)
        assert not codex.is_protected_account("fallback@example.com", None, protected)

    def test_public_config_is_fallback_when_app_identity_is_missing(
        self, monkeypatch, tmp_path
    ):
        update_config(
            codex_app_home=str(tmp_path / "missing-app-home"),
            protected_account={
                "email": "fallback@example.com",
                "account_id": "ACCT-FALLBACK",
            },
        )
        monkeypatch.delenv("SUBFLEET_CODEX_APP_HOME", raising=False)

        protected = codex.protected_account()

        assert protected["source"] == "config"
        assert codex.is_protected_account("fallback@example.com", None, protected)
        assert codex.is_protected_account(None, "acct-fallback", protected)

    def test_app_identity_is_token_free(self, monkeypatch, tmp_path):
        app = bind(tmp_path / "app-home", "ACCT-APP", "app@example.com")
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(app))

        identity = codex.app_home_identity()

        assert identity["email"] == "app@example.com"
        assert not any(key.startswith("_") for key in identity)
        assert "rt-secret" not in json.dumps(identity)


class TestSnapshotShadowing:
    @staticmethod
    def fleet(monkeypatch, tmp_path, app_account):
        lane_one = bind(tmp_path / ".codex-1", "ACCT-1", "one@example.com")
        lane_two = bind(tmp_path / ".codex-2", "ACCT-2", "two@example.com")
        update_config(codex_homes=[str(lane_one), str(lane_two)])
        app = bind(
            tmp_path / "app-home",
            app_account,
            f"{app_account.lower()}@example.com",
        )
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(app))
        return snapshot.build(live=False)

    def test_matching_lane_is_shadowed_and_app_is_not_a_duplicate(
        self, env_paths, monkeypatch, tmp_path
    ):
        result = self.fleet(monkeypatch, tmp_path, "ACCT-2")
        homes = {entry["home"].rsplit("/", 1)[-1]: entry for entry in result["codex"]["homes"]}

        assert set(homes) == {".codex-1", ".codex-2"}
        assert homes[".codex-2"]["shadowed_by_app"] is True
        assert homes[".codex-1"]["shadowed_by_app"] is False
        assert result["codex"]["app_home"]["account_id"] == "ACCT-2"
        assert [
            home.rsplit("/", 1)[-1]
            for home in result["codex"]["app_home"]["shadows"]
        ] == [".codex-2"]
        assert result["codex"]["duplicates"] == []
        assert all(entry["duplicate_of"] is None for entry in result["codex"]["homes"])

    def test_non_lane_app_account_shadows_nothing(
        self, env_paths, monkeypatch, tmp_path
    ):
        result = self.fleet(monkeypatch, tmp_path, "ACCT-9")
        assert result["codex"]["app_home"]["shadows"] == []
        assert not any(entry["shadowed_by_app"] for entry in result["codex"]["homes"])

    def test_shadowed_account_is_handicapped_in_dispatch(
        self, monkeypatch, tmp_path
    ):
        app = bind(tmp_path / "app-home", "ACCT-2", "two@example.com")
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(app))
        lane_one = codex_entry("/lanes/.codex-1", 15, "ACCT-1")
        lane_two = codex_entry("/lanes/.codex-2", 10, "ACCT-2")

        ranked = snapshot.rank_for_dispatch([lane_one, lane_two])

        assert ranked[0]["home"] == "/lanes/.codex-1"


def shadow_snapshot(app_email="two@example.com", shadows=("/lanes/.codex-2",)):
    homes = [
        codex_entry("/lanes/.codex-1", 15, "ACCT-1"),
        codex_entry("/lanes/.codex-2", 10, "ACCT-2"),
    ]
    for entry in homes:
        entry["shadowed_by_app"] = entry["home"] in shadows
    app_home = {
        "home": "/app/.codex",
        "status": "ok",
        "account_id": "ACCT-2",
        "email": app_email,
        "plan": "pro",
        "auth_last_refresh": None,
        "shadows": list(shadows),
    }
    return snapshot_document(homes, app_home=app_home)


class TestWatchdogShadow:
    def test_shadow_alert_is_transition_only(self, env_paths):
        current = shadow_snapshot()

        first = watchdog.run(snap=current)
        second = watchdog.run(snap=current)

        assert "codex-app-shadow:/lanes/.codex-2" in first["alerts_sent"]
        assert "codex-app-shadow:/lanes/.codex-2" not in second["alerts_sent"]
        notice = env_paths["notify_log"].read_text()
        assert "two@example.com" in notice
        assert "subfleet login codex" in notice

    def test_moving_app_account_alerts_new_lane_without_recovery_noise(self, env_paths):
        watchdog.run(snap=shadow_snapshot())
        moved = shadow_snapshot(
            app_email="one@example.com", shadows=("/lanes/.codex-1",)
        )
        moved["codex"]["app_home"]["account_id"] = "ACCT-1"

        summary = watchdog.run(snap=moved)

        assert "codex-app-shadow:/lanes/.codex-1" in summary["alerts_sent"]
        assert not any(key.startswith("codex-app-shadow") for key in summary["recovered"])
        assert "recovered: codex-app-shadow" not in env_paths["notify_log"].read_text()

    def test_no_shadow_is_silent(self, env_paths):
        current = shadow_snapshot(shadows=())
        current["codex"]["app_home"]["account_id"] = "ACCT-9"
        summary = watchdog.run(snap=current)
        assert not any(key.startswith("codex-app-shadow") for key in summary["alerts_sent"])


class TestRenderAppLine:
    def test_table_shows_app_identity_and_shadow(self, env_paths):
        output = render.table(shadow_snapshot())
        assert "app" in output
        assert "two@example.com" in output
        assert "lane shadowed" in output

    def test_table_shows_missing_app_login(self, env_paths):
        current = shadow_snapshot(shadows=())
        current["codex"]["app_home"] = {
            "home": "/app/.codex",
            "status": "missing",
            "shadows": [],
        }
        assert "(no login)" in render.table(current)


class TestFreePlanGuard:
    def test_free_plan_verdict_and_ranking_exclusion(self):
        verdict = getattr(snapshot, "codex_verdict", snapshot._codex_verdict)
        free_probe = wham_ok(used=0, weekly=0, email="free@example.com")
        free_probe["plan_type"] = "free"

        assert verdict({"status": "ok"}, free_probe, None) == "free-plan"
        assert verdict({"status": "ok"}, wham_ok(), None) == "ok"

        free_lane = codex_entry(
            "/lanes/.codex-6", 0, "ACCT-6", "free-plan", email="free@example.com"
        )
        paid_lane = codex_entry("/lanes/.codex-2", 40, "ACCT-2")
        assert [
            row["home"] for row in snapshot.rank_for_dispatch([free_lane, paid_lane])
        ] == ["/lanes/.codex-2"]

    def test_watchdog_alerts_and_recovers(self, env_paths):
        free_lane = codex_entry(
            "/lanes/.codex-6", 0, "ACCT-6", "free-plan", email="free@example.com"
        )
        paid_lane = codex_entry("/lanes/.codex-2", 40, "ACCT-2")

        first = watchdog.run(snap=snapshot_document([free_lane, paid_lane]))

        assert "codex-free-plan:/lanes/.codex-6" in first["alerts_sent"]
        notice = env_paths["notify_log"].read_text().lower()
        assert "free@example.com" in notice
        assert "paid" in notice or "pro" in notice
        assert "subfleet login codex" in notice

        upgraded = snapshot_document(
            [
                codex_entry("/lanes/.codex-6", 0, "ACCT-6", email="free@example.com"),
                paid_lane,
            ]
        )
        assert "codex-free-plan:/lanes/.codex-6" in watchdog.run(
            snap=upgraded
        )["recovered"]

    def test_table_labels_free_plan(self, env_paths):
        free_lane = codex_entry(
            "/lanes/.codex-6", 0, "ACCT-6", "free-plan", email="free@example.com"
        )
        assert "FREE-PLAN" in render.table(snapshot_document([free_lane]))
