import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

from subfleet import cli, paths, reset_policy, snapshot
from subfleet.util import now_local


NOW = now_local().replace(microsecond=0)


def row(
    lane: int,
    *,
    verdict="limited",
    limit_reached=True,
    weekly_used=100,
    reset_days=1,
    applicable=1,
    shadowed=False,
    in_flight=0,
):
    home = f"/h/.codex-{lane}"
    return {
        "home": home,
        "email": f"lane-{lane}@x.com",
        "account_id": f"acct-{lane}",
        "verdict": verdict,
        "duplicate_of": None,
        "probe": {"status": "ok", "limit_reached": limit_reached},
        "reset_credits": {"available": applicable, "applicable": applicable},
        "windows": {
            "weekly": {
                "used_percent": weekly_used,
                "reset_at": (NOW + timedelta(days=reset_days)).isoformat(),
            },
            "source": "live",
        },
        "shadowed_by_app": shadowed,
        "in_flight": in_flight,
        "recent_errors": {"usage_limit": [], "auth_revoked": []},
    }


def config(*, enabled=True, floor=15, interval=30):
    return {
        "enabled": enabled,
        "headroom_floor_pct": floor,
        "min_interval_min": interval,
    }


def test_trigger_boundaries_and_no_dispatchable_rule():
    limited = row(1)
    exactly = row(2, verdict="ok", limit_reached=False, weekly_used=85, applicable=0)
    below = row(3, verdict="ok", limit_reached=False, weekly_used=85.01, applicable=0)

    decision = reset_policy.evaluate(
        [limited, exactly], config=config(), now=NOW,
        dispatchable_homes={exactly["home"]},
    )
    assert decision["triggered"] is False
    assert decision["weekly_headroom_pct"] == 15

    decision = reset_policy.evaluate(
        [limited, below], config=config(), now=NOW,
        dispatchable_homes={below["home"]},
    )
    assert decision["triggered"] is True
    assert decision["trigger_reason"] == "weekly-headroom-below-floor"

    decision = reset_policy.evaluate(
        [limited], config=config(), now=NOW, dispatchable_homes=set()
    )
    assert decision["triggered"] is True
    assert decision["trigger_reason"] == "no-dispatchable-lanes"


def test_candidates_order_furthest_reset_then_in_flight_then_lane():
    candidates = reset_policy.ordered_candidates([
        row(3, reset_days=2),
        row(2, reset_days=5, in_flight=2),
        row(1, reset_days=5, in_flight=0),
    ])
    assert [candidate["lane_number"] for candidate in candidates] == [1, 2, 3]


def test_interval_blocks_second_evaluation_until_boundary():
    state = {
        "last_redeemed_at": (NOW - timedelta(minutes=29, seconds=59)).isoformat(),
        "lane": "/h/.codex-9",
        "credit_id": "spent",
    }
    blocked = reset_policy.evaluate(
        [row(1)], config=config(), state=state, now=NOW,
        dispatchable_homes=set(),
    )
    assert blocked["status"] == "interval-blocked"

    allowed = reset_policy.evaluate(
        [row(1)], config=config(), state={
            **state,
            "last_redeemed_at": (NOW - timedelta(minutes=30)).isoformat(),
        }, now=NOW, dispatchable_homes=set(),
    )
    assert allowed["status"] == "ready"


def test_shadowed_candidate_excluded_unless_it_is_the_only_option():
    shadowed = row(2, reset_days=9, shadowed=True)
    ordinary = row(1, reset_days=1)
    assert [item["lane_number"] for item in reset_policy.ordered_candidates(
        [shadowed, ordinary]
    )] == [1]
    assert [item["lane_number"] for item in reset_policy.ordered_candidates(
        [shadowed]
    )] == [2]


def test_only_server_limited_applicable_codex_rows_are_candidates():
    merely_exhausted = row(1, limit_reached=False)
    wrong_status = row(2, verdict="ok")
    no_credit = row(3, applicable=0)
    assert reset_policy.ordered_candidates([merely_exhausted, wrong_status, no_credit]) == []


