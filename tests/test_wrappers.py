"""Behavioral contracts for the repository-local dispatch wrappers.

These tests intentionally exercise stable outcomes instead of snapshotting the
shell scripts.  A few narrow source checks cover security properties that are
not safe to infer from a successful subprocess alone.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
WRAPPERS = ("carpool-claude", "carpool-codex")


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source)
    path.chmod(0o755)
    return path


def _fake_carpool_with_usage(fake_bin: Path) -> Path:
    return _write_executable(
        fake_bin / "carpool",
        """#!/usr/bin/env bash
set -u
if [ "$1" = secret ] && [ "$2" = get-for-account ]; then
  [ "$3" = lane@example.com ] || exit 91
  printf 'setup-token-example\n'
  exit 0
fi
[ "$1" = lane-usage ] || exit 90
exec "$WRAPPER_PYTHON" -m carpool.cli "$@"
""",
    )


def _run(*args: str | Path, cwd: Path | None = None, env: dict[str, str] | None = None):
    return subprocess.run(
        [os.fspath(arg) for arg in args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _git(repo: Path, *args: str) -> str:
    result = _run("git", "-C", repo, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _fake_codex_bin(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    files = {
        "args": tmp_path / "codex.args",
        "home": tmp_path / "codex.home",
        "count": tmp_path / "codex.count",
    }
    _write_executable(
        fake_bin / "codex",
        """#!/usr/bin/env bash
set -u
count=0
[ ! -f "$WRAPPER_COUNT" ] || count=$(<"$WRAPPER_COUNT")
count=$((count + 1))
printf '%s' "$count" >"$WRAPPER_COUNT"
printf '%s\n' "$@" >"$WRAPPER_ARGS"
printf '%s' "${CODEX_HOME:-}" >"$WRAPPER_HOME"
if [ "${WRAPPER_FAIL_FIRST:-0}" = 1 ] && [ "$count" = 1 ]; then
  echo "model capacity temporarily unavailable" >&2
  exit 1
fi
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then
    shift
    out=$1
    break
  fi
  shift
done
[ -n "$out" ] || exit 91
printf 'fake result\n' >"$out"
""",
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    return fake_bin, files


def _codex_env(fake_bin: Path, files: dict[str, Path]) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "CARPOOL_CODEX_GUARD": "off",
        "WRAPPER_ARGS": os.fspath(files["args"]),
        "WRAPPER_HOME": os.fspath(files["home"]),
        "WRAPPER_COUNT": os.fspath(files["count"]),
    }


@pytest.mark.parametrize("name", WRAPPERS)
def test_bin_entrypoints_are_executable(name):
    path = BIN / name
    assert path.is_file()
    assert path.stat().st_mode & stat.S_IXUSR


def test_codex_run_forwards_effort_home_and_prompt(tmp_path):
    fake_bin, files = _fake_codex_bin(tmp_path)
    env = _codex_env(fake_bin, files)
    workdir = tmp_path / "work"
    codex_home = tmp_path / "codex-home"
    workdir.mkdir()
    codex_home.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Fix the example test")
    output = tmp_path / "answer.md"

    result = _run(
        "bash",
        BIN / "carpool-codex",
        "-H",
        codex_home,
        "-m",
        "sol",
        "-e",
        "ultra",
        "-C",
        workdir,
        "-p",
        prompt,
        "-o",
        output,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text() == "fake result\n"
    assert files["home"].read_text() == os.fspath(codex_home)
    args = files["args"].read_text().splitlines()
    effort = args.index("model_reasoning_effort=\"ultra\"")
    assert args[effort - 1] == "-c"
    assert args[-1] == "Fix the example test"


def test_codex_run_retries_transient_failure_without_losing_arguments(tmp_path):
    fake_bin, files = _fake_codex_bin(tmp_path)
    env = {**_codex_env(fake_bin, files), "WRAPPER_FAIL_FIRST": "1"}
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Implement the example")
    output = tmp_path / "answer.md"

    result = _run(
        "bash",
        BIN / "carpool-codex",
        "-H",
        tmp_path / "codex-home",
        "-m",
        "sol",
        "-r",
        "1",
        "-C",
        workdir,
        "-p",
        prompt,
        "-o",
        output,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert files["count"].read_text() == "2"
    assert "transient failure" in result.stderr
    assert output.read_text() == "fake result\n"


def test_codex_run_salvages_dirty_tree_without_moving_head_or_index(tmp_path):
    fake_bin, files = _fake_codex_bin(tmp_path)
    env = _codex_env(fake_bin, files)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Tester")
    _git(repo, "config", "user.email", "tester@example.com")
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "base")
    head_before = _git(repo, "rev-parse", "HEAD")
    index_before = _git(repo, "rev-parse", ":tracked.txt")

    tracked.write_text("after\n")
    (repo / "untracked.txt").write_text("also saved\n")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Preserve this work")
    result = _run(
        "bash",
        BIN / "carpool-codex",
        "-H",
        tmp_path / "codex-home",
        "-m",
        "sol",
        "-C",
        repo,
        "-p",
        prompt,
        "-o",
        tmp_path / "answer.md",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/codex-salvage").splitlines()
    assert len(refs) == 1
    salvage_ref = refs[0]
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "rev-parse", ":tracked.txt") == index_before
    assert tracked.read_text() == "after\n"
    assert (repo / "untracked.txt").read_text() == "also saved\n"
    assert _git(repo, "show", f"{salvage_ref}:tracked.txt") == "after"
    assert _git(repo, "show", f"{salvage_ref}:untracked.txt") == "also saved"
    assert "salvaged dirty state" in result.stderr


def test_claude_lane_detach_keeps_prompt_until_child_finishes(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_carpool = _fake_carpool_with_usage(fake_bin)
    invocation = tmp_path / "detach.invocation"
    release = tmp_path / "detach.release"
    done = tmp_path / "detach.done"
    prompt_capture = tmp_path / "detach.prompt"
    _write_executable(
        fake_bin / "nohup",
        """#!/usr/bin/env bash
