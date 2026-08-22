import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest

from carpool import paths, watchdog
from carpool.util import load_json, now_local

from conftest import wham_ok
from test_pick import entry
from test_watchdog import snap


@pytest.fixture(autouse=True)
def current_auth(monkeypatch):
    state = {
        "status": "ok",
        "email": "a@example.com",
        "account_id": "a",
        "last_refresh": "2026-08-01T00:00:00Z",
    }
    monkeypatch.setattr(watchdog.codex, "read_auth", lambda home: dict(state))
    return state


def expired_suspect(home: str, last_refresh: str = "2026-08-01T00:00:00Z") -> dict:
    lane = entry(home, 50, account="a", verdict="auth-suspect")
    lane["probe"] = {
        "status": "http-401",
        "error": "Provided authentication token is expired. Please sign in again.",
    }
    lane["auth_last_refresh"] = last_refresh
    return lane


def healthy_reprobe(used: float = 12) -> dict:
    return {
        "auth": {
            "status": "ok",
            "email": "lane@example.com",
            "plan": "pro",
            "account_id": "a",
            "last_refresh": "2026-08-12T10:00:00Z",
        },
        "probe": wham_ok(used=used, email="lane@example.com"),
        "observed": None,
    }


def test_expired_access_token_heals_and_recounts_fleet(env_paths, monkeypatch):
    refresh_calls = []
    monkeypatch.setattr(
        watchdog.codex,
        "refresh_via_cli",
        lambda home: refresh_calls.append(str(home))
        or {"status": "ok", "rc": 0, "detail": ""},
    )
    monkeypatch.setattr(watchdog, "_reprobe_home", lambda home: healthy_reprobe())
    state = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")],
        dispatchable=1,
    )

    summary = watchdog.run(snap=state)

    assert refresh_calls == ["/h/.codex-2"]
    assert summary["healed"] == ["/h/.codex-2"]
    healed = state["codex"]["homes"][0]
    assert healed["verdict"] == "ok"
    assert healed["windows"]["source"] == "live"
    assert healed["windows"]["five_hour"]["used_percent"] == 12
    assert state["codex"]["fleet"]["dispatchable_now"] == 2
    assert not any(key.startswith("codex-suspect:") for key in summary["alerts_sent"])
    assert load_json(paths.refresh_probes_path())["/h/.codex-2"]["result"] == "healed"


def test_refresh_revocation_latches_until_auth_timestamp_changes(
    env_paths, monkeypatch, current_auth
):
    refresh_calls = []

    def revoked(home):
        refresh_calls.append(str(home))
        return {"status": "revoked", "rc": 1, "detail": "refresh token was revoked"}

    monkeypatch.setattr(watchdog.codex, "refresh_via_cli", revoked)
    monkeypatch.setattr(
        watchdog,
        "_reprobe_home",
        lambda home: (_ for _ in ()).throw(AssertionError("revoked token was re-probed")),
    )
    first = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )
    first_summary = watchdog.run(snap=first)

    assert refresh_calls == ["/h/.codex-2"]
    assert first["codex"]["homes"][0]["verdict"] == "auth-revoked"
    assert first_summary["refresh_probes"][0]["result"] == "revoked"

    saved = load_json(paths.refresh_probes_path())
    saved["/h/.codex-2"]["attempted_at"] = (
        now_local() - timedelta(hours=1)
    ).isoformat(timespec="seconds")
    paths.refresh_probes_path().write_text(json.dumps(saved))

    same_auth = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )
    watchdog.run(snap=same_auth)
    assert refresh_calls == ["/h/.codex-2"]
    assert same_auth["codex"]["homes"][0]["verdict"] == "auth-revoked"
    assert same_auth["codex"]["homes"][0]["refresh_probe"]["latched"] is True

    relogged = snap(
        [
            expired_suspect("/h/.codex-2", last_refresh="2026-08-12T12:00:00Z"),
            entry("/h/.codex-3", 5, account="b"),
        ]
    )
    current_auth["last_refresh"] = "2026-08-12T12:00:00Z"
    watchdog.run(snap=relogged)
    assert refresh_calls == ["/h/.codex-2", "/h/.codex-2"]


