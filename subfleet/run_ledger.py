"""Durable, private artifacts and live-run state for subfleet dispatches."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import paths
from .util import atomic_write_json, iso, load_json, now_local, parse_iso

MAX_RUNS = 500
MAX_BYTES = 2 * 1024**3
_SLUG_LENGTH = 40


def _absolute(value: str | Path | None) -> str | None:
    if value is None or str(value) == "":
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(path)


def _time(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    parsed = parse_iso(value) if isinstance(value, str) else None
    return parsed if parsed else now_local()


def _slug(out_path: str | Path | None, prompt_path: str | Path | None) -> str:
    source = out_path or prompt_path or "run"
    raw = Path(str(source)).name[:_SLUG_LENGTH]
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned[:_SLUG_LENGTH] or "run"


@contextmanager
def _lock() -> Iterator[None]:
    root = paths.runs_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    with (root / ".lock").open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _allocate(started: datetime, slug: str) -> Path:
    base = f"{started.strftime('%Y%m%d-%H%M%S')}-{slug}"
    root = paths.runs_dir()
    for number in range(1, 10_000):
        name = base if number == 1 else f"{base}-{number:02d}"
        candidate = root / name
        try:
            candidate.mkdir(mode=0o700)
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"could not allocate a unique run id for {base}")


def _write_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    path = run_dir / "meta.json"
    atomic_write_json(path, meta)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _copy(source: str | Path | None, destination: Path, *, required: bool) -> None:
    """Copy one artifact privately; required destinations exist even on failure.

    Opening /dev/fd/N can share its seek position with the inherited descriptor
    on macOS. Rewind after copying so detached Codex can still consume its prompt.
    """
    copied = False
    if source:
        try:
            source_path = Path(source)
            with source_path.open("rb") as reader, destination.open("wb") as writer:
                shutil.copyfileobj(reader, writer)
                try:
                    reader.seek(0)
                except OSError:
                    pass
            copied = True
        except OSError:
            pass
    if required and not copied and not destination.exists():
        try:
            destination.touch(mode=0o600)
        except OSError:
            return
    if destination.exists():
        try:
            destination.chmod(0o600)
        except OSError:
            pass


def _git_output(workdir: str | Path | None, args: list[str]) -> str | None:
    if not workdir:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _git_head(workdir: str | Path | None) -> str | None:
    return _git_output(workdir, ["rev-parse", "--verify", "HEAD"])


def _salvage_refs(workdir: str | Path | None) -> list[dict[str, str]]:
    output = _git_output(
        workdir,
        [
            "for-each-ref",
            "--format=%(refname)\t%(objectname)",
            "refs/codex-salvage",
            "refs/claude-salvage",
        ],
    )
    if not output:
        return []
    refs = []
    for line in output.splitlines():
        ref, separator, sha = line.partition("\t")
        if separator and ref and sha:
            refs.append({"ref": ref, "sha": sha})
    return refs


def _decision(value: str | dict | None) -> dict | None:
    if isinstance(value, dict):
        return value
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def start_run(
    *,
    family: str,
    model: str,
    lane: str | None,
    workdir: str | Path,
    prompt: str | Path,
    out: str | Path,
    err: str | Path | None = None,
    lane_log: str | Path | None = None,
    original_out: str | None = None,
    decision_json: str | dict | None = None,
    started: str | datetime | None = None,
) -> str:
    """Create a RUNNING ledger entry and return its stable directory id."""
    started_dt = _time(started)
    source_paths = {
        "prompt": _absolute(prompt),
        "out": _absolute(out),
        "err": _absolute(err),
        "lane_log": _absolute(lane_log),
    }
    workdir_path = _absolute(workdir)
    with _lock():
        run_dir = _allocate(started_dt, _slug(original_out or out, prompt))
        refs_before = _salvage_refs(workdir_path)
        meta = {
            "id": run_dir.name,
            "family": family,
            "model": model,
            "lane": lane or None,
            "workdir": workdir_path,
            "git_head_before": _git_head(workdir_path),
            "git_head_after": None,
            "rc": None,
            "started_at": iso(started_dt),
            "finished_at": None,
            "duration_s": None,
            "original_out_path": (
                None
                if original_out == ""
                else original_out
                if original_out is not None
                else str(out)
            ),
            "session_id": None,
            "transcript_path": None,
            "salvage_refs": [],
            "routing_decision": _decision(decision_json),
            "_started_at_precise": started_dt.isoformat(),
            "_salvage_refs_before": refs_before,
            "_source_paths": source_paths,
        }
        _copy(source_paths["prompt"], run_dir / "prompt.md", required=True)
        _write_meta(run_dir, meta)
        _prune_locked()
    return run_dir.name


def _run_dir(run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("invalid run id")
    run_dir = paths.runs_dir() / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(run_id)
    return run_dir


def update_run(
    run_id: str,
    *,
    lane: str | None = None,
    session_id: str | None = None,
) -> None:
    """Refresh mutable RUNNING metadata, primarily after an internal re-pick."""
    with _lock():
        run_dir = _run_dir(run_id)
        meta = load_json(run_dir / "meta.json", {}) or {}
        if meta.get("finished_at") is not None:
            return
        if lane is not None:
            meta["lane"] = lane
        if session_id is not None:
            meta["session_id"] = session_id
        _write_meta(run_dir, meta)


def _transcript(session_id: str | None, explicit: str | Path | None) -> str | None:
    candidates: list[Path] = []
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            candidates.append(candidate)
    if session_id:
        projects = paths.claude_dir() / "projects"
        direct = projects / f"{session_id}.jsonl"
        if direct.is_file():
            candidates.append(direct)
        try:
            candidates.extend(
                candidate
                for candidate in projects.glob(f"*/{session_id}.jsonl")
                if candidate.is_file()
            )
        except OSError:
            pass
    if not candidates:
        return None
    try:
        return str(max(candidates, key=lambda item: item.stat().st_mtime))
    except OSError:
        return str(candidates[-1])


def finish_run(
    run_id: str,
    *,
    rc: int,
    lane: str | None = None,
    session_id: str | None = None,
    transcript_path: str | Path | None = None,
    finished: str | datetime | None = None,
) -> None:
    """Finalize artifacts and metadata without affecting the runner's outcome."""
    finished_dt = _time(finished)
    with _lock():
        run_dir = _run_dir(run_id)
        meta = load_json(run_dir / "meta.json", {}) or {}
        if meta.get("finished_at") is not None:
            return
        sources = meta.get("_source_paths") or {}
        # prompt.md is the immutable START snapshot. Re-copying here would
        # replace the prompt actually sent if a caller edited the source path
        # while the provider was running.
        _copy(sources.get("out"), run_dir / "out.md", required=True)
        _copy(sources.get("err"), run_dir / "err.log", required=True)
        if sources.get("lane_log"):
            _copy(sources["lane_log"], run_dir / "lane.log", required=True)

        before = {
            item.get("ref")
            for item in meta.pop("_salvage_refs_before", [])
            if isinstance(item, dict) and item.get("ref")
        }
        after = _salvage_refs(meta.get("workdir"))
        started_dt = parse_iso(meta.pop("_started_at_precise", None)) or parse_iso(
            meta.get("started_at")
        )
        decision = meta.get("routing_decision")
        if isinstance(decision, dict) and decision.get("result") is None:
            decision["result"] = int(rc)
        meta.update(
            {
                "lane": lane if lane is not None else meta.get("lane"),
                "git_head_after": _git_head(meta.get("workdir")),
                "rc": int(rc),
                "finished_at": iso(finished_dt),
                "duration_s": (
                    round(max(0.0, (finished_dt - started_dt).total_seconds()), 3)
                    if started_dt
                    else None
                ),
                "session_id": session_id or meta.get("session_id"),
                "transcript_path": _transcript(session_id, transcript_path),
                "salvage_refs": [
                    item for item in after if item.get("ref") not in before
                ],
                "routing_decision": decision,
            }
        )
        meta.pop("_source_paths", None)
        _write_meta(run_dir, meta)
        _prune_locked()


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _prune_locked(
    *, max_runs: int | None = None, max_bytes: int | None = None
) -> list[str]:
    max_runs = MAX_RUNS if max_runs is None else max_runs
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    try:
        entries = sorted(path for path in paths.runs_dir().iterdir() if path.is_dir())
    except OSError:
        return []
    sizes = {entry: _directory_size(entry) for entry in entries}
    total = sum(sizes.values())
    removed: list[str] = []
    while len(entries) > max_runs or total > max_bytes:
        victim = next((entry for entry in entries if _prunable(entry)), None)
        if victim is None:
            break
        entries.remove(victim)
        total -= sizes[victim]
        try:
            shutil.rmtree(victim)
            removed.append(victim.name)
        except OSError:
            pass
    return removed


