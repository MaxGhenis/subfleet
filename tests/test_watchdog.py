import json
from datetime import timedelta

from subfleet import watchdog
from subfleet.util import load_json, now_local

from conftest import wham_ok
from test_pick import entry


def snap(homes, duplicates=None, claude_active=None, dispatchable=None, statusline=None,
         claude_accounts=None):
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
            "account": {"email": "max@example.com"},
            "accounts": claude_accounts or [],
            "known_accounts": [],
            "subscription": "max",
            "tier": "default_claude_max_20x",
            "keychain": {"status": "ok"},
            "oauth_probe": {"status": "token-invalid"},
            "statusline": statusline,
            "recent_errors": [claude_active] if claude_active else [],
            "active_limit": claude_active,
            "verdict": "limited" if claude_active else "ok",
        },
    }


def read_alerts(env_paths):
    log = env_paths["notify_log"]
    return log.read_text() if log.exists() else ""


def configure_auto_reset(enabled: bool):
    from subfleet import codex

    config = codex.accounts_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"auto_reset": {"enabled": enabled}}))


def scoped_limit(model="Fable", percent=100, severity="critical", reset_at=None):
    return {
        "kind": "weekly_scoped",
        "group": "weekly",
        "percent": percent,
        "severity": severity,
        "resets_at": reset_at,
        "is_active": True,
        "scope_model": model,
        "scope_surface": None,
    }


