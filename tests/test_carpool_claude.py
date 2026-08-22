"""Focused subprocess contracts for the unified ``carpool claude`` runner."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "carpool-claude"


def _write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\nset -u\n" + body)
    path.chmod(0o755)
    return path


def _fixture(tmp_path: Path, claude_body: str) -> tuple[dict[str, str], dict[str, Path]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the example task\n")
    output = tmp_path / "answer.md"
    calls = tmp_path / "carpool.calls"

    claude = _write_executable(fake_bin / "claude", claude_body)
    carpool = _write_executable(
        fake_bin / "carpool",
        r'''log_call() {
  {
    printf '%s' "${1:-}"
    shift || true
    for arg in "$@"; do printf '\t%s' "$arg"; done
    printf '\n'
  } >>"$CARPOOL_CALLS"
}

log_call "$@"
case "${1:-}" in
  _record-run)
    phase=""
    previous=""
    for arg in "$@"; do
      if [ "$previous" = --phase ]; then phase=$arg; fi
      previous=$arg
    done
    [ "${RUN_HOOK_EXIT:-0}" = 0 ] || exit "$RUN_HOOK_EXIT"
    [ "$phase" != start ] || printf 'run-example\n'
    exit 0
    ;;
  secret)
    [ "${2:-}" = get-for-account ] || exit 87
    case ",${NO_TOKEN_LANES:-}," in
      *,"${3:-}",*) exit 1 ;;
    esac
    printf 'token:%s\n' "${3:-}"
    exit 0
    ;;
  pick)
    [ "${2:-}" = claude ] || exit 86
    shift 2
    excluded=,
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --exclude) excluded="${excluded}${2},"; shift 2 ;;
        *) shift ;;
      esac
    done
    old_ifs=$IFS
    IFS=,
    for candidate in ${CLAUDE_CANDIDATES:-alpha@example.com}; do
      case "$excluded" in
        *,"$candidate",*) ;;
        *)
          IFS=$old_ifs
          printf '%s\n' "$candidate"
          exit 0
          ;;
      esac
    done
    IFS=$old_ifs
    exit 1
    ;;
  lane-usage)
    exit "${USAGE_HOOK_EXIT:-0}"
    ;;
esac
exit 88
''',
    )
    _write_executable(
        fake_bin / "uuidgen",
        r'''count=$(cat "$UUID_COUNTER" 2>/dev/null || printf 0)
count=$((count + 1))
printf '%s\n' "$count" >"$UUID_COUNTER"
printf '00000000-0000-4000-8000-%012d\n' "$count"
''',
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
            "CLAUDE_LANE_CLAUDE": os.fspath(claude),
            "CLAUDE_LANE_CARPOOL": os.fspath(carpool),
            "CLAUDE_LANE_CLAUDE_DIR": os.fspath(tmp_path / "claude-state"),
            "CLAUDE_LANE_BACKOFF": "0",
            "CARPOOL_CALLS": os.fspath(calls),
            "UUID_COUNTER": os.fspath(tmp_path / "uuid-counter"),
        }
    )
    return env, {
        "fake_bin": fake_bin,
        "workdir": workdir,
        "prompt": prompt,
        "output": output,
        "calls": calls,
    }


def _run(
    env: dict[str, str], paths: dict[str, Path], *mode_args: str
) -> subprocess.CompletedProcess[str]:
    mode = list(mode_args) or ["-a", "alpha@example.com"]
    return subprocess.run(
        [
            os.fspath(SCRIPT),
            *mode,
            "-C",
            os.fspath(paths["workdir"]),
            "-p",
            os.fspath(paths["prompt"]),
            "-o",
            os.fspath(paths["output"]),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _calls(path: Path, command: str | None = None) -> list[list[str]]:
    rows = [] if not path.exists() else [line.split("\t") for line in path.read_text().splitlines()]
    return rows if command is None else [row for row in rows if row[0] == command]


def _option(call: list[str], name: str) -> str:
    return call[call.index(name) + 1]


def test_auto_repick_accumulates_exclusions_and_keeps_accounting_best_effort(tmp_path):
    env, paths = _fixture(
        tmp_path,
        r'''case "$CLAUDE_CODE_OAUTH_TOKEN" in
  token:alpha@example.com)
    printf '{"is_error":true,"result":"session limit; resets later"}\n'
    exit 9
    ;;
  token:beta@example.com)
    printf '{"is_error":true,"result":"weekly limit; resets later"}\n'
    exit 8
    ;;
  token:gamma@example.com)
    printf '{"is_error":false,"result":"third lane finished"}\n'
    ;;
  *) exit 97 ;;
esac
''',
    )
    env["CLAUDE_CANDIDATES"] = (
        "alpha@example.com,beta@example.com,gamma@example.com"
    )
    env["USAGE_HOOK_EXIT"] = "23"

    result = _run(env, paths, "-A")

    assert result.returncode == 0, result.stderr
    assert paths["output"].read_text() == "third lane finished\n"
    picks = _calls(paths["calls"], "pick")
    assert picks == [
        ["pick", "claude"],
        ["pick", "claude", "--exclude", "alpha@example.com"],
        [
            "pick",
            "claude",
            "--exclude",
            "alpha@example.com",
            "--exclude",
            "beta@example.com",
        ],
    ]
    secrets = _calls(paths["calls"], "secret")
    assert [call[2] for call in secrets] == [
        "alpha@example.com",
        "beta@example.com",
        "gamma@example.com",
    ]
    usage = _calls(paths["calls"], "lane-usage")
    assert [call[1] for call in usage] == [
        "record",
        "hard-limit",
        "record",
        "hard-limit",
        "record",
    ]

    run_calls = _calls(paths["calls"], "_record-run")
    updates = [call for call in run_calls if _option(call, "--phase") == "update"]
    assert [_option(call, "--lane") for call in updates] == [
        "alpha@example.com",
        "beta@example.com",
        "gamma@example.com",
    ]
    finish = next(call for call in run_calls if _option(call, "--phase") == "finish")
    assert _option(finish, "--lane") == "gamma@example.com"
    assert _option(finish, "--rc") == "0"
    assert _option(finish, "--session-id").endswith("000000000003")


def test_auto_pick_excludes_missing_tokens_but_pinned_missing_token_is_rc5(tmp_path):
    env, paths = _fixture(
        tmp_path,
        r'''printf '{"is_error":false,"result":"used %s"}\n' "$CLAUDE_CODE_OAUTH_TOKEN"
''',
    )
    env["CLAUDE_CANDIDATES"] = "missing@example.com,beta@example.com"
    env["NO_TOKEN_LANES"] = "missing@example.com"

    automatic = _run(env, paths, "-A")

    assert automatic.returncode == 0, automatic.stderr
    assert paths["output"].read_text() == "used token:beta@example.com\n"
    assert _calls(paths["calls"], "pick") == [
        ["pick", "claude"],
        ["pick", "claude", "--exclude", "missing@example.com"],
    ]
    assert "no stored setup token for missing@example.com" in automatic.stderr

    pinned_dir = tmp_path / "pinned"
    pinned_dir.mkdir()
    pinned_env, pinned_paths = _fixture(
        pinned_dir,
        "printf 'provider must not run\\n' >\"$CLAUDE_RAN\"\n",
    )
    pinned_env["NO_TOKEN_LANES"] = "missing@example.com"
    pinned_env["CLAUDE_RAN"] = os.fspath(pinned_dir / "provider-ran")

    pinned = _run(pinned_env, pinned_paths, "-a", "missing@example.com")

    assert pinned.returncode == 5
    assert not Path(pinned_env["CLAUDE_RAN"]).exists()
    assert [call[1] for call in _calls(pinned_paths["calls"], "lane-usage")] == [
        "auth-failure"
    ]
    finish = next(
        call
        for call in _calls(pinned_paths["calls"], "_record-run")
        if _option(call, "--phase") == "finish"
    )
    assert _option(finish, "--rc") == "5"


@pytest.mark.parametrize(
    ("claude_body", "expected_rc", "message"),
    [
        ("printf '401 authentication_error\\n' >&2\nexit 4\n", 5, "AUTH FAILURE"),
        (
            "printf '{\"is_error\":true,\"result\":\"subscription limit\"}\\n'\nexit 5\n",
            4,
            "LANE LIMITED",
        ),
        ("printf 'unclassified provider failure\\n' >&2\nexit 4\n", 1, "FAILED"),
        ("printf 'unclassified provider failure\\n' >&2\nexit 5\n", 1, "FAILED"),
    ],
)
def test_reserved_rc4_and_rc5_require_text_classification(
    tmp_path, claude_body, expected_rc, message
):
    env, paths = _fixture(tmp_path, claude_body)

    result = _run(env, paths, "-a", "alpha@example.com", "-r", "0")

    assert result.returncode == expected_rc
    assert message in result.stderr
    finish = next(
        call
        for call in _calls(paths["calls"], "_record-run")
        if _option(call, "--phase") == "finish"
    )
    assert _option(finish, "--rc") == str(expected_rc)


def test_provider_auth_failure_persists_lane_cooldown_hook(tmp_path):
    env, paths = _fixture(
        tmp_path,
        "printf '401 authentication_error\\n' >&2\nexit 4\n",
    )

    result = _run(env, paths, "-a", "alpha@example.com", "-r", "0")

    assert result.returncode == 5
    assert [call[1] for call in _calls(paths["calls"], "lane-usage")] == [
        "record",
        "auth-failure",
    ]


def test_rate_limit_reached_is_transient_not_a_hard_lane_limit(tmp_path):
    env, paths = _fixture(
        tmp_path,
        r'''count=$(cat "$CLAUDE_COUNTER" 2>/dev/null || printf 0)
count=$((count + 1))
printf '%s\n' "$count" >"$CLAUDE_COUNTER"
if [ "$count" = 1 ]; then
  printf '429 rate limit reached\n' >&2
  exit 4
fi
printf '{"is_error":false,"result":"recovered"}\n'
''',
    )
    env["CLAUDE_COUNTER"] = os.fspath(tmp_path / "claude-counter")

    result = _run(env, paths)

    assert result.returncode == 0, result.stderr
    assert paths["output"].read_text() == "recovered\n"
    assert [call[1] for call in _calls(paths["calls"], "lane-usage")] == [
        "record",
        "record",
    ]
    assert "transient failure" in result.stderr


def test_durable_hook_unavailability_never_changes_provider_success(tmp_path):
    env, paths = _fixture(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"finished\"}\\n'\n",
    )
    env["RUN_HOOK_EXIT"] = "71"

    result = _run(env, paths)

    assert result.returncode == 0, result.stderr
    assert paths["output"].read_text() == "finished\n"
    phases = [
        _option(call, "--phase") for call in _calls(paths["calls"], "_record-run")
    ]
    assert phases == ["start"]


def test_output_artifact_hardlink_alias_is_rejected_before_truncation(tmp_path):
    env, paths = _fixture(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"must not run\"}\\n'\n",
    )
    paths["output"].write_text("sentinel output\n")
    os.link(paths["output"], tmp_path / "answer.result.json")

    result = _run(env, paths)

    assert result.returncode == 2
    assert "must not alias" in result.stderr
    assert paths["output"].read_text() == "sentinel output\n"
    assert _calls(paths["calls"]) == []


def test_inline_transcript_model_check_scrubs_api_keys_and_clears_stale_marker(tmp_path):
    env, paths = _fixture(
        tmp_path,
        r'''session=""
requested=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --session-id) shift; session=$1 ;;
    --model) shift; requested=$1 ;;
  esac
  shift
done
printf '%s\n%s\n%s\n%s\n' \
  "${ANTHROPIC_API_KEY-UNSET}" \
  "${ANTHROPIC_AUTH_TOKEN-UNSET}" \
  "${CLAUDE_CODE_OAUTH_TOKEN-UNSET}" \
  "${CLAUDE_LANE_OWNED_PROMPT-UNSET}" >"$CHILD_ENV_CAPTURE"
project_key=$(printf '%s' "$PWD" | tr '/.' '--')
project_dir="$CLAUDE_LANE_CLAUDE_DIR/projects/$project_key"
mkdir -p "$project_dir"
printf '{"type":"assistant","message":{"model":"%s"}}\n' \
  "$SERVED_MODEL" >"$project_dir/$session.jsonl"
printf '{"is_error":false,"result":"served output"}\n'
''',
    )
    capture = tmp_path / "child.env"
    env.update(
        {
            "ANTHROPIC_API_KEY": "metered-api-example",
            "ANTHROPIC_AUTH_TOKEN": "metered-auth-example",
            "CHILD_ENV_CAPTURE": os.fspath(capture),
            "SERVED_MODEL": "claude-opus-example",
        }
    )

    first = _run(env, paths)

    marker = tmp_path / "answer.DOWNGRADED"
    assert first.returncode == 0, first.stderr
    assert capture.read_text().splitlines() == [
        "UNSET",
        "UNSET",
        "token:alpha@example.com",
        "UNSET",
    ]
    assert marker.is_file()
    assert "MODEL-DOWNGRADE" in marker.read_text()
    assert "latest: claude-opus-example" in marker.read_text()

    env["SERVED_MODEL"] = "claude-fable-5"
    second = _run(env, paths)

    assert second.returncode == 0, second.stderr
    assert not marker.exists()


def test_detached_child_owns_copy_and_scrubs_handoff_environment(tmp_path):
    env, paths = _fixture(
        tmp_path,
        r'''env >"$CHILD_ENV_FILE"
IFS= read -r prompt
printf '{"is_error":false,"result":"detached read: %s"}\n' "$prompt"
''',
    )
    prompt_root = tmp_path / "detached-prompts"
    prompt_root.mkdir()
    child_env = tmp_path / "child.env"
    env.update(
        {
            "CLAUDE_LANE_TMPDIR": os.fspath(prompt_root),
            "CLAUDE_LANE_DETACHED_START_DELAY": "0.2",
            "CHILD_ENV_FILE": os.fspath(child_env),
        }
    )

    parent = _run(env, paths, "-a", "alpha@example.com", "-d")

    assert parent.returncode == 0, parent.stderr
    assert "detached pid=" in parent.stdout
    paths["prompt"].unlink()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (
        not paths["output"].exists()
        or not child_env.exists()
        or list(prompt_root.glob("carpool-claude-prompt.*"))
    ):
        time.sleep(0.05)

    assert paths["output"].read_text() == "detached read: do the example task\n"
    child_keys = {line.partition("=")[0] for line in child_env.read_text().splitlines()}
    assert "CLAUDE_LANE_DETACHED" not in child_keys
    assert "CLAUDE_LANE_OWNED_PROMPT" not in child_keys
    assert "CLAUDE_LANE_DETACHED_PROMPT" not in child_keys
    assert list(prompt_root.glob("carpool-claude-prompt.*")) == []


def test_runner_source_uses_only_the_public_secret_bridge():
    source = SCRIPT.read_text()

    assert 'secret get-for-account "$EMAIL"' in source
    assert "agent" + "-secret" not in source
    assert "manage" + "-secret" not in source
    assert "_record-run" in source
    assert "lane-usage record" in source