def test_failed_refresh_is_spaced_and_keeps_suspect_alert(env_paths, monkeypatch):
    refresh_calls = []
    monkeypatch.setattr(
        watchdog.codex,
        "refresh_via_cli",
        lambda home: refresh_calls.append(str(home))
        or {"status": "failed", "rc": 3, "detail": "stream error"},
    )
    monkeypatch.setattr(
        watchdog,
        "_reprobe_home",
        lambda home: {
            "auth": {"status": "ok", "last_refresh": "2026-08-01T00:00:00Z"},
            "probe": {"status": "http-401", "error": "token expired"},
            "observed": None,
        },
    )
    first = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )
    summary = watchdog.run(snap=first)
    second = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )
    watchdog.run(snap=second)

    assert refresh_calls == ["/h/.codex-2"]
    assert summary["refresh_probes"][0]["result"] == "failed"
    assert first["codex"]["homes"][0]["verdict"] == "auth-suspect"
    assert any(key.startswith("codex-suspect:") for key in summary["alerts_sent"])
    assert second["codex"]["homes"][0]["refresh_probe"]["result"] == "failed"


def test_failed_probe_then_heal_sends_one_recovery(env_paths, monkeypatch):
    monkeypatch.setattr(
        watchdog.codex,
        "refresh_via_cli",
        lambda home: {"status": "failed", "rc": 3, "detail": "offline"},
    )
    monkeypatch.setattr(
        watchdog,
        "_reprobe_home",
        lambda home: {
            "auth": {"status": "ok", "last_refresh": "2026-08-01T00:00:00Z"},
            "probe": {"status": "network-error", "error": "offline"},
            "observed": None,
        },
    )
    first = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )
    assert "codex-suspect:/h/.codex-2" in watchdog.run(snap=first)["alerts_sent"]

    saved = load_json(paths.refresh_probes_path())
    saved["/h/.codex-2"]["attempted_at"] = (
        now_local() - timedelta(hours=1)
    ).isoformat(timespec="seconds")
    paths.refresh_probes_path().write_text(json.dumps(saved))
    monkeypatch.setattr(
        watchdog.codex,
        "refresh_via_cli",
        lambda home: {"status": "ok", "rc": 0, "detail": ""},
    )
    monkeypatch.setattr(watchdog, "_reprobe_home", lambda home: healthy_reprobe())
    healed = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )

    summary = watchdog.run(snap=healed)

    assert summary["healed"] == ["/h/.codex-2"]
    assert "codex-suspect:/h/.codex-2" in summary["recovered"]


def test_reprobe_with_unreadable_auth_is_not_reported_healed(env_paths, monkeypatch):
    monkeypatch.setattr(
        watchdog.codex,
        "refresh_via_cli",
        lambda home: {"status": "ok", "rc": 0, "detail": ""},
    )
    fresh = healthy_reprobe()
    fresh["auth"]["status"] = "unreadable"
    monkeypatch.setattr(watchdog, "_reprobe_home", lambda home: fresh)
    state = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )

    summary = watchdog.run(snap=state)

    assert summary["healed"] == []
    assert summary["refresh_probes"][0]["result"] == "failed"
    assert state["codex"]["homes"][0]["verdict"] == "no-auth"


def test_auth_change_after_snapshot_suppresses_refresh(
    env_paths, monkeypatch, current_auth
):
    current_auth["last_refresh"] = "2026-08-22T12:00:00Z"
    monkeypatch.setattr(
        watchdog.codex,
        "refresh_via_cli",
        lambda home: pytest.fail("stale snapshot triggered a refresh"),
    )
    state = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )

    summary = watchdog.run(snap=state)

    assert summary["refresh_probes"] == []
    assert state["codex"]["homes"][0]["refresh_probe"]["result"] \
        == "skipped-auth-changed"


def test_overlapping_watchdogs_claim_refresh_once(env_paths, current_auth):
    started = Event()
    release = Event()
    calls = []

    def refresh(home):
        calls.append(home)
        started.set()
        assert release.wait(timeout=5)
        return {"status": "ok", "rc": 0, "detail": ""}

    first = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )
    second = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_run = executor.submit(
            watchdog.heal_expired_codex_homes,
            first,
            False,
            refresh,
            lambda home: healthy_reprobe(),
        )
        assert started.wait(timeout=5)
        second_run = executor.submit(
            watchdog.heal_expired_codex_homes,
            second,
            False,
            refresh,
            lambda home: healthy_reprobe(),
        )
        assert second_run.result(timeout=5) == []
        release.set()
        assert first_run.result(timeout=5)[0]["result"] == "healed"

    assert calls == ["/h/.codex-2"]