def _prunable(run_dir: Path) -> bool:
    """A lock-held no-meta directory is abandoned, not an active starter."""
    meta = load_json(run_dir / "meta.json")
    return not isinstance(meta, dict) or meta.get("finished_at") is not None


def prune(
    *, max_runs: int | None = None, max_bytes: int | None = None
) -> list[str]:
    with _lock():
        return _prune_locked(max_runs=max_runs, max_bytes=max_bytes)


def run_directories(*, newest_first: bool = True) -> list[Path]:
    try:
        entries = [path for path in paths.runs_dir().iterdir() if path.is_dir()]
    except OSError:
        return []
    return sorted(entries, reverse=newest_first)


def list_runs(last: int = 20) -> list[dict[str, Any]]:
    if last <= 0:
        return []
    rows = []
    for run_dir in run_directories():
        meta = load_json(run_dir / "meta.json")
        if not isinstance(meta, dict):
            continue
        finished = meta.get("finished_at") is not None
        try:
            out_bytes = (run_dir / "out.md").stat().st_size
        except OSError:
            out_bytes = 0
        workdir = str(meta.get("workdir") or "")
        rows.append(
            {
                "id": run_dir.name,
                "family": meta.get("family"),
                "model": meta.get("model"),
                "lane": meta.get("lane"),
                "rc": meta.get("rc"),
                "status": "FINISHED" if finished else "RUNNING",
                "out_bytes": out_bytes,
                "duration_s": meta.get("duration_s"),
                "workdir": Path(workdir).name or workdir or None,
            }
        )
        if len(rows) >= last:
            break
    return rows


