"""Behavioral coverage for Codex lane selection and the PATH shim."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from subfleet import codex


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "bin" / "subfleet-codex"
SHIM = ROOT / "bin" / "codex"


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def test_codex_binary_resolves_known_location_without_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    binary = _executable(home / ".bun" / "bin" / "codex", "#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.delenv("SUBFLEET_CODEX_BIN", raising=False)

    assert codex._codex_binary() == str(binary)


def test_codex_binary_fails_closed_without_real_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(Path, "is_file", lambda _self: False)
    monkeypatch.delenv("SUBFLEET_CODEX_BIN", raising=False)

    assert codex._codex_binary() is None


def test_codex_binary_rejects_the_path_shim_as_an_override(monkeypatch):
    monkeypatch.setenv("SUBFLEET_CODEX_BIN", str(SHIM))

    assert codex._codex_binary() is None


def test_runner_auto_picks_and_repicks_after_usage_limit(tmp_path):
    fake_codex = _executable(
        tmp_path / "real-codex",
        """#!/usr/bin/env bash
count=0
[ ! -f "$CALL_COUNT" ] || count=$(<"$CALL_COUNT")
count=$((count + 1))
printf '%s' "$count" >"$CALL_COUNT"
printf '%s\n' "$CODEX_HOME" >>"$HOME_LOG"
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then out=$2; shift 2; else shift; fi
done
if [ "$count" = 1 ]; then
  echo "You've hit your usage limit" >&2
  exit 1
fi
printf 'finished on alternate lane\n' >"$out"
""",
    )
    lane_one = tmp_path / "codex-1"
    lane_two = tmp_path / "codex-2"
    lane_one.mkdir()
    lane_two.mkdir()
    fake_subfleet = _executable(
        tmp_path / "subfleet",
        """#!/usr/bin/env bash
case "$1" in
  _record-run)
    [ "$3" != start ] || printf 'run-1\n'
    exit 0 ;;
  _codex-binary)
    printf '%s\n' "$FAKE_CODEX_PATH"
    exit 0 ;;
  pick)
    printf '%s\n' "$*" >>"$PICK_LOG"
    case "$*" in
      *--exclude*) printf '%s\n' "$LANE_TWO" ;;
      *) printf '%s\n' "$LANE_ONE" ;;
    esac
    exit 0 ;;
esac
exit 90
""",
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the work")
    output = tmp_path / "out.md"
    home_log = tmp_path / "homes.log"
    pick_log = tmp_path / "picks.log"

    completed = subprocess.run(
        [
            str(RUNNER), "-m", "test-model", "-C", str(workdir),
            "-p", str(prompt), "-o", str(output), "-r", "1",
        ],
        env={
            **os.environ,
            "SUBFLEET_RUN_SUBFLEET": str(fake_subfleet),
            "SUBFLEET_CODEX_GUARD": "off",
            "FAKE_CODEX_PATH": str(fake_codex),
            "LANE_ONE": str(lane_one),
            "LANE_TWO": str(lane_two),
            "CALL_COUNT": str(tmp_path / "count"),
            "HOME_LOG": str(home_log),
            "PICK_LOG": str(pick_log),
        },
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert output.read_text() == "finished on alternate lane\n"
    assert home_log.read_text().splitlines() == [str(lane_one), str(lane_two)]
    assert f"--exclude {lane_one}" in pick_log.read_text()
    assert "re-picked" in completed.stderr


@pytest.mark.parametrize(("child_rc", "saved_output"), [(0, "codex finished\n"), (9, "")])
def test_runner_records_provider_success_and_failure(tmp_path, child_rc, saved_output):
    fake_codex = _executable(
        tmp_path / "real-codex",
        """#!/usr/bin/env bash
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then out=$2; shift 2; else shift; fi
done
if [ "$FAKE_CODEX_RC" -eq 0 ]; then
  printf 'codex finished\n' >"$out"
else
  printf 'forced codex failure\n' >&2
