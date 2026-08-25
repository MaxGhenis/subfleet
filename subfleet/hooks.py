"""Claude Code hook registration for subfleet (``subfleet hooks install``).

Three entries in ``~/.claude/settings.json``, all routed through
``bin/subfleet-hook`` so they can be found (and removed) by that one string:

* ``SessionStart`` / ``UserPromptSubmit`` → completion catch-up: a detached
  run that finished while its session was not running parked a notice
  (inbox.py); the hook hands it to the session as additional context the
  next time the session starts or the user prompts.
* ``PreToolUse`` (Bash) → the attached-runner guard: a provider runner
  launched straight from a session (``subfleet codex``, ``subfleet claude``,
  bare ``codex exec``, or their wrapper scripts) dies with that session on an
  account switch or the desktop app's idle SIGTERM. The guard blocks the
  launch with the ``subfleet run`` replacement; ``SUBFLEET_ATTACHED_OK=1`` on
  the command line is the explicit one-off override.

Install is idempotent and preserves every other setting byte-for-byte in
structure (2-space JSON, key order kept); a timestamped backup is written
beside the file before any change.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths

MARKER = "subfleet-hook"
TIMEOUT = 5


def hook_script() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "subfleet-hook"


def settings_path() -> Path:
    override = os.environ.get("SUBFLEET_CLAUDE_SETTINGS")
    return Path(override).expanduser() if override else paths.claude_dir() / "settings.json"


def desired_groups(script: Path | None = None) -> dict[str, dict[str, Any]]:
    script = script or hook_script()

    def command(event: str) -> dict[str, Any]:
        return {"type": "command", "command": f"{script} {event}", "timeout": TIMEOUT}

    return {
        "PreToolUse": {"matcher": "Bash", "hooks": [command("pre-bash")]},
        "UserPromptSubmit": {"hooks": [command("user-prompt")]},
        "SessionStart": {"hooks": [command("session-start")]},
    }


def _is_ours(hook: Any) -> bool:
    command = str(hook.get("command") or "") if isinstance(hook, dict) else ""
    return MARKER in command


def _strip_ours(groups: list[Any]) -> tuple[list[Any], int]:
    """Remove subfleet entries from every group; drop groups left empty."""
    kept: list[Any] = []
    removed = 0
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            kept.append(group)
            continue
        remaining = [hook for hook in hooks if not _is_ours(hook)]
        removed += len(hooks) - len(remaining)
        if remaining:
            kept.append({**group, "hooks": remaining})
        elif len(hooks) == 0:
            kept.append(group)
    return kept, removed


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _save(path: Path, data: dict[str, Any]) -> Path | None:
    backup = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def status(path: Path | None = None) -> dict[str, Any]:
    path = path or settings_path()
    try:
        data = _load(path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "path": str(path), "error": str(exc), "installed": {}}
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    installed = {}
    for event in desired_groups():
        groups = hooks.get(event) if isinstance(hooks.get(event), list) else []
        commands = [
            hook.get("command") for group in groups if isinstance(group, dict)
            for hook in (group.get("hooks") or [])
            if _is_ours(hook)
        ]
        installed[event] = commands
    script = hook_script()
    return {
        "ok": True,
        "path": str(path),
        "script": str(script),
        "script_executable": os.access(script, os.X_OK),
        "installed": installed,
        "complete": all(installed[event] for event in desired_groups()),
    }


def install(*, dry_run: bool = False, path: Path | None = None) -> dict[str, Any]:
    path = path or settings_path()
    script = hook_script()
    if not (script.is_file() and os.access(script, os.X_OK)):
        # Never write a dead command into the settings file: an installed
        # wheel carries only the package, not this repository's bin/ scripts.
        return {
            "ok": False,
            "path": str(path),
            "error": f"hook script missing or not executable: {script} "
                     "(hooks need a source checkout)",
        }
    try:
        data = _load(path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    changed = 0
    for event, group in desired_groups().items():
        existing = hooks.get(event) if isinstance(hooks.get(event), list) else []
        stripped, removed = _strip_ours(existing)
        stripped.append(group)
        if stripped != existing:
            changed += 1
        hooks[event] = stripped
    data["hooks"] = hooks
    backup = None
    if changed and not dry_run:
        backup = _save(path, data)
    return {
        "ok": True,
        "path": str(path),
        "dry_run": dry_run,
        "changed_events": changed,
        "backup": str(backup) if backup else None,
        "installed": status(path)["installed"] if not dry_run else {
            event: [hook["command"] for hook in group["hooks"]]
            for event, group in desired_groups().items()
        },
    }


def uninstall(*, dry_run: bool = False, path: Path | None = None) -> dict[str, Any]:
    path = path or settings_path()
    try:
        data = _load(path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}
    hooks = data.get("hooks")
    removed_total = 0
    if isinstance(hooks, dict):
        for event, groups in list(hooks.items()):
            if not isinstance(groups, list):
                continue
            stripped, removed = _strip_ours(groups)
            removed_total += removed
            if stripped:
                hooks[event] = stripped
            else:
                hooks.pop(event)
    backup = None
    if removed_total and not dry_run:
        backup = _save(path, data)
    return {
        "ok": True,
        "path": str(path),
        "dry_run": dry_run,
        "removed": removed_total,
        "backup": str(backup) if backup else None,
    }


def format_report(report: dict[str, Any]) -> str:
    if not report.get("ok", True):
        return f"subfleet hooks: {report.get('error')} ({report.get('path')})"
    lines = [f"subfleet hooks: {report.get('path')}"]
    if "removed" in report:
        verb = "would remove" if report.get("dry_run") else "removed"
        lines.append(f"  {verb} {report['removed']} subfleet hook entr{'y' if report['removed'] == 1 else 'ies'}")
        if report.get("backup"):
            lines.append(f"  backup: {report['backup']}")
        return "\n".join(lines)
    if "changed_events" in report:
        verb = "would update" if report.get("dry_run") else "updated"
        lines.append(f"  {verb} {report['changed_events']} event list(s)")
        if report.get("backup"):
            lines.append(f"  backup: {report['backup']}")
    if "script" in report:
        lines.append(
            f"  script: {report['script']} "
            f"({'executable' if report.get('script_executable') else 'NOT EXECUTABLE'})"
        )
    for event, commands in (report.get("installed") or {}).items():
        state = ", ".join(commands) if commands else "MISSING"
        lines.append(f"  {event}: {state}")
    if "complete" in report:
        lines.append("  complete" if report["complete"] else "  incomplete — run: subfleet hooks install")
    return "\n".join(lines)
