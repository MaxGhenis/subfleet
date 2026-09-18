"""~/.codex is the ChatGPT/Codex desktop app's home, not a dispatch lane
(2026-08-19). It is observed for identity: the lane bound to the same account
is "shadowed" (watchdog-named) because two token copies of one
account revoke each other on refresh."""

import json
from datetime import datetime, timedelta

from subfleet import codex, paths, snapshot, watchdog
from conftest import make_auth_json


def bind(home, account_id, email):
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(make_auth_json(account_id, email)))
    return home


class TestLaneDiscovery:
    def test_app_home_is_not_a_lane(self, monkeypatch, tmp_path):
        fake_home = tmp_path / "home"
        for name in (".codex", ".codex-1", ".codex-2", ".codex-5"):
            (fake_home / name).mkdir(parents=True)
        monkeypatch.setattr(paths, "HOME", fake_home)
        monkeypatch.delenv("SUBFLEET_CODEX_HOMES", raising=False)
        monkeypatch.delenv("SUBFLEET_CODEX_APP_HOME", raising=False)
        lanes = [p.name for p in paths.codex_homes()]
        assert lanes == [".codex-1", ".codex-2", ".codex-5"]
        assert paths.app_codex_home() == fake_home / ".codex"
        assert paths.primary_codex_home() == fake_home / ".codex-1"

    def test_empty_override_means_no_lanes(self, monkeypatch):
        monkeypatch.setenv("SUBFLEET_CODEX_HOMES", "")
        assert paths.codex_homes() == []


class TestProtectedAccount:
    def test_app_home_identity_beats_config(self, monkeypatch, tmp_path):
        app = bind(tmp_path / "app", "ACCT-APP", "app@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(app))
        cfg = tmp_path / "codex-accounts.json"
        cfg.write_text(json.dumps({"protected_account": {"email": "config@x.com"}}))
        monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(cfg))
        p = codex.protected_account()
        assert p["source"] == "app-home"
        assert codex.is_protected_account("app@x.com", None, p)
        assert codex.is_protected_account(None, "acct-app", p)
        assert not codex.is_protected_account("config@x.com", None, p)

    def test_config_is_the_fallback_when_app_home_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(tmp_path / "absent"))
        cfg = tmp_path / "codex-accounts.json"
        cfg.write_text(json.dumps({"protected_account": {"email": "config@x.com"}}))
        monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(cfg))
        p = codex.protected_account()
        assert p["source"] == "config"
        assert codex.is_protected_account("config@x.com", None, p)

    def test_identity_is_token_free(self, monkeypatch, tmp_path):
        app = bind(tmp_path / "app", "ACCT-APP", "app@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(app))
        ident = codex.app_home_identity()
        assert ident["email"] == "app@x.com"
        assert not any(k.startswith("_") for k in ident)


class TestSnapshotShadowing:
    def _fleet(self, monkeypatch, tmp_path, app_account):
        lane1 = bind(tmp_path / ".codex-1", "ACCT-1", "one@x.com")
        lane2 = bind(tmp_path / ".codex-2", "ACCT-2", "two@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_HOMES", f"{lane1}:{lane2}")
        app = bind(tmp_path / "app", app_account, f"{app_account.lower()}@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(app))
        monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(tmp_path / "absent.json"))
        return snapshot.build(live=False)

    def test_app_on_a_lane_account_marks_that_lane_shadowed(self, env_paths, monkeypatch, tmp_path):
        snap = self._fleet(monkeypatch, tmp_path, "ACCT-2")
        homes = {e["home"].rsplit("/", 1)[-1]: e for e in snap["codex"]["homes"]}
        assert set(homes) == {".codex-1", ".codex-2"}  # app home is not a lane
        assert homes[".codex-2"]["shadowed_by_app"] is True
        assert homes[".codex-1"]["shadowed_by_app"] is False
        app = snap["codex"]["app_home"]
        assert app["account_id"] == "ACCT-2"
        assert [h.rsplit("/", 1)[-1] for h in app["shadows"]] == [".codex-2"]
        # The app home never counts as a same-account duplicate among lanes.
        assert snap["codex"]["duplicates"] == []
        assert all(e["duplicate_of"] is None for e in snap["codex"]["homes"])

    def test_app_on_a_non_lane_account_shadows_nothing(self, env_paths, monkeypatch, tmp_path):
        snap = self._fleet(monkeypatch, tmp_path, "ACCT-9")
        assert snap["codex"]["app_home"]["shadows"] == []
        assert not any(e["shadowed_by_app"] for e in snap["codex"]["homes"])

    def test_shadowed_lane_keeps_waterfall_position(self, env_paths, monkeypatch, tmp_path):
        from test_pick import entry

        app = bind(tmp_path / "app", "ACCT-2", "two@x.com")
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(app))
        monkeypatch.setenv("SUBFLEET_CODEX_ACCOUNTS", str(tmp_path / "absent.json"))
        soon = (datetime.now().astimezone() + timedelta(days=1)).isoformat(timespec="seconds")
        later = (datetime.now().astimezone() + timedelta(days=2)).isoformat(timespec="seconds")
        e1 = entry("/h/.codex-1", 15, account="ACCT-1", weekly_reset=later)
        e2 = entry("/h/.codex-2", 10, account="ACCT-2", weekly_reset=soon)
        ranked = snapshot.rank_for_dispatch([e1, e2])
        assert ranked[0]["home"] == "/h/.codex-2"