def test_config_defaults_and_override(tmp_path):
    assert reset_policy.load_config(tmp_path / "missing.json") == reset_policy.DEFAULTS
    cfg = tmp_path / "accounts.json"
    cfg.write_text(json.dumps({
        "auto_reset": {
            "enabled": False,
            "headroom_floor_pct": 12.5,
            "min_interval_min": 45,
        }
    }))
    assert reset_policy.load_config(cfg) == {
        "enabled": False,
        "headroom_floor_pct": 12.5,
        "min_interval_min": 45.0,
    }


def test_reset_history_has_exact_required_shape():
    reset_policy.record_redemption(
        lane="/h/.codex-4",
        email="lane-4@x.com",
        credit_id="credit-4",
        remaining=2,
        occurred=NOW,
        metadata={"automatic": False},
    )
    event = json.loads(paths.history_path().read_text())
    assert event == {
        "ts": NOW.isoformat(),
        "event": "reset",
        "lane": "/h/.codex-4",
        "email": "lane-4@x.com",
        "credit_id": "credit-4",
        "remaining": 2,
    }
    state = json.loads(paths.reset_policy_path().read_text())
    assert state["lane"] == "/h/.codex-4"
    assert state["credit_id"] == "credit-4"


class SequenceOpener:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, request, timeout):
        assert self.steps, f"unexpected {request.get_method()} {request.full_url}"
        method, url, response = self.steps.pop(0)
        assert (request.get_method(), request.full_url) == (method, url)
        self.calls.append(request)
        return 200, json.dumps(response).encode()


def usage(*, limited, weekly, email, reset_at):
    return {
        "email": email,
        "plan_type": "pro",
        "rate_limit": {
            "allowed": not limited,
            "limit_reached": limited,
            "primary_window": {
                "used_percent": weekly,
                "limit_window_seconds": 604800,
                "reset_at": int(reset_at.timestamp()),
            },
        },
        "rate_limit_reset_credits": {
            "available_count": 1,
            "applicable_available_count": 1 if limited else 0,
        },
    }


def test_run_consumes_one_polls_through_propagation_and_blocks_second(
    monkeypatch, codex_home_factory
):
    make_home, _ = codex_home_factory
    home = make_home(".codex-1", "acct-1", "lane-1@x.com")
    candidate = row(1, reset_days=4)
    candidate["home"] = str(home)
    candidate["lane"] = str(home)
    new_reset = NOW + timedelta(days=7)
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [{
                "id": "credit-one",
                "status": "available",
                "reset_type": "codex_rate_limits",
            }]
        }),
        ("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume", {
            "code": "reset", "windows_reset": 2,
        }),
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage(limited=True, weekly=100, email="lane-1@x.com", reset_at=new_reset)),
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage(limited=False, weekly=0, email="lane-1@x.com", reset_at=new_reset)),
    ])

    decision = reset_policy.run(
        [candidate], config=config(), now=NOW, opener=opener,
        dispatchable_homes=set(), sleep_fn=lambda seconds: None,
        poll_timeout=10, poll_interval=10,
    )
    assert decision["status"] == "redeemed"
    assert decision["redeemed"]["home"] == str(home)
    assert decision["redeemed"]["poll_attempts"] == 2
    assert decision["redeemed"]["weekly_reset_at"] == new_reset.isoformat()
    post_body = json.loads(opener.calls[1].data)
    assert post_body["credit_id"] == "credit-one"
    assert str(uuid.UUID(post_body["redeem_request_id"])) == post_body["redeem_request_id"]
    assert not opener.steps

    blocked = reset_policy.run(
        [candidate], config=config(), now=NOW + timedelta(minutes=1),
        opener=lambda *args: (_ for _ in ()).throw(AssertionError("network called")),
        dispatchable_homes=set(),
    )
    assert blocked["status"] == "interval-blocked"
    assert len(paths.history_path().read_text().splitlines()) == 1


