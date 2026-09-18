"""Safety gates and sequential behavior for gifted Codex reset credits."""

import json
import uuid
from types import SimpleNamespace

from subfleet import cli, codex, paths


def usage_payload(*, limited: bool, weekly: int, email: str) -> dict:
    return {
        "email": email,
        "plan_type": "pro",
        "rate_limit": {
            "allowed": not limited,
            "limit_reached": limited,
            "primary_window": {
                "used_percent": weekly,
                "limit_window_seconds": 604800,
                "reset_at": 1788000000,
            },
        },
        "rate_limit_reset_credits": {
            "available_count": 1,
            "applicable_available_count": 1 if limited else 0,
        },
    }


def available_credit(credit_id="credit-1") -> dict:
    return {
        "id": credit_id,
        "reset_type": "codex_rate_limits",
        "status": "available",
        "title": "Full reset",
    }


class SequenceOpener:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, request, timeout):
        assert self.steps, f"unexpected request {request.get_method()} {request.full_url}"
        expected_method, expected_url, response = self.steps.pop(0)
        assert request.get_method() == expected_method
        assert request.full_url == expected_url
        self.calls.append(request)
        return 200, json.dumps(response).encode()


def reset_args(target="1", *, dry_run=False, policy=False):
    return SimpleNamespace(target=target, dry_run=dry_run, policy=policy)


def one_home(monkeypatch, codex_home_factory, *, slot="1", account="acct-1"):
    make_home, _ = codex_home_factory
    home = make_home(f".codex-{slot}", account, f"lane-{slot}@example.com")
    monkeypatch.setenv("SUBFLEET_CODEX_HOMES", str(home))
    return home


def test_not_limited_gate_never_lists_or_consumes(
    monkeypatch, codex_home_factory, capsys
):
    one_home(monkeypatch, codex_home_factory)
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=False, weekly=20, email="lane-1@example.com")),
    ])

    assert cli.cmd_reset_codex(reset_args(), opener=opener) == 1
    assert not opener.steps
    assert "not LIMITED" in capsys.readouterr().err
    assert not paths.runs_dir().exists()


def test_no_qualifying_credit_gate_never_posts(
    monkeypatch, codex_home_factory, capsys
):
    one_home(monkeypatch, codex_home_factory)
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=True, weekly=100, email="lane-1@example.com")),
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [
                {"id": "wrong-kind", "reset_type": "other", "status": "available"},
                {"id": "spent", "reset_type": "codex_rate_limits", "status": "redeemed"},
            ],
            "available_count": 1,
            "immediate_reset_purchase_eligible": False,
        }),
    ])

    assert cli.cmd_reset_codex(reset_args(), opener=opener) == 1
    assert not opener.steps
    assert "no available codex_rate_limits reset credit" in capsys.readouterr().err
    assert not paths.runs_dir().exists()


