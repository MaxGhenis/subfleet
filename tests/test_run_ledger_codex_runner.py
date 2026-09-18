"""End-to-end run-ledger coverage for the hardened Codex shell runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parent.parent / "bin" / "subfleet-codex"


def _worktree_subfleet(path: Path) -> Path:
    """CLI wrapper that imports this checkout, not bin/subfleet's fixed install."""
    root = SCRIPT.parent.parent
    path.write_text(
        "#!/bin/bash\n"
        f'export PYTHONPATH="{root}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        f'exec "{sys.executable}" -P -m subfleet.cli "$@"\n'
    )
    path.chmod(0o755)
    return path


def _fake_codex(path: Path) -> Path:
    path.write_text(
        """#!/bin/bash
if [ "${1:-}" = "app-server" ]; then
  if [ "${FAKE_CODEX_POLICY_RC:-0}" -ne 0 ]; then
    printf 'private-policy-sentinel\n' >&2
    exit "$FAKE_CODEX_POLICY_RC"
  fi
  exec python3 -c '
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        result = {}
    elif method == "configRequirements/read":
        result = {"requirements": json.loads(os.environ.get("FAKE_CODEX_REQUIREMENTS", "null"))}
    elif method == "config/read":
        result = {"layers": json.loads(os.environ.get("FAKE_CODEX_CONFIG_LAYERS", "[{\\"name\\": {\\"type\\": \\"system\\"}, \\"config\\": {}}]"))}
    else:
        continue
    print(json.dumps({"id": msg["id"], "result": result}), flush=True)
'
fi
if [ "${1:-}" = "mcp" ]; then
  if [ -n "${FAKE_CODEX_MCP_LOG:-}" ]; then
    printf '%s\n' "$CODEX_HOME" >> "$FAKE_CODEX_MCP_LOG"
  fi
  if [ "$CODEX_HOME" = "${FAKE_CODEX_SECOND_HOME:-}" ]; then
    printf '%s\n' "${FAKE_CODEX_MCP_SECOND_JSON:-[]}"
  else
    printf '%s\n' "${FAKE_CODEX_MCP_JSON:-[]}"
  fi
  exit "${FAKE_CODEX_MCP_RC:-0}"
fi
if [ -n "${FAKE_CODEX_ARGV:-}" ]; then
  printf '%s\n' "$@" > "$FAKE_CODEX_ARGV"
fi
if [ "$CODEX_HOME" = "${FAKE_CODEX_USAGE_LIMIT_HOME:-}" ]; then
  printf 'You have hit your usage limit; try again soon\n' >&2
  exit 9
fi
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then out=$2; shift 2; else shift; fi
done
if [ -n "${FAKE_CODEX_SESSION_ID:-}" ]; then
  printf 'session id: %s\n' "$FAKE_CODEX_SESSION_ID" >&2
  rollout_dir="$CODEX_HOME/sessions/2026/08/28"
  mkdir -p "$rollout_dir"
  printf '{}\n' > "$rollout_dir/rollout-test-$FAKE_CODEX_SESSION_ID.jsonl"
fi
if [ -n "${FAKE_CODEX_NESTED_SESSION_ID:-}" ]; then
  printf 'tool output follows\nsession id: %s\n' "$FAKE_CODEX_NESTED_SESSION_ID" >&2
fi
if [ "${FAKE_CODEX_RC:-0}" -eq 0 ]; then
  printf 'codex finished\n' > "$out"
else
  printf 'forced codex failure\n' >&2
fi
exit "${FAKE_CODEX_RC:-0}"
"""
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(("child_rc", "expected_out"), [(0, "codex finished\n"), (9, "")])
def test_codex_runner_records_success_and_failure(
    tmp_path, child_rc, expected_out
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    state = tmp_path / "state"
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / "codex-home"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("preamble\n\ndo the task\n")
    out = tmp_path / "caller-output.md"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "SUBFLEET_CODEX_GUARD": "off",
            "SUBFLEET_RUN_SUBFLEET": str(subfleet),
            "SUBFLEET_STATE_DIR": str(state),
            "FAKE_CODEX_RC": str(child_rc),
        }
    )

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-m", "gpt-test", "-C", str(workdir),
            "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == child_rc
    run_dirs = [path for path in (state / "runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["family"] == "codex"
    assert meta["model"] == "gpt-test"
    assert meta["lane"] == str(home)
    assert meta["rc"] == child_rc
    assert meta["finished_at"] is not None
    assert meta["original_out_path"] == str(out)
    assert (run_dir / "prompt.md").read_text() == prompt.read_text()
    assert (run_dir / "out.md").read_text() == expected_out
    if child_rc:
        assert "forced codex failure" in (run_dir / "err.log").read_text()


def test_codex_runner_records_failure_before_lane_pick(tmp_path):
    picker = tmp_path / "failing-picker"
    picker.write_text("#!/bin/bash\nexit 7\n")
    picker.chmod(0o755)
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    state = tmp_path / "state"
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("dispatch even if no lane is available\n")
    out = tmp_path / "caller-output.md"
    env = os.environ.copy()
    env.update(
        {
            "SUBFLEET_CODEX_PICK": str(picker),
            "SUBFLEET_RUN_SUBFLEET": str(subfleet),
            "SUBFLEET_STATE_DIR": str(state),
        }
    )

    completed = subprocess.run(
        [
            str(SCRIPT), "-m", "gpt-test", "-C", str(workdir),
            "-p", str(prompt), "-o", str(out),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 1
    run_dirs = [path for path in (state / "runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["lane"] is None
    assert meta["rc"] == 1
    assert meta["finished_at"] is not None
    assert (run_dir / "prompt.md").read_text() == prompt.read_text()
    assert (run_dir / "out.md").read_text() == ""
    assert (run_dir / "err.log").read_text() == ""


def test_codex_isolated_review_disables_project_instructions(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    state = tmp_path / "state"
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    review_root = tmp_path / "untrusted-review-root"
    review_root.mkdir()
    home = tmp_path / "codex-home"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review the immutable bundle\n")
    out = tmp_path / "out.md"
    argv_path = tmp_path / "codex-argv.txt"
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env.get('PATH', '')}",
        "SUBFLEET_CODEX_GUARD": "on",
        "SUBFLEET_RUN_SUBFLEET": str(subfleet),
        "SUBFLEET_STATE_DIR": str(state),
        "FAKE_CODEX_ARGV": str(argv_path),
        "FAKE_CODEX_CONFIG_LAYERS": json.dumps([
            {
                "name": {"type": "user"},
                "config": {
                    "features": {"apps": True, "hooks": True},
                    "sandbox_mode": "danger-full-access",
                    "private-policy-sentinel": "never log user config",
                },
            },
            {"name": {"type": "sessionFlags"}, "config": {"features": {"apps": False}}},
            {"name": {"type": "system"}, "config": {}},
        ]),
        "FAKE_CODEX_MCP_JSON": json.dumps([
            {
                "name": "local_tool",
                "transport": {
                    "type": "stdio",
                    "command": "untrusted-command",
                    "env": {"TOKEN": "private-inventory-sentinel"},
                },
            },
            {
                "name": "hosted-tool",
                "transport": {
                    "type": "streamable_http",
                    "url": "https://private-inventory-sentinel.invalid/mcp",
                },
            },
        ]),
    })

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-m", "gpt-test",
            "-C", str(neutral), "-p", str(prompt), "-o", str(out),
            "-s", "read-only", "-I", "-D", str(review_root), "-r", "0",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    argv = argv_path.read_text().splitlines()
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "project_doc_max_bytes=0" in argv
    assert "--add-dir" not in argv
    assert argv[argv.index("-C") + 1] == str(neutral)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    for feature in (
        "apps", "plugins", "remote_plugin", "hooks", "multi_agent",
        "multi_agent_v2", "tool_suggest", "skill_mcp_dependency_install",
        "computer_use", "browser_use", "browser_use_external",
        "browser_use_full_cdp_access", "in_app_browser", "image_generation",
        "goals", "memories", "shell_snapshot",
    ):
        assert f"features.{feature}=false" in argv
    assert 'web_search="disabled"' in argv
    assert 'history.persistence="none"' in argv
    assert "shell_environment_policy.experimental_use_profile=false" in argv
    assert 'mcp_servers.local_tool.command="false"' in argv
    assert "mcp_servers.local_tool.enabled=false" in argv
    assert 'mcp_servers.hosted-tool.url="https://invalid.invalid"' in argv
    assert "mcp_servers.hosted-tool.enabled=false" in argv
    assert not any(arg.startswith("hooks=") for arg in argv)
    assert not any("sandbox_workspace_write" in arg for arg in argv)
    assert "guard preflight disabled" in completed.stderr
    output = argv_path.read_text() + completed.stdout + completed.stderr
    assert "private-inventory-sentinel" not in output
    assert "private-policy-sentinel" not in output
    assert "untrusted-command" not in output


@pytest.mark.parametrize(
    "extra",
    [
        ["-I"],
        ["-I", "-D", "{root}", "-s", "workspace-write"],
        ["-D", "{root}", "-s", "read-only"],
        ["-I", "-D", "{root}", "-s", "read-only", "-b", "salvage"],
        ["-I", "-D", "{root}", "-s", "read-only", "-T", "existing-thread"],
        ["-I", "-D", "{root}/missing", "-s", "read-only"],
    ],
)
def test_codex_isolated_review_rejects_incomplete_or_write_mode(tmp_path, extra):
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review\n")
    out = tmp_path / "out.md"
    expanded = [value.replace("{root}", str(workdir)) for value in extra]
    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-m", "gpt-test",
            "-C", str(workdir), "-p", str(prompt), "-o", str(out), *expanded,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 2


@pytest.mark.parametrize(
    ("inventory", "inventory_rc"),
    [
        ("private-inventory-sentinel: not JSON", 0),
        ("[]", 7),
        ('{"private-inventory-sentinel": true}', 0),
        (json.dumps([{"name": "new", "transport": {"type": "unknown"}}]), 0),
        (json.dumps([{"name": "bad.name", "transport": {"type": "stdio"}}]), 0),
    ],
)
def test_codex_isolated_review_fails_closed_on_unsafe_mcp_inventory(
    tmp_path, inventory, inventory_rc
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review\n")
    argv_path = tmp_path / "codex-argv.txt"
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env.get('PATH', '')}",
        "SUBFLEET_CODEX_GUARD": "on",
        "SUBFLEET_RUN_SUBFLEET": str(subfleet),
        "SUBFLEET_STATE_DIR": str(tmp_path / "state"),
        "FAKE_CODEX_ARGV": str(argv_path),
        "FAKE_CODEX_MCP_JSON": inventory,
        "FAKE_CODEX_MCP_RC": str(inventory_rc),
    })

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-m", "gpt-test",
            "-C", str(workdir), "-p", str(prompt), "-o", str(tmp_path / "out.md"),
            "-s", "read-only", "-I", "-D", str(workdir), "-r", "0",
        ],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 2
    assert "cannot safely isolate review configuration/MCP servers" in completed.stderr
    assert "private-inventory-sentinel" not in completed.stdout + completed.stderr
    assert not argv_path.exists(), "unsafe inventory must stop before codex exec"


@pytest.mark.parametrize(
    "policy_env",
    [
        {"FAKE_CODEX_REQUIREMENTS": '{"allowedSandboxModes": ["workspaceWrite"]}'},
        {"FAKE_CODEX_REQUIREMENTS": '{"featureRequirements": {"hooks": true}}'},
        {"FAKE_CODEX_REQUIREMENTS": "{}"},
        {"FAKE_CODEX_POLICY_RC": "7"},
        {"FAKE_CODEX_CONFIG_LAYERS": "[]"},
        *(
            {"FAKE_CODEX_CONFIG_LAYERS": json.dumps([
                {
                    "name": {"type": source},
                    "config": {"private-policy-sentinel": "must not be logged"},
                },
            ])}
            for source in ("system", "project", "mdm", "newUnknownLayer")
        ),
    ],
)
def test_codex_isolated_review_fails_closed_on_managed_or_unreadable_policy(
    tmp_path, policy_env
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review\n")
    argv_path = tmp_path / "codex-argv.txt"
    mcp_log = tmp_path / "mcp.log"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "SUBFLEET_CODEX_GUARD": "on",
        "SUBFLEET_RUN_SUBFLEET": str(subfleet),
        "SUBFLEET_STATE_DIR": str(tmp_path / "state"),
        "FAKE_CODEX_ARGV": str(argv_path),
        "FAKE_CODEX_MCP_LOG": str(mcp_log),
        **policy_env,
    }

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-m", "gpt-test",
            "-C", str(workdir), "-p", str(prompt), "-o", str(tmp_path / "out.md"),
            "-s", "read-only", "-I", "-D", str(workdir), "-r", "0",
        ],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 2
    assert "managed settings are unsupported" in completed.stderr
    assert "private-policy-sentinel" not in completed.stdout + completed.stderr
    assert not argv_path.exists(), "unsafe policy must stop before codex exec"
    assert not mcp_log.exists(), "unsafe policy must stop before MCP inventory"


