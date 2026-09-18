"""Safe subprocess tests for the subfleet-claude (headless Claude lane) script."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parent.parent / "bin" / "subfleet-claude"


def _write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)
    return path


def _fixture_env(tmp_path: Path, claude_body: str, *, hook_exit: int = 0) -> tuple[dict, dict]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the task\n")
    output = tmp_path / "answer.md"
    hook_log = tmp_path / "hook.log"
    pick_log = tmp_path / "pick.log"
    config_dir = tmp_path / "claude-config"
    transcript_dir = config_dir / "projects" / "-fixture-project"
    transcript_dir.mkdir(parents=True)
    for counter in range(1, 5):
        session_id = f"00000000-0000-4000-8000-{counter:012d}"
        (transcript_dir / f"{session_id}.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "message": {"model": "claude-fable-5-1"},
                }
            )
            + "\n"
        )

    claude = _write_executable(fake_bin / "claude", claude_body)
    secret = _write_executable(fake_bin / "agent-secret", "printf 'test-token\\n'\n")
    hook = _write_executable(
        fake_bin / "subfleet",
        """if [ "${1:-}" = "_canonical-model" ]; then
  case "${2:-}" in
    fable) printf 'claude-fable-5-1\n' ;;
    opus) printf 'claude-opus-5\n' ;;
    claude-fable-5) printf 'claude-fable-5-1\n' ;;
    'claude-fable-5[1m]') printf 'claude-fable-5-1[1m]\n' ;;
    *) printf '%s\n' "${2:-}" ;;
  esac
  exit 0
fi
if [ "${1:-}" = "_record-run" ]; then
  for arg in "$@"; do
    [ "$arg" = "start" ] && { printf 'fake-run\n'; break; }
  done
  exit 0
fi
if [ "${1:-}" = "capacity" ]; then
  candidates=${CAPACITY_CANDIDATES:-${CAPACITY_PICK:-}}
  printf '{"families":{"claude":{"best":"%s"}}}\n' "${CAPACITY_PICK:-${candidates%%,*}}"
  exit 0
fi
if [ "${1:-}" = "pick" ] && [ "${2:-}" = "claude" ]; then
  {
    printf 'CALL'
    for arg in "$@"; do printf '\\t%s' "$arg"; done
    printf '\\n'
  } >> "$PICK_LOG"
  shift 2
  excluded=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --exclude) excluded+=("$2"); shift 2 ;;
      --model) shift 2 ;;
      *) shift ;;
    esac
  done
  old_ifs=$IFS
  IFS=,
  for candidate in ${CAPACITY_CANDIDATES:-${CAPACITY_PICK:-}}; do
    skip=0
    for denied in "${excluded[@]}"; do
      [ "$candidate" = "$denied" ] && skip=1
    done
    if [ "$skip" -eq 0 ] && [ -n "$candidate" ]; then
      printf '%s\n' "$candidate"
      IFS=$old_ifs
      exit 0
    fi
  done
  IFS=$old_ifs
  exit 1