def test_success_consumes_reprobes_prints_weekly_change_and_records_event(
    monkeypatch, codex_home_factory, capsys
):
    home = one_home(monkeypatch, codex_home_factory)
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=True, weekly=100, email="lane-1@example.com")),
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [available_credit()], "available_count": 1,
            "immediate_reset_purchase_eligible": False,
        }),
        ("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
         {"code": "reset", "windows_reset": 2}),
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=False, weekly=0, email="lane-1@example.com")),
    ])

    assert cli.cmd_reset_codex(reset_args(), opener=opener) == 0
    assert not opener.steps
    post = opener.calls[2]
    body = json.loads(post.data)
    assert body["credit_id"] == "credit-1"
    assert str(uuid.UUID(body["redeem_request_id"])) == body["redeem_request_id"]
    out = capsys.readouterr().out
    assert "weekly 100% -> 0%" in out and "code reset" in out

    run_dirs = [path for path in paths.runs_dir().iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    assert {path.name for path in run_dirs[0].iterdir()} == {"meta.json"}
    meta = json.loads((run_dirs[0] / "meta.json").read_text())
    assert meta["event"] == "reset" and meta["lane"] == str(home)
    assert meta["event_meta"]["response"] == {
        "status": "ok", "code": "reset", "windows_reset": 2, "error": None,
    }
    assert meta["event_meta"]["before"]["weekly_used_percent"] == 100
    assert meta["event_meta"]["after"]["weekly_used_percent"] == 0
    history = json.loads(paths.history_path().read_text())
    assert history == {
        "ts": history["ts"],
        "event": "reset",
        "lane": str(home),
        "email": "lane-1@example.com",
        "credit_id": "credit-1",
        "remaining": 0,
    }


def test_manual_consume_is_durable_before_post_probe(
    monkeypatch, codex_home_factory
):
    one_home(monkeypatch, codex_home_factory)
    probes = []

    def parsed_probe(*, limited, weekly):
        return {
            "status": "ok",
            "email": "lane-1@example.com",
            "allowed": not limited,
            "limit_reached": limited,
            "primary": {
                "used_percent": weekly,
                "window_seconds": 604800,
                "reset_at": "2026-08-29T12:00:00+00:00",
            },
            "secondary": None,
            "reset_credits": {
                "available": 1,
                "applicable": 1 if limited else 0,
            },
        }

    def fake_probe(auth, opener=None):
        if not probes:
            probes.append("before")
            return parsed_probe(limited=True, weekly=100)
        event = json.loads(paths.history_path().read_text())
        assert event["credit_id"] == "credit-immediate"
        assert any(paths.runs_dir().iterdir())
        probes.append("after")
        return parsed_probe(limited=False, weekly=0)

    monkeypatch.setattr(codex, "probe_wham", fake_probe)
    monkeypatch.setattr(codex, "list_reset_credits", lambda auth, opener=None: {
        "status": "ok", "credits": [available_credit("credit-immediate")]
    })
    monkeypatch.setattr(codex, "consume_reset_credit", lambda *args, **kwargs: {
        "status": "ok",
        "code": "reset",
        "windows_reset": 2,
        "redeem_request_id": "request-id",
    })

    assert cli.cmd_reset_codex(reset_args()) == 0
    assert probes == ["before", "after"]


def test_manual_all_never_claims_unknown_fleet_remaining(
    monkeypatch, codex_home_factory
):
    make_home, _ = codex_home_factory
    first = make_home(".codex-1", "acct-1", "lane-1@example.com")
    second = make_home(".codex-2", "acct-2", "lane-2@example.com")
    monkeypatch.setenv("SUBFLEET_CODEX_HOMES", f"{first}:{second}")
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=True, weekly=100, email="lane-1@example.com")),
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [available_credit("credit-1")], "available_count": 1,
        }),
        ("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
         {"code": "reset", "windows_reset": 2}),
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=False, weekly=0, email="lane-1@example.com")),
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=True, weekly=100, email="lane-2@example.com")),
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [available_credit("credit-2")], "available_count": 1,
        }),
        ("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
         {"code": "reset", "windows_reset": 2}),
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=False, weekly=0, email="lane-2@example.com")),
    ])

    assert cli.cmd_reset_codex(reset_args("all"), opener=opener) == 0
    events = [json.loads(line) for line in paths.history_path().read_text().splitlines()]
    assert [event["remaining"] for event in events] == [None, 0]


def test_non_success_code_stops_all_before_next_lane(
    monkeypatch, codex_home_factory, capsys
):
    make_home, _ = codex_home_factory
    first = make_home(".codex-1", "acct-1", "lane-1@example.com")
    second = make_home(".codex-2", "acct-2", "lane-2@example.com")
    monkeypatch.setenv("SUBFLEET_CODEX_HOMES", f"{first}:{second}")
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=True, weekly=100, email="lane-1@example.com")),
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [available_credit()], "available_count": 1,
        }),
        ("POST", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
         {"code": "nothing_to_reset", "windows_reset": 0}),
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=True, weekly=100, email="lane-1@example.com")),
    ])

    assert cli.cmd_reset_codex(reset_args("all"), opener=opener) == 1
    assert not opener.steps
    assert all(
        dict(request.header_items())["Chatgpt-account-id"] == "acct-1"
        for request in opener.calls
    )
    captured = capsys.readouterr()
    assert "code nothing_to_reset" in captured.out
    assert "stopping after non-success code nothing_to_reset" in captured.err


def test_dry_run_lists_credit_without_post_reprobe_or_ledger(
    monkeypatch, codex_home_factory, capsys
):
    one_home(monkeypatch, codex_home_factory)
    opener = SequenceOpener([
        ("GET", "https://chatgpt.com/backend-api/wham/usage",
         usage_payload(limited=True, weekly=100, email="lane-1@example.com")),
        ("GET", "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {
            "credits": [available_credit("credit-dry")], "available_count": 1,
        }),
    ])

    assert cli.cmd_reset_codex(reset_args(dry_run=True), opener=opener) == 0
    assert not opener.steps
    assert "would consume reset credit credit-dry" in capsys.readouterr().out
    assert not paths.runs_dir().exists()


def test_cli_parser_routes_reset_codex_dry_run(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_reset_codex", lambda args: seen.append(args) or 0)
    assert cli.main(["reset", "codex", "7", "--dry-run"]) == 0
    assert seen[0].target == "7" and seen[0].dry_run is True


def test_cli_parser_routes_policy_without_target(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_reset_codex", lambda args: seen.append(args) or 0)
    assert cli.main(["reset", "codex", "--policy", "--dry-run"]) == 0
    assert seen[0].target is None and seen[0].policy is True and seen[0].dry_run is True
