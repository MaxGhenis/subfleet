import json
import subprocess
from pathlib import Path

import pytest

from subfleet import config, paths, secret_store


BASE = {"accounts": [], "enrolled": {}}


class TestConfigPaths:
    def test_env_config_and_xdg_state_dirs(self, tmp_path):
        assert config.config_dir() == tmp_path / "config"
        assert config.accounts_path() == tmp_path / "config" / "accounts.json"
        assert config.state_dir() == tmp_path / "xdg-state" / "subfleet"

    def test_default_dirs_follow_home(self, tmp_path, monkeypatch):
        home = tmp_path / "clean-home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("SUBFLEET_CONFIG_DIR")
        monkeypatch.delenv("XDG_STATE_HOME")

        assert config.config_dir() == home / ".config" / "subfleet"
        assert config.accounts_path() == home / ".config" / "subfleet" / "accounts.json"
        assert config.state_dir() == home / ".local" / "state" / "subfleet"

    def test_paths_module_delegates_shared_state(self):
        assert paths.state_dir() == config.state_dir()
        assert paths.snapshot_path().parent == config.state_dir()
        assert paths.alerts_path().parent == config.state_dir()
        assert paths.history_path().parent == config.state_dir()
        assert paths.rollout_cache_path().parent == config.state_dir()


class TestConfigIO:
    def test_save_round_trip_and_creates_parent(self, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "config"
        monkeypatch.setenv("SUBFLEET_CONFIG_DIR", str(target))
        data = {
            "accounts": ["alpha@example.com"],
            "enrolled": {"alpha@example.com": "lane-alpha"},
            "codex_homes": [],
        }

        config.save(data)

        assert config.load(strict=True) == data
        assert json.loads((target / "accounts.json").read_text()) == data

    def test_missing_and_malformed_non_strict_are_empty(self):
        config.accounts_path().unlink(missing_ok=True)
        assert config.load() == {}
        config.accounts_path().parent.mkdir(parents=True, exist_ok=True)
        config.accounts_path().write_text("{not json")
        assert config.load(strict=False) == {}

    def test_malformed_strict_raises(self):
        config.accounts_path().write_text("{not json")
        with pytest.raises(ValueError):
            config.load(strict=True)


class TestCodexHomes:
    def test_setting_distinguishes_missing_from_explicit_empty(self):
        config.save(BASE)
        assert config.codex_homes_setting() is None

        config.save({**BASE, "codex_homes": []})
        assert config.codex_homes_setting() == []
        assert paths.codex_homes() == []

    def test_configured_homes_expand_and_preserve_order(self, tmp_path):
        first = tmp_path / "lane-b"
        second = tmp_path / "lane-a"
        config.save({**BASE, "codex_homes": [str(first), str(second)]})

        assert config.codex_homes_setting() == [first, second]
        assert paths.codex_homes() == [first, second]

    def test_default_discovery_globs_all_numeric_suffixes(self, tmp_path, monkeypatch):
        home = tmp_path / "discovery-home"
        monkeypatch.setattr(paths, "HOME", home)
        monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(home / ".codex"))
        home.mkdir()
        for name in (".codex", ".codex-10", ".codex-2"):
            (home / name).mkdir()
        (home / ".codex-word").mkdir()
        (home / ".codex-4").write_text("not a directory")
        config.save(BASE)

        assert paths.codex_homes() == [home / ".codex-2", home / ".codex-10"]
        assert paths.app_codex_home() == home / ".codex"
        assert paths.app_codex_home() not in paths.codex_homes()


class TestSecretNamesAndCommands:
    def test_default_and_configured_prefix(self):
        config.save(BASE)
        assert config.secret_name_prefix() == "claude-quota-"
        assert config.secret_name_for("alpha@example.com") == "claude-quota-alpha@example.com"

        config.save({**BASE, "secret_name_prefix": "lane-token-"})
        assert config.secret_name_prefix() == "lane-token-"
        assert config.secret_name_for("beta@example.com") == "lane-token-beta@example.com"

    def test_enrollment_mapping_is_authoritative(self):
        config.save(
            {
                "accounts": ["alpha@example.com", "beta@example.com"],
                "enrolled": {"alpha@example.com": "existing-secret-name"},
                "secret_name_prefix": "new-prefix-",
            }
        )

        assert config.secret_name_for("alpha@example.com", require_enrolled=True) == "existing-secret-name"
        assert config.secret_name_for("alpha@example.com") == "existing-secret-name"
        assert config.secret_name_for("beta@example.com") == "new-prefix-beta@example.com"
        assert config.secret_name_for("beta@example.com", require_enrolled=True) is None

    def test_command_accepts_shell_words_or_argv(self):
        config.save({**BASE, "notify_cmd": 'notify-tool --label "two words"'})
        assert config.command("notify_cmd") == ["notify-tool", "--label", "two words"]

        config.save({**BASE, "secret_store_cmd": ["secret-tool", "--profile", "test"]})
        assert config.command("secret_store_cmd") == ["secret-tool", "--profile", "test"]
        assert config.command("notify_cmd") is None

    def test_invalid_explicit_command_fails_closed(self, monkeypatch):
        config.save({**BASE, "secret_store_cmd": {"executable": "secret-tool"}})

        def run(*args, **kwargs):
            raise AssertionError("invalid config fell back to the default store")

        monkeypatch.setattr(secret_store.subprocess, "run", run)
        with pytest.raises(config.ConfigError, match="secret_store_cmd"):
            config.command("secret_store_cmd")
        assert secret_store.get("lane-alpha") is None
        assert secret_store.set("lane-alpha", "test-token") is False
        assert secret_store.delete("lane-alpha") is False


