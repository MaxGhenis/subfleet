"""Unified public CLI routing contracts."""

from __future__ import annotations

import pytest

from carpool import cli, login


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
    from carpool import delegate

    seen = []
    monkeypatch.setattr(delegate, "main", lambda argv: seen.extend(argv) or 0)

    assert cli.main(["run", "-m", "sol", "--dry-run", "do work"]) == 0
    assert seen == ["-m", "sol", "--dry-run", "do work"]


@pytest.mark.parametrize(
    ("command", "script"),
    [
        ("codex", "carpool-codex"),
        ("claude", "carpool-claude"),
        ("mirror", "carpool-mirror"),
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