fi
{
  printf 'CALL'
  for arg in "$@"; do printf '\\t%s' "$arg"; done
  printf '\\n'
} >> "$HOOK_LOG"
exit "${HOOK_EXIT:-0}"
""",
    )
    model = _write_executable(fake_bin / "claude-model", "printf 'latest: claude-fable-5-1\\n'\n")
    _write_executable(
        fake_bin / "uuidgen",
        """n=$(cat "$UUID_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\\n' "$n" > "$UUID_COUNTER"
printf '00000000-0000-4000-8000-%012d\\n' "$n"
""",
    )
    # Minimal jq surface used by subfleet-claude, kept local so these tests do not
    # depend on a system jq installation.
    jq = fake_bin / "jq"
    jq.write_text(
        """#!/usr/bin/env python3
import json
import sys

raw_args = sys.argv[1:]
if "-s" in raw_args:
    try:
        sid = raw_args[raw_args.index("--arg") + 2]
        rows = [json.loads(line) for line in open(raw_args[-1]) if line.strip()]
        assistant = [
            row
            for row in rows
            if isinstance(row, dict) and row.get("type") == "assistant"
        ]
        if (
            not rows
            or not all(isinstance(row, dict) for row in rows)
            or not assistant
            or any(row.get("sessionId") != sid for row in assistant)
            or any(
                not isinstance((row.get("message") or {}).get("model"), str)
                or not row["message"]["model"]
                for row in assistant
            )
        ):
            raise ValueError("invalid transcript")
    except Exception:
        raise SystemExit(1)
    for row in assistant:
        print(row["message"]["model"])
    raise SystemExit(0)

args = [arg for arg in sys.argv[1:] if arg != "-r"]
expr = args[0]
try:
    data = json.load(open(args[1])) if len(args) > 1 else json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
result = data.get("result") if isinstance(data, dict) else None
if expr.startswith("if type=="):
    ok = isinstance(data, dict) and data.get("is_error") is False and isinstance(result, str) and bool(result)
    print("ok" if ok else "bad")
elif expr in (".result", ".result // empty"):
    if result is not None:
        print(result)
elif expr == ".families.claude.best // empty":
    best = ((data.get("families") or {}).get("claude") or {}).get("best")
    if best is not None:
        print(best)
else:
    raise SystemExit(2)
"""
    )
    jq.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "CLAUDE_LANE_CLAUDE": str(claude),
            "CLAUDE_LANE_AGENT_SECRET": str(secret),
            "CLAUDE_LANE_SUBFLEET": str(hook),
            "CLAUDE_LANE_CLAUDE_MODEL": str(model),
            "CLAUDE_LANE_BACKOFF": "0",
            # Never let a test reach the real usage endpoint: default to the
            # legacy "dead token" answer; tests override to alive/unknown.
            "CLAUDE_LANE_AUTH_PROBE": "dead",
            "CLAUDE_LANE_MODEL_CHECK_BACKOFF": "0",
            "CLAUDE_LANE_MODEL_CHECK_RETRIES": "0",
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "HOOK_LOG": str(hook_log),
            "PICK_LOG": str(pick_log),
            "HOOK_EXIT": str(hook_exit),
            "UUID_COUNTER": str(tmp_path / "uuid-counter"),
        }
    )
    paths = {
        "fake_bin": fake_bin,
        "workdir": workdir,
        "prompt": prompt,
        "output": output,
        "hook_log": hook_log,
        "pick_log": pick_log,
    }
    return env, paths


def _run(env: dict, paths: dict, *lane_args: str) -> subprocess.CompletedProcess:
    args = list(lane_args) or ["-a", "lane@example.com"]
    return subprocess.run(
        [
            str(SCRIPT),
            *args,
            "-C",
            str(paths["workdir"]),
            "-p",
            str(paths["prompt"]),
            "-o",
            str(paths["output"]),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _hook_calls(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [line.split("\t")[1:] for line in path.read_text().splitlines()]


def _pick_calls(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [line.split("\t")[1:] for line in path.read_text().splitlines()]


def _options(call: list[str]) -> dict[str, str]:
    assert call[0] == "_record-lane-run"
    return dict(zip(call[1::2], call[2::2]))


def _captured_args(path: Path) -> list[str]:
    return [line[1:-1] for line in path.read_text().splitlines()]


# A fixed identity and no host git config (global or system), so the fixture
# commits and the runner's salvage commit-tree/push never depend on this
# machine's init.defaultBranch, commit.gpgsign, core.hooksPath, or hooks.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "subfleet-test",
    "GIT_AUTHOR_EMAIL": "subfleet-test@example.com",
    "GIT_COMMITTER_NAME": "subfleet-test",
    "GIT_COMMITTER_EMAIL": "subfleet-test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env={**os.environ, **_GIT_IDENTITY},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def _git_workdir_with_origin(tmp_path: Path, env: dict, workdir: Path) -> Path:
    """Make the fixture workdir a repo with a bare origin (main pushed) and one
    untracked file, so a salvage push has a branch to land on and something
    dirty to snapshot."""
    env.update(_GIT_IDENTITY)
    origin = tmp_path / "origin.git"
    for init_args in (
        ["init", "-q", "--bare", "-b", "main", str(origin)],
        ["init", "-q", "-b", "main", str(workdir)],
    ):
        subprocess.run(
            ["git", *init_args],
            env={**os.environ, **_GIT_IDENTITY},
            check=True,
            capture_output=True,
            timeout=30,
        )
    _git(workdir, "commit", "-q", "--allow-empty", "-m", "init")
    _git(workdir, "remote", "add", "origin", str(origin))
    _git(workdir, "push", "-q", "origin", "main")
    (workdir / "dirty.txt").write_text("wip\n")
    return origin


def _heads(repo: Path) -> dict[str, str]:
    out = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    return dict(line.split() for line in out.splitlines())


def _salvage_refs(repo: Path) -> dict[str, str]:
    out = _git(
        repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/claude-salvage"
    )
    return dict(line.split() for line in out.splitlines())


def test_read_only_research_allows_web_tools_without_mutating_tools(tmp_path):
    args_file = tmp_path / "claude-args"
    env, paths = _fixture_env(
        tmp_path,
        """{
  for arg in "$@"; do printf '<%s>\n' "$arg"; done
} > "$CLAUDE_ARGS_FILE"
printf '{"is_error":false,"result":"reviewed"}\n'
""",
    )
    hostile_config = tmp_path / "hostile-config"
    hostile_config.mkdir()
    hostile = '{"permissions":{"defaultMode":"bypassPermissions"}}\n'
    (hostile_config / "settings.json").write_text(hostile)
    (paths["workdir"] / ".claude").mkdir()
    (paths["workdir"] / ".claude" / "settings.json").write_text(hostile)
    env.update({
        "CLAUDE_CONFIG_DIR": str(hostile_config),
        "CLAUDE_ARGS_FILE": str(args_file),
    })

    result = _run(env, paths, "-a", "lane@example.com", "-s", "read-only")

    assert result.returncode == 0, result.stderr
    args = _captured_args(args_file)
    assert args[args.index("--permission-mode") + 1] == "plan"
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep,WebSearch,WebFetch"
    assert args[args.index("--allowedTools") + 1] == "Read,Glob,Grep,WebSearch,WebFetch"
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert {
        "--safe-mode",
        "--no-chrome",
        "--strict-mcp-config",
        "--disable-slash-commands",
    }.issubset(args)
    assert "--dangerously-skip-permissions" not in args
    assert "--add-dir" not in args


def test_workspace_write_keeps_legacy_permission_flag(tmp_path):
    args_file = tmp_path / "claude-args"
    env, paths = _fixture_env(
        tmp_path,
        """{
  for arg in "$@"; do printf '<%s>\n' "$arg"; done
} > "$CLAUDE_ARGS_FILE"
printf '{"is_error":false,"result":"built"}\n'
""",
    )
    env["CLAUDE_ARGS_FILE"] = str(args_file)

    result = _run(env, paths)

    assert result.returncode == 0, result.stderr
    args = _captured_args(args_file)
    assert "--dangerously-skip-permissions" in args
    assert "--permission-mode" not in args
    assert "--safe-mode" not in args


def test_isolated_review_requires_read_only_and_review_root(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"must not run\"}\\n'\n",
    )

    writable = _run(env, paths, "-a", "lane@example.com", "-I")
    missing_root = _run(
        env, paths, "-a", "lane@example.com", "-s", "read-only", "-I"
    )
    not_a_directory = tmp_path / "source.txt"
    not_a_directory.write_text("not a directory\n")
    invalid_root = _run(
        env,
        paths,
        "-a",
        "lane@example.com",
        "-s",
        "read-only",
        "-I",
        "-D",
        str(not_a_directory),
    )
    unpaired_root = _run(
        env, paths, "-a", "lane@example.com", "-D", str(paths["workdir"])
    )

    assert writable.returncode == 2
    assert "-I requires -s read-only" in writable.stderr
    assert missing_root.returncode == 2
    assert "-I requires -D <review_root>" in missing_root.stderr
    assert invalid_root.returncode == 2
    assert "review root not found" in invalid_root.stderr
    assert unpaired_root.returncode == 2
    assert "-D requires -I" in unpaired_root.stderr
    assert _hook_calls(paths["hook_log"]) == []


SALVAGE_BRANCH_REFUSAL = (
    "subfleet claude: refusing -b main|master — the salvage push force-pushes "
    "WIP onto that branch; use a salvage branch name (e.g. -b claude-salvage/<lane>)"
)


@pytest.mark.parametrize(
    "extra_args",
    [[], ["-d"], ["-A"], ["-s", "read-only", "-I", "-D", "{workdir}"]],
)
@pytest.mark.parametrize("bad_branch", ["main", "master"])
def test_b_main_and_master_refused_before_anything(tmp_path, bad_branch, extra_args):
    """Mirror of the Codex runner's refusal: the salvage trap force-pushes to
    -b on EVERY exit, so main/master must be rejected before token access, the
    ledger record, the detached re-exec, and the trap itself."""
    env, paths = _fixture_env(
        tmp_path,
        ': > "$CLAUDE_RAN"\nprintf \'{"is_error":false,"result":"must not run"}\\n\'\n',
    )
    claude_ran = tmp_path / "claude-ran"
    env["CLAUDE_RAN"] = str(claude_ran)
    token_access = tmp_path / "token-access"
    env["TOKEN_ACCESS"] = str(token_access)
    env["CLAUDE_LANE_AGENT_SECRET"] = str(_write_executable(
        paths["fake_bin"] / "tracked-agent-secret",
        ': > "$TOKEN_ACCESS"\nprintf "test-token\\n"\n',
    ))
    # Every subfleet CLI call (ledger _record-run, _canonical-model, pick, the
    # accounting hook) goes through this wrapper, so an empty log means none ran.
    subfleet_calls = tmp_path / "subfleet-calls.log"
    env["SUBFLEET_CALLS"] = str(subfleet_calls)
    env["FIXTURE_SUBFLEET"] = env["CLAUDE_LANE_SUBFLEET"]
    env["CLAUDE_LANE_SUBFLEET"] = str(_write_executable(
        paths["fake_bin"] / "tracked-subfleet",
        'printf \'%s\\n\' "$*" >> "$SUBFLEET_CALLS"\nexec "$FIXTURE_SUBFLEET" "$@"\n',
    ))
    env["CAPACITY_CANDIDATES"] = "lane@example.com"
    origin = _git_workdir_with_origin(tmp_path, env, paths["workdir"])
    origin_before = _heads(origin)

    extra = [arg.replace("{workdir}", str(paths["workdir"])) for arg in extra_args]
    result = _run(env, paths, "-a", "lane@example.com", "-b", bad_branch, *extra)

    assert result.returncode == 2, result.stderr
    assert SALVAGE_BRANCH_REFUSAL in result.stderr
    assert "detached pid=" not in result.stdout, "must refuse before the detached re-exec"
    assert "salvaged dirty state" not in result.stderr
    assert "subfleet claude: pushed" not in result.stderr
    assert not claude_ran.exists(), "claude must not run"
    assert not token_access.exists(), "the lane token must not be read"
    assert not subfleet_calls.exists(), "no ledger record, pick, or accounting call"
    assert not paths["output"].exists()
    assert _salvage_refs(paths["workdir"]) == {}, "the salvage trap must not have armed"
    assert _heads(origin) == origin_before, "nothing may reach the remote"


def test_salvage_branch_names_still_work(tmp_path):
    """The refusal is exact: any other -b name still gets the on-exit salvage
    push, and main on the remote is left alone."""
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"finished\"}\\n'\n",
    )
    origin = _git_workdir_with_origin(tmp_path, env, paths["workdir"])
    origin_before = _heads(origin)

    result = _run(env, paths, "-a", "lane@example.com", "-b", "claude-salvage/lane")

    assert result.returncode == 0, result.stderr
    assert paths["output"].read_text().strip() == "finished"
    assert "subfleet claude: pushed" in result.stderr
    salvage = _salvage_refs(paths["workdir"])
    assert len(salvage) == 1, salvage
    (salvage_sha,) = salvage.values()
    heads = _heads(origin)
    assert heads.pop("refs/heads/claude-salvage/lane") == salvage_sha
    assert heads == origin_before


def test_isolated_review_keeps_source_only_tools_and_validated_review_root(tmp_path):
    args_file = tmp_path / "claude-args"
    review_root = tmp_path / "source"
    review_root.mkdir()
    env, paths = _fixture_env(
        tmp_path,
        """{
  for arg in "$@"; do printf '<%s>\n' "$arg"; done
} > "$CLAUDE_ARGS_FILE"
printf '{"is_error":false,"result":"reviewed"}\n'
""",
    )
    env["CLAUDE_ARGS_FILE"] = str(args_file)

    result = _run(
        env,
        paths,
        "-a",
        "lane@example.com",
        "-s",
        "read-only",
        "-I",
        "-D",
        str(review_root),
    )

    assert result.returncode == 0, result.stderr
    args = _captured_args(args_file)
    assert args[args.index("--add-dir") + 1] == str(review_root.resolve())
    assert args[args.index("--permission-mode") + 1] == "plan"
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    assert args[args.index("--allowedTools") + 1] == "Read,Glob,Grep"


def test_isolated_review_overrides_inherited_memory_loading_env(tmp_path):
    child_env_file = tmp_path / "isolation-env"
    review_root = tmp_path / "source"
    review_root.mkdir()
    (review_root / "CLAUDE.md").write_text("Untrusted checkout instructions.\n")
    env, paths = _fixture_env(
        tmp_path,
        """for name in $ISOLATION_ENV_KEYS; do
  printf '%s=%s\\n' "$name" "${!name-<unset>}"
done > "$ISOLATION_ENV_FILE"
printf '{"is_error":false,"result":"reviewed"}\\n'
""",
    )
    forced = {
        "CLAUDE_CODE_SAFE_MODE": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_ORG_MEMORY": "1",
    }
    removed = [
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
        "CLAUDE_MEMORY_STORES",
        "CLAUDE_CODE_REMOTE_MEMORY_DIR",
        "CLAUDE_COWORK_MEMORY_GUIDELINES",
        "CLAUDE_COWORK_MEMORY_EXTRA_GUIDELINES",
        "CLAUDE_COWORK_MEMORY_INDEX_CONTENT",
        "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
    ]
    env.update({key: "0" for key in forced})
    env.update({key: "untrusted-parent-value" for key in removed})
    env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] = "1"
    env["ISOLATION_ENV_KEYS"] = " ".join([*forced, *removed])
    env["ISOLATION_ENV_FILE"] = str(child_env_file)

    result = _run(
        env,
        paths,
        "-a",
        "lane@example.com",
        "-s",
        "read-only",
        "-I",
        "-D",
        str(review_root),
    )

    assert result.returncode == 0, result.stderr
    actual = dict(line.split("=", 1) for line in child_env_file.read_text().splitlines())
    assert actual == {**forced, **{key: "<unset>" for key in removed}}


@pytest.mark.parametrize(
    "policy_override",
    [
        "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
        "CLAUDE_CODE_REMOTE_SETTINGS_PATH",
        "CLAUDE_CODE_MOCK_REMOTE_SETTINGS",
    ],
)
def test_isolated_review_refuses_inherited_policy_override(tmp_path, policy_override):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"must not run\"}\\n'\n",
    )
    env[policy_override] = str(paths["workdir"] / "untrusted-policy.json")

    result = _run(
        env,
        paths,
        "-a",
        "lane@example.com",
        "-s",
        "read-only",
        "-I",
        "-D",
        str(paths["workdir"]),
    )

    assert result.returncode == 2
    assert f"isolated review refuses inherited {policy_override}" in result.stderr
    assert _hook_calls(paths["hook_log"]) == []
    assert not paths["output"].exists()


def test_success_hook_has_exact_context_and_failure_is_ignored(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"finished\"}\\n'\nexit 0\n",
        hook_exit=23,
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert paths["output"].read_text().strip() == "finished"
    calls = _hook_calls(paths["hook_log"])
    assert len(calls) == 1
    opts = _options(calls[0])
    assert opts == {
        "--email": "lane@example.com",
        "--session-id": "00000000-0000-4000-8000-000000000001",
        "--rc": "0",
        "--workdir": str(paths["workdir"]),
        "--err-file": str(tmp_path / "answer.err.log"),
        "--raw-file": str(tmp_path / "answer.result.json"),
        "--model": "claude-fable-5-1",
    }


def test_hard_limit_records_before_auto_repick(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        """n=$(cat "$CLAUDE_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\\n' "$n" > "$CLAUDE_COUNTER"
if [ "$n" -eq 1 ]; then
  cat <<'JSON'
{"is_error":true,"result":"You've hit your limit"}
JSON
  exit 9
fi
printf '{"is_error":false,"result":"second lane worked"}\\n'
""",
    )
    env["CLAUDE_COUNTER"] = str(tmp_path / "claude-counter")
    pick = _write_executable(
        paths["fake_bin"] / "claude-pick",
        """n=$(cat "$PICK_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\\n' "$n" > "$PICK_COUNTER"
if [ "$n" -eq 1 ]; then printf 'first@example.com\\n'; else printf 'second@example.com\\n'; fi
""",
    )
    env["CLAUDE_LANE_PICK"] = str(pick)
    env["PICK_COUNTER"] = str(tmp_path / "pick-counter")

    result = _run(env, paths, "-A")

    assert result.returncode == 0
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert [(call["--email"], call["--rc"]) for call in calls] == [
        ("first@example.com", "4"),
        ("second@example.com", "0"),
    ]
    assert [call["--model"] for call in calls] == [
        "claude-fable-5-1", "claude-fable-5-1",
    ]
    assert calls[0]["--session-id"] != calls[1]["--session-id"]


def test_auto_pick_uses_model_scoped_picker(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"capacity lane worked\"}\\n'\nexit 0\n",
    )
    env["CAPACITY_PICK"] = "capacity-best@example.com"

    result = _run(env, paths, "-A", "-m", "claude-opus-5")

    assert result.returncode == 0
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert len(calls) == 1
    assert calls[0]["--email"] == "capacity-best@example.com"
    assert calls[0]["--rc"] == "0"
    assert calls[0]["--model"] == "claude-opus-5"
    assert _pick_calls(paths["pick_log"]) == [[
        "pick", "claude", "--model", "claude-opus-5",
    ]]


def test_auto_repick_survives_failed_accounting_hook(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        """if [ "$CLAUDE_CODE_OAUTH_TOKEN" = "test-token" ] && [ ! -e "$FIRST_USED" ]; then
  : > "$FIRST_USED"
  printf '{"is_error":true,"result":"session limit; resets 6:40pm"}\n'
  exit 9
fi
printf '{"is_error":false,"result":"fallback lane worked"}\n'
""",
        hook_exit=23,
    )
    env["FIRST_USED"] = str(tmp_path / "first-used")
    env["CAPACITY_CANDIDATES"] = "first@example.com,second@example.com"

    result = _run(env, paths, "-A")

    assert result.returncode == 0
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert [(call["--email"], call["--rc"]) for call in calls] == [
        ("first@example.com", "4"),
        ("second@example.com", "0"),
    ]
    assert _pick_calls(paths["pick_log"]) == [
        ["pick", "claude", "--model", "claude-fable-5-1"],
        [
            "pick", "claude", "--model", "claude-fable-5-1",
            "--exclude", "first@example.com",
        ],
    ]


def test_auto_repick_stops_when_every_candidate_is_excluded(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":true,\"result\":\"weekly limit reached\"}\\n'\nexit 9\n",
        hook_exit=23,
    )
    env["CAPACITY_CANDIDATES"] = "only@example.com"

    result = _run(env, paths, "-A")

    assert result.returncode == 4
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert [(call["--email"], call["--rc"]) for call in calls] == [
        ("only@example.com", "4"),
    ]
    assert "no alternate lane available" in result.stderr


def test_auto_pick_skips_lane_with_missing_keychain_token(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        """printf '{"is_error":false,"result":"token fallback worked"}\n'
""",
    )
    env["CAPACITY_CANDIDATES"] = "missing@example.com,usable@example.com"
    env["CLAUDE_LANE_AGENT_SECRET"] = str(_write_executable(
        paths["fake_bin"] / "selective-agent-secret",
        """case "${2:-}" in
  claude-quota-missing@example.com) exit 1 ;;
  claude-quota-usable@example.com) printf 'usable-token\n' ;;
  *) exit 1 ;;
