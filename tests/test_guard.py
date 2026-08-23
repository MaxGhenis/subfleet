"""Focused tests for the portable Codex PreToolUse command guard."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "bin" / "subfleet-guard-hook"
GUARD = ROOT / "bin" / "subfleet-guard"
KEY = "/<session-flags>/config.toml:pre_tool_use:0:0"
MATCHER = "Bash"
STATUS = "subfleet safety guard"
PROCESS_TIMEOUT = 20


def run(argv, *, stdin=None, cwd=None, env=None, timeout=PROCESS_TIMEOUT):
    return subprocess.run(
        [str(value) for value in argv],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
        env=env,
        timeout=timeout,
    )


def event(command, cwd, *, tool="Bash", event_name="PreToolUse", tool_input=None):
    return json.dumps(
        {
            "session_id": "guard-test",
            "turn_id": "guard-test",
            "tool_use_id": "guard-test",
            "hook_event_name": event_name,
            "tool_name": tool,
            "cwd": str(cwd),
            "tool_input": tool_input if tool_input is not None else {"command": command},
        }
    )


def call_hook(command, cwd, **kwargs):
    return run([HOOK], stdin=event(command, cwd, **kwargs))


def deny_reason(completed):
    if not completed.stdout:
        return ""
    return json.loads(completed.stdout)["hookSpecificOutput"][
        "permissionDecisionReason"
    ]


def assert_denied(command, cwd, tag):
    completed = call_hook(command, cwd)
    assert completed.returncode == 0
    output = json.loads(completed.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"].startswith(tag)


def assert_allowed(command, cwd, **kwargs):
    completed = call_hook(command, cwd, **kwargs)
    assert completed.returncode == 0
    assert completed.stdout == ""


def git(cwd, *args):
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=guard-test",
            "-c",
            "user.email=guard.test.invalid",
            *args,
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=PROCESS_TIMEOUT,
    )


@pytest.fixture
def git_repositories(tmp_path):
    single = tmp_path / "single"
    single.mkdir()
    git(single, "init", "-q", "-b", "main")
    git(single, "commit", "-q", "--allow-empty", "-m", "initial")

    shared = tmp_path / "shared"
    shared.mkdir()
    git(shared, "init", "-q", "-b", "main")
    git(shared, "commit", "-q", "--allow-empty", "-m", "initial")
    linked = tmp_path / "linked"
    git(shared, "worktree", "add", "-q", str(linked), "-b", "linked")

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "commit", "-q", "--allow-empty", "-m", "initial")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    run_result = run(["git", "clone", "-q", origin, clone])
    assert run_result.returncode == 0, run_result.stderr

    return {
        "single": single,
        "shared": shared,
        "linked": linked,
        "clone": clone,
    }


@pytest.mark.parametrize("raw", ["", "{", "null", "[]", '"text"'])
def test_malformed_payloads_fail_open(raw):
    completed = run([HOOK], stdin=raw)
    assert completed.returncode == 0
    assert completed.stdout == ""


def test_irrelevant_events_and_tools_fail_open(tmp_path):
    assert_allowed("find / -name result", tmp_path, event_name="PostToolUse")
    assert_allowed("find / -name result", tmp_path, tool="apply_patch")
    completed = run(
        [HOOK],
        stdin=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "cwd": str(tmp_path),
                "tool_input": {},
            }
        ),
    )
    assert completed.returncode == 0
    assert completed.stdout == ""


def test_command_array_is_accepted(tmp_path):
    completed = call_hook(
        None,
        tmp_path,
        tool_input={"command": ["find", "/", "-name", "result"]},
    )
    assert deny_reason(completed).startswith("[unscoped-search]")


@pytest.mark.parametrize(
    "command",
    [
        "find / -name result",
        'sudo find "/Users" -type f',
        'rg needle "$HOME"',
        "rg --files /tmp",
        "grep -R needle /var/tmp",
        "command rg needle /home",
        "cd / && find . -name result",
        "rg needle src\nfind /private/tmp -name result",
    ],
)
def test_unscoped_search_denials(command, tmp_path):
    assert_denied(command, tmp_path, "[unscoped-search]")


def test_dot_search_denied_from_broad_cwd(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    assert_denied("rg needle .", home, "[unscoped-search]")


@pytest.mark.parametrize(
    "command",
    [
        "find / -maxdepth 2 -name result",
        "rg --max-depth 2 needle /",
        "find src -name result",
        'rg "/" src',
        "grep -R needle src",
        'echo "find / -name result"',
        "rg needle /tmp/a-specific-project",
        "grep needle /",
        "ls / | rg needle",
    ],
)
def test_scoped_or_non_recursive_searches_allowed(command, tmp_path):
    assert_allowed(command, tmp_path)


def test_keychain_dump_policy(tmp_path):
    assert_denied("security dump-keychain -d", tmp_path, "[keychain-dump]")
    assert_denied(
        "sudo /usr/bin/security -v dump-keychain -d login.keychain-db",
        tmp_path,
        "[keychain-dump]",
    )
    assert_allowed("security dump-keychain", tmp_path)
    assert_allowed("security find-generic-password -s one-item", tmp_path)


def test_stash_only_denied_in_shared_worktree_repository(git_repositories):
    assert_denied("git stash", git_repositories["shared"], "[stash-shared]")
    assert_denied("git stash pop", git_repositories["linked"], "[stash-shared]")
    assert_allowed("git stash list", git_repositories["shared"])
    assert_allowed("git stash", git_repositories["single"])


def test_local_default_branch_policy(git_repositories):
    clone = git_repositories["clone"]
    assert_denied("git checkout -b feature main", clone, "[local-main]")
    assert_denied("git switch -c feature", clone, "[local-main]")
    assert_allowed("git checkout -b feature origin/main", clone)
    assert_allowed("git switch -c feature origin/main", clone)
    assert_allowed("git checkout -b feature main", git_repositories["single"])


def test_denial_logging_is_opt_in_and_fail_safe(tmp_path):
    log_path = tmp_path / "denials.log"
    env = os.environ.copy()
    env["SUBFLEET_GUARD_LOG"] = str(log_path)
    completed = run(
        [HOOK],
        stdin=event("find / -name result", tmp_path),
        env=env,
    )
    assert deny_reason(completed).startswith("[unscoped-search]")
    assert "find / -name result" in log_path.read_text()

    env["SUBFLEET_GUARD_LOG"] = str(tmp_path / "missing" / "directory")
    completed = run(
        [HOOK],
        stdin=event("find / -name result", tmp_path),
        env=env,
    )
    assert deny_reason(completed).startswith("[unscoped-search]")


def shell_quote(path):
    if path and re.fullmatch(r"[A-Za-z0-9@%+=:,./_-]+", path, re.ASCII):
        return path
    return "'" + path.replace("'", "'\\''") + "'"


def expected_hash(hook_path, *, timeout=60, matcher=MATCHER, status=STATUS):
    identity = {
        "event_name": "pre_tool_use",
        "matcher": matcher,
        "hooks": [
            {
                "type": "command",
                "command": shell_quote(str(hook_path)),
                "timeout": timeout,
                "async": False,
                "statusMessage": status,
            }
        ],
    }
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def test_hash_matches_normalized_identity():
    completed = run([GUARD, "hash"])
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected_hash(HOOK)


def test_override_is_valid_toml_and_embeds_hash():
    completed = run([GUARD, "override"])
    assert completed.returncode == 0, completed.stderr
    assert "\n" not in completed.stdout.rstrip("\n")
    parsed = tomllib.loads(completed.stdout)
    handler = parsed["hooks"]["PreToolUse"][0]
    assert handler["matcher"] == MATCHER
    assert handler["hooks"][0]["command"] == str(HOOK)
    assert handler["hooks"][0]["timeout"] == 60
    assert handler["hooks"][0]["statusMessage"] == STATUS
    assert parsed["hooks"]["state"][KEY] == {
        "trusted_hash": expected_hash(HOOK),
        "enabled": True,
    }


def test_hash_and_override_quote_candidate_hook_path(tmp_path):
    candidate = tmp_path / "candidate guard's hook"
    shutil.copy2(HOOK, candidate)
    candidate.chmod(0o755)
    completed = run([GUARD, "hash", "--hook", candidate, "--timeout", "17"])
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected_hash(candidate, timeout=17)
    override = run([GUARD, "override", "--hook", candidate]).stdout
    parsed = tomllib.loads(override)
    assert parsed["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == shell_quote(
        str(candidate)
    )


def test_key_and_check_commands(tmp_path):
    assert run([GUARD, "key"]).stdout.strip() == KEY
    denied = run([GUARD, "check", "find / -name result", tmp_path])
    assert denied.returncode == 1
    assert denied.stdout.startswith("deny: [unscoped-search]")
    allowed = run([GUARD, "check", "find src -name result", tmp_path])
    assert allowed.returncode == 0
    assert allowed.stdout == "allow\n"
    irrelevant = run(
        [GUARD, "check", "--tool", "apply_patch", "find / -name result", tmp_path]
    )
    assert irrelevant.returncode == 0
    assert irrelevant.stdout == "allow\n"


def test_check_reads_stdin(tmp_path):
    completed = run(
        [GUARD, "check", "-", tmp_path],
        stdin="grep -R needle /tmp",
    )
    assert completed.returncode == 1
    assert completed.stdout.startswith("deny: [unscoped-search]")


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["unknown"],
        ["hash", "--hook"],
        ["hash", "--hook", "relative"],
        ["hash", "--timeout", "zero"],
        ["preflight", "-H", "/tmp"],
    ],
)
def test_usage_errors_exit_two(arguments):
    assert run([GUARD, *arguments]).returncode == 2


def make_codex_stub(path):
    path.write_text(
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
  echo "codex-cli guard-test"
  exit 0
fi
if [ "${1:-}" = "app-server" ]; then
  config=no; hooks=no; auth=no
  [ -f "$CODEX_HOME/config.toml" ] && config=yes
  [ -f "$CODEX_HOME/hooks.json" ] && hooks=yes
  [ -e "$CODEX_HOME/auth.json" ] && auth=yes
  if [ -n "${SUBFLEET_TEST_REPORT:-}" ]; then
    printf 'config=%s hooks=%s auth=%s\n' "$config" "$hooks" "$auth" >> "$SUBFLEET_TEST_REPORT"
  fi
  while IFS= read -r request; do
    case "$request" in
      *'"id":2'*)
        printf '{"jsonrpc":"2.0","id":2,"result":{"data":[{"warnings":[],"errors":[],"hooks":[{"key":"%s","enabled":true,"trustStatus":"%s","currentHash":"%s"}]}]}}\n' \
          "$SUBFLEET_TEST_KEY" "${SUBFLEET_TEST_TRUST:-trusted}" "$SUBFLEET_TEST_HASH"
        exit 0
        ;;
    esac
  done
fi
exit 1
"""
    )
    path.chmod(0o755)