def shadow_snap(app_email="two@x.com", shadows=("/h/.codex-2",)):
    from test_pick import entry

    homes = [entry("/h/.codex-1", 15, account="ACCT-1"), entry("/h/.codex-2", 10, account="ACCT-2")]
    for e in homes:
        e["shadowed_by_app"] = e["home"] in shadows
    from test_watchdog import snap as base

    s = base(homes)
    s["codex"]["app_home"] = {
        "home": "/h/.codex", "status": "ok", "account_id": "ACCT-2", "email": app_email,
        "plan": "pro", "auth_last_refresh": None, "shadows": list(shadows),
    }
    return s


class TestWatchdogShadow:
    def test_shadow_alerts_once_and_never_realerts(self, env_paths):
        from datetime import timedelta

        from subfleet import paths as p
        from subfleet.util import load_json, now_local

        s = shadow_snap()
        first = watchdog.run(snap=s)
        assert "codex-app-shadow:/h/.codex-2" in first["alerts_sent"]
        log = env_paths["notify_log"].read_text()
        assert "two@x.com" in log and "codex login" in log and "subfleet" in log
        # Persisting past the re-alert window: still silent (transition-only).
        state = load_json(p.alerts_path())
        for k in state:
            state[k]["last_sent"] = (now_local() - timedelta(hours=30)).isoformat(timespec="seconds")
        p.alerts_path().write_text(json.dumps(state))
        again = watchdog.run(snap=s)
        assert "codex-app-shadow:/h/.codex-2" not in again["alerts_sent"]

    def test_app_moving_to_another_lane_is_one_new_alert_no_recovery_noise(self, env_paths):
        watchdog.run(snap=shadow_snap())
        moved = shadow_snap(app_email="one@x.com", shadows=("/h/.codex-1",))
        moved["codex"]["app_home"]["account_id"] = "ACCT-1"
        summary = watchdog.run(snap=moved)
        assert "codex-app-shadow:/h/.codex-1" in summary["alerts_sent"]
        assert not any(k.startswith("codex-app-shadow") for k in summary["recovered"])
        assert "recovered: codex-app-shadow" not in env_paths["notify_log"].read_text()

    def test_no_shadow_no_alert(self, env_paths):
        s = shadow_snap(shadows=())
        s["codex"]["app_home"]["account_id"] = "ACCT-9"
        assert not any(k.startswith("codex-app-shadow") for k in watchdog.run(snap=s)["alerts_sent"])


class TestRenderAppLine:
    def test_table_shows_app_identity_and_shadow(self, env_paths):
        from subfleet import render

        out = render.table(shadow_snap())
        assert "app" in out and "two@x.com" in out and "lane shadowed" in out

    def test_table_shows_no_login(self, env_paths):
        from subfleet import render

        s = shadow_snap(shadows=())
        s["codex"]["app_home"] = {"home": "/h/.codex", "status": "missing", "shadows": []}
        assert "(no login)" in render.table(s)


class TestFreePlanGuard:
    """A lane logged in before its Pro upgrade (2026-08-19, ~/.codex-6) must
    never rank as dispatchable."""

    def test_verdict_and_ranking(self):
        from subfleet import snapshot as snap_mod
        from conftest import wham_ok
        from test_pick import entry

        probe = wham_ok(used=0, weekly=0, email="new@x.com")
        probe["plan_type"] = "free"
        assert snap_mod.codex_verdict({"status": "ok"}, probe, None) == "free-plan"
        assert snap_mod.codex_verdict({"status": "ok"}, wham_ok(), None) == "ok"
        free = entry("/h/.codex-6", 0, account="ACCT-6", verdict="free-plan")
        paid = entry("/h/.codex-2", 40, account="ACCT-2")
        assert [r["home"] for r in snap_mod.rank_for_dispatch([free, paid])] == ["/h/.codex-2"]

    def test_watchdog_names_the_upgrade(self, env_paths):
        from test_pick import entry
        from test_watchdog import snap as base

        free = entry("/h/.codex-6", 0, account="ACCT-6", verdict="free-plan")
        free["email"] = "new@x.com"
        s = base([free, entry("/h/.codex-2", 40, account="ACCT-2")])
        summary = watchdog.run(snap=s)
        assert "codex-free-plan:/h/.codex-6" in summary["alerts_sent"]
        log = env_paths["notify_log"].read_text()
        assert "buy Pro on new@x.com" in log and "codex login" in log
        healed = base([entry("/h/.codex-6", 0, account="ACCT-6"),
                       entry("/h/.codex-2", 40, account="ACCT-2")])
        assert "codex-free-plan:/h/.codex-6" in watchdog.run(snap=healed)["recovered"]

    def test_table_label(self, env_paths):
        from subfleet import render
        from test_pick import entry
        from test_watchdog import snap as base

        out = render.table(base([entry("/h/.codex-6", 0, account="ACCT-6", verdict="free-plan")]))
        assert "FREE-PLAN" in out
