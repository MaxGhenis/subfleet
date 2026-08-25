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
        "wait", "kill", "sessions", "notify", "hooks", "tickle", "muster",
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


def test_runs_routes_filters_and_reap(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_runs", lambda args: seen.append(args) or 0)

    assert cli.main(["runs", "--mine", "--running", "--last", "5"]) == 0
    assert seen[0].mine is True and seen[0].running is True and seen[0].last == 5
    assert seen[0].runs_command is None
    assert cli.main(["runs", "reap", "--dry-run", "--grace", "0"]) == 0
    assert seen[1].runs_command == "reap"
    assert seen[1].dry_run is True and seen[1].grace == 0.0


def test_wait_routes_ids_and_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_wait", lambda args: seen.setdefault("args", args) and 0)

    assert cli.main(["wait", "a", "b", "--mine", "--last", "--timeout", "30",
                     "--interval", "0.5", "--cat"]) == 0
    args = seen["args"]
    assert args.ids == ["a", "b"] and args.mine is True and args.last is True
    assert args.timeout == 30.0 and args.interval == 0.5 and args.cat is True


def test_kill_requires_and_routes_ids(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_kill", lambda args: seen.setdefault("args", args) and 0)

    assert cli.main(["kill", "one", "two"]) == 0
    assert seen["args"].ids == ["one", "two"]
    with pytest.raises(SystemExit) as error:
        cli.main(["kill"])
    assert error.value.code == 2


def test_sessions_and_notify_route(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_sessions", lambda args: seen.setdefault("sessions", args) and 0)
    monkeypatch.setattr(cli, "cmd_notify_session", lambda args: seen.setdefault("notify", args) and 0)

    assert cli.main(["sessions", "--json"]) == 0
    assert seen["sessions"].json is True
    assert cli.main(["notify", "hello", "--session", "sid", "--mode", "prompting"]) == 0
    notify = seen["notify"]
    assert notify.text == "hello" and notify.session == "sid" and notify.mode == "prompting"


def test_hooks_defaults_to_status_and_routes_subcommands(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_hooks", lambda args: seen.append(args) or 0)

    assert cli.main(["hooks"]) == 0
    assert seen[0].hooks_command == "status"
    assert cli.main(["hooks", "install", "--dry-run"]) == 0
    assert seen[1].hooks_command == "install" and seen[1].dry_run is True
    assert cli.main(["hooks", "uninstall"]) == 0
    assert seen[2].hooks_command == "uninstall"


def test_tickle_and_muster_route(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_tickle", lambda args: seen.setdefault("tickle", args) and 0)
    monkeypatch.setattr(cli, "cmd_muster", lambda args: seen.setdefault("muster", args) and 0)
    monkeypatch.setattr(cli, "cmd_tickle_worker", lambda args: seen.setdefault("worker", args) and 0)

    assert cli.main(["tickle", "--session", "sid", "--force", "--dry-run"]) == 0
    args = seen["tickle"]
    assert args.session == "sid" and args.force is True and args.dry_run is True
    assert cli.main(["muster", "--dry-run"]) == 0
    assert seen["muster"].dry_run is True
    assert cli.main(["_tickle", "--session", "sid", "--delay", "2.5"]) == 0
    assert seen["worker"].session == "sid" and seen["worker"].delay == 2.5


def test_record_run_accepts_the_adopt_phase(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_record_run", lambda args: seen.setdefault("args", args) and 0)

    assert cli.main(["_record-run", "--phase", "adopt", "--run-id", "r1",
                     "--pid", "4242", "--launcher", "subfleet run"]) == 0
    args = seen["args"]
    assert args.phase == "adopt" and args.run_id == "r1"
    assert args.pid == 4242 and args.launcher == "subfleet run"