esac
""",
    ))

    result = _run(env, paths, "-A")

    assert result.returncode == 0
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert len(calls) == 1
    assert calls[0]["--email"] == "usable@example.com"
    assert "no keychain token for missing@example.com" in result.stderr


def test_pinned_missing_keychain_token_is_auth_failure(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"must not run\"}\\n'\n",
    )
    env["CLAUDE_LANE_AGENT_SECRET"] = str(_write_executable(
        paths["fake_bin"] / "missing-agent-secret", "exit 1\n"
    ))

    result = _run(env, paths)

    assert result.returncode == 5
    assert "no keychain token" in result.stderr
    assert _hook_calls(paths["hook_log"]) == []


def test_pinned_first_lane_with_auto_repick(tmp_path):
    """`-a lane -A`: start on the named lane, re-pick only on a hard limit.
    This is how `subfleet run` launches detached Claude work: its own capacity
    pick for the first attempt, the runner's re-pick if that lane is limited."""
    env, paths = _fixture_env(
        tmp_path,
        """n=$(cat "$CLAUDE_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\\n' "$n" > "$CLAUDE_COUNTER"
if [ "$n" -eq 1 ]; then
  printf '{"is_error":true,"result":"weekly limit reached; resets 6:40pm"}\\n'
  exit 9
fi
printf '{"is_error":false,"result":"re-picked lane worked"}\\n'
""",
    )
    env["CLAUDE_COUNTER"] = str(tmp_path / "claude-counter")
    env["CAPACITY_CANDIDATES"] = "pinned@example.com,fallback@example.com"

    result = _run(env, paths, "-a", "pinned@example.com", "-A")

    assert result.returncode == 0, result.stderr
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert [(call["--email"], call["--rc"]) for call in calls] == [
        ("pinned@example.com", "4"),
        ("fallback@example.com", "0"),
    ]
    assert _pick_calls(paths["pick_log"]) == [[
        "pick", "claude", "--model", "claude-fable-5-1",
        "--exclude", "pinned@example.com",
    ]]
    assert paths["output"].read_text().strip() == "re-picked lane worked"