fi
exit "$FAKE_CODEX_RC"
""",
    )
    state = tmp_path / "state"
    workdir = tmp_path / "work"
    home = tmp_path / "codex-home"
    workdir.mkdir()
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("preamble\n\ndo the task\n")
    output = tmp_path / "caller-output.md"
    env = {
        **os.environ,
        "SUBFLEET_CODEX_BIN": str(fake_codex),
        "SUBFLEET_CODEX_GUARD": "off",
        "SUBFLEET_STATE_DIR": str(state),
        "FAKE_CODEX_RC": str(child_rc),
    }
    env.pop("SUBFLEET_RUN_OWNED_PROMPT", None)

    completed = subprocess.run(
        [
            str(RUNNER), "-H", str(home), "-m", "test-model", "-C", str(workdir),
            "-p", str(prompt), "-o", str(output), "-r", "0",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == child_rc, completed.stderr
    run_dirs = [path for path in (state / "runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    metadata = json.loads((run_dirs[0] / "meta.json").read_text())
    assert metadata["family"] == "codex"
    assert metadata["lane"] == str(home)
    assert metadata["rc"] == child_rc
    assert metadata["finished_at"] is not None
    assert (run_dirs[0] / "prompt.md").read_text() == prompt.read_text()
    assert (run_dirs[0] / "out.md").read_text() == saved_output
    if child_rc:
        assert "forced codex failure" in (run_dirs[0] / "err.log").read_text()


def test_runner_records_failure_before_auto_pick(tmp_path):
    picker = _executable(tmp_path / "picker", "#!/bin/sh\nexit 7\n")
    state = tmp_path / "state"
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Record this dispatch even when no lane is available\n")
    output = tmp_path / "out.md"
    env = {
        **os.environ,
        "SUBFLEET_CODEX_PICK": str(picker),
        "SUBFLEET_CODEX_GUARD": "off",
        "SUBFLEET_STATE_DIR": str(state),
    }
    env.pop("SUBFLEET_RUN_OWNED_PROMPT", None)

    completed = subprocess.run(
        [
            str(RUNNER), "-m", "test-model", "-C", str(workdir),
            "-p", str(prompt), "-o", str(output),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 1
    run_dirs = [path for path in (state / "runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    metadata = json.loads((run_dirs[0] / "meta.json").read_text())
    assert metadata["lane"] is None
    assert metadata["rc"] == 1
    assert metadata["finished_at"] is not None


@pytest.mark.parametrize("command", ["exec", "e", "review"])
def test_path_shim_auto_picks_headless_commands(tmp_path, command):
    lane = tmp_path / "codex-1"
    lane.mkdir()
    real = _executable(
        tmp_path / "real-codex",
        """#!/usr/bin/env bash
printf '%s' "${CODEX_HOME:-}" >"$CAPTURE_HOME"
printf '%s\n' "$@" >"$CAPTURE_ARGS"
""",
    )
    fake_subfleet = _executable(
        tmp_path / "subfleet",
        """#!/usr/bin/env bash
if [ "$1" = _codex-binary ]; then printf '%s\n' "$REAL_CODEX_PATH"; exit 0; fi
if [ "$1" = pick ] && [ "$2" = codex ]; then printf '%s\n' "$PICK_LANE"; exit 0; fi
if [ "$1" = _api-lane-check ]; then printf '%s\n' "$2" >>"$CHECK_LOG"; exit 0; fi
exit 1
""",
    )
    env = os.environ.copy()
    env.pop("CODEX_HOME", None)
    env.pop("SUBFLEET_NO_AUTOPICK", None)
    env.update(
        {
            "SUBFLEET_SHIM_SUBFLEET": str(fake_subfleet),
            "REAL_CODEX_PATH": str(real),
            "PICK_LANE": str(lane),
            "CAPTURE_HOME": str(tmp_path / "home"),
            "CAPTURE_ARGS": str(tmp_path / "args"),
            "CHECK_LOG": str(tmp_path / "checks"),
        }
    )

    completed = subprocess.run(
        [str(SHIM), command, "--example"], env=env, text=True, capture_output=True
    )

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "home").read_text() == str(lane)
    assert (tmp_path / "args").read_text().splitlines() == [command, "--example"]
    # The picked lane still passes through the subscription-only gate.
    assert (tmp_path / "checks").read_text().splitlines() == [str(lane)]


def test_path_shim_respects_autopick_opt_out(tmp_path):
    real = _executable(
        tmp_path / "real-codex",
        "#!/usr/bin/env bash\nprintf '%s' \"${CODEX_HOME:-}\" >\"$CAPTURE_HOME\"\n",
    )
    fake_subfleet = _executable(
        tmp_path / "subfleet",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$REAL_CODEX_PATH\"\n",
    )
    env = {
        **os.environ,
        "SUBFLEET_SHIM_SUBFLEET": str(fake_subfleet),
        "REAL_CODEX_PATH": str(real),
        "SUBFLEET_NO_AUTOPICK": "1",
        "CAPTURE_HOME": str(tmp_path / "home"),
    }
    env.pop("CODEX_HOME", None)

    completed = subprocess.run([str(SHIM), "exec"], env=env, capture_output=True)

    assert completed.returncode == 0
    assert (tmp_path / "home").read_text() == ""


@pytest.mark.parametrize("pick_mode", ["failure", "missing-directory"])
def test_path_shim_fails_closed_when_pick_is_unusable(tmp_path, pick_mode):
    marker = tmp_path / "vendor-ran"
    real = _executable(
        tmp_path / "real-codex",
        "#!/usr/bin/env bash\ntouch \"$VENDOR_MARKER\"\n",
    )
    fake_subfleet = _executable(
        tmp_path / "subfleet",
        """#!/usr/bin/env bash