def test_codex_isolated_review_reinventories_mcp_after_lane_rotation(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    workdir = tmp_path / "work"
    workdir.mkdir()
    homes = [tmp_path / "home-1", tmp_path / "home-2"]
    for home in homes:
        home.mkdir()
    picker = tmp_path / "picker"
    picker.write_text(f"#!/bin/bash\nprintf '%s\\n' '{homes[1]}'\n")
    picker.chmod(0o755)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review\n")
    argv_path = tmp_path / "codex-argv.txt"
    mcp_log = tmp_path / "mcp.log"
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env.get('PATH', '')}",
        "SUBFLEET_CODEX_GUARD": "on",
        "SUBFLEET_CODEX_PICK": str(picker),
        "SUBFLEET_RUN_SUBFLEET": str(subfleet),
        "SUBFLEET_STATE_DIR": str(tmp_path / "state"),
        "DELEGATE_STATE_DIR": str(tmp_path / "delegate-state"),
        "FAKE_CODEX_ARGV": str(argv_path),
        "FAKE_CODEX_MCP_LOG": str(mcp_log),
        "FAKE_CODEX_USAGE_LIMIT_HOME": str(homes[0]),
        "FAKE_CODEX_SECOND_HOME": str(homes[1]),
        "FAKE_CODEX_MCP_SECOND_JSON": json.dumps([
            {"name": "only_on_second_lane", "transport": {"type": "stdio"}},
        ]),
    })

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(homes[0]), "-A", "-m", "gpt-test",
            "-C", str(workdir), "-p", str(prompt), "-o", str(tmp_path / "out.md"),
            "-s", "read-only", "-I", "-D", str(workdir), "-r", "1",
        ],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert mcp_log.read_text().splitlines() == [str(home) for home in homes]
    assert "mcp_servers.only_on_second_lane.enabled=false" in argv_path.read_text()


