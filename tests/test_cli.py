import io
import json
import sys

import pytest

from carpool import claude, cli, config, paths, secret_store


def test_help_exposes_generic_watch_without_removed_couplings(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out.lower()
    assert "watch" in help_text
    assert "brief" in help_text
    assert "statusline" not in help_text
    assert "launchd" not in help_text


def test_capacity_json_has_one_normalized_row_per_account(monkeypatch, capsys):
    report = {
        "generated_at": "2026-07-22T12:00:00-04:00",
        "cache": {"checked_at": "2026-07-22T12:00:00-04:00", "hit": True,
                  "ttl_seconds": 120},
        "accounts": [
            {
                "family": "claude",
                "id": "lane@example.com",
                "email": "lane@example.com",
                "home": None,
                "resource": "lane@example.com",
                "five_hour": {"unit": "tokens", "tokens": 120, "used": 120,
                              "capacity": None, "used_percent": None,
                              "remaining_percent": None, "reset_at": None,
                              "confidence": "estimated"},
                "weekly": {"unit": "tokens", "tokens": 450, "used": 450,
                           "capacity": None, "used_percent": None,
                           "remaining_percent": None, "reset_at": None,
                           "confidence": "estimated"},
                "learned_capacity": None,
                "limited_until": None,
                "confidence": "estimated",
                "dispatchable": True,
                "status": "estimated",
            }
        ],
        "families": {
            "codex": {"score": 0, "best_resource": None, "earliest_reset": None,
                      "dispatchable": 0},
            "claude": {"score": 100, "best_resource": "lane@example.com",
                       "earliest_reset": None, "dispatchable": 1},
        },
    }
    monkeypatch.setattr(cli.capacity, "build", lambda: report)

    assert cli.main(["capacity", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output == report
    row = output["accounts"][0]
    assert {
        "family", "id", "five_hour", "weekly", "learned_capacity",
        "limited_until", "confidence",
    } <= row.keys()


def test_enroll_uses_configured_prefix_store_and_accounts_file(monkeypatch, capsys):
    config.save(
        {
            "accounts": ["alpha@example.com"],
            "enrolled": {},
            "codex_homes": [],
            "secret_name_prefix": "lane-token-",
            "notify_cmd": ["notify-tool"],
        }
    )
    stored = []
    monkeypatch.setattr(sys, "stdin", io.StringIO("test-token\n"))
    monkeypatch.setattr(
        claude,
        "probe_oauth_usage",
        lambda token: {
            "status": "ok",
            "five_hour": {"used_percent": 12},
            "seven_day": {"used_percent": 34},
        },
    )
    monkeypatch.setattr(secret_store, "set", lambda name, value: stored.append((name, value)) or True)

    assert cli.main(["enroll", "alpha@example.com"]) == 0

    assert stored == [("lane-token-alpha@example.com", "test-token")]
    saved = config.load(strict=True)
    assert saved["enrolled"] == {"alpha@example.com": "lane-token-alpha@example.com"}
    assert saved["notify_cmd"] == ["notify-tool"]
    rendered = config.accounts_path().read_text()
    assert json.loads(rendered) == saved
    captured = capsys.readouterr()
    assert "test-token" not in captured.out + captured.err


def test_enroll_store_failure_does_not_mark_account_enrolled(monkeypatch, capsys):
    config.save({"accounts": ["beta@example.com"], "enrolled": {}, "codex_homes": []})
    monkeypatch.setattr(sys, "stdin", io.StringIO("test-token\n"))
    monkeypatch.setattr(claude, "probe_oauth_usage", lambda token: {"status": "ok"})
    monkeypatch.setattr(secret_store, "set", lambda name, value: False)

    assert cli.main(["enroll", "beta@example.com"]) == 1

    assert config.load(strict=True)["enrolled"] == {}
    captured = capsys.readouterr()
    assert "secret store failed" in captured.err.lower()
    assert "test-token" not in captured.out + captured.err


@pytest.mark.parametrize("probe_status", ["http-403", "rate-limited"])
def test_enroll_accepts_inference_only_token_status_and_clears_cooldown(
    monkeypatch, capsys, probe_status
):
    email = "lane@example.com"
    config.save({"accounts": [email], "enrolled": {}, "codex_homes": []})
    paths.cooldowns_path().parent.mkdir(parents=True, exist_ok=True)
    paths.cooldowns_path().write_text(json.dumps({email.upper(): "2099-01-01T00:00:00Z"}))
    monkeypatch.setattr(sys, "stdin", io.StringIO("inference-token\n"))
    monkeypatch.setattr(
        claude,
        "probe_oauth_usage",
        lambda token: {"status": probe_status},
    )
    stored = []
    monkeypatch.setattr(
        secret_store,
        "set",
        lambda name, value: stored.append((name, value)) or True,
    )

    assert cli.main(["enroll", email]) == 0

    assert stored == [(f"claude-quota-{email}", "inference-token")]
    assert json.loads(paths.cooldowns_path().read_text()) == {}
    output = capsys.readouterr().out
    assert "expected for an inference-only setup token" in output
    assert "inference-token" not in output


def test_enroll_rejects_bad_token_before_secret_store(monkeypatch, capsys):
    config.save({"accounts": ["beta@example.com"], "enrolled": {}, "codex_homes": []})
    monkeypatch.setattr(sys, "stdin", io.StringIO("test-token\n"))
    monkeypatch.setattr(claude, "probe_oauth_usage", lambda token: {"status": "token-invalid"})
    monkeypatch.setattr(secret_store, "set", lambda *args: pytest.fail("invalid token was stored"))

    assert cli.main(["enroll", "beta@example.com"]) == 1
    assert config.load(strict=True)["enrolled"] == {}
    assert "rejected" in capsys.readouterr().err.lower()