def scoped_account(email, limits):
    return {
        "email": email,
        "capacity": {"email": email, "scoped_limits": limits},
    }


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

    def test_realert_after_window(self, env_paths):
        s = snap([entry("/h/.codex-2", 50, account="a", verdict="auth-revoked"),
                  entry("/h/.codex-3", 5, account="b")])
        s["codex"]["homes"][0]["probe"] = {"status": "token-revoked"}
        watchdog.run(snap=s)
        # Age the alert state past the re-alert window.
        from subfleet import paths

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

    def test_idle_reset_credits_alert_once_per_transition(self, env_paths):
        configure_auto_reset(False)
        first = entry("/h/.codex-2", 100, account="a", verdict="limited")
        second = entry("/h/.codex-3", 100, account="b", verdict="limited")
        healthy = entry("/h/.codex-4", 5, account="c")
        first["reset_credits"] = {"available": 1, "applicable": 1}
        second["reset_credits"] = {"available": 1, "applicable": 1}
        s = snap([first, second, healthy], dispatchable=1)

        initial = watchdog.run(snap=s)
        assert "codex-resets-idle" in initial["alerts_sent"]
        out = read_alerts(env_paths)
        assert "a@x.com" in out and "b@x.com" in out
        assert "subfleet reset codex all" in out

        # Even after the ordinary re-alert window, `once` keeps it silent.
        from subfleet import paths

        state = load_json(paths.alerts_path())
        state["codex-resets-idle"]["last_sent"] = (
            now_local() - timedelta(hours=7)
        ).isoformat(timespec="seconds")
        paths.alerts_path().write_text(json.dumps(state))
        assert "codex-resets-idle" not in watchdog.run(snap=s)["alerts_sent"]

        # Clearing rearms a later transition without a recovery notification.
        second["reset_credits"]["applicable"] = 0
        cleared = watchdog.run(snap=s)
        assert "codex-resets-idle" not in cleared["recovered"]
        second["reset_credits"]["applicable"] = 1
        assert "codex-resets-idle" in watchdog.run(snap=s)["alerts_sent"]

    def test_reset_redemption_refreshes_fleet_and_notifies_only_once(
        self, env_paths
    ):
        limited = entry(
            "/h/.codex-2", 100, weekly=100, account="a", verdict="limited"
        )
        limited["probe"]["limit_reached"] = True
        limited["reset_credits"] = {"available": 3, "applicable": 1}
        limited["recent_errors"]["usage_limit"] = [{
            "observed_at": now_local().isoformat(timespec="seconds"),
            "reset_at": (now_local() + timedelta(minutes=10)).isoformat(timespec="seconds"),
            "try_again": "later",
        }]
        nearly_empty = entry("/h/.codex-3", 90, weekly=90, account="b")
        s = snap([limited, nearly_empty], dispatchable=1)
        weekly_reset = (now_local() + timedelta(days=7)).isoformat(timespec="seconds")
        after = wham_ok(used=0, weekly=0, email="a@x.com")
        after["secondary"]["reset_at"] = weekly_reset
        outcomes = [{
            "status": "redeemed",
            "enabled": True,
            "redeemed": {
                "home": limited["home"],
                "lane": limited["home"],
                "email": "a@x.com",
                "credit_id": "credit-a",
                "remaining": 2,
                "weekly_reset_at": weekly_reset,
                "probe": after,
            },
        }, {"status": "not-triggered", "enabled": True}]
        calls = []

        def fake_policy(rows, **kwargs):
            calls.append({"rows": rows, **kwargs})
            return outcomes.pop(0)

        first = watchdog.run(snap=s, policy_fn=fake_policy)
        second = watchdog.run(snap=s, policy_fn=fake_policy)

        assert len(calls) == 2
        assert calls[0]["dry_run"] is False
        assert calls[0]["dispatchable_homes"] == {nearly_empty["home"]}
        assert first["reset_policy"]["status"] == "redeemed"
        assert first["reset_redemption"]["credit_id"] == "credit-a"
        assert second["reset_policy"]["status"] == "not-triggered"
        assert second["reset_redemption"] is None
        assert limited["verdict"] == "ok"
        assert limited["windows"]["weekly"]["used_percent"] == 0
        assert limited["recent_errors"]["usage_limit"] == []
        assert s["codex"]["fleet"]["dispatchable_now"] == 2
        assert not any(key.startswith("codex-fleet") for key in first["conditions"])
        notices = read_alerts(env_paths)
        assert notices.count("reset redeemed on a@x.com") == 1
        assert "2 credits remain fleet-wide" in notices
        assert "Weekly reset now" in notices

    def test_stale_reset_propagation_is_authoritative_for_this_cycle(
        self, env_paths
    ):
        current = now_local().replace(microsecond=0)
        limited = entry(
            "/h/.codex-2", 100, weekly=100, account="a", verdict="limited"
        )
        limited["probe"]["limit_reached"] = True
        limited["reset_credits"] = {"available": 1, "applicable": 1}
        healthy = entry("/h/.codex-3", 90, weekly=90, account="b")
        s = snap([limited, healthy], dispatchable=1)
        s["generated_at"] = current.isoformat(timespec="seconds")
        old_reset = (current + timedelta(hours=2)).isoformat(timespec="seconds")
        new_reset = (current + timedelta(days=7)).isoformat(timespec="seconds")
        stale = wham_ok(
            used=100, weekly=100, email="a@x.com", limit_reached=True
        )
        stale["secondary"]["reset_at"] = old_reset

        def redeemed(_rows, **_kwargs):
            return {
                "status": "redeemed",
                "enabled": True,
                "redeemed": {
                    "home": limited["home"],
                    "lane": limited["home"],
                    "email": "a@x.com",
                    "credit_id": "credit-a",
                    "remaining": 0,
                    "weekly_reset_at": new_reset,
                    "probe": stale,
                    "propagated": False,
                },
            }

        summary = watchdog.run(snap=s, policy_fn=redeemed)

        assert limited["verdict"] == "ok"
        assert limited["probe"]["propagation_pending"] is True
        assert limited["windows"]["weekly"]["used_percent"] == 0
        assert limited["windows"]["weekly"]["reset_at"] == new_reset
        assert s["codex"]["fleet"]["dispatchable_now"] == 2
        assert not any(key.startswith("codex-fleet") for key in summary["conditions"])
        assert "Weekly reset now" in read_alerts(env_paths)

    def test_disabled_policy_does_not_redeem_and_idle_warning_fires(
        self, env_paths
    ):
        configure_auto_reset(False)
        first = entry("/h/.codex-2", 100, account="a", verdict="limited")
        second = entry("/h/.codex-3", 100, account="b", verdict="limited")
        healthy = entry("/h/.codex-4", 5, account="c")
        first["reset_credits"] = {"available": 1, "applicable": 1}
        second["reset_credits"] = {"available": 1, "applicable": 1}
        s = snap([first, second, healthy], dispatchable=1)
        calls = []

        def fake_policy(_rows, **kwargs):
            calls.append(kwargs)
            assert kwargs["config"]["enabled"] is False
            return {"status": "disabled", "enabled": False}

        summary = watchdog.run(snap=s, policy_fn=fake_policy)

        assert len(calls) == 1
        assert summary["reset_policy"].get("redeemed") is None
        assert "codex-resets-idle" in summary["alerts_sent"]
        notices = read_alerts(env_paths)
        assert "reset credits idle" in notices
        assert "reset redeemed" not in notices

    def test_watchdog_forwards_dry_run_to_policy(self, env_paths):
        s = snap([
            entry("/h/.codex-2", 10, account="a"),
            entry("/h/.codex-3", 20, account="b"),
        ])
        seen = []

        def fake_policy(_rows, **kwargs):
            seen.append(kwargs["dry_run"])
            return {"status": "not-triggered", "enabled": True}

        watchdog.run(snap=s, dry_run=True, policy_fn=fake_policy)

        assert seen == [True]

    def test_capacity_expiring_warns_at_most_once_per_day_across_retrigger(
        self, env_paths
    ):
        from subfleet import paths

        current = now_local().replace(microsecond=0)
        reset = (current + timedelta(days=2)).isoformat(timespec="seconds")
        first = entry("/h/.codex-1", 10, weekly=10, account="a")
        second = entry("/h/.codex-2", 10, weekly=10, account="b")
        for lane in (first, second):
            lane["windows"]["secondary"]["reset_at"] = reset
            lane["windows"]["weekly"] = lane["windows"]["secondary"]
        s = snap([first, second], dispatchable=2)
        s["generated_at"] = current.isoformat(timespec="seconds")
        paths.history_path().parent.mkdir(parents=True, exist_ok=True)
        paths.history_path().write_text(json.dumps({
            "ts": (current - timedelta(hours=6)).isoformat(timespec="seconds"),
            "codex": {
                first["home"]: {"v": "ok", "p5h": 10, "wk": 10},
                second["home"]: {"v": "ok", "p5h": 10, "wk": 10},
            },
        }) + "\n")

        def no_reset(_rows, **_kwargs):
            return {"status": "not-triggered", "enabled": True}

        initial = watchdog.run(snap=s, policy_fn=no_reset)
        assert "codex-capacity-expiring" in initial["alerts_sent"]
        log = read_alerts(env_paths)
        assert "queue more sol work" in log
        assert "a@x.com, b@x.com will expire ~180% unused" in log

        # Clear the condition, then retrigger it: the strict 24h limiter still
        # applies even though the generic alert state became inactive.
        first["windows"]["weekly"]["used_percent"] = 100
        second["windows"]["weekly"]["used_percent"] = 100
        assert "codex-capacity-expiring" not in watchdog.run(
            snap=s, policy_fn=no_reset
        )["conditions"]
        first["windows"]["weekly"]["used_percent"] = 10
        second["windows"]["weekly"]["used_percent"] = 10
        assert "codex-capacity-expiring" not in watchdog.run(
            snap=s, policy_fn=no_reset
        )["alerts_sent"]

        alert_state = load_json(paths.alerts_path())
        alert_state["codex-capacity-expiring"]["last_sent"] = (
            now_local() - timedelta(hours=25)
        ).isoformat(timespec="seconds")
        paths.alerts_path().write_text(json.dumps(alert_state))
        assert "codex-capacity-expiring" in watchdog.run(
            snap=s, policy_fn=no_reset
        )["alerts_sent"]

    def test_capacity_expiring_thresholds_are_strict(self, env_paths):
        current = now_local().replace(microsecond=0)
        s = snap([entry("/h/.codex-1", 10, account="a")])
        s["generated_at"] = current.isoformat(timespec="seconds")
        s["codex"]["capacity_expiry"] = {
            "projected_unused_windows": 1.0,
            "earliest_reset_at": (current + timedelta(hours=71)).isoformat(),
            "lanes": {},
            "at_risk_lanes": [],
        }
        assert "codex-capacity-expiring" not in {
            condition["key"] for condition in watchdog.evaluate_conditions(s)
        }
        s["codex"]["capacity_expiry"].update({
            "projected_unused_windows": 1.0,
            "projected_unused_windows_raw": 1.001,
            "earliest_reset_at": (current + timedelta(hours=71)).isoformat(),
            "at_risk_lanes": ["/h/.codex-1"],
        })
        assert "codex-capacity-expiring" in {
            condition["key"] for condition in watchdog.evaluate_conditions(s)
        }
        s["codex"]["capacity_expiry"].update({
            "projected_unused_windows": 1.01,
            "projected_unused_windows_raw": 1.01,
            "earliest_reset_at": (current + timedelta(hours=72)).isoformat(),
        })
        assert "codex-capacity-expiring" not in {
            condition["key"] for condition in watchdog.evaluate_conditions(s)
        }

    def test_idle_reset_credits_requires_two_limited_holders_and_low_fleet(
        self, env_paths
    ):
        limited = entry("/h/.codex-2", 100, account="a", verdict="limited")
        other = entry("/h/.codex-3", 100, account="b", verdict="limited")
        limited["reset_credits"] = {"available": 1, "applicable": 1}
        other["reset_credits"] = {"available": 1, "applicable": 0}
        one_holder = snap([limited, other], dispatchable=0)
        assert "codex-resets-idle" not in {
            condition["key"] for condition in watchdog.evaluate_conditions(one_holder)
        }

        other["reset_credits"]["applicable"] = 1
        fleet_healthy = snap([limited, other], dispatchable=2)
        assert "codex-resets-idle" not in {
            condition["key"] for condition in watchdog.evaluate_conditions(fleet_healthy)
        }

    def test_claude_active_limit_alerts_and_dedups_by_reset(self, env_paths):
        future = (now_local() + timedelta(hours=1)).isoformat(timespec="seconds")
        active = {"kind": "session-limit", "reset_at": future, "observed_at": future,
                  "text": "You've hit your session limit", "sessions": 3, "count": 5}
        s = snap([entry("/h/.codex-3", 5, account="b")], claude_active=active)
        first = watchdog.run(snap=s)
        second = watchdog.run(snap=s)
        assert any(k.startswith("claude-limit:") for k in first["alerts_sent"])
        assert second["alerts_sent"] == []

    def test_scoped_limit_alert_names_model_reset_and_dedups(self, env_paths):
        reset = (now_local() + timedelta(days=1)).isoformat(timespec="seconds")
        accounts = [scoped_account(
            "max@example.com", [scoped_limit(reset_at=reset)]
        )]
        s = snap(
            [entry("/h/.codex-3", 5, account="b")],
            claude_accounts=accounts,
        )

        first = watchdog.run(snap=s)
        second = watchdog.run(snap=s)

        assert any(
            key.startswith("claude-scoped-limit:max@example.com:fable:")
            for key in first["alerts_sent"]
        )
        assert second["alerts_sent"] == []
        out = read_alerts(env_paths)
        assert "Fable scoped limit critical" in out
        assert "Fable weekly_scoped is 100% critical" in out
        assert "Resets " in out

    def test_scoped_limit_critical_crossing_realerts(self, env_paths):
        reset = (now_local() + timedelta(hours=2)).isoformat(timespec="seconds")
        critical = snap(
            [entry("/h/.codex-3", 5, account="b")],
            claude_accounts=[scoped_account(
                "max@example.com", [scoped_limit(reset_at=reset)]
            )],
        )
        normal = snap(
            [entry("/h/.codex-3", 5, account="b")],
            claude_accounts=[scoped_account(
                "max@example.com",
                [scoped_limit(percent=99, severity="normal", reset_at=reset)],
            )],
        )

        assert watchdog.run(snap=critical)["alerts_sent"]
        assert watchdog.run(snap=normal)["alerts_sent"] == []
        assert watchdog.run(snap=critical)["alerts_sent"]
        assert read_alerts(env_paths).count("Fable scoped limit critical") == 2

    def test_percent_100_alerts_even_without_critical_severity(self, env_paths):
        s = snap(
            [entry("/h/.codex-3", 5, account="b")],
            claude_accounts=[scoped_account(
                "other@x.com",
                [scoped_limit(percent=100, severity="normal")],
            )],
        )
        summary = watchdog.run(snap=s)
        assert any(
            key.startswith("claude-scoped-limit:other@x.com:fable:")
            for key in summary["alerts_sent"]
        )
        assert "100% normal" in read_alerts(env_paths)

    def test_malformed_scoped_limits_are_ignored(self, env_paths):
        accounts = [
            None,
            {"email": "missing-capacity@x"},
            {"email": "bad-capacity@x", "capacity": []},
            scoped_account("bad-limits@x", {"not": "a list"}),
            scoped_account("bad-entries@x", [None, "bad", {"scope_model": "Fable"}]),
        ]
        s = snap(
            [
                entry("/h/.codex-2", 5, account="a"),
                entry("/h/.codex-3", 5, account="b"),
            ],
            claude_accounts=accounts,
        )
        assert watchdog.run(snap=s)["alerts_sent"] == []
        assert read_alerts(env_paths) == ""

    def test_scoped_limit_still_alerts_when_codex_is_offline(self, env_paths):
        offline = entry("/h/.codex-3", 5, account="b", verdict="unknown")
        offline["probe"] = {"status": "network-error"}
        s = snap(
            [offline],
            dispatchable=0,
            claude_accounts=[scoped_account(
                "max@example.com", [scoped_limit()]
            )],
        )
        summary = watchdog.run(snap=s)
        assert any(key.startswith("claude-scoped-limit:") for key in summary["alerts_sent"])

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
        from subfleet import paths

        assert paths.snapshot_path().exists()
        assert paths.brief_path().exists()
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