def test_dry_run_lists_all_concrete_candidates_without_post(
    codex_home_factory,
):
    make_home, _ = codex_home_factory
    first = make_home(".codex-1", "acct-1", "one@x.com")
    second = make_home(".codex-2", "acct-2", "two@x.com")
    rows = [row(1, reset_days=5), row(2, reset_days=2)]
    rows[0]["home"] = str(first)
    rows[1]["home"] = str(second)
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [{"id": "one", "status": "available", "reset_type": "codex_rate_limits"}]
        }),
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [{"id": "two", "status": "available", "reset_type": "codex_rate_limits"}]
        }),
    ])
    decision = reset_policy.run(
        rows, dry_run=True, config=config(), now=NOW, opener=opener,
        dispatchable_homes=set(),
    )
    assert decision["status"] == "dry-run-ready"
    assert [item["credit_id"] for item in decision["candidates"]] == ["one", "two"]
    assert all(request.get_method() == "GET" for request in opener.calls)


def test_dry_run_inspects_concrete_candidates_even_when_not_triggered(
    codex_home_factory,
):
    make_home, _ = codex_home_factory
    limited_home = make_home(".codex-1", "acct-1", "one@x.com")
    limited = row(1, reset_days=5)
    limited["home"] = str(limited_home)
    healthy = row(
        2, verdict="ok", limit_reached=False, weekly_used=20, applicable=0
    )
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [{
                "id": "inspect-me",
                "status": "available",
                "reset_type": "codex_rate_limits",
            }]
        }),
    ])

    decision = reset_policy.run(
        [limited, healthy], dry_run=True, config=config(), now=NOW,
        opener=opener, dispatchable_homes={healthy["home"]},
    )

    assert decision["status"] == "not-triggered"
    assert [candidate["credit_id"] for candidate in decision["candidates"]] == [
        "inspect-me"
    ]
    assert len(opener.calls) == 1


def test_concrete_credit_gate_falls_back_to_shadowed_lane(
    codex_home_factory,
):
    make_home, _ = codex_home_factory
    ordinary_home = make_home(".codex-1", "acct-1", "one@x.com")
    shadow_home = make_home(".codex-2", "acct-2", "two@x.com")
    ordinary = row(1, reset_days=9)
    shadowed = row(2, reset_days=2, shadowed=True)
    ordinary["home"] = str(ordinary_home)
    shadowed["home"] = str(shadow_home)
    new_reset = NOW + timedelta(days=7)
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [],
        }),
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [{
                "id": "shadow-credit",
                "status": "available",
                "reset_type": "codex_rate_limits",
            }]
        }),
        ("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume", {
            "code": "reset", "windows_reset": 2,
        }),
    ])

    decision = reset_policy.run(
        [ordinary, shadowed], config=config(), now=NOW, opener=opener,
        dispatchable_homes=set(), clock_fn=lambda: NOW,
        probe_fn=lambda auth, opener=None: {
            "status": "ok",
            "allowed": True,
            "limit_reached": False,
            "weekly": {"used_percent": 0, "reset_at": new_reset.isoformat()},
        },
    )

    assert decision["status"] == "redeemed"
    assert decision["redeemed"]["home"] == str(shadow_home)
    assert decision["redeemed"]["credit_id"] == "shadow-credit"
    assert [request.get_method() for request in opener.calls] == ["GET", "GET", "POST"]