set -u
{
  printf 'marker=%s\n' "${CLAUDE_LANE_DETACHED-}"
  printf 'prompt=%s\n' "${CLAUDE_LANE_DETACHED_PROMPT-}"
  printf 'arg=%s\n' "$@"
} >"$DETACH_INVOCATION_LOG"
while [ ! -e "$DETACH_RELEASE" ]; do
  /bin/sleep 0.01
done
"$@"
rc=$?
printf '%s\n' "$rc" >"$DETACH_DONE"
exit "$rc"
""",
    )
    fake_claude = _write_executable(
        fake_bin / "claude",
        """#!/usr/bin/env bash
set -u
session=""
requested=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --session-id) shift; session=$1 ;;
    --model) shift; requested=$1 ;;
  esac
  shift
done
[ -n "$session" ] && [ -n "$requested" ] || exit 92
cat >"$DETACH_CHILD_PROMPT_CAPTURE"
project_key=$(printf '%s' "$PWD" | tr '/.' '--')
project_dir="$FAKE_CLAUDE_DIR/projects/$project_key"
mkdir -p "$project_dir"
printf '{"type":"assistant","message":{"id":"message-1","model":"%s","usage":{"input_tokens":2,"output_tokens":3}}}\n' \
  "$requested" >"$project_dir/$session.jsonl"