def test_org_disabled_subscription_access_is_an_auth_failure(tmp_path):
    """One org account, 2026-08-23: the org blocks Claude Code entirely.
    Classified with the dead-token outcomes so auto mode rotates past it and
    the front door parks the lane for 30 days."""
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":true,\"result\":\"Your organization has disabled Claude subscription access for Claude Code\"}\\n'\nexit 1\n",
    )
    result = _run(env, paths, "-a", "blocked@example.com")
    assert result.returncode == 5
    assert "AUTH FAILURE" in result.stderr
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert [(call["--email"], call["--rc"]) for call in calls] == [("blocked@example.com", "5")]


def test_auth_signature_with_live_token_is_transient_not_auth_failure(tmp_path):
    """2026-09-03: six lanes were parked for 30 days on an auth-looking phrase
    while their tokens still served Opus. When the usage endpoint says the
    token authenticates, the phrase is not a dead token: retry, and account
    the attempt as transient (rc 1), never rc 5."""
    env, paths = _fixture_env(
        tmp_path,
        """n=$(cat "$CLAUDE_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\\n' "$n" > "$CLAUDE_COUNTER"
if [ "$n" -eq 1 ]; then printf '401 authentication_error\\n' >&2; exit 8; fi
printf '{"is_error":false,"result":"token was fine after all"}\\n'
""",
    )
    env["CLAUDE_COUNTER"] = str(tmp_path / "claude-counter")
    env["CLAUDE_LANE_AUTH_PROBE"] = "alive"

    result = _run(env, paths)

    assert result.returncode == 0, result.stderr
    assert "still authenticates" in result.stderr
    assert "AUTH FAILURE" not in result.stderr
    # The raw child rc is accounted like any other transient death (cf. the
    # "7" in test_auth_transient_and_bad_envelope_classifications); never 5.
    assert [
        _options(call)["--rc"] for call in _hook_calls(paths["hook_log"])
    ] == ["8", "0"]
    assert paths["output"].read_text().strip() == "token was fine after all"


def test_auth_signature_with_live_token_exhausting_retries_is_not_rc5(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '401 authentication_error\\n' >&2\nexit 8\n",
    )
    env["CLAUDE_LANE_AUTH_PROBE"] = "alive"

    result = _run(env, paths)

    assert result.returncode == 8  # the child's own rc, not the reserved 5
    assert "AUTH FAILURE" not in result.stderr
    assert "FAILED rc=8" in result.stderr
    rcs = [_options(call)["--rc"] for call in _hook_calls(paths["hook_log"])]
    assert rcs and "5" not in rcs


def test_org_block_stays_auth_failure_even_with_live_token(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":true,\"result\":\"Your organization has disabled Claude subscription access for Claude Code\"}\\n'\nexit 1\n",
    )
    env["CLAUDE_LANE_AUTH_PROBE"] = "alive"

    result = _run(env, paths, "-a", "blocked@example.com")

    assert result.returncode == 5
    assert "AUTH FAILURE" in result.stderr
    assert "org-blocked" in result.stderr


def test_auth_signature_with_unknown_probe_keeps_legacy_rc5(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '401 authentication_error\\n' >&2\nexit 8\n",
    )
    env["CLAUDE_LANE_AUTH_PROBE"] = "unknown"

    result = _run(env, paths)

    assert result.returncode == 5
    assert "token probe: unknown" in result.stderr


def test_out_prefers_the_transcript_text_when_the_result_rendering_is_shorter(tmp_path):
    """2026-09-04: a captured .out lost 1,912 interior characters while keeping
    its sentinel and frame headers. The transcript holds the message verbatim:
    when it is longer than the envelope's .result, the .out is taken from the
    transcript, the rendering is kept beside it, and the lane log says so."""
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"FRAME A1 …CK@ #A2 END-OF-REPORT\"}\n'\n",
    )
    config_dir = Path(env["CLAUDE_CONFIG_DIR"])
    transcript_dir = config_dir / "projects" / "-fixture-project"
    sid = "00000000-0000-4000-8000-000000000001"
    full = "FRAME A1 the complete body that the rendering elided CK@ #A2 more body END-OF-REPORT"
    (transcript_dir / f"{sid}.jsonl").write_text(json.dumps({
        "type": "assistant", "sessionId": sid,
        "message": {"model": "claude-fable-5-1", "content": [{"type": "text", "text": full}]},
    }) + "\n")

    result = _run(env, paths)

    assert result.returncode == 0, result.stderr
    assert paths["output"].read_text() == full
    assert "OUT taken from the session transcript" in result.stderr
    kept = paths["output"].with_name(paths["output"].stem + ".result-text.md")
    assert kept.read_text().strip() == "FRAME A1 …CK@ #A2 END-OF-REPORT"


def test_pinned_lane_without_auto_fails_fast_on_limit(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":true,\"result\":\"session limit; resets 6:40pm\"}\\n'\nexit 9\n",
    )
    env["CAPACITY_CANDIDATES"] = "pinned@example.com,fallback@example.com"

    result = _run(env, paths, "-a", "pinned@example.com")

    assert result.returncode == 4
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert [(call["--email"], call["--rc"]) for call in calls] == [("pinned@example.com", "4")]


@pytest.mark.parametrize("artifact", ["output", "raw", "error", "log"])
def test_prompt_artifact_collisions_fail_before_truncation(tmp_path, artifact):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"must not run\"}\\n'\n",
    )
    output = tmp_path / "collision.md"
    derived = {
        "output": output,
        "raw": tmp_path / "collision.result.json",
        "error": tmp_path / "collision.err.log",
        "log": tmp_path / "collision.lane.log",
    }
    prompt = derived[artifact]
    prompt.write_text("sentinel prompt\n")
    args = [
        str(SCRIPT), "-a", "lane@example.com",
        "-C", str(paths["workdir"]), "-p", str(prompt), "-o", str(output),
    ]
    if artifact == "log":
        args.append("-d")

    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)

    assert result.returncode == 2
    assert "must not alias" in result.stderr
    assert prompt.read_text() == "sentinel prompt\n"
    assert _hook_calls(paths["hook_log"]) == []


