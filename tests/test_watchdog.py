import json
from datetime import timedelta

from carpool import config, watchdog
from carpool.util import load_json, now_local

from test_pick import entry


def snap(homes, duplicates=None, claude_active=None, dispatchable=None):
    n_ok = sum(1 for e in homes if e["verdict"] == "ok" and not e["duplicate_of"])
    return {
        "generated_at": now_local().isoformat(timespec="seconds"),
        "codex": {
            "homes": homes,
            "duplicates": duplicates or [],
            "fleet": {
                "total_homes": len(homes),
                "dispatchable_now": dispatchable if dispatchable is not None else n_ok,
                "best_home": next((e["home"] for e in homes if e["verdict"] == "ok"), None),
                "earliest_reset": None,
            },
        },
        "claude": {
            "account": {"email": "active@example.com"},
            "known_accounts": [],
            "subscription": "max",
            "tier": "default_claude_max_20x",
            "keychain": {"status": "ok"},
            "oauth_probe": {"status": "token-invalid"},
            "recent_errors": [claude_active] if claude_active else [],
            "active_limit": claude_active,
            "verdict": "limited" if claude_active else "ok",
        },
    }


def read_alerts(env_paths):
    log = env_paths["notify_log"]
    return log.read_text() if log.exists() else ""


class TestConditions:
    def test_revoked_home_alerts(self, env_paths):
        s = snap([entry("/h/.codex-2", 50, account="a", verdict="auth-revoked"),
                  entry("/h/.codex-3", 5, account="b")])
        s["codex"]["homes"][0]["probe"] = {"status": "token-revoked"}
        summary = watchdog.run(snap=s)
        assert any(k.startswith("codex-revoked:") for k in summary["alerts_sent"])
        out = read_alerts(env_paths)
        assert "token revoked" in out
        assert "codex login" in out

    def test_dedup_within_window(self, env_paths):
        s = snap([entry("/h/.codex-2", 50, account="a", verdict="auth-revoked"),
                  entry("/h/.codex-3", 5, account="b")])
        s["codex"]["homes"][0]["probe"] = {"status": "token-revoked"}
        first = watchdog.run(snap=s)
        second = watchdog.run(snap=s)
        assert first["alerts_sent"]
        assert second["alerts_sent"] == []

    def test_stderr_fallback_is_deduplicated(self, env_paths, capsys):
        current = config.load(strict=True)
        current.pop("notify_cmd", None)
        config.save(current)
        s = snap([entry("/h/.codex-2", 50, account="a", verdict="auth-revoked"),
                  entry("/h/.codex-3", 5, account="b")])
        s["codex"]["homes"][0]["probe"] = {"status": "token-revoked"}

        first = watchdog.run(snap=s)
        second = watchdog.run(snap=s)

        assert first["alerts_sent"]
        assert second["alerts_sent"] == []
        err = capsys.readouterr().err
        assert "token revoked" in err

    def test_realert_after_window(self, env_paths):
        s = snap([entry("/h/.codex-2", 50, account="a", verdict="auth-revoked"),
                  entry("/h/.codex-3", 5, account="b")])
        s["codex"]["homes"][0]["probe"] = {"status": "token-revoked"}
        watchdog.run(snap=s)
        # Age the alert state past the re-alert window.
        from carpool import paths

        state = load_json(paths.alerts_path())
        for k in state:
            state[k]["last_sent"] = (now_local() - timedelta(hours=7)).isoformat(timespec="seconds")
        paths.alerts_path().write_text(json.dumps(state))
        again = watchdog.run(snap=s)
        assert again["alerts_sent"]

    def test_recovery_notice(self, env_paths):
        bad = snap([entry("/h/.codex-2", 50, account="a", verdict="auth-revoked"),
                    entry("/h/.codex-3", 5, account="b")])
        bad["codex"]["homes"][0]["probe"] = {"status": "token-revoked"}
        watchdog.run(snap=bad)
        good = snap([entry("/h/.codex-2", 50, account="a"),
                     entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=good)
        assert any(k.startswith("codex-revoked:") for k in summary["recovered"])
        assert "recovered" in read_alerts(env_paths)

    def test_duplicate_accounts_critical(self, env_paths):
        s = snap(
            [entry("/h/.codex-2", 5, account="a"),
             entry("/h/.codex-3", 9, account="a", duplicate_of="/h/.codex-2")],
            duplicates=[{"account_id": "a", "homes": ["/h/.codex-2", "/h/.codex-3"]}],
        )
        summary = watchdog.run(snap=s)
        assert any(k.startswith("codex-dup:") for k in summary["alerts_sent"])
        assert "revocation trap" in read_alerts(env_paths)

    def test_fleet_empty_critical(self, env_paths):
        s = snap([entry("/h/.codex-2", 100, account="a", verdict="limited"),
                  entry("/h/.codex-3", 100, account="b", verdict="limited")], dispatchable=0)
        summary = watchdog.run(snap=s)
        assert "codex-fleet-empty" in summary["alerts_sent"]

    def test_fleet_low_warns(self, env_paths):
        s = snap([entry("/h/.codex-2", 100, account="a", verdict="limited"),
                  entry("/h/.codex-3", 5, account="b")], dispatchable=1)
        summary = watchdog.run(snap=s)
        assert "codex-fleet-low" in summary["alerts_sent"]

    def test_claude_active_limit_alerts_and_dedups_by_reset(self, env_paths):
        future = (now_local() + timedelta(hours=1)).isoformat(timespec="seconds")
        active = {"kind": "session-limit", "reset_at": future, "observed_at": future,
                  "text": "You've hit your session limit", "sessions": 3, "count": 5}
        s = snap([entry("/h/.codex-3", 5, account="b")], claude_active=active)
        first = watchdog.run(snap=s)
        second = watchdog.run(snap=s)
        assert any(k.startswith("claude-limit:") for k in first["alerts_sent"])
        assert second["alerts_sent"] == []

    def test_offline_run_is_silent(self, env_paths):
        e2 = entry("/h/.codex-2", 50, account="a", verdict="unknown")
        e3 = entry("/h/.codex-3", 5, account="b", verdict="unknown")
        e2["probe"] = {"status": "network-error"}
        e3["probe"] = {"status": "network-error"}
        s = snap([e2, e3], dispatchable=0)
        summary = watchdog.run(snap=s)
        assert summary["alerts_sent"] == []
        assert read_alerts(env_paths) == ""

    def test_healthy_run_no_alerts_but_writes_state(self, env_paths):
        s = snap([entry("/h/.codex-2", 10, account="a"),
                  entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=s)
        assert summary["alerts_sent"] == []
        from carpool import paths

        assert paths.snapshot_path().exists()
        assert paths.history_path().exists()

    def test_noauth_home_alerts(self, env_paths):
        e = entry("/h/.codex", 0, account=None, verdict="no-auth", primary_home=True)
        e["account_id"] = None
        e["windows"]["primary"] = None
        s = snap([e, entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=s)
        assert any(k.startswith("codex-noauth:") for k in summary["alerts_sent"])
        assert "NO credentials" in read_alerts(env_paths)

    def test_revoked_to_noauth_is_not_recovery(self, env_paths):
        bad = snap([entry("/h/.codex", 50, account="a", verdict="auth-revoked"),
                    entry("/h/.codex-3", 5, account="b")])
        bad["codex"]["homes"][0]["probe"] = {"status": "token-revoked"}
        watchdog.run(snap=bad)
        noauth = entry("/h/.codex", 0, account=None, verdict="no-auth")
        noauth["windows"]["primary"] = None
        after = snap([noauth, entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=after)
        assert summary["recovered"] == []  # revoked cleared, but home still broken
        assert any(k.startswith("codex-noauth:") for k in summary["alerts_sent"])
        # Full heal: no-auth -> ok sends exactly one recovery for the noauth key.
        healed = snap([entry("/h/.codex", 10, account="a", primary_home=True),
                       entry("/h/.codex-3", 5, account="b")])
        summary2 = watchdog.run(snap=healed)
        assert any(k.startswith("codex-noauth:") for k in summary2["recovered"])

    def test_refresh_revoked_error_in_rollout_is_critical(self, env_paths):
        e = entry("/h/.codex-2", 10, account="a")
        e["recent_errors"]["auth_revoked"] = [{"observed_at": "2026-07-11T02:00:00-04:00"}]
        s = snap([e, entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=s)
        assert any(k.startswith("codex-refresh-revoked:") for k in summary["alerts_sent"])
        assert "REFRESH token revoked" in read_alerts(env_paths)


class TestRender:
    def test_table_smoke(self, env_paths):
        from carpool import render

        e_rev = entry("/h/.codex-2", 50, account="a", verdict="auth-revoked")
        e_rev["probe"] = {"status": "token-revoked"}
        s = snap([e_rev, entry("/h/.codex-3", 5, account="b")])
        out = render.table(s)
        assert "AUTH-REVOKED" in out