if [ "$1" = _codex-binary ]; then printf '%s\n' "$REAL_CODEX_PATH"; exit 0; fi
if [ "$1" = pick ]; then
  [ "$PICK_MODE" != failure ] || exit 3
  printf '%s\n' "$MISSING_LANE"
  exit 0
fi
exit 1
""",
    )
    env = {
        **os.environ,
        "SUBFLEET_SHIM_SUBFLEET": str(fake_subfleet),
        "REAL_CODEX_PATH": str(real),
        "PICK_MODE": pick_mode,
        "MISSING_LANE": str(tmp_path / "not-a-lane"),
        "VENDOR_MARKER": str(marker),
    }
    env.pop("CODEX_HOME", None)
    env.pop("SUBFLEET_NO_AUTOPICK", None)

    completed = subprocess.run(
        [str(SHIM), "exec", "task"], env=env, text=True, capture_output=True
    )

    assert completed.returncode == 1
    assert not marker.exists()
    assert "refusing to use the desktop app home" in completed.stderr


def test_path_shim_does_not_recurse_when_binary_resolution_fails(tmp_path):
    fake_subfleet = _executable(tmp_path / "subfleet", "#!/bin/sh\nexit 1\n")
    env = {
        **os.environ,
        "SUBFLEET_SHIM_SUBFLEET": str(fake_subfleet),
    }

    completed = subprocess.run(
        [str(SHIM), "exec"], env=env, text=True, capture_output=True, timeout=5
    )

    assert completed.returncode == 127
    assert "real codex binary not found" in completed.stderr


def _api_key_home(tmp_path: Path, name: str = "codex-api") -> Path:
    home = tmp_path / name
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-test", "auth_mode": "apikey", "tokens": None})
    )
    return home


def _chatgpt_home(tmp_path: Path, name: str = "codex-1") -> Path:
    home = tmp_path / name
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": None, "auth_mode": "chatgpt"}))
    return home


def _env_capturing_codex(path: Path, capture: Path) -> Path:
    """A fake codex that records whether CODEX_API_KEY reached it."""
    return _executable(
        path,
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"${{CODEX_API_KEY:-unset}}\" >'{capture}'\n"
        "out=''\n"
        'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then out=$2; shift 2; else shift; fi; done\n'
        "printf 'codex finished\\n' >\"$out\"\n",
    )


def _runner_env(tmp_path: Path, fake_codex: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("SUBFLEET_ALLOW_API_LANE", "CODEX_API_KEY", "SUBFLEET_RUN_OWNED_PROMPT"):
        env.pop(name, None)
    env.update(
        {
            "SUBFLEET_CODEX_BIN": str(fake_codex),
            "SUBFLEET_CODEX_GUARD": "off",
            "SUBFLEET_STATE_DIR": str(tmp_path / "state"),
        }
    )
    env.update(extra)
    return env


def _runner_args(tmp_path: Path, home: Path, model: str = "gpt-6-astra") -> list[str]:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the task\n")
    return [
        str(RUNNER), "-H", str(home), "-m", model, "-C", str(workdir),
        "-p", str(prompt), "-o", str(tmp_path / "caller-output.md"), "-r", "0",
    ]


def test_runner_refuses_an_api_key_home_before_any_codex_call(tmp_path):
    marker = tmp_path / "vendor-ran"
    fake_codex = _executable(
        tmp_path / "real-codex", "#!/usr/bin/env bash\ntouch \"$VENDOR_MARKER\"\n"
    )
    env = _runner_env(tmp_path, fake_codex, VENDOR_MARKER=str(marker))

    completed = subprocess.run(
        _runner_args(tmp_path, _api_key_home(tmp_path)),
        env=env, text=True, capture_output=True, timeout=10,
    )

    assert completed.returncode == 7
    assert "ChatGPT subscriptions only" in completed.stderr
    assert "SUBFLEET_ALLOW_API_LANE=1" in completed.stderr
    assert not marker.exists()  # codex was never invoked
    run_dirs = [path for path in (tmp_path / "state" / "runs").iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    metadata = json.loads((run_dirs[0] / "meta.json").read_text())
    assert metadata["rc"] == 7
    assert metadata["finished_at"] is not None


def test_runner_drops_codex_api_key_from_the_lane_environment(tmp_path):
    capture = tmp_path / "seen-key.txt"
    fake_codex = _env_capturing_codex(tmp_path / "real-codex", capture)
    env = _runner_env(tmp_path, fake_codex, CODEX_API_KEY="sk-must-not-leak")

    completed = subprocess.run(
        _runner_args(tmp_path, _chatgpt_home(tmp_path), model="gpt-test"),
        env=env, text=True, capture_output=True, timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert capture.read_text().strip() == "unset"
    assert (tmp_path / "caller-output.md").read_text() == "codex finished\n"


def test_runner_override_allows_a_deliberate_api_dispatch(tmp_path):
    capture = tmp_path / "seen-key.txt"
    fake_codex = _env_capturing_codex(tmp_path / "real-codex", capture)
    env = _runner_env(
        tmp_path, fake_codex, SUBFLEET_ALLOW_API_LANE="1", CODEX_API_KEY="sk-deliberate"
    )

    completed = subprocess.run(
        _runner_args(tmp_path, _api_key_home(tmp_path)),
        env=env, text=True, capture_output=True, timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert capture.read_text().strip() == "sk-deliberate"


def _shim_env(tmp_path: Path, real: Path, home: Path, **extra: str) -> dict[str, str]:
    """Drive the shim with the real subfleet CLI and an explicit CODEX_HOME."""
    env = os.environ.copy()
    for name in ("SUBFLEET_ALLOW_API_LANE", "SUBFLEET_NO_AUTOPICK", "SUBFLEET_SHIM_SUBFLEET"):
        env.pop(name, None)
    env.update(
        {
            "SUBFLEET_CODEX_BIN": str(real),
            "CODEX_HOME": str(home),
            "CAPTURE_HOME": str(tmp_path / "home"),
            "VENDOR_MARKER": str(tmp_path / "vendor-ran"),
        }
    )
    env.update(extra)
    return env


def _shim_real_codex(tmp_path: Path) -> Path:
    return _executable(
        tmp_path / "real-codex",
        "#!/usr/bin/env bash\n"
        "touch \"$VENDOR_MARKER\"\n"
        "printf '%s' \"${CODEX_HOME:-}\" >\"$CAPTURE_HOME\"\n",
    )


def test_path_shim_refuses_an_explicit_api_key_home(tmp_path):
    real = _shim_real_codex(tmp_path)
    env = _shim_env(tmp_path, real, _api_key_home(tmp_path))

    completed = subprocess.run(
        [str(SHIM), "exec", "task"], env=env, text=True, capture_output=True, timeout=20
    )

    assert completed.returncode == 7, completed.stderr
    assert "ChatGPT subscriptions only" in completed.stderr
    assert not (tmp_path / "vendor-ran").exists()


def test_path_shim_passes_an_explicit_chatgpt_home(tmp_path):
    real = _shim_real_codex(tmp_path)
    home = _chatgpt_home(tmp_path)
    env = _shim_env(tmp_path, real, home)

    completed = subprocess.run(
        [str(SHIM), "exec", "task"], env=env, text=True, capture_output=True, timeout=20
    )

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "home").read_text() == str(home)


def test_path_shim_override_allows_a_deliberate_api_home(tmp_path):
    real = _shim_real_codex(tmp_path)
    home = _api_key_home(tmp_path)
    env = _shim_env(tmp_path, real, home, SUBFLEET_ALLOW_API_LANE="1")

    completed = subprocess.run(
        [str(SHIM), "exec", "task"], env=env, text=True, capture_output=True, timeout=20
    )

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "home").read_text() == str(home)


def test_path_shim_leaves_interactive_commands_alone_even_on_an_api_home(tmp_path):
    real = _shim_real_codex(tmp_path)
    home = _api_key_home(tmp_path)
    env = _shim_env(tmp_path, real, home)

    completed = subprocess.run(
        [str(SHIM), "login", "status"], env=env, text=True, capture_output=True, timeout=20
    )

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "vendor-ran").exists()