def test_reclaimed_late_lane_is_not_run_by_stale_owner(env_paths, current_auth):
    calls = []

    def refresh(home):
        calls.append(home)
        if home == "/h/.codex-2":
            watchdog._store_refresh_state(
                "/h/.codex-3",
                {
                    "attempted_at": now_local().isoformat(timespec="seconds"),
                    "result": "in-progress",
                    "lease_id": "new-owner",
                    "auth_last_refresh": current_auth["last_refresh"],
                },
            )
        return {"status": "ok", "rc": 0, "detail": ""}

    state = snap(
        [
            expired_suspect("/h/.codex-2"),
            expired_suspect("/h/.codex-3"),
        ]
    )

    events = watchdog.heal_expired_codex_homes(
        state,
        refresh_fn=refresh,
        reprobe_fn=lambda home: healthy_reprobe(),
    )

    assert calls == ["/h/.codex-2"]
    assert [event["home"] for event in events] == ["/h/.codex-2"]


def test_ownership_check_renews_lease_before_another_watchdog_can_reclaim(
    env_paths, current_auth
):
    home = "/h/.codex-2"
    old_attempt = (now_local() - timedelta(hours=1)).isoformat(timespec="seconds")
    watchdog._store_refresh_state(
        home,
        {
            "attempted_at": old_attempt,
            "result": "in-progress",
            "lease_id": "current-owner",
            "auth_last_refresh": current_auth["last_refresh"],
        },
    )

    assert watchdog._refresh_claim_owned(home, "current-owner") is True
    refreshed = load_json(paths.refresh_probes_path())[home]["attempted_at"]
    assert refreshed != old_attempt
    competing_calls = []
    competing = snap(
        [expired_suspect(home), entry("/h/.codex-3", 5, account="b")]
    )

    events = watchdog.heal_expired_codex_homes(
        competing,
        refresh_fn=lambda lane: competing_calls.append(lane)
        or {"status": "ok", "rc": 0, "detail": ""},
        reprobe_fn=lambda lane: healthy_reprobe(),
    )

    assert events == []
    assert competing_calls == []


def test_revoked_or_nonexpired_lanes_are_never_refreshed(env_paths, monkeypatch):
    monkeypatch.setattr(
        watchdog.codex,
        "refresh_via_cli",
        lambda home: (_ for _ in ()).throw(AssertionError("ineligible lane refreshed")),
    )
    revoked = entry("/h/.codex-2", 50, account="a", verdict="auth-revoked")
    revoked["probe"] = {"status": "token-revoked", "error": "token revoked"}
    forbidden = entry("/h/.codex-3", 20, account="b", verdict="auth-suspect")
    forbidden["probe"] = {"status": "http-403", "error": "forbidden"}

    summary = watchdog.run(snap=snap([revoked, forbidden], dispatchable=0))

    assert summary["refresh_probes"] == []


def test_successful_refresh_can_reveal_free_plan_without_false_revocation(
    env_paths, monkeypatch
):
    monkeypatch.setattr(
        watchdog.codex,
        "refresh_via_cli",
        lambda home: {"status": "ok", "rc": 0, "detail": ""},
    )
    reprobe = healthy_reprobe()
    reprobe["probe"]["plan_type"] = "free"
    monkeypatch.setattr(watchdog, "_reprobe_home", lambda home: reprobe)
    state = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )

    summary = watchdog.run(snap=state)

    assert summary["refresh_probes"][0]["result"] == "healed"
    assert state["codex"]["homes"][0]["verdict"] == "free-plan"
    assert not any(key.startswith("codex-revoked:") for key in summary["conditions"])


def test_dry_run_does_not_execute_or_persist_refresh_state(env_paths, monkeypatch):
    monkeypatch.setattr(
        watchdog.codex,
        "refresh_via_cli",
        lambda home: (_ for _ in ()).throw(AssertionError("dry run executed refresh")),
    )
    state = snap(
        [expired_suspect("/h/.codex-2"), entry("/h/.codex-3", 5, account="b")]
    )

    summary = watchdog.run(snap=state, dry_run=True)

    assert summary["refresh_probes"] == []
    assert not paths.refresh_probes_path().exists()
