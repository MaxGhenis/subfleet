import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from subfleet import delegate


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
        ("subfleet", "DELEGATE_SUBFLEET"),
        ("subfleet-codex", "DELEGATE_CODEX_RUN"),
        ("subfleet-claude", "DELEGATE_CLAUDE_LANE"),
    ],
)
def test_repo_bin_precedes_path(name, env_var, tmp_path, monkeypatch):
    monkeypatch.delenv(env_var, raising=False)
    path_tool = make_executable(tmp_path / "path-bin" / name)
    monkeypatch.setenv("PATH", str(path_tool.parent))

    assert Path(delegate._discover_tool(name, env_var)) == BIN_DIR / name


def test_explicit_tool_override_precedes_discovery(tmp_path, monkeypatch):
    override = make_executable(tmp_path / "custom" / "picker")
    monkeypatch.setenv("DELEGATE_SUBFLEET", str(override))

    assert Path(delegate._discover_tool("subfleet", "DELEGATE_SUBFLEET")) == override


def test_path_fallback_when_repo_copy_is_absent(tmp_path, monkeypatch):
    fake_module = tmp_path / "isolated-repo" / "subfleet" / "delegate.py"
    fake_module.parent.mkdir(parents=True)
    path_tool = make_executable(tmp_path / "path-bin" / "subfleet")
    monkeypatch.setattr(delegate, "__file__", str(fake_module))
    monkeypatch.delenv("DELEGATE_SUBFLEET", raising=False)
    monkeypatch.setenv("PATH", str(path_tool.parent))

    assert Path(delegate._discover_tool("subfleet", "DELEGATE_SUBFLEET")) == path_tool


def test_missing_tool_raises_clear_error(tmp_path, monkeypatch):
    fake_module = tmp_path / "isolated-repo" / "subfleet" / "delegate.py"
    fake_module.parent.mkdir(parents=True)
    monkeypatch.setattr(delegate, "__file__", str(fake_module))
    monkeypatch.delenv("DELEGATE_SUBFLEET", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    with pytest.raises(FileNotFoundError, match="subfleet"):
        delegate._discover_tool("subfleet", "DELEGATE_SUBFLEET")


def test_subfleet_launcher_is_executable_and_works_outside_repo(tmp_path):
    shim = BIN_DIR / "subfleet"
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


def test_subfleet_run_uses_temp_config_and_shared_state(tmp_path):
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
    capacity_state = state_home / "subfleet"
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
        "SUBFLEET_CONFIG_DIR": str(config_dir),
        "XDG_STATE_HOME": str(state_home),
        "HOME": str(tmp_path / "home"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    cp = subprocess.run(
        [str(BIN_DIR / "subfleet"), "run", "--dry-run", "--why", "fix the failing test"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )

    assert cp.returncode == 0, cp.stderr
    # Build work is standard work (Opus); with no Claude lane at all it moves
    # upward to Astra on the one dispatchable Codex home.
    assert str(BIN_DIR / "subfleet-codex") in cp.stdout
    assert "gpt-6-astra" in cp.stdout
    assert "-e ultra" in cp.stdout
    assert "CAPABILITY FALLBACK" in cp.stderr and "build/standard upward to astra" in cp.stderr
    assert '"class": "build"' in cp.stderr
    decisions = state_home / "subfleet" / "decisions.jsonl"
    records = [json.loads(line) for line in decisions.read_text().splitlines()]
    assert records[-1]["model"] == "astra"
    assert records[-1]["requested_model"] == "opus"
    assert "upward to astra" in records[-1]["routing"]
    assert records[-1]["lane/home"] == codex_home
