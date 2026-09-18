"""The unified subfleet CLI: subcommand routing and the login ritual."""

import os
import stat
from pathlib import Path

import pytest

from subfleet import cli, login


class TestRouting:
    def test_pick_defaults_to_codex(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "cmd_pick", lambda a: (seen.__setitem__("codex", a), 0)[1])
        monkeypatch.setattr(cli, "cmd_claude_pick", lambda a: (seen.__setitem__("claude", a), 0)[1])
        assert cli.main(["pick"]) == 0
        assert "codex" in seen and "claude" not in seen
        assert seen["codex"].min_headroom == 5.0

    def test_pick_claude_routes_with_exclusions(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "cmd_claude_pick", lambda a: (seen.__setitem__("a", a), 0)[1])
        assert cli.main([
            "pick", "claude", "--model", "claude-opus-5",
            "--exclude", "x@y", "--all",
        ]) == 0
        assert seen["a"].exclude == ["x@y"] and seen["a"].all is True
        assert seen["a"].model == "claude-opus-5"
        assert cli.main(["pick", "claude", "--model", "custom-model"]) == 0
        assert seen["a"].model == "custom-model"

    def test_bare_and_unknown_first_arg_mean_status(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "cmd_status", lambda a: calls.append(a) or 0)
        assert cli.main([]) == 0
        assert cli.main(["--cached"]) == 0
        assert calls[1].cached is True

    def test_run_passes_argv_to_delegate(self, monkeypatch):
        from subfleet import delegate

        seen = {}
        monkeypatch.setattr(delegate, "main", lambda argv: (seen.__setitem__("argv", argv), 0)[1])
        assert cli.main(["run", "-m", "sol", "--dry-run", "do x"]) == 0
        assert seen["argv"] == ["-m", "sol", "--dry-run", "do x"]

    def test_gate_routes_structured_arguments(self, monkeypatch):
        from subfleet import consensus

        seen = {}
        monkeypatch.setattr(
            consensus,
            "run",
            lambda args: seen.update(
                command=args.gate_command,
                target=args.target,
                peer=args.peer,
                action=args.on_agreement,
            ) or 0,
        )
        assert cli.main([
            "gate", "pr", "42", "--peer", "fable", "--main-approve",
            "--on-agreement", "merge", "--merge-method", "squash",
        ]) == 0
        assert seen == {
            "command": "pr", "target": "42", "peer": "fable", "action": "merge"
        }

    @pytest.mark.parametrize("kind,target", [("plan", "plan.md"), ("pr", "42")])
    @pytest.mark.parametrize("limit_args,expected", [([], 0), (["--max-rounds", "3"], 3)])
    def test_new_gate_defaults_to_unlimited_but_keeps_explicit_caps(
        self, monkeypatch, kind, target, limit_args, expected,
    ):
        seen = {}
        monkeypatch.setattr(cli, "cmd_gate", lambda args: seen.update(max_rounds=args.max_rounds) or 0)
        assert cli.main(["gate", kind, target, "--peer", "sol", *limit_args]) == 0
        assert seen["max_rounds"] == expected

    @pytest.mark.parametrize("limit_args,expected", [([], None), (["--max-rounds", "0"], 0), (["--max-rounds", "8"], 8)])
    def test_gate_continue_distinguishes_preserved_and_replaced_limits(
        self, monkeypatch, limit_args, expected,
    ):
        seen = {}
        monkeypatch.setattr(cli, "cmd_gate", lambda args: seen.update(max_rounds=args.max_rounds) or 0)
        assert cli.main(["gate", "continue", "gate-1", *limit_args]) == 0
        assert seen["max_rounds"] == expected

    @pytest.mark.parametrize("gate_args", [
        ["plan", "plan.md", "--peer", "fable"],
        ["pr", "42", "--peer", "fable"],
        ["continue", "gate-1"],
    ])
    def test_gate_account_routing_arguments(self, monkeypatch, gate_args):
        seen = {}
        monkeypatch.setattr(cli, "cmd_gate", lambda args: seen.update(
            account=args.peer_account, excluded=args.exclude_account,
        ) or 0)
        assert cli.main([
            "gate", *gate_args, "--peer-account", "review@example.org",
            "--exclude-account", "interactive@example.org",
            "--exclude-account", "busy@example.org",
        ]) == 0
        assert seen == {
            "account": "review@example.org",
            "excluded": ["interactive@example.org", "busy@example.org"],
        }

    def test_tool_passthrough_execs_sibling_script(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "_exec_tool", lambda name, rest: seen.update(name=name, rest=rest) or 0)
        assert cli.main(["codex", "-m", "gpt-5.6-terra", "-C", "/w"]) == 0
        assert seen == {"name": "subfleet-codex", "rest": ["-m", "gpt-5.6-terra", "-C", "/w"]}
        cli.main(["mirror", "--quiet"])
        assert seen["name"] == "subfleet-mirror"


class TestLoginRitual:
    def _fake_codex(self, tmp_path, url="https://auth.openai.com/oauth/authorize?x=1"):
        b = tmp_path / "fake-codex"
        b.write_text(f"#!/bin/sh\n[ \"$1\" = login ] || exit 9\necho 'Open this URL: {url} to sign in'\nsleep 5\n")
        b.chmod(b.stat().st_mode | stat.S_IEXEC)
        return b

    def test_stages_server_and_prints_url(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        from subfleet import paths
        monkeypatch.setattr(paths, "HOME", tmp_path)
        rc = login.codex_login("7", watch=False, open_browser=False,
                               codex_bin=str(self._fake_codex(tmp_path)), timeout_s=5)
        out = capsys.readouterr().out
        assert rc == 0
        assert "https://auth.openai.com/oauth/authorize?x=1" in out
        assert (tmp_path / ".codex-7").is_dir()
        assert (tmp_path / "chief-of-staff" / "state" / "codex-login-7.pid").exists()
        assert "lane ~/.codex-7" in out

    def test_app_target_maps_to_codex_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        from subfleet import paths
        monkeypatch.setattr(paths, "HOME", tmp_path)
        monkeypatch.delenv("SUBFLEET_CODEX_APP_HOME", raising=False)
        home, slot = login._target_home("app")
        assert home == tmp_path / ".codex" and slot == "app"

    def test_bad_target_rejected(self):
        with pytest.raises(SystemExit):
            login._target_home("banana")

    def test_no_url_is_a_clean_failure(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        from subfleet import paths
        monkeypatch.setattr(paths, "HOME", tmp_path)
        b = tmp_path / "silent-codex"
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(b.stat().st_mode | stat.S_IEXEC)
        rc = login.codex_login("8", watch=False, open_browser=False, codex_bin=str(b), timeout_s=2)
        assert rc == 1
        assert "no authorize URL" in capsys.readouterr().err


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

    def test_gate_parser_accepts_astra_peer(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "cmd_gate", lambda a: (seen.update(peer=a.peer), 0)[1])
        assert cli.main(["gate", "pr", "42", "--peer", "astra", "--dry-run"]) == 0
        assert seen == {"peer": "astra"}
