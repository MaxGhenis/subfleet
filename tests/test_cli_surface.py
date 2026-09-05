"""Unified public CLI routing contracts."""

from __future__ import annotations

import pytest

from subfleet import cli, login


def test_help_exposes_unified_subcommand_surface(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    for command in (
        "status", "capacity", "runs", "pick", "run", "codex", "claude",
        "login", "enroll", "mirror", "errors", "watch", "brief",
    ):
        assert command in output


def test_pick_defaults_to_codex(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_pick", lambda args: seen.setdefault("codex", args) and 0)
    monkeypatch.setattr(
        cli,
        "cmd_claude_pick",
        lambda args: pytest.fail("default pick routed to Claude"),
    )

    assert cli.main(["pick"]) == 0
    assert seen["codex"].min_headroom == 5.0


def test_pick_claude_routes_repeatable_exclusions(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli,
        "cmd_claude_pick",
        lambda args: seen.setdefault("args", args) and 0,
    )

    assert cli.main(
        ["pick", "claude", "--exclude", "one@example.com", "--exclude", "two@example.com"]
    ) == 0
    assert seen["args"].exclude == ["one@example.com", "two@example.com"]


def test_bare_and_status_flags_route_to_status(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_status", lambda args: calls.append(args) or 0)

    assert cli.main([]) == 0
    assert cli.main(["--cached"]) == 0
    assert calls[1].cached is True


def test_run_routes_directly_to_delegate(monkeypatch):
    from subfleet import delegate

    seen = []
    monkeypatch.setattr(delegate, "main", lambda argv: seen.extend(argv) or 0)

    assert cli.main(["run", "-m", "sol", "--dry-run", "do work"]) == 0
    assert seen == ["-m", "sol", "--dry-run", "do work"]


@pytest.mark.parametrize(
    ("command", "script"),
    [
        ("codex", "subfleet-codex"),
        ("claude", "subfleet-claude"),
        ("mirror", "subfleet-mirror"),
    ],
)
def test_pass_through_commands_exec_sibling_scripts(monkeypatch, command, script):
    seen = {}
    monkeypatch.setattr(
        cli,
        "_exec_tool",
        lambda name, rest: seen.update(name=name, rest=rest) or 0,
    )

    assert cli.main([command, "--example", "value"]) == 0
    assert seen == {"name": script, "rest": ["--example", "value"]}


def test_login_routes_target_and_interaction_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        login,
        "codex_login",
        lambda target, **kwargs: seen.update(target=target, **kwargs) or 0,
    )

    assert cli.main(["login", "codex", "app", "--no-watch", "--no-open"]) == 0
    assert seen == {"target": "app", "watch": False, "open_browser": False}


def test_brief_renders_cached_snapshot(monkeypatch, capsys):
    state = {"generated_at": "example"}
    monkeypatch.setattr(cli, "_load_snapshot", lambda cached: state if cached else None)
    monkeypatch.setattr(cli.render, "brief_md", lambda snap: f"brief:{snap['generated_at']}")

    assert cli.main(["brief"]) == 0
    assert capsys.readouterr().out == "brief:example\n"


class TestApiLaneCheck:
    """`subfleet _api-lane-check HOME`: the runner and shim's subscription-only gate."""

    def test_api_key_home_is_refused_with_rc_7(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("SUBFLEET_ALLOW_API_LANE", raising=False)
        home = tmp_path / "codex-api"
        home.mkdir()
        (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-test", "auth_mode": "apikey"}')
        assert cli.main(["_api-lane-check", str(home)]) == 7
        err = capsys.readouterr().err
        assert "subfleet:" in err and "ChatGPT subscriptions only" in err

    def test_chatgpt_and_missing_homes_pass(self, tmp_path, capsys):
        home = tmp_path / "codex-1"
        home.mkdir()
        (home / "auth.json").write_text('{"OPENAI_API_KEY": null, "auth_mode": "chatgpt", "tokens": {}}')
        assert cli.main(["_api-lane-check", str(home)]) == 0
        assert cli.main(["_api-lane-check", str(tmp_path / "absent")]) == 0
        assert capsys.readouterr().err == ""

    def test_override_env_passes(self, tmp_path, monkeypatch):
        home = tmp_path / "codex-api"
        home.mkdir()
        (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-test", "auth_mode": "apikey"}')
        monkeypatch.setenv("SUBFLEET_ALLOW_API_LANE", "1")
        assert cli.main(["_api-lane-check", str(home)]) == 0