@pytest.mark.parametrize("independent", [False, True])
def test_codex_isolation_does_not_change_normal_workspace_write_and_salvage(
    tmp_path, independent
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    workdir = tmp_path / "work"
    workdir.mkdir()
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Subfleet test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Subfleet test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(["git", "init", "-q", str(workdir)], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(workdir), "-c", "core.hooksPath=/dev/null", "commit",
         "--allow-empty", "-qm", "test fixture"],
        check=True, env=git_env,
    )
    dirty = workdir / "dirty.txt"
    dirty.write_text("preserve this change\n")
    home = tmp_path / "home"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("perform the task\n")
    argv_path = tmp_path / "codex-argv.txt"
    mcp_log = tmp_path / "mcp.log"
    env = {
        **git_env,
        "PATH": f"{fake_bin}:{git_env.get('PATH', '')}",
        "SUBFLEET_CODEX_GUARD": "off",
        "SUBFLEET_RUN_SUBFLEET": str(subfleet),
        "SUBFLEET_STATE_DIR": str(tmp_path / "state"),
        "FAKE_CODEX_ARGV": str(argv_path),
        "FAKE_CODEX_MCP_LOG": str(mcp_log),
    }
    extra = ["-I", "-D", str(workdir), "-s", "read-only"] if independent else []

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-m", "gpt-test",
            "-C", str(workdir), "-p", str(prompt), "-o", str(tmp_path / "out.md"),
            "-r", "0", *extra,
        ],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    argv = argv_path.read_text().splitlines()
    sandbox = "read-only" if independent else "workspace-write"
    assert argv[argv.index("--sandbox") + 1] == sandbox
    assert ("--ignore-user-config" in argv) is independent
    assert mcp_log.exists() is independent
    assert any("sandbox_workspace_write.writable_roots=" in arg for arg in argv) is not independent
    refs = subprocess.run(
        ["git", "-C", str(workdir), "for-each-ref", "refs/codex-salvage"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert bool(refs.strip()) is not independent
    assert dirty.read_text() == "preserve this change\n"
    assert subprocess.run(
        ["git", "-C", str(workdir), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout == "?? dirty.txt\n"


def test_codex_usage_limit_records_fifteen_minute_home_cooldown(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "codex"
    fake.write_text(
        "#!/bin/bash\n"
        "echo \"You've hit your usage limit; try again soon\" >&2\n"
        "exit 9\n"
    )
    fake.chmod(0o755)
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    state = tmp_path / "state"
    delegate_state = tmp_path / "delegate-state"
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / ".codex-4"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("use the lane\n")
    out = tmp_path / "out.md"
    started = datetime.now().astimezone()
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env.get('PATH', '')}",
        "SUBFLEET_CODEX_GUARD": "off",
        "SUBFLEET_RUN_SUBFLEET": str(subfleet),
        "SUBFLEET_STATE_DIR": str(state),
        "DELEGATE_STATE_DIR": str(delegate_state),
    })

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-m", "gpt-test", "-C", str(workdir),
            "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 9
    cooldowns = json.loads((delegate_state / "cooldowns.json").read_text())
    until = datetime.fromisoformat(cooldowns[str(home)]["*"])
    assert timedelta(minutes=14) < until - started < timedelta(minutes=16)


def test_codex_runner_captures_first_anchored_session_header_and_rollout(
    tmp_path,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    state = tmp_path / "state"
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / ".codex-5"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("capture the thread\n")
    out = tmp_path / "out.md"
    own_id = "01a046af-4a1f-79e0-abf4-9aa5bfa6f05f"
    nested_id = "01a030c3-164a-7583-bd2e-b7c145c3bdfa"
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env.get('PATH', '')}",
        "SUBFLEET_CODEX_GUARD": "off",
        "SUBFLEET_RUN_SUBFLEET": str(subfleet),
        "SUBFLEET_STATE_DIR": str(state),
        "FAKE_CODEX_SESSION_ID": own_id,
        "FAKE_CODEX_NESTED_SESSION_ID": nested_id,
    })

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-m", "gpt-test",
            "-C", str(workdir), "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    run_dir = next(path for path in (state / "runs").iterdir() if path.is_dir())
    meta = json.loads((run_dir / "meta.json").read_text())
    expected_rollout = (
        home / "sessions" / "2026" / "08" / "28" / f"rollout-test-{own_id}.jsonl"
    )
    assert meta["codex_thread_id"] == own_id
    assert meta["codex_home"] == str(home)
    assert meta["rollout_path"] == str(expected_rollout)
    assert meta["resumed_from"] is None