printf '{"is_error":false,"result":"detached result"}\n'
""",
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    durable_dir = tmp_path / "durable"
    durable_dir.mkdir()
    original_prompt = tmp_path / "ephemeral-prompt.txt"
    original_prompt.write_text("prompt survives launcher exit\n")
    output = tmp_path / "answer.md"
    claude_dir = tmp_path / "claude-state"
    state_dir = tmp_path / "carpool-state"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": os.fspath(durable_dir),
        "DETACH_INVOCATION_LOG": os.fspath(invocation),
        "DETACH_RELEASE": os.fspath(release),
        "DETACH_DONE": os.fspath(done),
        "DETACH_CHILD_PROMPT_CAPTURE": os.fspath(prompt_capture),
        "CLAUDE_LANE_CARPOOL": os.fspath(fake_carpool),
        "CLAUDE_LANE_CLAUDE": os.fspath(fake_claude),
        "CLAUDE_LANE_CLAUDE_DIR": os.fspath(claude_dir),
        "FAKE_CLAUDE_DIR": os.fspath(claude_dir),
        "CARPOOL_STATE_DIR": os.fspath(state_dir),
        "PYTHONPATH": os.fspath(ROOT),
        "WRAPPER_PYTHON": sys.executable,
    }

    launcher = _run(
        "bash",
        BIN / "carpool-claude",
        "-d",
        "-a",
        "lane@example.com",
        "-s",
        "read-only",
        "-C",
        workdir,
        "-p",
        original_prompt,
        "-o",
        output,
        "--",
        env=env,
    )

    assert launcher.returncode == 0, launcher.stderr
    assert "detached pid=" in launcher.stdout
    deadline = time.monotonic() + 5
    while not invocation.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert invocation.is_file()
    lines = invocation.read_text().splitlines()
    detached_prompt = Path(
        next(line.removeprefix("prompt=") for line in lines if line.startswith("prompt="))
    )
    args = [line.removeprefix("arg=") for line in lines if line.startswith("arg=")]
    try:
        assert "marker=1" in lines
        assert args[-2:] == ["-p", os.fspath(detached_prompt)]
        assert detached_prompt.is_file()
        assert detached_prompt.read_text() == original_prompt.read_text()
        original_prompt.unlink()
    finally:
        release.touch()
        deadline = time.monotonic() + 5
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

    assert done.read_text().strip() == "0"
    assert prompt_capture.read_text() == "prompt survives launcher exit\n"
    assert output.read_text() == "detached result\n"
    assert not detached_prompt.exists()


def test_claude_lane_detach_fails_when_prompt_copy_cannot_be_created(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("detached prompt")
    env = {**os.environ, "TMPDIR": os.fspath(tmp_path / "missing-tmp")}

    result = _run(
        "bash",
        BIN / "carpool-claude",
        "-d",
        "-a",
        "lane@example.com",
        "-C",
        workdir,
        "-p",
        prompt,
        "-o",
        tmp_path / "answer.md",
        env=env,
    )

    assert result.returncode == 2
    assert "could not create a durable prompt copy" in result.stderr


def test_claude_lane_scrubs_api_keys_and_marks_transcript_model_mismatch(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    env_capture = tmp_path / "claude.env"
    fake_carpool = _fake_carpool_with_usage(fake_bin)
    fake_claude = _write_executable(
        fake_bin / "claude",
        """#!/usr/bin/env bash
set -u
session=""
requested=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --session-id) shift; session=$1 ;;
    --model) shift; requested=$1 ;;
  esac
  shift
done
[ -n "$session" ] && [ -n "$requested" ] || exit 92
printf '%s\n%s\n%s\n' \
  "${ANTHROPIC_API_KEY-UNSET}" \
  "${ANTHROPIC_AUTH_TOKEN-UNSET}" \
  "${CLAUDE_CODE_OAUTH_TOKEN-UNSET}" >"$CLAUDE_ENV_CAPTURE"
project_key=$(printf '%s' "$PWD" | tr '/.' '--')
project_dir="$FAKE_CLAUDE_DIR/projects/$project_key"
mkdir -p "$project_dir"
printf '{"type":"assistant","message":{"id":"message-1","model":"%s","usage":{"input_tokens":3,"output_tokens":5}}}\n' \
  "$FAKE_SERVED_MODEL" >"$project_dir/$session.jsonl"
printf '{"type":"assistant","message":{"id":"message-1","model":"%s","usage":{"input_tokens":7,"output_tokens":11}}}\n' \
  "$FAKE_SERVED_MODEL" >>"$project_dir/$session.jsonl"
printf '{"type":"assistant","message":{"id":"message-2","model":"%s","usage":{"input_tokens":13,"output_tokens":17}}}\n' \
  "$FAKE_SERVED_MODEL" >>"$project_dir/$session.jsonl"
printf '{"is_error":false,"result":"fake result"}\n'
""",
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Review the example")
    output = tmp_path / "answer.md"
    claude_dir = tmp_path / "claude-state"
    state_dir = tmp_path / "carpool-state"
    env = {
        **os.environ,
        "ANTHROPIC_API_KEY": "metered-api-value",
        "ANTHROPIC_AUTH_TOKEN": "metered-auth-value",
        "CLAUDE_ENV_CAPTURE": os.fspath(env_capture),
        "CLAUDE_LANE_CARPOOL": os.fspath(fake_carpool),
        "CLAUDE_LANE_CLAUDE": os.fspath(fake_claude),
        "CLAUDE_LANE_CLAUDE_DIR": os.fspath(claude_dir),
        "FAKE_CLAUDE_DIR": os.fspath(claude_dir),
        "FAKE_SERVED_MODEL": "claude-opus-example",
        "CARPOOL_STATE_DIR": os.fspath(state_dir),
        "PYTHONPATH": os.fspath(ROOT),
        "WRAPPER_PYTHON": sys.executable,
    }

    result = _run(
        "bash",
        BIN / "carpool-claude",
        "-a",
        "lane@example.com",
        "-m",
        "claude-fable-5",
        "-s",
        "read-only",
        "-C",
        workdir,
        "-p",
        prompt,
        "-o",
        output,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text() == "fake result\n"
    assert env_capture.read_text().splitlines() == [
        "UNSET",
        "UNSET",
        "setup-token-example",
    ]
    marker = tmp_path / "answer.DOWNGRADED"
    assert marker.is_file()
    assert "MODEL-DOWNGRADE" in marker.read_text()
    assert "latest: claude-opus-example" in marker.read_text()
    assert "MODEL-DOWNGRADE" in result.stderr
    records = [
        json.loads(line) for line in (state_dir / "lane-usage.jsonl").read_text().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["email"] == "lane@example.com"
    assert records[0]["session_id"]
    assert records[0]["input_tokens"] == 20
    assert records[0]["output_tokens"] == 28
    assert records[0]["total_tokens"] == 48


def test_claude_lane_records_hard_limit_before_returning_rc4(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_carpool = _fake_carpool_with_usage(fake_bin)
    fake_claude = _write_executable(
        fake_bin / "claude",
        """#!/usr/bin/env bash
