"""Filesystem locations shared by the monitor and dispatchers.

All runtime state is configurable and dispatch homes are deliberately separate
from the observed desktop-app home.
"""

import os
from pathlib import Path

from . import config

HOME = Path.home()


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


def codex_homes() -> list[Path]:
    """Configured lanes or numeric ``~/.codex-N`` homes in natural order.

    ``~/.codex`` belongs to the desktop app and is never returned here.
    """
    configured = config.codex_homes_setting()
    if configured is not None:
        app = app_codex_home().resolve(strict=False)
        lanes: list[Path] = []
        seen: set[Path] = set()
        for candidate in configured:
            resolved = candidate.resolve(strict=False)
            if resolved == app or resolved in seen:
                continue
            seen.add(resolved)
            lanes.append(candidate)
        return lanes
    numbered = []
    for candidate in HOME.glob(".codex-*"):
        suffix = candidate.name.removeprefix(".codex-")
        if suffix.isdigit() and candidate.is_dir():
            numbered.append((int(suffix), candidate))
    return [path for _, path in sorted(numbered)]


def app_codex_home() -> Path:
    """Observed desktop-app home; this path is never a dispatch lane."""
    return config.path("codex_app_home", "SUBFLEET_CODEX_APP_HOME", HOME / ".codex")


def primary_codex_home() -> Path:
    """First configured lane, used only as a protected-account fallback."""
    homes = codex_homes()
    return homes[0] if homes else HOME / ".codex-1"


def state_dir() -> Path:
    return config.state_dir()


def claude_dir() -> Path:
    return _env_path("SUBFLEET_CLAUDE_DIR", Path.home() / ".claude")


def claude_json() -> Path:
    return _env_path("SUBFLEET_CLAUDE_JSON", Path.home() / ".claude.json")


def cc_mirror_log_path() -> Path:
    return config.path(
        "mirror_log", "SUBFLEET_MIRROR_LOG", state_dir() / "subfleet-mirror.log"
    )


def cc_mirror_heartbeat_path() -> Path:
    return config.path(
        "mirror_heartbeat",
        "SUBFLEET_MIRROR_HEARTBEAT",
        claude_dir() / "cc-mirror-state.json",
    )


def runs_dir() -> Path:
    return state_dir() / "runs"


def refresh_probes_path() -> Path:
    return state_dir() / "refresh-probes.json"


def brief_path() -> Path:
    return state_dir() / "brief.md"


def statusline_state_path() -> Path:
    return state_dir() / "claude-statusline.json"


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