def test_codex_runner_resumes_on_pinned_home_and_records_provenance(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_codex(fake_bin / "codex")
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    argv_path = tmp_path / "codex.argv"
    state = tmp_path / "state"
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / ".codex-2"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("continue this thread\n")
    out = tmp_path / "out.md"
    thread_id = "01a03958-91a2-7682-a883-d08e638b0192"
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env.get('PATH', '')}",
        "SUBFLEET_CODEX_GUARD": "off",
        "SUBFLEET_RUN_SUBFLEET": str(subfleet),
        "SUBFLEET_STATE_DIR": str(state),
        "SUBFLEET_RUN_RESUMED_FROM": "20260825-163459-source-run",
        "FAKE_CODEX_ARGV": str(argv_path),
        "FAKE_CODEX_SESSION_ID": thread_id,
    })

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-T", thread_id, "-m", "gpt-test",
            "-C", str(workdir), "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert argv_path.read_text().splitlines() == [
        "exec",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--sandbox",
        "workspace-write",
        "-m",
        "gpt-test",
        "-C",
        str(workdir),
        "-o",
        str(out),
        "resume",
        thread_id,
        "continue this thread",
    ]
    run_dir = next(path for path in (state / "runs").iterdir() if path.is_dir())
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["lane"] == meta["codex_home"] == str(home)
    assert meta["codex_thread_id"] == thread_id
    assert meta["resumed_from"] == "20260825-163459-source-run"


