"""Resume a Codex run on the exact account home that owns its thread."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import capacity, notify, run_ledger

DEFAULT_PROMPT = (
    "Continue the interrupted task from where you left off. "
    "Reinspect the current worktree state first."
)
BLOCKED_STATUSES = {"cooldown", "limited", "exhausted"}


def _command_option(meta: dict[str, Any], flag: str) -> str | None:
    decision = meta.get("routing_decision")
    command = decision.get("cmd") if isinstance(decision, dict) else None
    if not isinstance(command, list):
        return None
    for index, value in enumerate(command[:-1]):
        if value == flag and isinstance(command[index + 1], str):
            return command[index + 1]
    return None


def _capacity_block(
    home: str,
    report_fn: Callable[[], dict[str, Any]],
) -> tuple[str, str | None] | None:
    """Return (status, reset) when the recorded home is known unavailable."""
    cooldown = capacity.lane_cooldown(home)
    if cooldown is not None:
        return "cooldown", cooldown.isoformat()
    try:
        report = report_fn()
    except (OSError, TypeError, ValueError):
        # Capacity observation is advisory here. The pinned runner remains the
        # authority and will record a fresh cooldown if the provider rejects it.
        return None
    for row in report.get("accounts") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("home") or row.get("id") or "") != home:
            continue
        status = str(row.get("status") or "")
        if status in BLOCKED_STATUSES:
            return status, row.get("limited_until") or row.get("reset_at")
        return None
    return None


def _finish_unclaimed(run_id: str, rc: int, home: str) -> None:
    """Close a pre-created entry if the shell runner died before adopting it."""
    try:
        _run_dir, meta = run_ledger.load_run(run_id)
        if meta.get("finished_at") is None:
            run_ledger.finish_run(run_id, rc=rc, lane=home)
    except (OSError, TypeError, ValueError):
        pass


def run(
    source_run_id: str,
    *,
    prompt: str | None = None,
    output: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    capacity_report: Callable[[], dict[str, Any]] | None = None,
) -> int:
    """Resume ``source_run_id`` synchronously, pinned to its original home."""
    try:
        target = run_ledger.codex_resume_target(source_run_id)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        print(f"subfleet resume-codex: {exc}", file=sys.stderr)
        return 2

    meta = target["meta"]
    if meta.get("finished_at") is None:
        print(
            f"subfleet resume-codex: run {source_run_id} is still RUNNING",
            file=sys.stderr,
        )
        return 2

    thread_id = target.get("codex_thread_id")
    home = target.get("codex_home")
    model = target.get("model")
    workdir = target.get("workdir")
    missing = [
        name
        for name, value in (
            ("Codex thread id", thread_id),
            ("CODEX_HOME", home),
            ("model", model),
            ("workdir", workdir),
        )
        if not value
    ]
    if missing:
        print(
            f"subfleet resume-codex: run {source_run_id} has no " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    home = str(Path(str(home)).expanduser())
    workdir = str(Path(str(workdir)).expanduser())
    if not Path(home).is_dir():
        print(f"subfleet resume-codex: recorded home is missing: {home}", file=sys.stderr)
        return 2
    if not Path(workdir).is_dir():
        print(f"subfleet resume-codex: recorded workdir is missing: {workdir}", file=sys.stderr)
        return 2

    blocked = _capacity_block(home, capacity_report or capacity.report)
    if blocked is not None:
        status, reset = blocked
        detail = f" until {reset}" if reset else ""
        print(
            f"subfleet resume-codex: parked — original lane {home} is {status}{detail}; "
            "the thread cannot migrate to another CODEX_HOME",
            file=sys.stderr,
        )
        return 3

    text = prompt or DEFAULT_PROMPT
    descriptor, prompt_path = tempfile.mkstemp(prefix="delegate-prompt-", suffix=".md")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)

    sandbox = _command_option(meta, "-s") or "workspace-write"
    effort = _command_option(meta, "-e")
    effective_command = [
        "subfleet-codex",
        "-H",
        home,
        "-T",
        str(thread_id),
        "-m",
        str(model),
        "-C",
        workdir,
        "-s",
        sandbox,
    ]
    if effort:
        effective_command += ["-e", effort]
    caller = notify.caller_context(cwd=workdir)
    new_run_id: str | None = None
    try:
        new_run_id = run_ledger.start_run(
            family="codex",
            model=str(model),
            lane=home,
            workdir=workdir,
            prompt=prompt_path,
            out=output,
            original_out=output or "",
            decision_json={
                "kind": "codex-resume",
                "source_run": source_run_id,
                "cmd": effective_command,
                "result": None,
            },
            caller=caller,
            slug=f"resume-{str(thread_id)[:8]}",
            launcher="subfleet resume-codex",
            resumed_from=source_run_id,
        )
        run_paths = run_ledger.run_paths(new_run_id)
        out_path = run_paths["out"]
        if not out_path:
            raise OSError("ledger did not allocate an output path")
        command = [
            str(Path(__file__).resolve().parent.parent / "bin" / "subfleet-codex"),
            "-H",
            home,
            "-T",
            str(thread_id),
            "-m",
            str(model),
            "-C",
            workdir,
            "-p",
            prompt_path,
            "-o",
            out_path,
            "-s",
            sandbox,
        ]
        if effort:
            command += ["-e", effort]
        env = os.environ.copy()
        env["SUBFLEET_RUN_ID"] = new_run_id
        env["SUBFLEET_RUN_RESUMED_FROM"] = source_run_id
        env["SUBFLEET_RUN_OWNED_PROMPT"] = prompt_path
        env["SUBFLEET_RUN_ORIGINAL_OUT"] = output or ""
        completed = runner(command, env=env, capture_output=True, text=True)
        if completed.stdout:
            sys.stderr.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        _finish_unclaimed(new_run_id, int(completed.returncode), home)
        run_dir, final_meta = run_ledger.load_run(new_run_id)
        print(
            run_ledger.summary_line(new_run_id, final_meta, prefix="subfleet resume-codex"),
            file=sys.stderr,
        )
        if completed.returncode == 0 and output is None:
            try:
                sys.stdout.write((run_dir / "out.md").read_text(encoding="utf-8"))
            except OSError:
                pass
        return int(completed.returncode)
    except OSError as exc:
        if new_run_id is not None:
            _finish_unclaimed(new_run_id, 127, home)
        print(f"subfleet resume-codex: launch failed: {exc}", file=sys.stderr)
        return 127
    finally:
        try:
            Path(prompt_path).unlink()
        except OSError:
            pass
