import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from carpool import delegate


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"


def make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    ("name", "env_var"),
    [
        ("carpool", "DELEGATE_CARPOOL"),
        ("carpool-codex", "DELEGATE_CODEX_RUN"),
        ("carpool-claude", "DELEGATE_CLAUDE_LANE"),
    ],
)
def test_repo_bin_precedes_path(name, env_var, tmp_path, monkeypatch):
    monkeypatch.delenv(env_var, raising=False)
    path_tool = make_executable(tmp_path / "path-bin" / name)
    monkeypatch.setenv("PATH", str(path_tool.parent))

    assert Path(delegate._discover_tool(name, env_var)) == BIN_DIR / name


def test_explicit_tool_override_precedes_discovery(tmp_path, monkeypatch):
    override = make_executable(tmp_path / "custom" / "picker")
    monkeypatch.setenv("DELEGATE_CARPOOL", str(override))

    assert Path(delegate._discover_tool("carpool", "DELEGATE_CARPOOL")) == override


def test_path_fallback_when_repo_copy_is_absent(tmp_path, monkeypatch):
    fake_module = tmp_path / "isolated-repo" / "carpool" / "delegate.py"
    fake_module.parent.mkdir(parents=True)
    path_tool = make_executable(tmp_path / "path-bin" / "carpool")
    monkeypatch.setattr(delegate, "__file__", str(fake_module))
    monkeypatch.delenv("DELEGATE_CARPOOL", raising=False)
    monkeypatch.setenv("PATH", str(path_tool.parent))

    assert Path(delegate._discover_tool("carpool", "DELEGATE_CARPOOL")) == path_tool


def test_missing_tool_raises_clear_error(tmp_path, monkeypatch):
    fake_module = tmp_path / "isolated-repo" / "carpool" / "delegate.py"
    fake_module.parent.mkdir(parents=True)
    monkeypatch.setattr(delegate, "__file__", str(fake_module))
    monkeypatch.delenv("DELEGATE_CARPOOL", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    with pytest.raises(FileNotFoundError, match="carpool"):
        delegate._discover_tool("carpool", "DELEGATE_CARPOOL")


def test_carpool_launcher_is_executable_and_works_outside_repo(tmp_path):
    shim = BIN_DIR / "carpool"
    assert shim.is_file()
    assert os.access(shim, os.X_OK)

    cp = subprocess.run(
        [str(shim), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert cp.returncode == 0, cp.stderr
    assert "usage:" in cp.stdout.lower()


def test_carpool_run_uses_temp_config_and_shared_state(tmp_path):
    config_dir = tmp_path / "config"
    state_home = tmp_path / "state"
    workdir = tmp_path / "work"
    config_dir.mkdir(exist_ok=True)
    workdir.mkdir()
    codex_home = "/tmp/example-codex-home"
    (config_dir / "accounts.json").write_text(
        json.dumps({"accounts": [], "enrolled": {}, "codex_homes": [codex_home]})
    )
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    capacity_state = state_home / "carpool"
    capacity_state.mkdir(parents=True)
    (capacity_state / "capacity-cache.json").write_text(
        json.dumps(
            {
                "checked_at": checked_at,
                "codex": [
                    {
                        "home": codex_home,
                        "auth": {
                            "status": "ok", "home": codex_home,
                            "account_id": "example-account", "email": "lane@example.com",
                        },
                        "probe": {
                            "status": "ok", "checked_at": checked_at, "allowed": True,
                            "limit_reached": False,
                            "primary": {"used_percent": 10, "reset_at": None},
                            "secondary": {"used_percent": 20, "reset_at": None},
                        },
                    }
                ],
                "claude": {
                    "identity": {}, "credentials": {"status": "skipped"},
                    "probe": {"status": "skipped"},
                },
            }
        )
    )
    env = {
        **os.environ,
        "CARPOOL_CONFIG_DIR": str(config_dir),
        "XDG_STATE_HOME": str(state_home),
        "HOME": str(tmp_path / "home"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    cp = subprocess.run(
        [str(BIN_DIR / "carpool"), "run", "--dry-run", "--why", "fix the failing test"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )

    assert cp.returncode == 0, cp.stderr
    assert str(BIN_DIR / "carpool-codex") in cp.stdout
    assert "gpt-5.6-sol" in cp.stdout
    assert "-e ultra" in cp.stdout
    assert '"class": "build"' in cp.stderr
    decisions = state_home / "carpool" / "decisions.jsonl"
    records = [json.loads(line) for line in decisions.read_text().splitlines()]
    assert records[-1]["model"] == "sol"
    assert records[-1]["lane/home"] == codex_home