@pytest.mark.parametrize(
    "home_args",
    [[], ["-H", "HOME", "-A"]],
)
def test_codex_resume_rejects_unpinned_or_auto_lane(tmp_path, home_args):
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / ".codex-4"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("continue\n")
    out = tmp_path / "out.md"
    expanded = [str(home) if value == "HOME" else value for value in home_args]

    completed = subprocess.run(
        [
            str(SCRIPT), *expanded,
            "-T", "01a03958-91a2-7682-a883-d08e638b0192",
            "-m", "gpt-test", "-C", str(workdir),
            "-p", str(prompt), "-o", str(out),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert "cannot migrate homes" in completed.stderr or "original -H" in completed.stderr


def _env_capturing_codex(path: Path, capture: Path) -> Path:
    """A fake codex that records whether CODEX_API_KEY reached it."""
    path.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"${{CODEX_API_KEY:-unset}}\" > '{capture}'\n"
        "out=''\n"
        'while [ "$#" -gt 0 ]; do if [ "$1" = "-o" ]; then out=$2; shift 2; else shift; fi; done\n'
        "printf 'codex finished\\n' > \"$out\"\n"
    )
    path.chmod(0o755)
    return path


def _runner_env(tmp_path: Path, fake_bin: Path, subfleet: Path, **extra) -> dict:
    env = os.environ.copy()
    env.pop("SUBFLEET_ALLOW_API_LANE", None)
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "SUBFLEET_CODEX_GUARD": "off",
            "SUBFLEET_RUN_SUBFLEET": str(subfleet),
            "SUBFLEET_STATE_DIR": str(tmp_path / "state"),
        }
    )
    env.update(extra)
    return env