def test_output_artifact_hardlink_collision_is_rejected(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"must not run\"}\\n'\n",
    )
    output = tmp_path / "collision.md"
    output.write_text("sentinel output\n")
    os.link(output, tmp_path / "collision.result.json")

    result = subprocess.run(
        [
            str(SCRIPT), "-a", "lane@example.com", "-C", str(paths["workdir"]),
            "-p", str(paths["prompt"]), "-o", str(output),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 2
    assert "must not alias" in result.stderr
    assert output.read_text() == "sentinel output\n"
    assert _hook_calls(paths["hook_log"]) == []


def test_auth_transient_and_bad_envelope_classifications(tmp_path):
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    auth_env, auth_paths = _fixture_env(
        auth_dir,
        "printf '401 authentication_error\\n' >&2\nexit 8\n",
    )
    auth = _run(auth_env, auth_paths)
    assert auth.returncode == 5
    assert _options(_hook_calls(auth_paths["hook_log"])[0])["--rc"] == "5"

    transient_dir = tmp_path / "transient"
    transient_dir.mkdir()
    transient_env, transient_paths = _fixture_env(
        transient_dir,
        """n=$(cat "$CLAUDE_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\\n' "$n" > "$CLAUDE_COUNTER"
if [ "$n" -eq 1 ]; then printf 'overloaded\\n' >&2; exit 7; fi
printf '{"is_error":false,"result":"recovered"}\\n'
""",
    )
    transient_env["CLAUDE_COUNTER"] = str(transient_dir / "claude-counter")
    transient = _run(transient_env, transient_paths)
    assert transient.returncode == 0
    assert [
        _options(call)["--rc"] for call in _hook_calls(transient_paths["hook_log"])
    ] == ["7", "0"]

    bad_dir = tmp_path / "bad-envelope"
    bad_dir.mkdir()
    bad_env, bad_paths = _fixture_env(
        bad_dir,
        "printf '{\"is_error\":true,\"result\":\"\"}\\n'\nexit 0\n",
    )
    bad = _run(bad_env, bad_paths)
    assert bad.returncode == 1
    assert _options(_hook_calls(bad_paths["hook_log"])[0])["--rc"] == "1"


def test_rate_limit_reached_is_transient_not_capacity_calibration(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        """n=$(cat "$CLAUDE_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\n' "$n" > "$CLAUDE_COUNTER"
if [ "$n" -eq 1 ]; then printf '429 rate limit reached\n' >&2; exit 9; fi
printf '{"is_error":false,"result":"recovered"}\n'
""",
    )
    env["CLAUDE_COUNTER"] = str(tmp_path / "claude-counter")

    result = _run(env, paths)

    assert result.returncode == 0
    assert [_options(call)["--rc"] for call in _hook_calls(paths["hook_log"])] == ["9", "0"]


def test_unclassified_raw_4_and_5_do_not_create_reserved_outcomes(tmp_path):
    transient_dir = tmp_path / "raw-four"
    transient_dir.mkdir()
    transient_env, transient_paths = _fixture_env(
        transient_dir,
        """n=$(cat "$CLAUDE_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\n' "$n" > "$CLAUDE_COUNTER"
if [ "$n" -eq 1 ]; then printf 'overloaded\n' >&2; exit 4; fi
printf '{"is_error":false,"result":"recovered"}\n'
""",
    )
    transient_env["CLAUDE_COUNTER"] = str(transient_dir / "claude-counter")
    transient = _run(transient_env, transient_paths)
    assert transient.returncode == 0
    assert [
        _options(call)["--rc"] for call in _hook_calls(transient_paths["hook_log"])
    ] == ["1", "0"]

    generic_dir = tmp_path / "raw-five"
    generic_dir.mkdir()
    generic_env, generic_paths = _fixture_env(
        generic_dir,
        "printf 'unclassified failure\n' >&2\nexit 5\n",
    )
    generic = _run(generic_env, generic_paths)
    assert generic.returncode == 1
    assert _options(_hook_calls(generic_paths["hook_log"])[0])["--rc"] == "1"


def test_verified_rerun_clears_stale_downgrade_marker(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"finished\"}\\n'\n",
    )
    model = Path(env["CLAUDE_LANE_CLAUDE_MODEL"])
    _write_executable(model, "printf 'latest: claude-opus-4-1\\n'\n")

    first = _run(env, paths)

    marker = tmp_path / "answer.DOWNGRADED"
    assert first.returncode == 0
    assert marker.exists()
    _write_executable(model, "printf 'latest: claude-fable-5-1\\n'\n")

    second = _run(env, paths)

    assert second.returncode == 0
    assert not marker.exists()
    assert (tmp_path / "answer.MODEL_ATTESTED").exists()


@pytest.mark.parametrize(
    ("requested", "served"),
    [
        ("claude-fable-5-1", "claude-fable-5-1"),
        ("sonnet", "claude-sonnet-4-6"),
        ("claude-fable-5-1", "claude-fable-5-1-20260829"),
    ],
)
def test_matching_served_model_writes_positive_attestation(tmp_path, requested, served):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"finished\"}\\n'\n",
    )

    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]), f"printf 'latest: {served}\\n'\n"
    )
    session_id = "00000000-0000-4000-8000-000000000001"
    transcript = (
        Path(env["CLAUDE_CONFIG_DIR"])
        / "projects"
        / "-fixture-project"
        / f"{session_id}.jsonl"
    )
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "message": {"model": served},
            }
        )
        + "\n"
    )

    result = _run(env, paths, "-a", "lane@example.com", "-m", requested)

    marker = tmp_path / "answer.MODEL_ATTESTED"
    assert result.returncode == 0
    assert marker.read_text().splitlines() == [
        f"requested: {requested}",
        f"served: {served}",
        "session: 00000000-0000-4000-8000-000000000001",
    ]
    assert not (tmp_path / "answer.DOWNGRADED").exists()