set -u
session=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = --session-id ]; then
    shift
    session=$1
  fi
  shift
done
[ -n "$session" ] || exit 92
project_key=$(printf '%s' "$PWD" | tr '/.' '--')
project_dir="$FAKE_CLAUDE_DIR/projects/$project_key"
mkdir -p "$project_dir"
printf '%s\n' \
  '{"type":"assistant","message":{"id":"limit-message","model":"claude-fable-5","usage":{"input_tokens":19,"output_tokens":23}}}' \
  >"$project_dir/$session.jsonl"
printf '%s\n' \
  '{"is_error":true,"result":"You have hit your session limit; resets 6:40pm (America/New_York)"}'
exit 1
""",
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Review the example")
    state_dir = tmp_path / "carpool-state"
    claude_dir = tmp_path / "claude-state"
    env = {
        **os.environ,
        "CARPOOL_STATE_DIR": os.fspath(state_dir),
        "CLAUDE_LANE_CARPOOL": os.fspath(fake_carpool),
        "CLAUDE_LANE_CLAUDE": os.fspath(fake_claude),
        "CLAUDE_LANE_CLAUDE_DIR": os.fspath(claude_dir),
        "FAKE_CLAUDE_DIR": os.fspath(claude_dir),
        "PYTHONPATH": os.fspath(ROOT),
        "WRAPPER_PYTHON": sys.executable,
    }

    result = _run(
        "bash",
        BIN / "carpool-claude",
        "-a",
        "lane@example.com",
        "-C",
        workdir,
        "-p",
        prompt,
        "-o",
        tmp_path / "answer.md",
        env=env,
    )

    assert result.returncode == 4, result.stderr
    assert "carpool claude: hard limit reset=" in result.stderr
    records = [
        json.loads(line) for line in (state_dir / "lane-usage.jsonl").read_text().splitlines()
    ]
    assert len(records) == 2
    assert records[0]["total_tokens"] == 42
    assert records[1]["event"] == "hard_limit"
    assert records[1]["email"] == "lane@example.com"
    assert records[1]["reset"]


def test_wrapper_sources_retain_hardening_primitives():
    codex = (BIN / "carpool-codex").read_text()
    claude = (BIN / "carpool-claude").read_text()

    for source, ref_namespace in (
        (codex, "refs/codex-salvage/"),
        (claude, "refs/claude-salvage/"),
    ):
        assert "commit-tree" in source
        assert "GIT_INDEX_FILE" in source
        assert "update-ref" in source
        assert ref_namespace in source
        assert "trap on_exit EXIT" in source
        assert "trap 'exit 130' INT" in source
        assert "trap 'exit 143' TERM" in source

    assert "unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN" in claude
    assert "MODEL-DOWNGRADE" in claude
    assert ".DOWNGRADED" in claude
    assert "latest:" in claude


def test_bin_sources_pass_the_public_scrub_contract():
    sources = "\n".join(path.read_text() for path in sorted(BIN.iterdir()) if path.is_file())
    private_markers = (
        "max" + "ghe" + "nis",
        "m" + "ghe" + "nis",
        "chief" + "-of-" + "staff",
        "policy" + "engine",
        "ax" + "iom",
        "hive" + "sight",
        "ubi" + "center",
        "thesis" + "institute",
        "far" + "ness",
        "opti" + "qal",
        "tele" + "gram",
        "sk-ant" + "-oat",
    )
    folded = sources.casefold()
    assert not [marker for marker in private_markers if marker in folded]

    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", sources, re.IGNORECASE)
    assert all(email.casefold().endswith("@example.com") for email in emails)