def _api_key_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex-api"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-test", "auth_mode": "apikey", "tokens": None})
    )
    return home


def test_codex_runner_refuses_an_api_key_home_before_any_codex_call(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv_file = tmp_path / "argv.txt"
    _fake_codex(fake_bin / "codex")
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review x\n")
    out = tmp_path / "caller-output.md"
    env = _runner_env(tmp_path, fake_bin, subfleet, FAKE_CODEX_ARGV=str(argv_file))

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(_api_key_home(tmp_path)), "-m", "gpt-6-astra",
            "-C", str(workdir), "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 7
    assert "ChatGPT subscriptions only" in completed.stderr
    assert not argv_file.exists()  # codex was never invoked
    run_dirs = [path for path in (tmp_path / "state" / "runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    meta = json.loads((run_dirs[0] / "meta.json").read_text())
    assert meta["rc"] == 7
    assert meta["finished_at"] is not None


def test_codex_runner_drops_codex_api_key_from_the_lane_environment(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "seen-key.txt"
    _env_capturing_codex(fake_bin / "codex", capture)
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": None, "auth_mode": "chatgpt"}))
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the task\n")
    out = tmp_path / "caller-output.md"
    env = _runner_env(tmp_path, fake_bin, subfleet, CODEX_API_KEY="sk-must-not-leak")

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(home), "-m", "gpt-test", "-C", str(workdir),
            "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert capture.read_text().strip() == "unset"
    assert out.read_text() == "codex finished\n"


def test_codex_runner_override_allows_a_deliberate_api_dispatch(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "seen-key.txt"
    _env_capturing_codex(fake_bin / "codex", capture)
    subfleet = _worktree_subfleet(tmp_path / "subfleet")
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the task\n")
    out = tmp_path / "caller-output.md"
    env = _runner_env(
        tmp_path, fake_bin, subfleet,
        SUBFLEET_ALLOW_API_LANE="1", CODEX_API_KEY="sk-deliberate",
    )

    completed = subprocess.run(
        [
            str(SCRIPT), "-H", str(_api_key_home(tmp_path)), "-m", "gpt-6-astra",
            "-C", str(workdir), "-p", str(prompt), "-o", str(out), "-r", "0",
        ],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert capture.read_text().strip() == "sk-deliberate"