def test_model_checker_receives_unique_exact_session_transcript_path(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    config_dir = tmp_path / "exact-claude-config"
    transcript = (
        config_dir
        / "projects"
        / "-private-tmp-review-with-hyphen-normalized-cwd"
        / "00000000-0000-4000-8000-000000000001.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"assistant","sessionId":"00000000-0000-4000-8000-000000000001","message":{"model":"claude-fable-5-1"}}\n'
    )
    args_file = tmp_path / "model-args"
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["MODEL_ARGS_FILE"] = str(args_file)
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """printf '%s\n' "$@" > "$MODEL_ARGS_FILE"
printf 'latest: claude-fable-5-1\n'
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert args_file.read_text().splitlines() == ["-n", "10", str(transcript)]
    assert (tmp_path / "answer.MODEL_ATTESTED").exists()


def test_ambiguous_exact_session_transcripts_fail_closed(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    config_dir = tmp_path / "claude-config"
    session_name = "00000000-0000-4000-8000-000000000001.jsonl"
    for project_name in ("-project-one", "-project-two"):
        transcript = config_dir / "projects" / project_name / session_name
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            '{"type":"assistant","message":{"model":"claude-fable-5-1"}}\n'
        )
    called = tmp_path / "model-called"
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["MODEL_CALLED"] = str(called)
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """touch "$MODEL_CALLED"
printf 'latest: claude-fable-5-1\n'
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert not called.exists()
    assert not (tmp_path / "answer.MODEL_ATTESTED").exists()
    assert not (tmp_path / "answer.DOWNGRADED").exists()
    assert "ambiguous session transcript after 1 checks" in result.stderr


def test_missing_exact_session_transcript_never_falls_back_to_checker(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    empty_config = tmp_path / "empty-claude-config"
    (empty_config / "projects").mkdir(parents=True)
    called = tmp_path / "model-called"
    env["CLAUDE_CONFIG_DIR"] = str(empty_config)
    env["MODEL_CALLED"] = str(called)
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """touch "$MODEL_CALLED"
printf 'latest: claude-fable-5-1\n'
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert not called.exists()
    assert not (tmp_path / "answer.MODEL_ATTESTED").exists()
    assert not (tmp_path / "answer.DOWNGRADED").exists()
    assert "session transcript not found after 1 checks" in result.stderr


def test_incomplete_session_transcript_scan_never_proves_uniqueness(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    transcript = (
        Path(env["CLAUDE_CONFIG_DIR"])
        / "projects"
        / "-fixture-project"
        / "00000000-0000-4000-8000-000000000001.jsonl"
    )
    called = tmp_path / "model-called"
    env["TRANSCRIPT_TO_PRINT"] = str(transcript)
    env["MODEL_CALLED"] = str(called)
    _write_executable(
        paths["fake_bin"] / "find",
        """printf '%s\\0' "$TRANSCRIPT_TO_PRINT"
exit 2
""",
    )
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """touch "$MODEL_CALLED"
printf 'latest: claude-fable-5-1\n'
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert not called.exists()
    assert not (tmp_path / "answer.MODEL_ATTESTED").exists()
    assert not (tmp_path / "answer.DOWNGRADED").exists()
    assert "session transcript scan failed after 1 checks" in result.stderr


def test_malformed_session_transcript_never_attests_an_earlier_model(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    transcript = (
        Path(env["CLAUDE_CONFIG_DIR"])
        / "projects"
        / "-fixture-project"
        / "00000000-0000-4000-8000-000000000001.jsonl"
    )
    with transcript.open("a") as stream:
        stream.write('{"type":"assistant"')
    called = tmp_path / "model-called"
    env["MODEL_CALLED"] = str(called)
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """touch "$MODEL_CALLED"
printf 'latest: claude-fable-5-1\n'
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert not called.exists()
    assert not (tmp_path / "answer.MODEL_ATTESTED").exists()
    assert not (tmp_path / "answer.DOWNGRADED").exists()
    assert "session transcript incomplete or invalid after 1 checks" in result.stderr


def test_any_mixed_session_model_records_a_downgrade(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    session_id = "00000000-0000-4000-8000-000000000001"
    transcript = (
        Path(env["CLAUDE_CONFIG_DIR"])
        / "projects"
        / "-fixture-project"
        / f"{session_id}.jsonl"
    )
    transcript.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "message": {"model": model},
                }
            )
            for model in ("claude-opus-5", "claude-fable-5-1")
        )
        + "\n"
    )
    called = tmp_path / "model-called"
    env["MODEL_CALLED"] = str(called)
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """touch "$MODEL_CALLED"
printf 'latest: claude-fable-5-1\n'
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert not called.exists()
    assert not (tmp_path / "answer.MODEL_ATTESTED").exists()
    downgraded = tmp_path / "answer.DOWNGRADED"
    assert downgraded.exists()
    assert "served claude-opus-5, requested claude-fable-5-1" in downgraded.read_text()


def test_model_checker_retries_after_transient_checker_failure(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    counter = tmp_path / "model-counter"
    env["MODEL_COUNTER"] = str(counter)
    env["CLAUDE_LANE_MODEL_CHECK_RETRIES"] = "2"
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """n=$(cat "$MODEL_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\n' "$n" > "$MODEL_COUNTER"
if [ "$n" -lt 3 ]; then
  printf 'transcript not ready\n' >&2
  exit 2
fi
printf 'latest: claude-fable-5-1\n'
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert counter.read_text().strip() == "3"
    assert (tmp_path / "answer.MODEL_ATTESTED").read_text().splitlines() == [
        "requested: claude-fable-5-1",
        "served: claude-fable-5-1",
        "session: 00000000-0000-4000-8000-000000000001",
    ]
    assert not (tmp_path / "answer.DOWNGRADED").exists()


def test_model_checker_retries_until_exact_session_transcript_file_appears(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    transcript = (
        Path(env["CLAUDE_CONFIG_DIR"])
        / "projects"
        / "-fixture-project"
        / "00000000-0000-4000-8000-000000000001.jsonl"
    )
    transcript.unlink()
    env["CLAUDE_LANE_MODEL_CHECK_RETRIES"] = "1"
    env["CLAUDE_LANE_MODEL_CHECK_BACKOFF"] = "1"
    env["TRANSCRIPT_TO_CREATE"] = str(transcript)
    _write_executable(
        paths["fake_bin"] / "sleep",
        """printf '%s\n' '{"type":"assistant","sessionId":"00000000-0000-4000-8000-000000000001","message":{"model":"claude-fable-5-1"}}' > "$TRANSCRIPT_TO_CREATE"
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert transcript.exists()
    assert (tmp_path / "answer.MODEL_ATTESTED").exists()
    assert "served-model check inconclusive" not in result.stderr


def test_model_checker_retries_after_success_without_a_latest_model(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    counter = tmp_path / "model-counter"
    env["MODEL_COUNTER"] = str(counter)
    env["CLAUDE_LANE_MODEL_CHECK_RETRIES"] = "1"
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """n=$(cat "$MODEL_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\n' "$n" > "$MODEL_COUNTER"
if [ "$n" -eq 1 ]; then
  printf 'no assistant turns yet\n'
  exit 0
fi
printf 'latest: claude-fable-5-1\n'
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert counter.read_text().strip() == "2"
    assert (tmp_path / "answer.MODEL_ATTESTED").exists()
    assert "served-model check inconclusive" not in result.stderr


def test_model_checker_exhausts_bounded_retries_and_fails_closed(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    counter = tmp_path / "model-counter"
    env["MODEL_COUNTER"] = str(counter)
    env["CLAUDE_LANE_MODEL_CHECK_RETRIES"] = "2"
    (tmp_path / "answer.MODEL_ATTESTED").write_text("stale attestation\n")
    (tmp_path / "answer.DOWNGRADED").write_text("stale downgrade\n")
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """n=$(cat "$MODEL_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\n' "$n" > "$MODEL_COUNTER"
printf 'transcript not ready\n' >&2
exit 2
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert counter.read_text().strip() == "3"
    assert not (tmp_path / "answer.MODEL_ATTESTED").exists()
    assert not (tmp_path / "answer.DOWNGRADED").exists()
    assert result.stderr.count("served-model check inconclusive") == 1
    assert "checker failed after 3 checks" in result.stderr


def test_model_checker_retry_still_records_a_downgrade(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    counter = tmp_path / "model-counter"
    env["MODEL_COUNTER"] = str(counter)
    env["CLAUDE_LANE_MODEL_CHECK_RETRIES"] = "1"
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        """n=$(cat "$MODEL_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\n' "$n" > "$MODEL_COUNTER"
if [ "$n" -eq 1 ]; then exit 2; fi
printf 'latest: claude-opus-5\n'
""",
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert counter.read_text().strip() == "2"
    assert not (tmp_path / "answer.MODEL_ATTESTED").exists()
    downgraded = tmp_path / "answer.DOWNGRADED"
    assert downgraded.exists()
    assert "served claude-opus-5, requested claude-fable-5-1" in downgraded.read_text()

@pytest.mark.parametrize(
    "report",
    [
        "printf 'no transcript found\\n'\n",
        "printf 'latest: claude-fable-5-1\\n'\nexit 2\n",
    ],
)
def test_inconclusive_served_model_clears_stale_attestation(tmp_path, report):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"finished\"}\\n'\n",
    )
    marker = tmp_path / "answer.MODEL_ATTESTED"
    marker.write_text("stale attestation\n")
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        report,
    )

    result = _run(env, paths)

    assert result.returncode == 0
    assert not marker.exists()
    assert not (tmp_path / "answer.DOWNGRADED").exists()
    assert "served-model check inconclusive" in result.stderr


def test_missing_model_checker_leaves_positive_attestation_absent(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"finished\"}\\n'\n",
    )
    marker = tmp_path / "answer.MODEL_ATTESTED"
    marker.write_text("stale attestation\n")
    Path(env["CLAUDE_LANE_CLAUDE_MODEL"]).unlink()
    env["PATH"] = f"{paths['fake_bin']}:/usr/bin:/bin:/usr/sbin:/sbin"

    result = _run(env, paths)

    assert result.returncode == 0
    assert not marker.exists()
    assert not (tmp_path / "answer.DOWNGRADED").exists()
    assert "claude-model not found" in result.stderr


def test_mismatched_served_model_writes_only_downgrade_marker(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"finished\"}\\n'\n",
    )
    attested = tmp_path / "answer.MODEL_ATTESTED"
    attested.write_text("stale attestation\n")
    _write_executable(
        Path(env["CLAUDE_LANE_CLAUDE_MODEL"]),
        "printf 'latest: claude-opus-5\\n'\n",
    )

    result = _run(env, paths, "-a", "lane@example.com", "-m", "claude-fable-5-1")

    downgraded = tmp_path / "answer.DOWNGRADED"
    assert result.returncode == 0
    assert not attested.exists()
    assert downgraded.exists()
    assert "served claude-opus-5, requested claude-fable-5-1" in downgraded.read_text()


def test_detached_child_is_the_instrumented_script(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        """IFS= read -r prompt
printf '{"is_error":false,"result":"detached read: %s"}\n' "$prompt"
exit 0
""",
    )
    env["CLAUDE_LANE_DETACHED_START_DELAY"] = "0.25"
    private_tmp = tmp_path / "private-prompts"
    private_tmp.mkdir()
    env["CLAUDE_LANE_TMPDIR"] = str(private_tmp)

    parent = _run(env, paths, "-a", "lane@example.com", "-d")

    assert parent.returncode == 0
    assert "detached pid=" in parent.stdout
    paths["prompt"].unlink()
    deadline = time.monotonic() + 20  # generous: suites run under heavy lane load
    while time.monotonic() < deadline and (
        not _hook_calls(paths["hook_log"])
        or list(private_tmp.glob("subfleet-claude-prompt.*"))
    ):
        time.sleep(0.05)
    calls = _hook_calls(paths["hook_log"])
    assert len(calls) == 1
    assert _options(calls[0])["--rc"] == "0"
    assert paths["output"].read_text().strip() == "detached read: do the task"
    assert list(private_tmp.glob("subfleet-claude-prompt.*")) == []


def test_detached_child_scrubs_lane_ownership_env(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        """env > "$CHILD_ENV_FILE"
printf '{"is_error":false,"result":"finished"}\n'
""",
    )
    child_env = tmp_path / "child.env"
    env["CHILD_ENV_FILE"] = str(child_env)
    private_tmp = tmp_path / "private-prompts"
    private_tmp.mkdir()
    env["CLAUDE_LANE_TMPDIR"] = str(private_tmp)

    parent = _run(env, paths, "-a", "lane@example.com", "-d")

    assert parent.returncode == 0
    deadline = time.monotonic() + 20  # generous: suites run under heavy lane load
    while time.monotonic() < deadline and (
        not child_env.exists()
        or not paths["output"].exists()
        or list(private_tmp.glob("subfleet-claude-prompt.*"))
    ):
        time.sleep(0.05)
    child_keys = {line.partition("=")[0] for line in child_env.read_text().splitlines()}
    assert "CLAUDE_LANE_DETACHED" not in child_keys
    assert "CLAUDE_LANE_OWNED_PROMPT" not in child_keys
    assert paths["output"].read_text().strip() == "finished"
    assert list(private_tmp.glob("subfleet-claude-prompt.*")) == []


def test_detached_prompt_is_excluded_from_dirty_worktree_salvage(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        """IFS= read -r prompt
printf '{"is_error":false,"result":"salvaged: %s"}\n' "$prompt"
""",
    )
    workdir = paths["workdir"]
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, check=True)
    (workdir / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workdir, check=True)
    (workdir / "dirty.txt").write_text("keep me\n")
    # Deliberately put the private copy in the repository. The child's EXIT
    # trap must remove it before the dirty-tree snapshot is created.
    env["CLAUDE_LANE_TMPDIR"] = str(workdir)
    env["CLAUDE_LANE_DETACHED_START_DELAY"] = "0.25"

    parent = _run(env, paths, "-a", "lane@example.com", "-d")

    assert parent.returncode == 0
    paths["prompt"].unlink()
    deadline = time.monotonic() + 20  # generous: suites run under heavy lane load
    refs: list[str] = []
    while time.monotonic() < deadline:
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/claude-salvage"],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if refs and not list(workdir.glob("subfleet-claude-prompt.*")):
            break
        time.sleep(0.05)
    assert len(refs) == 1
    tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", refs[0]],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "dirty.txt" in tree
    assert not any("subfleet-claude-prompt" in path for path in tree)
    assert list(workdir.glob("subfleet-claude-prompt.*")) == []


def test_detached_prompt_cleanup_failure_skips_salvage(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        """owned=''
for candidate in "$CLAUDE_LANE_TMPDIR"/subfleet-claude-prompt.*; do
  [ -f "$candidate" ] || continue
  owned=$candidate
  break
done
[ -n "$owned" ] || exit 97
rm -f "$owned"
mkdir "$owned"
printf 'private material\n' > "$owned/secret.txt"
printf '{"is_error":false,"result":"finished"}\n'
""",
    )
    workdir = paths["workdir"]
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, check=True)
    (workdir / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workdir, check=True)
    (workdir / "dirty.txt").write_text("keep me\n")
    env["CLAUDE_LANE_TMPDIR"] = str(workdir)
    env["CLAUDE_LANE_DETACHED_START_DELAY"] = "0.1"

    parent = _run(env, paths, "-a", "lane@example.com", "-d")

    assert parent.returncode == 0
    lane_log = tmp_path / "answer.lane.log"
    deadline = time.monotonic() + 20  # generous: suites run under heavy lane load
    while time.monotonic() < deadline and (
        not lane_log.exists() or "skipping salvage" not in lane_log.read_text()
    ):
        time.sleep(0.05)
    assert "skipping salvage and push" in lane_log.read_text()
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/claude-salvage"],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert refs == []


def test_inherited_prompt_directory_does_not_skip_salvage(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":false,\"result\":\"finished\"}\\n'\n",
    )
    workdir = paths["workdir"]
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, check=True)
    (workdir / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workdir, check=True)
    (workdir / "dirty.txt").write_text("keep me\n")
    inherited = tmp_path / "subfleet-claude-prompt.outer"
    inherited.mkdir()
    (inherited / "secret.txt").write_text("private material\n")
    env["CLAUDE_LANE_TMPDIR"] = str(tmp_path)
    env["CLAUDE_LANE_DETACHED"] = "1"
    env["CLAUDE_LANE_OWNED_PROMPT"] = str(inherited)
    env["CLAUDE_LANE_DETACHED_START_DELAY"] = "0.1"

    parent = _run(env, paths, "-a", "lane@example.com", "-d")

    assert parent.returncode == 0
    assert "detached pid=" in parent.stdout
    lane_log = tmp_path / "answer.lane.log"
    refs: list[str] = []
    owned_copies: list[Path] = []
    deadline = time.monotonic() + 20  # generous: suites run under heavy lane load
    while time.monotonic() < deadline:
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/claude-salvage"],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        owned_copies = [
            path for path in tmp_path.glob("subfleet-claude-prompt.*")
            if path != inherited
        ]
        if refs and not owned_copies and lane_log.exists():
            break
        time.sleep(0.05)
    assert inherited.is_dir()
    assert (inherited / "secret.txt").read_text() == "private material\n"
    assert owned_copies == []
    assert "skipping salvage and push" not in lane_log.read_text()
    assert len(refs) == 1


def test_real_hook_resolves_transcript_and_appends_ledger(tmp_path):
    env, run_paths = _fixture_env(
        tmp_path,
        """sid=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--session-id" ]; then sid=$2; shift 2; continue; fi
  shift
done
mkdir -p "$SUBFLEET_CLAUDE_DIR/projects/test"
printf '%s\n' '{"message":{"id":"m1","usage":{"input_tokens":10,"cache_creation_input_tokens":5,"cache_read_input_tokens":3,"output_tokens":2}}}' > "$SUBFLEET_CLAUDE_DIR/projects/test/$sid.jsonl"
printf '%s\n' '{"is_error":false,"result":"recorded"}'
""",
    )
    state = tmp_path / "state"
    env.update({
        "CLAUDE_LANE_SUBFLEET": str(SCRIPT.parent / "subfleet"),
        "SUBFLEET_STATE_DIR": str(state),
        "SUBFLEET_CLAUDE_DIR": str(tmp_path / "claude"),
        "DELEGATE_STATE_DIR": str(tmp_path / "delegate-state"),
    })

    result = _run(env, run_paths)

    assert result.returncode == 0
    records = [
        json.loads(line)
        for line in (state / "lane-usage.jsonl").read_text().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["email"] == "lane@example.com"
    assert records[0]["input_tokens"] == 18
    assert records[0]["output_tokens"] == 2
    assert records[0]["total_tokens"] == 20


@pytest.mark.parametrize(
    ("claude_body", "expected_rc", "expected_out"),
    [
        ("printf '{\"is_error\":false,\"result\":\"claude finished\"}\\n'\n", 0,
         "claude finished\n"),
        ("printf 'forced claude failure\\n' >&2\nexit 9\n", 9, ""),
    ],
)
def test_runner_records_durable_run_on_success_and_failure(
    tmp_path, claude_body, expected_rc, expected_out
):
    env, run_paths = _fixture_env(tmp_path, claude_body)
    state = tmp_path / "state"
    env.update({
        "CLAUDE_LANE_SUBFLEET": str(SCRIPT.parent / "subfleet"),
        "SUBFLEET_STATE_DIR": str(state),
        "SUBFLEET_CLAUDE_DIR": str(tmp_path / "claude"),
        "DELEGATE_STATE_DIR": str(tmp_path / "delegate-state"),
    })

    result = _run(env, run_paths, "-a", "lane@example.com", "-r", "0")

    assert result.returncode == expected_rc
    run_dirs = [path for path in (state / "runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["family"] == "claude"
    assert meta["model"] == "claude-fable-5-1"
    assert meta["lane"] == "lane@example.com"
    assert meta["rc"] == expected_rc
    assert meta["finished_at"] is not None
    assert meta["session_id"] == "00000000-0000-4000-8000-000000000001"
    assert (run_dir / "prompt.md").read_text() == run_paths["prompt"].read_text()
    assert (run_dir / "out.md").read_text() == expected_out
    if expected_rc:
        assert "forced claude failure" in (run_dir / "err.log").read_text()


def test_relative_artifacts_reach_real_hook_for_reset_parsing(tmp_path):
    env, run_paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":true,\"result\":\"session limit; resets 6:40pm (America/New_York)\"}\\n'\nexit 9\n",
    )
    state = tmp_path / "state"
    env.update({
        "CLAUDE_LANE_SUBFLEET": str(SCRIPT.parent / "subfleet"),
        "SUBFLEET_STATE_DIR": str(state),
        "SUBFLEET_CLAUDE_DIR": str(tmp_path / "claude"),
        "DELEGATE_STATE_DIR": str(tmp_path / "delegate-state"),
    })

    result = subprocess.run(
        [
            str(SCRIPT), "-a", "lane@example.com", "-C", "work",
            "-p", "prompt.md", "-o", "relative.md",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 4
    records = [
        json.loads(line)
        for line in (state / "lane-usage.jsonl").read_text().splitlines()
    ]
    hard_limit = next(record for record in records if record.get("event") == "hard_limit")
    reset = hard_limit["reset"]
    assert hard_limit["model"] == "claude-fable-5-1"
    assert reset is not None
    assert reset[11:16] == "18:40"


def test_retired_fable_5_served_for_the_current_pin_is_a_downgrade(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    session_id = "00000000-0000-4000-8000-000000000001"
    transcript = (
        Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "-fixture-project" / f"{session_id}.jsonl"
    )
    transcript.write_text(
        json.dumps({"type": "assistant", "sessionId": session_id, "message": {"model": "claude-fable-5"}})
        + "\n"
    )
    _write_executable(Path(env["CLAUDE_LANE_CLAUDE_MODEL"]), "printf 'latest: claude-fable-5\\n'\n")

    result = _run(env, paths)

    assert result.returncode == 0
    assert not (tmp_path / "answer.MODEL_ATTESTED").exists()
    downgraded = tmp_path / "answer.DOWNGRADED"
    assert downgraded.exists()
    assert "served claude-fable-5, requested claude-fable-5-1" in downgraded.read_text()


def test_cli_too_old_for_the_model_fails_fast_without_rotating(tmp_path):
    """Claude Code 2.1.228 refused claude-fable-5-1 with a 400 (2026-09-02).
    The host CLI, not the lane, is at fault: no re-pick, no cooldown."""
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":true,\"result\":\"API Error: 400 Claude Code 2.1.228 does not support this model; version 2.1.251 or newer is required. Run claude update, or update the Claude desktop app, then try again.\"}\\n'\nexit 1\n",
    )
    env["CAPACITY_CANDIDATES"] = "pinned@example.com,fallback@example.com"

    result = _run(env, paths, "-a", "pinned@example.com", "-A")

    assert result.returncode == 6
    assert "CLAUDE CLI TOO OLD for model claude-fable-5-1" in result.stderr
    assert "Claude Code 2.1.228 does not support this model; version 2.1.251 or newer is required" in result.stderr
    assert "Run: claude update" in result.stderr
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert [(call["--email"], call["--rc"]) for call in calls] == [("pinned@example.com", "1")]
    assert _pick_calls(paths["pick_log"]) == []


def test_out_of_usage_credits_is_a_hard_limit_for_a_pinned_lane(tmp_path):
    """2026-09-02: a lane answered "You're out of usage credits" for Fable
    while still serving Opus/Haiku. That is a hard, model-scoped limit: no
    transient retries, rc=4 so the front door cools this model on this lane."""
    env, paths = _fixture_env(
        tmp_path,
        "printf '{\"is_error\":true,\"result\":\"You\u2019re out of usage credits. Switch to another model, or manage usage credits at claude.ai/settings/usage?from=cc_cli_limit_message, to continue.\"}\\n'\nexit 1\n",
    )
    env["CAPACITY_CANDIDATES"] = "pinned@example.com,fallback@example.com"

    result = _run(env, paths, "-a", "pinned@example.com")

    assert result.returncode == 4
    assert "LANE LIMITED" in result.stderr
    assert "transient failure" not in result.stderr
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert [(call["--email"], call["--rc"]) for call in calls] == [("pinned@example.com", "4")]


def test_out_of_usage_credits_re_picks_under_auto(tmp_path):
    env, paths = _fixture_env(
        tmp_path,
        """n=$(cat "$CLAUDE_COUNTER" 2>/dev/null || printf 0)
n=$((n + 1))
printf '%s\\n' "$n" > "$CLAUDE_COUNTER"
if [ "$n" -eq 1 ]; then
  printf '{"is_error":true,"result":"You are out of usage credits. Switch to another model, or manage usage credits at claude.ai/settings/usage, to continue."}\\n'
  exit 1
fi
printf '{"is_error":false,"result":"re-picked lane worked"}\\n'
""",
    )
    env["CLAUDE_COUNTER"] = str(tmp_path / "claude-counter")
    env["CAPACITY_CANDIDATES"] = "pinned@example.com,fallback@example.com"

    result = _run(env, paths, "-a", "pinned@example.com", "-A")

    assert result.returncode == 0, result.stderr
    calls = [_options(call) for call in _hook_calls(paths["hook_log"])]
    assert [(call["--email"], call["--rc"]) for call in calls] == [
        ("pinned@example.com", "4"),
        ("fallback@example.com", "0"),
    ]
    assert _pick_calls(paths["pick_log"]) == [[
        "pick", "claude", "--model", "claude-fable-5-1",
        "--exclude", "pinned@example.com",
    ]]
    assert paths["output"].read_text().strip() == "re-picked lane worked"


@pytest.mark.parametrize(("requested", "dispatched"), [
    ("fable", "claude-fable-5-1"),
    ("claude-fable-5", "claude-fable-5-1"),
    ("claude-fable-5-1[1m]", "claude-fable-5-1[1m]"),
    ("claude-fable-5[1m]", "claude-fable-5-1[1m]"),
])
def test_runner_resolves_aliases_and_retired_pins_before_dispatch(tmp_path, requested, dispatched):
    """`-m fable` / a retired pin / the app's [1m] form all dispatch the
    canonical id (suffix kept), and the attestation names the bare id the
    transcript records — independent of the CLI's own alias table."""
    args_file = tmp_path / "claude-args"
    env, paths = _fixture_env(
        tmp_path,
        """{
  for arg in "$@"; do printf '<%s>\n' "$arg"; done
} > "$CLAUDE_ARGS_FILE"
printf '{"is_error":false,"result":"finished"}\n'
""",
    )
    env["CLAUDE_ARGS_FILE"] = str(args_file)

    result = _run(env, paths, "-a", "lane@example.com", "-m", requested)

    assert result.returncode == 0, result.stderr
    args = _captured_args(args_file)
    assert args[args.index("--model") + 1] == dispatched
    attested = tmp_path / "answer.MODEL_ATTESTED"
    assert attested.exists(), result.stderr
    assert attested.read_text().splitlines()[:2] == [
        "requested: claude-fable-5-1", "served: claude-fable-5-1",
    ]
    assert not (tmp_path / "answer.DOWNGRADED").exists()
    assert "served by claude-fable-5-1 (as requested)" in result.stderr


def test_runner_prefers_the_native_launcher_without_an_override(tmp_path):
    """Without CLAUDE_LANE_CLAUDE the runner uses ~/.local/bin/claude when it
    exists (launchd PATHs put a stale Homebrew cask first), else PATH."""
    env, paths = _fixture_env(
        tmp_path,
        'printf \'{"is_error":false,"result":"finished"}\\n\'\n',
    )
    home = tmp_path / "home"
    native = home / ".local" / "bin" / "claude"
    native.parent.mkdir(parents=True)
    marker = tmp_path / "native-ran"
    _write_executable(native, f"""touch "{marker}"
printf '{{"is_error":false,"result":"native"}}\\n'
""")
    env["HOME"] = str(home)
    env.pop("CLAUDE_LANE_CLAUDE", None)

    result = _run(env, paths)

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert paths["output"].read_text().strip() == "native"
