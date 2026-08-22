"""Behavioral coverage for Codex lane selection and the PATH shim."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from carpool import codex


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "bin" / "carpool-codex"
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
    monkeypatch.delenv("CARPOOL_CODEX_BIN", raising=False)

    assert codex._codex_binary() == str(binary)


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
    fake_carpool = _executable(
        tmp_path / "carpool",
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
            "CARPOOL_RUN_CARPOOL": str(fake_carpool),
            "CARPOOL_CODEX_GUARD": "off",
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
        "CARPOOL_CODEX_BIN": str(fake_codex),
        "CARPOOL_CODEX_GUARD": "off",
        "CARPOOL_STATE_DIR": str(state),
        "FAKE_CODEX_RC": str(child_rc),
    }
    env.pop("CARPOOL_RUN_OWNED_PROMPT", None)

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
        "CARPOOL_CODEX_PICK": str(picker),
        "CARPOOL_CODEX_GUARD": "off",
        "CARPOOL_STATE_DIR": str(state),
    }
    env.pop("CARPOOL_RUN_OWNED_PROMPT", None)

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
    fake_carpool = _executable(
        tmp_path / "carpool",
        """#!/usr/bin/env bash
if [ "$1" = _codex-binary ]; then printf '%s\n' "$REAL_CODEX_PATH"; exit 0; fi
if [ "$1" = pick ] && [ "$2" = codex ]; then printf '%s\n' "$PICK_LANE"; exit 0; fi
exit 1
""",
    )
    env = os.environ.copy()
    env.pop("CODEX_HOME", None)
    env.pop("CARPOOL_NO_AUTOPICK", None)
    env.update(
        {
            "CARPOOL_SHIM_CARPOOL": str(fake_carpool),
            "REAL_CODEX_PATH": str(real),
            "PICK_LANE": str(lane),
            "CAPTURE_HOME": str(tmp_path / "home"),
            "CAPTURE_ARGS": str(tmp_path / "args"),
        }
    )

    completed = subprocess.run(
        [str(SHIM), command, "--example"], env=env, text=True, capture_output=True
    )

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "home").read_text() == str(lane)
    assert (tmp_path / "args").read_text().splitlines() == [command, "--example"]


def test_path_shim_respects_autopick_opt_out(tmp_path):
    real = _executable(
        tmp_path / "real-codex",
        "#!/usr/bin/env bash\nprintf '%s' \"${CODEX_HOME:-}\" >\"$CAPTURE_HOME\"\n",
    )
    fake_carpool = _executable(
        tmp_path / "carpool",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$REAL_CODEX_PATH\"\n",
    )
    env = {
        **os.environ,
        "CARPOOL_SHIM_CARPOOL": str(fake_carpool),
        "REAL_CODEX_PATH": str(real),
        "CARPOOL_NO_AUTOPICK": "1",
        "CAPTURE_HOME": str(tmp_path / "home"),
    }
    env.pop("CODEX_HOME", None)

    completed = subprocess.run([str(SHIM), "exec"], env=env, capture_output=True)

    assert completed.returncode == 0
    assert (tmp_path / "home").read_text() == ""