def test_consume_is_audited_before_stale_poll_and_uses_actual_consume_clock(
    codex_home_factory,
):
    make_home, _ = codex_home_factory
    home = make_home(".codex-1", "acct-1", "one@x.com")
    candidate = row(1, reset_days=1)
    candidate["home"] = str(home)
    consumed_at = NOW + timedelta(minutes=4)
    old_reset = NOW + timedelta(days=1)
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [{
                "id": "durable-first",
                "status": "available",
                "reset_type": "codex_rate_limits",
            }]
        }),
        ("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume", {
            "code": "reset", "windows_reset": 2,
        }),
    ])
    probes = []

    def stale_probe(auth, opener=None):
        # The exact history event and meta-only run must exist before the first
        # potentially interrupted propagation request.
        event = json.loads(paths.history_path().read_text())
        assert event["credit_id"] == "durable-first"
        assert event["ts"] == consumed_at.isoformat()
        assert any(paths.runs_dir().iterdir())
        probes.append(True)
        return {
            "status": "ok",
            "allowed": False,
            "limit_reached": True,
            "primary": {
                "used_percent": 100,
                "window_seconds": 604800,
                "reset_at": old_reset.isoformat(),
            },
        }

    decision = reset_policy.run(
        [candidate], config=config(), now=NOW, opener=opener,
        dispatchable_homes=set(), clock_fn=lambda: consumed_at,
        probe_fn=stale_probe, sleep_fn=lambda seconds: None,
        poll_timeout=10, poll_interval=10,
    )

    expected_reset = consumed_at + timedelta(days=7)
    assert decision["status"] == "redeemed"
    assert decision["redeemed"]["propagated"] is False
    assert decision["redeemed"]["weekly_reset_at"] == expected_reset.isoformat()
    assert len(probes) == 2
    state = json.loads(paths.reset_policy_path().read_text())
    assert state["last_redeemed_at"] == consumed_at.isoformat()

    reset_policy.apply_redemption_to_snapshot_row(
        candidate, decision["redeemed"], now=consumed_at
    )
    assert candidate["verdict"] == "ok"
    assert candidate["windows"]["weekly"]["reset_at"] == expected_reset.isoformat()
    assert candidate["windows"]["five_hour"]["window_seconds"] == 18000
    assert snapshot.rank_for_dispatch([candidate], now=consumed_at)[0]["home"] == str(home)

    blocked = reset_policy.evaluate(
        [row(2)], config=config(), state=state,
        now=consumed_at + timedelta(minutes=29, seconds=59),
        dispatchable_homes=set(),
    )
    assert blocked["status"] == "interval-blocked"


def test_fleet_remaining_is_unknown_instead_of_undercounted():
    known = row(1, applicable=2)
    unknown = row(2)
    unknown["reset_credits"] = {"available": None, "applicable": 1}
    duplicate = row(3, applicable=9)
    duplicate["duplicate_of"] = known["home"]

    assert reset_policy.fleet_credits_remaining(
        [known, unknown, duplicate], known["home"]
    ) is None
    assert reset_policy.fleet_credits_remaining(
        [known, duplicate], known["home"]
    ) == 1


def test_picker_returns_redeemed_lane_after_simulated_propagation_delay(
    monkeypatch, codex_home_factory, capsys
):
    make_home, _ = codex_home_factory
    home = make_home(".codex-3", "acct-3", "lane-3@x.com")
    candidate = row(3, reset_days=4)
    candidate["home"] = str(home)
    snap = {
        "generated_at": NOW.isoformat(),
        "codex": {
            "homes": [candidate],
            "duplicates": [],
            "fleet": {
                "total_homes": 1,
                "dispatchable_now": 0,
                "best_home": None,
                "earliest_reset": candidate["windows"]["weekly"]["reset_at"],
            },
        },
    }
    monkeypatch.setattr(cli, "_load_snapshot", lambda cached: snap)
    new_reset = NOW + timedelta(days=7)
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [{
                "id": "credit-picker", "status": "available",
                "reset_type": "codex_rate_limits",
            }]
        }),
        ("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume", {
            "code": "reset", "windows_reset": 2,
        }),
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage(limited=True, weekly=100, email="lane-3@x.com", reset_at=new_reset)),
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage(limited=False, weekly=0, email="lane-3@x.com", reset_at=new_reset)),
    ])
    args = SimpleNamespace(
        cached=False, no_handicap=False, handicap=10.0, min_headroom=5.0,
        json=False, all=False,
    )

    assert cli.cmd_pick(args, opener=opener, sleep_fn=lambda seconds: None) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == str(home)
    assert (
        "subfleet: redeemed reset on lane-3@x.com (0 credits remain fleet-wide); "
        f"weekly reset now {new_reset.isoformat()}"
    ) in captured.err
    assert not opener.steps