def preflight_env(tmp_path, stub):
    env = os.environ.copy()
    env.update(
        {
            "SUBFLEET_GUARD_CACHE": str(tmp_path / "guard-cache"),
            "SUBFLEET_TEST_HASH": expected_hash(HOOK),
            "SUBFLEET_TEST_KEY": KEY,
            "SUBFLEET_TEST_REPORT": str(tmp_path / "stub-report"),
            "SUBFLEET_GUARD_PREFLIGHT_TIMEOUT": "3",
        }
    )
    return env


def test_preflight_uses_scratch_home_and_cache(tmp_path):
    stub = tmp_path / "codex-stub"
    make_codex_stub(stub)
    lane = tmp_path / "lane"
    lane.mkdir()
    (lane / "config.toml").write_text("[features]\nhooks = true\n")
    (lane / "hooks.json").write_text("{}\n")
    auth = lane / "auth.json"
    auth.write_text("credential-sentinel\n")
    workdir = tmp_path / "work"
    workdir.mkdir()
    env = preflight_env(tmp_path, stub)
    command = [
        GUARD,
        "preflight",
        "-H",
        lane,
        "-C",
        workdir,
        "--codex",
        stub,
    ]

    first = run(command, env=env)
    assert first.returncode == 0, first.stderr
    assert first.stdout.startswith("preflight: ok (codex-cli guard-test")
    assert auth.read_text() == "credential-sentinel\n"
    report = Path(env["SUBFLEET_TEST_REPORT"])
    assert report.read_text().splitlines() == ["config=yes hooks=yes auth=no"]
    assert len(list(Path(env["SUBFLEET_GUARD_CACHE"]).glob("ok-*"))) == 1

    second = run(command, env=env)
    assert second.returncode == 0, second.stderr
    assert second.stdout.startswith("preflight: cached ok (codex-cli guard-test")
    assert report.read_text().splitlines() == ["config=yes hooks=yes auth=no"]

    (lane / "config.toml").write_text("[features]\nhooks = true\nplugins = false\n")
    third = run(command, env=env)
    assert third.returncode == 0, third.stderr
    assert third.stdout.startswith("preflight: ok (codex-cli guard-test")
    assert report.read_text().splitlines() == [
        "config=yes hooks=yes auth=no",
        "config=yes hooks=yes auth=no",
    ]