def suspect(home, account="a", last_refresh="2026-08-01T00:00:00Z"):
    """A home whose usage probe 401'd over an expired ACCESS token."""
    e = entry(home, 50, account=account, verdict="auth-suspect")
    e["probe"] = {
        "status": "http-401",
        "error": "Provided authentication token is expired. Please try signing in again.",
    }
    e["auth_last_refresh"] = last_refresh
    return e


class TestRefreshProbe:
    def _fakes(self, monkeypatch, refresh_result, reprobe_result=None):
        calls = {"refresh": [], "reprobe": []}

        def fake_refresh(home, **kwargs):
            calls["refresh"].append(str(home))
            return dict(refresh_result)

        def fake_reprobe(home):
            calls["reprobe"].append(str(home))
            return reprobe_result or {
                "auth": {"status": "ok", "email": "a@x.com", "plan": "pro",
                         "account_id": "a", "last_refresh": "2026-08-12T10:00:00Z"},
                "probe": wham_ok(used=12),
                "observed": None,
            }

        monkeypatch.setattr(watchdog.codex, "refresh_via_cli", fake_refresh)
        monkeypatch.setattr(watchdog, "_reprobe_home", fake_reprobe)
        return calls

    def test_expired_token_heals_to_ok(self, env_paths, monkeypatch):
        calls = self._fakes(monkeypatch, {"status": "ok", "rc": 0, "detail": ""})
        s = snap([suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")],
                 dispatchable=1)
        summary = watchdog.run(snap=s)
        assert calls["refresh"] == ["/h/.codex-2"]
        assert summary["healed"] == ["/h/.codex-2"]
        healed = s["codex"]["homes"][0]
        assert healed["verdict"] == "ok"
        assert healed["windows"]["source"] == "live"
        assert healed["windows"]["five_hour"]["used_percent"] == 12
        assert healed["auth_last_refresh"] == "2026-08-12T10:00:00Z"
        # Fleet recount: the healed lane counts, so no suspect/fleet alerts.
        assert s["codex"]["fleet"]["dispatchable_now"] == 2
        assert not any(k.startswith(("codex-suspect:", "codex-fleet"))
                       for k in summary["alerts_sent"])
        from subfleet import paths

        assert load_json(paths.refresh_probes_path())["/h/.codex-2"]["result"] == "healed"

    def test_exhausted_but_refreshed_home_reads_limited(self, env_paths, monkeypatch):
        # The exec turn can fail on a usage limit while STILL having refreshed
        # the token at startup — the re-probe must be trusted over the rc.
        reprobe = {
            "auth": {"status": "ok", "email": "a@x.com", "plan": "pro",
                     "account_id": "a", "last_refresh": "2026-08-12T10:00:00Z"},
            "probe": wham_ok(used=100, limit_reached=True),
            "observed": None,
        }
        self._fakes(monkeypatch, {"status": "failed", "rc": 1, "detail": "usage limit"},
                    reprobe_result=reprobe)
        s = snap([suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=s)
        assert s["codex"]["homes"][0]["verdict"] == "limited"
        assert summary["healed"] == ["/h/.codex-2"]
        assert not any(k.startswith("codex-suspect:") for k in summary["alerts_sent"])

    def test_revoked_probe_downgrades_latches_and_reopens_on_relogin(
            self, env_paths, monkeypatch):
        calls = self._fakes(
            monkeypatch, {"status": "revoked", "rc": 1, "detail": "refresh token was revoked"})
        s = snap([suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=s)
        assert calls["refresh"] == ["/h/.codex-2"]
        assert calls["reprobe"] == []  # revoked is definitive, no re-probe
        assert s["codex"]["homes"][0]["verdict"] == "auth-revoked"
        assert summary["refresh_probes"][0]["result"] == "revoked"
        assert any(k.startswith("codex-revoked:") for k in summary["alerts_sent"])
        out = read_alerts(env_paths)
        assert "refresh token was revoked" in out
        assert "🚨" in out  # probe-confirmed revocation is critical

        # Aged past the spacing window, same auth.json: the latch holds the
        # auth-revoked verdict without another exec.
        from subfleet import paths

        state = load_json(paths.refresh_probes_path())
        state["/h/.codex-2"]["attempted_at"] = (
            now_local() - timedelta(hours=2)).isoformat(timespec="seconds")
        paths.refresh_probes_path().write_text(json.dumps(state))
        s2 = snap([suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")])
        watchdog.run(snap=s2)
        assert calls["refresh"] == ["/h/.codex-2"]  # still exactly one exec
        assert s2["codex"]["homes"][0]["verdict"] == "auth-revoked"
        assert s2["codex"]["homes"][0]["refresh_probe"]["latched"] is True

        # A re-login rewrites last_refresh, which reopens the gate.
        s3 = snap([suspect("/h/.codex-2", last_refresh="2026-08-12T12:00:00Z"),
                   entry("/h/.codex-3", 5, account="b")])
        watchdog.run(snap=s3)
        assert calls["refresh"] == ["/h/.codex-2", "/h/.codex-2"]

    def test_failed_probe_keeps_suspect_alerts_and_spaces_retries(
            self, env_paths, monkeypatch):
        reprobe = {
            "auth": {"status": "ok", "email": "a@x.com", "plan": "pro",
                     "account_id": "a", "last_refresh": "2026-08-01T00:00:00Z"},
            "probe": {"status": "http-401",
                      "error": "Provided authentication token is expired."},
            "observed": None,
        }
        calls = self._fakes(monkeypatch,
                            {"status": "failed", "rc": 3, "detail": "stream error"},
                            reprobe_result=reprobe)
        s = snap([suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=s)
        assert s["codex"]["homes"][0]["verdict"] == "auth-suspect"
        assert summary["refresh_probes"][0]["result"] == "failed"
        assert summary["healed"] == []
        assert any(k.startswith("codex-suspect:") for k in summary["alerts_sent"])
        out = read_alerts(env_paths)
        assert "FAILED" in out
        assert "stream error" in out
        # Immediate second run lands inside the spacing window: no second exec.
        s2 = snap([suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")])
        watchdog.run(snap=s2)
        assert calls["refresh"] == ["/h/.codex-2"]

    def test_non_expired_suspect_is_not_probed(self, env_paths, monkeypatch):
        calls = self._fakes(monkeypatch, {"status": "ok", "rc": 0, "detail": ""})
        e = entry("/h/.codex-2", 50, account="a", verdict="auth-suspect")
        e["probe"] = {"status": "http-403", "error": "forbidden"}
        s = snap([e, entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=s)
        assert calls["refresh"] == []
        assert summary["refresh_probes"] == []
        assert any(k.startswith("codex-suspect:") for k in summary["alerts_sent"])

    def test_dry_run_never_execs(self, env_paths, monkeypatch):
        calls = self._fakes(monkeypatch, {"status": "ok", "rc": 0, "detail": ""})
        s = snap([suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=s, dry_run=True)
        assert calls["refresh"] == []
        assert summary["refresh_probes"] == []
        from subfleet import paths

        assert not paths.refresh_probes_path().exists()

    def test_suspect_recovery_notice_after_heal(self, env_paths, monkeypatch):
        reprobe_fail = {
            "auth": {"status": "ok", "last_refresh": "2026-08-01T00:00:00Z"},
            "probe": {"status": "network-error", "error": "no route"},
            "observed": None,
        }
        self._fakes(monkeypatch, {"status": "failed", "rc": 1, "detail": "no route"},
                    reprobe_result=reprobe_fail)
        s = snap([suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")])
        first = watchdog.run(snap=s)
        assert any(k.startswith("codex-suspect:") for k in first["alerts_sent"])

        # Next cycle (aged past spacing) the heal lands -> one all-clear.
        from subfleet import paths

        state = load_json(paths.refresh_probes_path())
        state["/h/.codex-2"]["attempted_at"] = (
            now_local() - timedelta(hours=1)).isoformat(timespec="seconds")
        paths.refresh_probes_path().write_text(json.dumps(state))
        self._fakes(monkeypatch, {"status": "ok", "rc": 0, "detail": ""})
        s2 = snap([suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")])
        summary = watchdog.run(snap=s2)
        assert summary["healed"] == ["/h/.codex-2"]
        assert any(k.startswith("codex-suspect:") for k in summary["recovered"])
        assert "recovered" in read_alerts(env_paths)


class TestRender:
    def test_table_smoke(self, env_paths):
        from subfleet import render

        e_rev = entry("/h/.codex-2", 50, account="a", verdict="auth-revoked")
        e_rev["probe"] = {"status": "token-revoked"}
        s = snap([e_rev, entry("/h/.codex-3", 5, account="b")],
                 statusline={"five_hour_pct": 42.0, "seven_day_pct": 12.0, "fresh": True,
                             "updated_at": now_local().isoformat(timespec="seconds")})
        out = render.table(s)
        assert "AUTH-REVOKED" in out
        assert "42.0% used" in out
        brief = render.brief_md(s)
        assert "## AI capacity" in brief
        assert "codex login" in brief

    def test_reset_credit_markers_and_limited_lane_count(self, env_paths):
        from subfleet import render

        first = entry("/h/.codex-2", 100, account="a", verdict="limited")
        second = entry("/h/.codex-3", 100, account="b", verdict="limited")
        first["reset_credits"] = {"available": 1, "applicable": 1}
        second["reset_credits"] = {"available": 2, "applicable": 2}
        s = snap([first, second], dispatchable=0)

        table = render.table(s)
        assert table.count("LIMITED · reset available") == 2
        assert "fleet: 0/2 dispatchable · resets available: 2" in table
        assert (
            "- codex: 2 limited lanes hold an unused reset credit"
            in render.brief_md(s)
        )

    def test_reset_marker_requires_limited_and_applicable(self, env_paths):
        from subfleet import render

        merely_available = entry("/h/.codex-2", 100, account="a", verdict="limited")
        merely_available["reset_credits"] = {"available": 1, "applicable": 0}
        healthy = entry("/h/.codex-3", 5, account="b")
        healthy["reset_credits"] = {"available": 1, "applicable": 1}
        out = render.table(snap([merely_available, healthy], dispatchable=1))
        assert "reset available" not in out
        assert "resets available:" not in out
