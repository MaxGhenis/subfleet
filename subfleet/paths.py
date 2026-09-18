"""Filesystem locations, all overridable via env for tests."""

import os
import shutil
from pathlib import Path

HOME = Path.home()


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


def codex_homes() -> list[Path]:
    """Dispatch LANES in canonical order (~/.codex-1, ~/.codex-2, ...).

    ~/.codex is deliberately NOT a lane (since 2026-08-19): it is the
    ChatGPT/Codex desktop app's home (and bare interactive `codex`), rebound
    every time Max signs the app in or out — which used to evaporate a fleet
    lane's identity and create same-account duplicates. See app_codex_home().

    SUBFLEET_CODEX_HOMES (colon-separated) overrides discovery entirely; set
    but empty means "no lanes" (test isolation).
    """
    override = os.environ.get("SUBFLEET_CODEX_HOMES")
    if override is not None:
        return [Path(p).expanduser() for p in override.split(":") if p]
    homes = [HOME / f".codex-{i}" for i in range(1, 10)]
    return [h for h in homes if h.is_dir()]


def app_codex_home() -> Path:
    """The desktop app's CODEX_HOME (~/.codex): observed for identity — which
    account the app is signed into — never dispatched to. SUBFLEET_CODEX_APP_HOME
    overrides (tests point it at a tmp path)."""
    return _env_path("SUBFLEET_CODEX_APP_HOME", HOME / ".codex")


def primary_codex_home() -> Path:
    """First LANE in canonical order (~/.codex-1), retained as identity
    metadata fallback when no protected/app account can be resolved."""
    homes = codex_homes()
    return homes[0] if homes else HOME / ".codex-1"


def state_dir() -> Path:
    return _env_path("SUBFLEET_STATE_DIR", HOME / "chief-of-staff" / "state" / "subfleet")


def runs_dir() -> Path:
    """Private, durable artifacts for every agent dispatch."""
    return state_dir() / "runs"


def gates_dir() -> Path:
    """Private, durable main/peer consensus gates."""
    return state_dir() / "gates"


def delegate_state_dir() -> Path:
    """Delegate's runtime state (cooldowns/rotation), independently overridable."""
    return _env_path("DELEGATE_STATE_DIR", HOME / ".local" / "state" / "delegate")


def claude_dir() -> Path:
    return _env_path("SUBFLEET_CLAUDE_DIR", HOME / ".claude")


def claude_json() -> Path:
    return _env_path("SUBFLEET_CLAUDE_JSON", HOME / ".claude.json")


def cc_mirror_log_path() -> Path:
    """subfleet mirror launchd log (summary lines; silent on no-op runs)."""
    return state_dir().parent / "logs" / "subfleet-mirror.log"


def cc_mirror_heartbeat_path() -> Path:
    """The mirror's state sidecar (~/.claude/cc-mirror-state.json, owned by the
    mirror script) — rewritten atomically on EVERY pass
    (verified 2026-08-19: mtime advances each minute), unlike the log, which
    --quiet keeps silent when nothing changed. This is the mirror's heartbeat."""
    return claude_dir() / "cc-mirror-state.json"


def notify_bin() -> Path:
    return _env_path("SUBFLEET_NOTIFY", HOME / "chief-of-staff" / "bin" / "notify")


def snapshot_path() -> Path:
    return state_dir() / "snapshot.json"


def alerts_path() -> Path:
    return state_dir() / "alerts.json"


def history_path() -> Path:
    return state_dir() / "history.jsonl"


def reset_policy_path() -> Path:
    """Automatic Codex reset-redemption state and interval ledger."""
    return state_dir() / "reset-policy.json"


def brief_path() -> Path:
    return state_dir() / "brief.md"


def statusline_state_path() -> Path:
    return state_dir() / "claude-statusline.json"


def oauth_raw_path() -> Path:
    """Last successful OAuth usage payload (written by snapshot.build)."""
    return state_dir() / "claude-oauth-raw.json"


def reserve_policy_path() -> Path:
    """Fable-reserve policy overrides (``{"enabled": false}`` turns it off)."""
    return state_dir() / "reserve-policy.json"


def reserve_cache_path() -> Path:
    """Cached per-account reserve readings (percentages only, never tokens)."""
    return state_dir() / "reserve-usage-cache.json"


def claude_logins_dir() -> Path:
    """Full-scope Claude Code logins, one config dir per account (subfleet-v2)."""
    return _env_path("SUBFLEET_CLAUDE_LOGINS", HOME / ".subfleet" / "logins")


def rollout_cache_path() -> Path:
    return state_dir() / "rollout-scan-cache.json"


def capacity_cache_path() -> Path:
    return state_dir() / "capacity-live-cache.json"


def refresh_probes_path() -> Path:
    """Watchdog expired-token heal ledger: per-home last attempt + result."""
    return state_dir() / "refresh-probes.json"


def lane_usage_path() -> Path:
    return state_dir() / "lane-usage.jsonl"


def keepalive_state_path() -> Path:
    """Claude lane keepalive outcomes and auth-dead suppression state."""
    return state_dir() / "keepalive.json"


def delegate_cooldowns_path() -> Path:
    return delegate_state_dir() / "cooldowns.json"


def claude_bin() -> str:
    """The Claude Code launcher every lane, probe, revive, and keepalive runs.

    CLAUDE_LANE_CLAUDE wins when set (tests, stripped launchd environments).
    Otherwise the native installer's launcher, ~/.local/bin/claude, is
    preferred over whatever ``claude`` PATH resolves: launchd jobs put
    /opt/homebrew/bin first, and on 2026-09-02 a Homebrew cask frozen at
    2.1.87 shadowed a current native install there, so every Fable 5.1 probe
    got "does not support this model" and revive parked sessions on capacity.
    ``claude update`` maintains the native launcher; the cask cannot reach the
    version the current models require.
    """
    explicit = os.environ.get("CLAUDE_LANE_CLAUDE")
    if explicit:
        return explicit
    native = Path.home() / ".local" / "bin" / "claude"
    if os.access(native, os.X_OK):
        return str(native)
    return shutil.which("claude") or "claude"