def test_preflight_rejects_untrusted_hook_and_no_cache_writes_nothing(tmp_path):
    stub = tmp_path / "codex-stub"
    make_codex_stub(stub)
    lane = tmp_path / "lane"
    lane.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()
    env = preflight_env(tmp_path, stub)
    env["SUBFLEET_TEST_TRUST"] = "modified"
    completed = run(
        [
            GUARD,
            "preflight",
            "--no-cache",
            "-H",
            lane,
            "-C",
            workdir,
            "--codex",
            stub,
        ],
        env=env,
    )
    assert completed.returncode == 1
    assert "preflight: FAILED - hook enabled=true trustStatus=modified" in completed.stderr
    assert not Path(env["SUBFLEET_GUARD_CACHE"]).exists()


def test_preflight_validates_paths_before_launch(tmp_path):
    missing = tmp_path / "missing"
    completed = run(
        [GUARD, "preflight", "-H", missing, "-C", tmp_path, "--codex", "/bin/false"]
    )
    assert completed.returncode == 1
    assert "codex home not found" in completed.stderr


def test_scripts_are_executable_and_bash_32_syntax_clean():
    for script in (HOOK, GUARD):
        assert script.stat().st_mode & stat.S_IXUSR
        completed = run(["/bin/bash", "-n", script])
        assert completed.returncode == 0, completed.stderr