class TestDefaultSecretStore:
    def test_security_get(self, monkeypatch):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "stored-value\n", "")

        config.save(BASE)
        monkeypatch.setattr(secret_store.subprocess, "run", run)

        assert secret_store.get("lane-alpha") == "stored-value"
        cmd, kwargs = calls[0]
        assert cmd[0:2] == ["security", "find-generic-password"]
        assert cmd[cmd.index("-s") + 1] == "lane-alpha"
        assert "-w" in cmd
        assert kwargs["capture_output"] is True and kwargs["text"] is True

    def test_security_set_and_delete(self, monkeypatch):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        config.save(BASE)
        monkeypatch.setattr(secret_store.subprocess, "run", run)

        assert secret_store.set("lane-alpha", "test-token") is True
        assert secret_store.delete("lane-alpha") is True
        set_cmd = calls[0][0]
        delete_cmd = calls[1][0]
        assert set_cmd[0:2] == ["security", "add-generic-password"]
        assert "-U" in set_cmd
        assert set_cmd[set_cmd.index("-s") + 1] == "lane-alpha"
        assert set_cmd[set_cmd.index("-w") + 1] == "test-token"
        assert delete_cmd[0:2] == ["security", "delete-generic-password"]
        assert delete_cmd[delete_cmd.index("-s") + 1] == "lane-alpha"

    def test_custom_store_uses_get_set_del_protocol(self, tmp_path, monkeypatch):
        helper = tmp_path / "secret-helper"
        config.save({**BASE, "secret_store_cmd": [str(helper), "--profile", "test"]})
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            stdout = "custom-value\n" if "get" in cmd else ""
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        monkeypatch.setattr(secret_store.subprocess, "run", run)

        assert secret_store.get("lane-alpha") == "custom-value"
        assert secret_store.set("lane-alpha", "test-token") is True
        assert secret_store.delete("lane-alpha") is True
        prefix = [str(helper), "--profile", "test"]
        assert calls[0][0] == [*prefix, "get", "lane-alpha"]
        assert calls[1][0] == [*prefix, "set", "lane-alpha"]
        assert calls[1][1]["input"] == "test-token\n"
        assert calls[2][0] == [*prefix, "del", "lane-alpha"]

    def test_store_failures_are_non_throwing(self, monkeypatch):
        def run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "missing")

        config.save(BASE)
        monkeypatch.setattr(secret_store.subprocess, "run", run)
        assert secret_store.get("lane-alpha") is None
        assert secret_store.set("lane-alpha", "test-token") is False
        assert secret_store.delete("lane-alpha") is False


class TestSecretCLI:
    def test_get_for_account_prints_only_secret(self, monkeypatch, capsys):
        from subfleet import cli

        config.save(
            {
                "accounts": ["alpha@example.com"],
                "enrolled": {"alpha@example.com": "lane-alpha"},
                "codex_homes": [],
            }
        )
        monkeypatch.setattr(secret_store, "get", lambda name: "test-token" if name == "lane-alpha" else None)

        assert cli.main(["secret", "get-for-account", "alpha@example.com"]) == 0
        captured = capsys.readouterr()
        assert captured.out == "test-token\n"
        assert captured.err == ""

    def test_get_for_unenrolled_account_fails_closed(self, monkeypatch, capsys):
        from subfleet import cli

        config.save({"accounts": ["beta@example.com"], "enrolled": {}, "codex_homes": []})
        monkeypatch.setattr(secret_store, "get", lambda name: pytest.fail("secret store should not run"))

        assert cli.main(["secret", "get-for-account", "beta@example.com"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "not enrolled" in captured.err.lower()