def load_run(run_id: str) -> tuple[Path, dict[str, Any]]:
    run_dir = _run_dir(run_id)
    meta = load_json(run_dir / "meta.json")
    if not isinstance(meta, dict):
        raise ValueError(f"invalid metadata for run {run_id}")
    return run_dir, meta


def in_flight_counts() -> dict[tuple[str, str], int]:
    """Count RUNNING ledger entries by exact provider lane, without pgrep."""
    counts: dict[tuple[str, str], int] = {}
    for run_dir in run_directories(newest_first=False):
        meta = load_json(run_dir / "meta.json")
        if not isinstance(meta, dict) or meta.get("finished_at") is not None:
            continue
        family, lane = meta.get("family"), meta.get("lane")
        if not isinstance(family, str) or not isinstance(lane, str) or not lane:
            continue
        key = (family, lane.casefold() if family == "claude" else lane)
        counts[key] = counts.get(key, 0) + 1
    return counts


def format_runs(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no recorded runs"
    id_width = max(56, *(len(str(row.get("id") or "-")) for row in rows))
    lines = [
        f"{'id':<{id_width}} {'family':<7} {'model':<24} {'lane':<24} "
        f"{'rc':>7} {'bytes':>9} {'seconds':>8} workdir"
    ]
    for row in rows:
        rc = "RUNNING" if row["status"] == "RUNNING" else str(row.get("rc"))
        duration = "-" if row.get("duration_s") is None else f"{row['duration_s']:.1f}"
        lines.append(
            f"{str(row.get('id') or '-'):<{id_width}} "
            f"{str(row.get('family') or '-'):<7.7} "
            f"{str(row.get('model') or '-'):<24.24} "
            f"{str(row.get('lane') or '-'):<24.24} "
            f"{rc:>7.7} {int(row.get('out_bytes') or 0):>9} {duration:>8} "
            f"{row.get('workdir') or '-'}"
        )
    return "\n".join(lines)
