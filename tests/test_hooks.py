"""`subfleet hooks install|uninstall|status` and the PreToolUse attached-runner guard."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from subfleet import cli, hooks

HOOK = Path(__file__).parent.parent / "bin" / "subfleet-hook"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "$schema": "x",
        "model": "fable",
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "/hooks/guard.sh"}]},
                {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "/hooks/guard.sh"}]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "/hooks/stop.sh"}]}],
        },
        "permissions": {"allow": ["Bash(ls:*)"]},
    }, indent=2) + "\n")
    monkeypatch.setenv("SUBFLEET_CLAUDE_SETTINGS", str(path))
    return path


def test_install_is_idempotent_and_preserves_everything_else(settings, capsys):
    report = hooks.install()
    assert report["ok"] and report["changed_events"] == 3 and report["backup"]
    data = json.loads(settings.read_text())
    assert list(data.keys()) == ["$schema", "model", "hooks", "permissions"]
    assert data["model"] == "fable" and data["permissions"] == {"allow": ["Bash(ls:*)"]}
    pre = data["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "/hooks/guard.sh"  # untouched, still first
    assert pre[-1] == {"matcher": "Bash", "hooks": [
        {"type": "command", "command": f"{hooks.hook_script()} pre-bash", "timeout": 5}]}
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith("subfleet-hook session-start")
    assert data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("subfleet-hook user-prompt")
    assert data["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "/hooks/stop.sh"}]}]
    before = settings.read_text()
    second = hooks.install()
    assert second["changed_events"] == 0 and second["backup"] is None
    assert settings.read_text() == before
    status = hooks.status()
    assert status["complete"] and status["script_executable"]
    assert cli.main(["hooks"]) == 0
    assert "complete" in capsys.readouterr().out


def test_install_replaces_a_stale_subfleet_entry_wherever_it_sits(settings):
    data = json.loads(settings.read_text())
    data["hooks"]["PreToolUse"][0]["hooks"].append(
        {"type": "command", "command": "/old/path/subfleet-hook pre-bash"})
    settings.write_text(json.dumps(data))
    hooks.install()
    data = json.loads(settings.read_text())
    commands = [h["command"] for g in data["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert commands.count("/hooks/guard.sh") == 2
    assert [c for c in commands if "subfleet-hook" in c] == [f"{hooks.hook_script()} pre-bash"]


def test_uninstall_removes_only_subfleet_entries(settings, capsys):
    hooks.install()
    report = hooks.uninstall()
    assert report["removed"] == 3
    data = json.loads(settings.read_text())
    assert set(data["hooks"]) == {"PreToolUse", "Stop"}
    assert all("subfleet-hook" not in h["command"] for g in data["hooks"]["PreToolUse"] for h in g["hooks"])
    assert hooks.uninstall()["removed"] == 0
    assert cli.main(["hooks", "install", "--dry-run"]) == 0
    assert "would update 3" in capsys.readouterr().out
    assert "subfleet-hook" not in settings.read_text()


def test_missing_settings_file_is_created(tmp_path, monkeypatch):
    path = tmp_path / "fresh" / "settings.json"
    monkeypatch.setenv("SUBFLEET_CLAUDE_SETTINGS", str(path))
    report = hooks.install()
    assert report["ok"] and path.exists()
    assert set(json.loads(path.read_text())["hooks"]) == {"PreToolUse", "UserPromptSubmit", "SessionStart"}


def _guard(command: str) -> dict | None:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    completed = subprocess.run([str(HOOK), "pre-bash"], input=payload, capture_output=True,
                               text=True, timeout=20, env=dict(os.environ))
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout) if completed.stdout.strip() else None


@pytest.mark.parametrize("command", [
    "subfleet codex -m sol -C . -p p.md -o o.md",
    "cd /x && subfleet claude -a lane@example.com -C . -p p.md -o o.md",
    "codex exec --sandbox read-only 'hi'",
    "CODEX_HOME=~/.codex-3 codex exec 'reply ok'",
    "codex-run -H ~/.codex-3 -m sol -C . -p p.md -o o.md",
    "nohup ~/bin/codex-run -H ~/.codex-1 -p p.md -o o.md > log 2>&1 &",
    "claude-lane -a lane@example.com -C . -p p.md -o o.md",
    "/opt/tools/subfleet/bin/subfleet-codex -m x -C . -p p -o o",
    "codex e 'one-liner'",
    "codex review",
    "cd /x && ~/tools/subfleet/bin/subfleet-codex -m x -C . -p p -o o",
    "time codex exec 'slow'",
    "exec codex exec 'x'",
    "FOO=1 BAR=2 subfleet codex -m x -C . -p p -o o",
])
def test_guard_blocks_attached_runner_launches(command):
    decision = _guard(command)
    assert decision and decision["decision"] == "block"
    assert decision["reason"].startswith("[attached-runner]")
    assert "subfleet run" in decision["reason"] and "subfleet wait" in decision["reason"]


@pytest.mark.parametrize("command", [
    "subfleet run -m sol -C . -p p.md -o o.md",
    "subfleet run --attach -m terra 'reply ok'",
    "subfleet runs --mine",
    "subfleet wait 20260101-1-x",
    "subfleet status",
    "subfleet codex -d -m sol -C . -p p.md -o o.md",
    "subfleet claude -d -a lane@example.com -C . -p p.md -o o.md",
    "SUBFLEET_ATTACHED_OK=1 codex exec 'reply ok'",
    "SUBFLEET_ATTACHED_OK=1 subfleet codex -m x -C . -p p -o o",
    "codex login",
    "codex --version",
    "git log --oneline | grep codex",
    "echo 'subfleet codex runs detached elsewhere' > notes.md",
    "bash -n bin/subfleet-claude && echo ok",
    "sed -i '' 's/a/b/' bin/subfleet-codex",
    "cd ~/tools/subfleet && python3 - <<'EOF'\np = Path('bin/subfleet-claude')\nEOF",
    "grep -n codex-run ~/bin/*",
    "cat ~/bin/codex-run",
    "ls",
])
def test_guard_allows_front_door_detached_and_unrelated_commands(command):
    assert _guard(command) is None


def test_install_refuses_missing_hook_script(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("{}\n")
    monkeypatch.setenv("SUBFLEET_CLAUDE_SETTINGS", str(settings))
    monkeypatch.setattr(hooks, "hook_script", lambda: tmp_path / "missing-hook")
    report = hooks.install()
    assert report["ok"] is False and "missing or not executable" in report["error"]
    assert settings.read_text() == "{}\n", "no dead command may be written"
