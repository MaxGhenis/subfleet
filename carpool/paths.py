"""Filesystem locations shared by the monitor and dispatchers."""

import os
import re
from pathlib import Path

from . import config


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


def codex_homes() -> list[Path]:
    """Configured homes or numeric ``~/.codex-N`` homes in natural order."""
    configured = config.codex_homes_setting()
    if configured is not None:
        return configured
    home = Path.home()
    numbered = []
    for candidate in home.glob(".codex-*"):
        match = re.fullmatch(r"\.codex-(\d+)", candidate.name)
        if match and candidate.is_dir():
            numbered.append((int(match.group(1)), candidate))
    homes = [home / ".codex", *(path for _, path in sorted(numbered))]
    return [path for path in homes if path.is_dir()]


def primary_codex_home() -> Path:
    """The home shared with the Codex desktop app / ChatGPT app gauge."""
    homes = codex_homes()
    return homes[0] if homes else Path.home() / ".codex"


def state_dir() -> Path:
    return config.state_dir()


def claude_dir() -> Path:
    return _env_path("CARPOOL_CLAUDE_DIR", Path.home() / ".claude")


def claude_json() -> Path:
    return _env_path("CARPOOL_CLAUDE_JSON", Path.home() / ".claude.json")


def snapshot_path() -> Path:
    return state_dir() / "snapshot.json"


def alerts_path() -> Path:
    return state_dir() / "alerts.json"


def history_path() -> Path:
    return state_dir() / "history.jsonl"


def rollout_cache_path() -> Path:
    return state_dir() / "rollout-scan-cache.json"


def oauth_raw_path() -> Path:
    return state_dir() / "claude-oauth-raw.json"


def cooldowns_path() -> Path:
    return state_dir() / "cooldowns.json"


def rotation_path() -> Path:
    return state_dir() / "rotation.json"


def decisions_path() -> Path:
    return state_dir() / "decisions.jsonl"


def lane_usage_path() -> Path:
    return state_dir() / "lane-usage.jsonl"


def capacity_cache_path() -> Path:
    return state_dir() / "capacity-cache.json"
